"""Transport-neutral State MCP tool contract and authorization boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from .state_store import (
    StateStoreBusy,
    StateStoreCapabilityError,
    StateStoreConflict,
    StateStoreIntegrityError,
    StateStoreNotFound,
    capability_manifest_to_document,
    invoke_state_store,
    validate_state_store_adapter,
)
from .state_events import (
    EVENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION_V4,
    LEGACY_EVENT_SCHEMA_VERSION,
    StateEventError,
    build_state_event,
    replay_state_events,
)
from .effect_scope_gate import (
    evaluate_effect_completion_gate,
    evaluate_claim_scope_gate,
    evaluate_effect_scope_gate,
    validate_scope,
)
from .typed_state import TypedStateError
from .experiment_lifecycle import (
    evaluate_attempt_gate,
    evaluate_experiment_activation_gate,
    experiment_time_verdict,
    experiment_contract_sha256,
)


REQUEST_SCHEMA_VERSION = "context.state-mcp-request/v1alpha1"
RESPONSE_SCHEMA_VERSION = "context.state-mcp-response/v1alpha1"

READ_TOOL = "context.state.read"
COMMIT_TOOL = "context.state.commit"
CLAIM_TOOL = "context.state.claim"
EFFECT_TOOL = "context.state.effect"
EFFECT_GATE_TOOL = "context.state.effect.gate"
EXPERIMENT_EFFECT_TOOL = "context.experiment.effect"
ATTEMPT_TOOL = "context.experiment.attempt"
PROMOTION_PROPOSE_TOOL = "context.experiment.promotion.propose"
PROMOTION_APPROVE_TOOL = "context.experiment.promotion.approve"
STATE_MCP_TOOLS = (
    READ_TOOL,
    COMMIT_TOOL,
    CLAIM_TOOL,
    EFFECT_TOOL,
    EFFECT_GATE_TOOL,
    EXPERIMENT_EFFECT_TOOL,
    ATTEMPT_TOOL,
    PROMOTION_PROPOSE_TOOL,
    PROMOTION_APPROVE_TOOL,
)

ATTEMPT_REQUEST_SCHEMA_VERSION = "context.experiment-attempt-request/v1alpha1"
EXPERIMENT_EFFECT_REQUEST_SCHEMA_VERSION = "context.experiment-effect-request/v1alpha1"
PROMOTION_PROPOSAL_REQUEST_SCHEMA_VERSION = (
    "context.experiment-promotion-proposal-request/v1alpha1"
)
PROMOTION_APPROVAL_REQUEST_SCHEMA_VERSION = (
    "context.experiment-promotion-approval-request/v1alpha1"
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMON_FIELDS = {"schema_version", "request_id", "project_id"}
_REQUEST_FIELDS = {
    READ_TOOL: _COMMON_FIELDS,
    COMMIT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "causation_ref",
        "correlation_ref",
        "supersedes_event_id",
        "changes",
    },
    CLAIM_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "claim_id",
        "scope_owners",
        "lease_expires_at",
        "causation_ref",
        "correlation_ref",
    },
    EFFECT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "action",
        "effect_id",
        "effect_key",
        "work_id",
        "claim_id",
        "operation",
        "scope_ref",
        "result_ref",
        "evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
    EFFECT_GATE_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "effect_id",
        "work_id",
        "claim_id",
        "operation",
        "scope_ref",
    },
    EXPERIMENT_EFFECT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "action",
        "effect_id",
        "effect_key",
        "work_id",
        "claim_id",
        "attempt_id",
        "operation",
        "scope_ref",
        "result_ref",
        "evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
    ATTEMPT_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "attempt_id",
        "work_id",
        "claim_id",
        "causation_ref",
        "correlation_ref",
    },
    PROMOTION_PROPOSE_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "expected_work_revision",
        "expected_target_work_revision",
        "attempt_id",
        "proposal_id",
        "criterion_evidence",
        "causation_ref",
        "correlation_ref",
    },
    PROMOTION_APPROVE_TOOL: _COMMON_FIELDS
    | {
        "expected_revision",
        "work_id",
        "expected_work_revision",
        "expected_target_work_revision",
        "proposal_id",
        "approval_id",
        "causation_ref",
        "correlation_ref",
    },
}
_AUTHORIZATION_ACTIONS = {
    READ_TOOL: "state.read",
    COMMIT_TOOL: "state.commit",
    CLAIM_TOOL: "state.claim",
    EFFECT_GATE_TOOL: "state.effect.gate",
    ATTEMPT_TOOL: "state.experiment.attempt.begin",
    PROMOTION_PROPOSE_TOOL: "state.experiment.promotion.propose",
    PROMOTION_APPROVE_TOOL: "state.experiment.promotion.approve",
}
_COMMIT_COLLECTION_ID_FIELDS = {
    "works": "work_id",
    "ideas": "idea_id",
    "decisions": "decision_id",
    "constraints": "constraint_id",
    "evidence": "evidence_id",
    "blockers": "blocker_id",
}
_ALL_COLLECTION_ID_FIELDS = {
    **_COMMIT_COLLECTION_ID_FIELDS,
    "claims": "claim_id",
    "effects": "effect_id",
    "experiment_attempts": "attempt_id",
    "experiment_promotions": "promotion_id",
}


def _event_schema_version(snapshot: dict[str, Any]) -> str:
    schema_version = snapshot.get("schema_version")
    if schema_version == "context.typed-state/v3alpha1":
        return EVENT_SCHEMA_VERSION_V4
    if schema_version == "context.typed-state/v2alpha1":
        return EVENT_SCHEMA_VERSION
    return LEGACY_EVENT_SCHEMA_VERSION


@dataclass(frozen=True)
class RequestContext:
    """Trusted caller identity supplied by the transport adapter."""

    subject_ref: str
    authorization_ref: str

    def __post_init__(self) -> None:
        for field in ("subject_ref", "authorization_ref"):
            value = getattr(self, field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")


class StateMCPAuthorizer(Protocol):
    """Authorization provider invoked before any project lookup."""

    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool: ...


class _DenyAllAuthorizer:
    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        return False


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": copy.deepcopy(properties),
    }


def _request_properties(tool: str) -> dict[str, Any]:
    common = {
        "schema_version": {"const": REQUEST_SCHEMA_VERSION},
        "request_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
    }
    if tool == READ_TOOL:
        return common
    if tool == ATTEMPT_TOOL:
        return {
            **common,
            "schema_version": {"const": ATTEMPT_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "attempt_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == EXPERIMENT_EFFECT_TOOL:
        return {
            **common,
            "schema_version": {"const": EXPERIMENT_EFFECT_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "action": {"enum": ["authorize", "complete"]},
            "effect_id": {"type": "string", "minLength": 1, "maxLength": 300},
            "effect_key": {"type": "string", "minLength": 1, "maxLength": 300},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 300},
            "claim_id": {"type": "string", "minLength": 1, "maxLength": 300},
            "attempt_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "operation": {"type": "string", "minLength": 1, "maxLength": 300},
            "scope_ref": {"type": "object"},
            "result_ref": {"type": ["string", "null"]},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == PROMOTION_PROPOSE_TOOL:
        return {
            **common,
            "schema_version": {"const": PROMOTION_PROPOSAL_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "expected_work_revision": {"type": "integer", "minimum": 0},
            "expected_target_work_revision": {"type": "integer", "minimum": 0},
            "attempt_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "proposal_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "criterion_evidence": {"type": "object", "minProperties": 1},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == PROMOTION_APPROVE_TOOL:
        return {
            **common,
            "schema_version": {"const": PROMOTION_APPROVAL_REQUEST_SCHEMA_VERSION},
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "expected_work_revision": {"type": "integer", "minimum": 0},
            "expected_target_work_revision": {"type": "integer", "minimum": 0},
            "proposal_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "approval_id": {"type": "string", "minLength": 1, "maxLength": 200},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == COMMIT_TOOL:
        return {
            **common,
            "expected_revision": {"type": "integer", "minimum": 0},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
            "supersedes_event_id": {"type": ["string", "null"]},
            "changes": {"type": "array", "minItems": 1},
        }
    if tool == CLAIM_TOOL:
        return {
            **common,
            "expected_revision": {"type": "integer", "minimum": 0},
            "work_id": {"type": "string", "minLength": 1},
            "claim_id": {"type": "string", "minLength": 1},
            "scope_owners": {"type": "array", "minItems": 1},
            "lease_expires_at": {"type": "string", "format": "date-time"},
            "causation_ref": {"type": ["string", "null"]},
            "correlation_ref": {"type": ["string", "null"]},
        }
    if tool == EFFECT_GATE_TOOL:
        return {
            **common,
            "expected_revision": {"type": "integer", "minimum": 0},
            "effect_id": {"type": "string", "minLength": 1},
            "work_id": {"type": "string", "minLength": 1},
            "claim_id": {"type": "string", "minLength": 1},
            "operation": {"type": "string", "minLength": 1},
            "scope_ref": {"type": "object"},
        }
    return {
        **common,
        "expected_revision": {"type": "integer", "minimum": 0},
        "action": {"enum": ["authorize", "complete"]},
        "effect_id": {"type": "string", "minLength": 1},
        "effect_key": {"type": "string", "minLength": 1},
        "work_id": {"type": "string", "minLength": 1},
        "claim_id": {"type": "string", "minLength": 1},
        "operation": {"type": "string", "minLength": 1},
        "scope_ref": {"type": "object"},
        "result_ref": {"type": ["string", "null"]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "causation_ref": {"type": ["string", "null"]},
        "correlation_ref": {"type": ["string", "null"]},
    }


def state_mcp_tool_definitions() -> list[dict[str, Any]]:
    """Return provider-neutral MCP tool metadata with strict input schemas."""
    descriptions = {
        READ_TOOL: "Read one authorized typed-state snapshot and its event head.",
        COMMIT_TOOL: "Commit an authorized non-claim state intent with CAS.",
        CLAIM_TOOL: "Claim one ready Work atomically with CAS.",
        EFFECT_TOOL: "Authorize or complete one claimed external effect with CAS.",
        EFFECT_GATE_TOOL: "Evaluate one effect authorization gate without a state write.",
        EXPERIMENT_EFFECT_TOOL: "Authorize or complete one Experiment effect bound to a persisted attempt.",
        ATTEMPT_TOOL: "Persist one authorized Experiment attempt before external effects.",
        PROMOTION_PROPOSE_TOOL: "Propose verified Experiment findings for a canonical target.",
        PROMOTION_APPROVE_TOOL: "Approve a proposed Experiment promotion with an independent verifier.",
    }
    return [
        {
            "name": tool,
            "description": descriptions[tool],
            "inputSchema": _object_schema(_request_properties(tool)),
        }
        for tool in STATE_MCP_TOOLS
    ]


def _response(
    *,
    tool: str,
    request_id: str | None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    ok = error_code is None
    return {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "request_id": request_id,
        "tool": tool,
        "ok": ok,
        "result": copy.deepcopy(result) if ok else None,
        "error": (
            None
            if ok
            else {
                "code": error_code,
                "message": error_message or "request failed",
            }
        ),
    }


def _request_id(arguments: Any) -> str | None:
    if isinstance(arguments, dict) and isinstance(arguments.get("request_id"), str):
        return arguments["request_id"]
    return None


def _validate_request(tool: str, arguments: Any) -> str | None:
    if not isinstance(arguments, dict) or set(arguments) != _REQUEST_FIELDS[tool]:
        return "request fields do not match the tool contract"
    expected_schema_version = {
        ATTEMPT_TOOL: ATTEMPT_REQUEST_SCHEMA_VERSION,
        EXPERIMENT_EFFECT_TOOL: EXPERIMENT_EFFECT_REQUEST_SCHEMA_VERSION,
        PROMOTION_PROPOSE_TOOL: PROMOTION_PROPOSAL_REQUEST_SCHEMA_VERSION,
        PROMOTION_APPROVE_TOOL: PROMOTION_APPROVAL_REQUEST_SCHEMA_VERSION,
    }.get(tool, REQUEST_SCHEMA_VERSION)
    if arguments["schema_version"] != expected_schema_version:
        return "unsupported request schema_version"
    for field in ("request_id", "project_id"):
        value = arguments[field]
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            return f"{field} must be a bounded non-empty string"
    if tool == READ_TOOL:
        return None
    revision = arguments["expected_revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        return "expected_revision must be a non-negative integer"
    if tool != EFFECT_GATE_TOOL:
        for field in ("causation_ref", "correlation_ref"):
            value = arguments[field]
            if value is not None and (
                not isinstance(value, str) or not value.strip() or len(value) > 500
            ):
                return f"{field} must be null or a bounded non-empty string"
    if tool == COMMIT_TOOL:
        supersedes = arguments["supersedes_event_id"]
        if supersedes is not None and (
            not isinstance(supersedes, str)
            or not supersedes.strip()
            or len(supersedes) > 200
        ):
            return "supersedes_event_id must be null or a bounded string"
        changes = arguments["changes"]
        if not isinstance(changes, list) or not changes or len(changes) > 100:
            return "changes must be a bounded non-empty array"
        for change in changes:
            if not isinstance(change, dict) or set(change) != {
                "collection",
                "object_id",
                "value",
            }:
                return "change fields do not match the contract"
            collection = change["collection"]
            if collection not in _COMMIT_COLLECTION_ID_FIELDS:
                return "claims and effects require dedicated State MCP tools"
            object_id = change["object_id"]
            value = change["value"]
            if (
                not isinstance(object_id, str)
                or not object_id.strip()
                or not isinstance(value, dict)
                or value.get(_COMMIT_COLLECTION_ID_FIELDS[collection]) != object_id
            ):
                return "change identity is invalid"
    if tool == CLAIM_TOOL:
        for field in ("work_id", "claim_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        scopes = arguments["scope_owners"]
        if not isinstance(scopes, list) or not scopes or len(scopes) > 100:
            return "scope_owners must be a bounded non-empty array"
        for scope in scopes:
            if not isinstance(scope, dict) or set(scope) != {"scope_kind", "scope_ref"}:
                return "scope_owners entries do not match the contract"
            if scope["scope_kind"] not in {
                "repo",
                "directory",
                "file",
                "symbol",
                "capability",
                "effect",
            } or not isinstance(scope["scope_ref"], str) or not scope["scope_ref"].strip():
                return "scope_owners entry is invalid"
        lease = arguments["lease_expires_at"]
        if not isinstance(lease, str) or not lease.strip():
            return "lease_expires_at must be a non-empty RFC3339 string"
        try:
            parsed_lease = datetime.fromisoformat(lease.replace("Z", "+00:00"))
        except ValueError:
            return "lease_expires_at must be a valid RFC3339 timestamp"
        if parsed_lease.tzinfo is None:
            return "lease_expires_at must include a timezone"
    if tool == ATTEMPT_TOOL:
        for field in ("attempt_id", "work_id", "claim_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
    if tool in {PROMOTION_PROPOSE_TOOL, PROMOTION_APPROVE_TOOL}:
        for field in ("work_id", "proposal_id"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 200:
                return f"{field} must be a bounded non-empty string"
        for field in ("expected_work_revision", "expected_target_work_revision"):
            value = arguments[field]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                return f"{field} must be a non-negative integer"
        if tool == PROMOTION_PROPOSE_TOOL:
            attempt_id = arguments["attempt_id"]
            if not isinstance(attempt_id, str) or not attempt_id.strip() or len(attempt_id) > 200:
                return "attempt_id must be a bounded non-empty string"
            criteria = arguments["criterion_evidence"]
            if not isinstance(criteria, dict) or not criteria:
                return "criterion_evidence must be a non-empty object"
            for criterion, evidence_ids in criteria.items():
                if not isinstance(criterion, str) or not criterion.strip():
                    return "criterion_evidence keys must be non-empty strings"
                if (
                    not isinstance(evidence_ids, list)
                    or not evidence_ids
                    or any(not isinstance(item, str) or not item.strip() for item in evidence_ids)
                    or len(evidence_ids) != len(set(evidence_ids))
                ):
                    return "criterion evidence must contain unique non-empty strings"
        else:
            approval_id = arguments["approval_id"]
            if not isinstance(approval_id, str) or not approval_id.strip() or len(approval_id) > 200:
                return "approval_id must be a bounded non-empty string"
    if tool in {EFFECT_TOOL, EFFECT_GATE_TOOL, EXPERIMENT_EFFECT_TOOL}:
        for field in ("effect_id", "work_id", "claim_id", "operation"):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 300:
                return f"{field} must be a bounded non-empty string"
        scope = arguments["scope_ref"]
        if not isinstance(scope, dict) or set(scope) != {"scope_kind", "scope_ref"}:
            return "scope_ref is invalid"
        if scope["scope_kind"] not in {
            "repo",
            "directory",
            "file",
            "symbol",
            "capability",
            "effect",
        } or not isinstance(scope["scope_ref"], str) or not scope["scope_ref"].strip():
            return "scope_ref is invalid"
    if tool in {EFFECT_TOOL, EXPERIMENT_EFFECT_TOOL}:
        if arguments["action"] not in {"authorize", "complete"}:
            return "action is unsupported"
        for field in ("effect_key",):
            value = arguments[field]
            if not isinstance(value, str) or not value.strip() or len(value) > 300:
                return f"{field} must be a bounded non-empty string"
        evidence_ids = arguments["evidence_ids"]
        if not isinstance(evidence_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in evidence_ids
        ) or len(evidence_ids) != len(set(evidence_ids)):
            return "evidence_ids must contain unique non-empty strings"
        if arguments["action"] == "complete":
            result_ref = arguments["result_ref"]
            if not isinstance(result_ref, str) or not result_ref.strip():
                return "complete requires result_ref"
        elif arguments["result_ref"] is not None:
            return "authorize requires a null result_ref"
        if tool == EXPERIMENT_EFFECT_TOOL:
            attempt_id = arguments["attempt_id"]
            if not isinstance(attempt_id, str) or not attempt_id.strip() or len(attempt_id) > 200:
                return "attempt_id must be a bounded non-empty string"
    return None


def _replace_or_append(
    snapshot: dict[str, Any],
    change: dict[str, Any],
) -> None:
    collection = change["collection"]
    object_id = change["object_id"]
    id_field = _ALL_COLLECTION_ID_FIELDS[collection]
    values = snapshot[collection]
    index = next(
        (index for index, item in enumerate(values) if item[id_field] == object_id),
        None,
    )
    if index is None:
        values.append(copy.deepcopy(change["value"]))
    else:
        values[index] = copy.deepcopy(change["value"])


def _upsert_change(
    changes: list[dict[str, Any]],
    *,
    collection: str,
    value: dict[str, Any],
) -> None:
    id_field = _ALL_COLLECTION_ID_FIELDS[collection]
    object_id = value[id_field]
    replacement = {
        "collection": collection,
        "object_id": object_id,
        "value": copy.deepcopy(value),
    }
    for index, change in enumerate(changes):
        if change["collection"] == collection and change["object_id"] == object_id:
            changes[index] = replacement
            return
    changes.append(replacement)


def _derive_project_projection(
    snapshot: dict[str, Any],
    *,
    revision: int,
    updated_at: str,
) -> None:
    project = snapshot["project"]
    active_work_ids = sorted(
        item["work_id"] for item in snapshot["works"] if item["status"] == "active"
    )
    current_primary = project["primary_work_id"]
    project.update(
        {
            "revision": revision,
            "active_work_ids": active_work_ids,
            "primary_work_id": (
                current_primary
                if current_primary in active_work_ids
                else (active_work_ids[0] if active_work_ids else None)
            ),
            "current_decision_ids": sorted(
                item["decision_id"]
                for item in snapshot["decisions"]
                if item["status"] == "accepted"
            ),
            "active_constraint_ids": sorted(
                item["constraint_id"]
                for item in snapshot["constraints"]
                if item["status"] == "active"
            ),
            "open_blocker_ids": sorted(
                item["blocker_id"]
                for item in snapshot["blockers"]
                if item["status"] == "open"
            ),
            "effect_high_watermark": max(
                (
                    item["sequence_no"]
                    for item in snapshot["effects"]
                    if item["status"] in {"succeeded", "failed", "compensated"}
                ),
                default=0,
            ),
            "updated_at": updated_at,
        }
    )


def _backend_error_response(
    *,
    tool: str,
    request_id: str | None,
    error: Exception,
) -> dict[str, Any]:
    if isinstance(error, StateStoreNotFound):
        code, message = "not_found", "project was not found"
    elif isinstance(error, StateStoreConflict):
        code, message = "conflict", "state changed concurrently"
    elif isinstance(error, StateStoreBusy):
        code, message = "busy", "state store is busy"
    elif isinstance(error, StateStoreCapabilityError):
        code, message = "capability", "state-store capability contract rejected the operation"
    elif isinstance(error, (StateStoreIntegrityError, StateEventError, TypedStateError)):
        code, message = "integrity", "state integrity validation failed"
    else:
        raise error
    return _response(
        tool=tool,
        request_id=request_id,
        error_code=code,
        error_message=message,
    )


class StateMCPService:
    """Apply State MCP authorization before dispatching to a StateStore."""

    def __init__(
        self,
        store: Any,
        *,
        authorizer: StateMCPAuthorizer | None = None,
        registry_digest: str,
        clock: Callable[[], str],
        event_id_factory: Callable[[str], str],
    ) -> None:
        self._manifest = validate_state_store_adapter(store)
        if not isinstance(registry_digest, str) or not _SHA256_RE.fullmatch(
            registry_digest
        ):
            raise ValueError("registry_digest must be lowercase SHA-256")
        if not callable(clock) or not callable(event_id_factory):
            raise ValueError("clock and event_id_factory must be callable")
        self._store = store
        self._authorizer = authorizer or _DenyAllAuthorizer()
        self._registry_hash_value = registry_digest
        self._clock = clock
        self._event_id_factory = event_id_factory
        self._request_receipts: dict[
            tuple[str, str], tuple[str, dict[str, Any]]
        ] = {}
        self._mutation_lock = threading.Lock()

    @staticmethod
    def _request_fingerprint(
        tool: str,
        arguments: dict[str, Any],
        context: RequestContext,
    ) -> str:
        payload = {
            "tool": tool,
            "arguments": arguments,
            "subject_ref": context.subject_ref,
            "authorization_ref": context.authorization_ref,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _request_replay(
        self,
        tool: str,
        arguments: dict[str, Any],
        context: RequestContext,
    ) -> dict[str, Any] | None:
        previous = self._request_receipts.get((tool, arguments["request_id"]))
        if previous is None:
            return None
        if previous[0] != self._request_fingerprint(tool, arguments, context):
            return _response(
                tool=tool,
                request_id=arguments["request_id"],
                error_code="conflict",
                error_message="request_id was already used for a different intent",
            )
        return copy.deepcopy(previous[1])

    def _remember_request(
        self,
        tool: str,
        arguments: dict[str, Any],
        context: RequestContext,
        response: dict[str, Any],
    ) -> None:
        self._request_receipts[(tool, arguments["request_id"])] = (
            self._request_fingerprint(tool, arguments, context),
            copy.deepcopy(response),
        )

    def _authorize(
        self,
        *,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        try:
            return self._authorizer.authorize(context, action, project_id) is True
        except Exception:
            return False

    def call_tool(
        self,
        tool: str,
        arguments: Any,
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Validate, authorize and execute one State MCP tool call."""
        request_id = _request_id(arguments)
        if tool not in STATE_MCP_TOOLS:
            return _response(
                tool=str(tool),
                request_id=request_id,
                error_code="unsupported",
                error_message="unsupported State MCP tool",
            )
        request_error = _validate_request(tool, arguments)
        if request_error is not None:
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="invalid_request",
                error_message=request_error,
            )

        if not isinstance(context, RequestContext):
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="permission_denied",
                error_message="trusted request context is required",
            )

        if tool == EXPERIMENT_EFFECT_TOOL:
            action = f"state.experiment.effect.{arguments['action']}"
        elif tool == EFFECT_TOOL:
            action = f"state.effect.{arguments['action']}"
        else:
            action = _AUTHORIZATION_ACTIONS[tool]
        if not self._authorize(
            context=context,
            action=action,
            project_id=arguments["project_id"],
        ):
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="permission_denied",
                error_message="request is not authorized",
            )

        if tool == EFFECT_GATE_TOOL:
            return self._effect_gate(arguments, context=context)
        if tool in {
            COMMIT_TOOL,
            CLAIM_TOOL,
            EFFECT_TOOL,
            EXPERIMENT_EFFECT_TOOL,
            ATTEMPT_TOOL,
            PROMOTION_PROPOSE_TOOL,
            PROMOTION_APPROVE_TOOL,
        }:
            with self._mutation_lock:
                replay = self._request_replay(tool, arguments, context)
                if replay is not None:
                    return replay
                if tool == COMMIT_TOOL:
                    return self._commit(arguments, context=context)
                if tool == CLAIM_TOOL:
                    return self._claim(arguments, context=context)
                if tool == ATTEMPT_TOOL:
                    return self._attempt(arguments, context=context)
                if tool in {PROMOTION_PROPOSE_TOOL, PROMOTION_APPROVE_TOOL}:
                    return self._promotion(tool, arguments, context=context)
                if tool == EXPERIMENT_EFFECT_TOOL:
                    return self._effect(
                        arguments,
                        context=context,
                        tool=EXPERIMENT_EFFECT_TOOL,
                    )
                return self._effect(arguments, context=context)
        if tool != READ_TOOL:
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="unsupported",
                error_message="State MCP write action is not implemented",
            )

        try:
            snapshot = invoke_state_store(
                self._store,
                "read_project",
                arguments["project_id"],
            )
            events = invoke_state_store(
                self._store,
                "read_events",
                arguments["project_id"],
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
        ) as exc:
            return _backend_error_response(
                tool=tool,
                request_id=request_id,
                error=exc,
            )

        event_head = None
        if events:
            if (
                events[-1]["project_id"] != snapshot["project"]["project_id"]
                or events[-1]["revision_after"] != snapshot["project"]["revision"]
            ):
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="snapshot and event head were read at different revisions",
                )
            event_head = {
                "sequence_no": events[-1]["sequence_no"],
                "event_sha256": events[-1]["event_sha256"],
            }
        return _response(
            tool=tool,
            request_id=request_id,
            result={
                "snapshot": snapshot,
                "revision": snapshot["project"]["revision"],
                "event_head": event_head,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )

    def _attempt(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Atomically record one authorized Experiment attempt in the v3 ledger."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            request_fingerprint = self._request_fingerprint(
                ATTEMPT_TOOL, arguments, context
            )
            event_id = self._event_id_factory(request_id)
            existing_event = next(
                (item for item in events if item["event_id"] == event_id), None
            )
            if existing_event is not None:
                transition = existing_event.get("experiment_transition")
                if (
                    not isinstance(transition, dict)
                    or transition.get("operation") != "attempt-started"
                    or transition.get("request_sha256") != request_fingerprint
                    or transition.get("attempt_id") != arguments["attempt_id"]
                    or events[-1]["event_id"] != event_id
                    or snapshot["project"]["revision"]
                    != existing_event["revision_after"]
                ):
                    return _response(
                        tool=ATTEMPT_TOOL,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="attempt request identity conflicts with durable state",
                    )
                response = _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    result={
                        "snapshot": snapshot,
                        "revision": snapshot["project"]["revision"],
                        "event_head": {
                            "sequence_no": existing_event["sequence_no"],
                            "event_sha256": existing_event["event_sha256"],
                        },
                        "event": existing_event,
                        "registry_digest": self._registry_hash_value,
                        "capabilities": capability_manifest_to_document(self._manifest),
                    },
                )
                self._remember_request(ATTEMPT_TOOL, arguments, context, response)
                return response
            if snapshot.get("schema_version") != "context.typed-state/v3alpha1":
                return _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="Experiment attempts require typed-state v3alpha1",
                )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            observed_at = self._clock()
            gate = evaluate_attempt_gate(
                snapshot,
                actor_ref=context.subject_ref,
                work_id=arguments["work_id"],
                claim_id=arguments["claim_id"],
                expected_revision=arguments["expected_revision"],
                observed_at=observed_at,
            )
            if gate["decision"] != "allow":
                return _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    error_code=(
                        "conflict"
                        if gate["reason"] in {"stale_revision", "claim_mismatch"}
                        else "integrity"
                    ),
                    error_message=(
                        "experiment attempt gate denied read-only: "
                        + gate["reason"]
                    ),
                )
            if any(
                item["attempt_id"] == arguments["attempt_id"]
                for item in snapshot["experiment_attempts"]
            ):
                return _response(
                    tool=ATTEMPT_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="attempt identity already exists",
                )
            work = next(
                item
                for item in snapshot["works"]
                if item["work_id"] == arguments["work_id"]
            )
            attempt = {
                "attempt_id": arguments["attempt_id"],
                "work_id": work["work_id"],
                "claim_id": arguments["claim_id"],
                "actor_ref": context.subject_ref,
                "attempt_no": gate["attempt_no"],
                "experiment_contract_sha256": experiment_contract_sha256(work),
                "started_at": observed_at,
            }
            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                },
            )
            revision_after = arguments["expected_revision"] + 1
            changes = [
                {
                    "collection": "experiment_attempts",
                    "object_id": attempt["attempt_id"],
                    "value": attempt,
                }
            ]
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=event_id,
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                experiment_transition={
                    "operation": "attempt-started",
                    "request_sha256": request_fingerprint,
                    "attempt_id": attempt["attempt_id"],
                    "promotion_id": None,
                    "proposal_id": None,
                },
                schema_version=EVENT_SCHEMA_VERSION_V4,
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=ATTEMPT_TOOL,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=ATTEMPT_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(ATTEMPT_TOOL, arguments, context, response)
        return response

    def _promotion(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Propose or independently approve immutable Experiment promotion records."""
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            request_fingerprint = self._request_fingerprint(tool, arguments, context)
            event_id = self._event_id_factory(request_id)
            existing_event = next(
                (item for item in events if item["event_id"] == event_id), None
            )
            if existing_event is not None:
                transition = existing_event.get("experiment_transition")
                expected_operation = (
                    "promotion-proposed"
                    if tool == PROMOTION_PROPOSE_TOOL
                    else "promotion-approved"
                )
                if (
                    not isinstance(transition, dict)
                    or transition.get("operation") != expected_operation
                    or transition.get("request_sha256") != request_fingerprint
                    or events[-1]["event_id"] != event_id
                    or snapshot["project"]["revision"]
                    != existing_event["revision_after"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="promotion request identity conflicts with durable state",
                    )
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    result={
                        "snapshot": snapshot,
                        "revision": snapshot["project"]["revision"],
                        "event_head": {
                            "sequence_no": existing_event["sequence_no"],
                            "event_sha256": existing_event["event_sha256"],
                        },
                        "event": existing_event,
                        "registry_digest": self._registry_hash_value,
                        "capabilities": capability_manifest_to_document(self._manifest),
                    },
                )
                self._remember_request(tool, arguments, context, response)
                return response
            if snapshot.get("schema_version") != "context.typed-state/v3alpha1":
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="Experiment promotion requires typed-state v3alpha1",
                )
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            observed_at = self._clock()
            work = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            if work is None or work["kind"] != "experiment":
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="promotion requires an Experiment Work",
                )
            target = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == work["promotion_target_work_id"]
                ),
                None,
            )
            if (
                work["revision"] != arguments["expected_work_revision"]
                or target is None
                or target["revision"] != arguments["expected_target_work_revision"]
                or not target["mainline_authority"]
            ):
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="Experiment promotion source or target revision is stale",
                )
            lifecycle_gate = evaluate_experiment_activation_gate(
                snapshot,
                work_id=work["work_id"],
                observed_at=observed_at,
            )
            if lifecycle_gate["reason"] in {
                "trusted_time_invalid",
                "trusted_time_regressed",
                "experiment_expired",
            }:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message=(
                        "experiment promotion gate denied read-only: "
                        + lifecycle_gate["reason"]
                    ),
                )
            contract_digest = experiment_contract_sha256(work)
            if tool == PROMOTION_PROPOSE_TOOL:
                attempt = next(
                    (
                        item
                        for item in snapshot["experiment_attempts"]
                        if item["attempt_id"] == arguments["attempt_id"]
                    ),
                    None,
                )
                if (
                    attempt is None
                    or attempt["work_id"] != work["work_id"]
                    or attempt["actor_ref"] != context.subject_ref
                    or attempt["experiment_contract_sha256"] != contract_digest
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion proposal requires a bound Experiment attempt",
                    )
                if any(
                    item["proposal_id"] == arguments["proposal_id"]
                    for item in snapshot["experiment_promotions"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="promotion proposal identity already exists",
                    )
                criteria = arguments["criterion_evidence"]
                if set(criteria) != set(work["exit_criteria"]):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion criteria do not match the Experiment contract",
                    )
                evidence_by_id = {
                    item["evidence_id"]: item for item in snapshot["evidence"]
                }
                if any(
                    evidence_id not in evidence_by_id
                    for evidence_ids in criteria.values()
                    for evidence_id in evidence_ids
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion criterion evidence is unknown",
                    )
                promotion = {
                    "promotion_id": arguments["proposal_id"],
                    "kind": "proposed",
                    "proposal_id": arguments["proposal_id"],
                    "work_id": work["work_id"],
                    "target_work_id": target["work_id"],
                    "actor_ref": context.subject_ref,
                    "source_work_revision": work["revision"],
                    "target_work_revision": target["revision"],
                    "attempt_id": attempt["attempt_id"],
                    "experiment_contract_sha256": contract_digest,
                    "criterion_evidence": copy.deepcopy(criteria),
                    "created_at": observed_at,
                }
                operation = "promotion-proposed"
                promotion_id = promotion["promotion_id"]
                proposal_id = promotion["proposal_id"]
            else:
                proposal = next(
                    (
                        item
                        for item in snapshot["experiment_promotions"]
                        if item["kind"] == "proposed"
                        and item["proposal_id"] == arguments["proposal_id"]
                    ),
                    None,
                )
                if proposal is None or proposal["work_id"] != work["work_id"]:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion approval requires its prior proposal",
                    )
                if (
                    proposal["source_work_revision"]
                    != arguments["expected_work_revision"]
                    or proposal["target_work_revision"]
                    != arguments["expected_target_work_revision"]
                    or proposal["source_work_revision"] != work["revision"]
                    or proposal["target_work_revision"] != target["revision"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="promotion proposal revisions are stale",
                    )
                if proposal["actor_ref"] == context.subject_ref:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="independent_verifier_required",
                    )
                if any(
                    item["kind"] == "approved"
                    and item["proposal_id"] == proposal["proposal_id"]
                    for item in snapshot["experiment_promotions"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="promotion proposal is already approved",
                    )
                evidence_by_id = {
                    item["evidence_id"]: item for item in snapshot["evidence"]
                }
                if any(
                    evidence_by_id[evidence_id]["validity"] != "verified"
                    for evidence_ids in proposal["criterion_evidence"].values()
                    for evidence_id in evidence_ids
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion approval requires verified criterion evidence",
                    )
                if any(
                    effect["work_id"] == work["work_id"]
                    and effect["status"] in {"authorized", "started"}
                    for effect in snapshot["effects"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="promotion approval requires no pending Experiment effect",
                    )
                promotion = copy.deepcopy(proposal)
                promotion.update(
                    {
                        "promotion_id": arguments["approval_id"],
                        "kind": "approved",
                        "actor_ref": context.subject_ref,
                        "created_at": observed_at,
                    }
                )
                operation = "promotion-approved"
                promotion_id = promotion["promotion_id"]
                proposal_id = proposal["proposal_id"]

            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {
                    "collection": "experiment_promotions",
                    "object_id": promotion_id,
                    "value": promotion,
                },
            )
            revision_after = arguments["expected_revision"] + 1
            changes = [
                {
                    "collection": "experiment_promotions",
                    "object_id": promotion_id,
                    "value": promotion,
                }
            ]
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=event_id,
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=observed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                experiment_transition={
                    "operation": operation,
                    "request_sha256": request_fingerprint,
                    "attempt_id": None,
                    "promotion_id": promotion_id,
                    "proposal_id": proposal_id,
                },
                schema_version=EVENT_SCHEMA_VERSION_V4,
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=tool,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=tool,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(tool, arguments, context, response)
        return response

    def _effect_gate(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Return a typed read-only effect verdict at the current revision."""
        request_id = arguments["request_id"]
        try:
            snapshot = invoke_state_store(
                self._store, "read_project", arguments["project_id"]
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
        ) as exc:
            return _backend_error_response(
                tool=EFFECT_GATE_TOOL,
                request_id=request_id,
                error=exc,
            )
        work = next(
            (
                item
                for item in snapshot["works"]
                if item["work_id"] == arguments["work_id"]
            ),
            None,
        )
        if work is not None and work["kind"] == "experiment":
            verdict = {
                "schema_version": "context.effect-scope-verdict/v1alpha1",
                "decision": "deny",
                "read_only": True,
                "reason": "experiment_attempt_provenance_required",
            }
            return _response(
                tool=EFFECT_GATE_TOOL,
                request_id=request_id,
                result={
                    "verdict": verdict,
                    "revision": snapshot["project"]["revision"],
                    "registry_digest": self._registry_hash_value,
                    "capabilities": capability_manifest_to_document(self._manifest),
                },
            )
        verdict = evaluate_effect_scope_gate(
            snapshot,
            actor_ref=context.subject_ref,
            work_id=arguments["work_id"],
            claim_id=arguments["claim_id"],
            expected_revision=arguments["expected_revision"],
            operation=arguments["operation"],
            requested_scope=arguments["scope_ref"],
            effect_id=arguments["effect_id"],
            observed_at=self._clock(),
        )
        return _response(
            tool=EFFECT_GATE_TOOL,
            request_id=request_id,
            result={
                "verdict": verdict,
                "revision": snapshot["project"]["revision"],
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )

    def _effect(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
        tool: str = EFFECT_TOOL,
    ) -> dict[str, Any]:
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            if snapshot["schema_version"] in {
                "context.typed-state/v2alpha1",
                "context.typed-state/v3alpha1",
            }:
                try:
                    validate_scope(arguments["scope_ref"])
                except ValueError:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="invalid_request",
                        error_message="scope_ref is not canonical",
                    )
            observed_at = self._clock()
            work_for_effect = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            if work_for_effect is not None and work_for_effect["kind"] == "experiment":
                if tool != EXPERIMENT_EFFECT_TOOL:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="Experiment effect requires context.experiment.effect",
                    )
                if arguments["action"] == "authorize":
                    # An existing, immutable attempt retains authority for its
                    # bounded effects. Budget exhaustion blocks a new attempt,
                    # not work that already consumed the final permitted slot.
                    lifecycle_gate = experiment_time_verdict(
                        snapshot,
                        work_id=work_for_effect["work_id"],
                        observed_at=observed_at,
                    )
                    if lifecycle_gate["decision"] != "allow":
                        return _response(
                            tool=tool,
                            request_id=request_id,
                            error_code="integrity",
                            error_message=(
                                "Experiment effect gate denied read-only: "
                                + lifecycle_gate["reason"]
                            ),
                        )
                attempt = next(
                    (
                        item
                        for item in snapshot["experiment_attempts"]
                        if item["attempt_id"] == arguments["attempt_id"]
                    ),
                    None,
                )
                if (
                    attempt is None
                    or attempt["work_id"] != work_for_effect["work_id"]
                    or attempt["claim_id"] != arguments["claim_id"]
                    or attempt["actor_ref"] != context.subject_ref
                    or attempt["experiment_contract_sha256"]
                    != experiment_contract_sha256(work_for_effect)
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="Experiment effect attempt provenance is invalid",
                    )
            elif tool == EXPERIMENT_EFFECT_TOOL:
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="integrity",
                    error_message="context.experiment.effect requires an Experiment Work",
                )
            if arguments["action"] == "authorize":
                gate = evaluate_effect_scope_gate(
                    snapshot,
                    actor_ref=context.subject_ref,
                    work_id=arguments["work_id"],
                    claim_id=arguments["claim_id"],
                    expected_revision=arguments["expected_revision"],
                    operation=arguments["operation"],
                    requested_scope=arguments["scope_ref"],
                    effect_id=arguments["effect_id"],
                    observed_at=observed_at,
                )
            else:
                gate = evaluate_effect_completion_gate(
                    snapshot,
                    actor_ref=context.subject_ref,
                    work_id=arguments["work_id"],
                    claim_id=arguments["claim_id"],
                    expected_revision=arguments["expected_revision"],
                    effect_id=arguments["effect_id"],
                    effect_key=arguments["effect_key"],
                    operation=arguments["operation"],
                    requested_scope=arguments["scope_ref"],
                )
            if gate["decision"] != "allow":
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code=(
                        "conflict"
                        if gate["reason"]
                        in {
                            "stale_revision",
                            "claim_revision_mismatch",
                            "claim_expired",
                            "effect_scope_conflict",
                        }
                        else "integrity"
                    ),
                    error_message=f"effect scope gate denied read-only: {gate['reason']}",
                )
            claim = next(
                (item for item in snapshot["claims"] if item["claim_id"] == arguments["claim_id"]),
                None,
            )
            work = next(
                (item for item in snapshot["works"] if item["work_id"] == arguments["work_id"]),
                None,
            )
            assert claim is not None and work is not None

            existing = next(
                (item for item in snapshot["effects"] if item["effect_id"] == arguments["effect_id"]),
                None,
            )
            if arguments["action"] == "authorize":
                if existing is not None:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="effect identity already exists",
                    )
                if any(
                    item["effect_key"] == arguments["effect_key"]
                    for item in snapshot["effects"]
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_message="effect key already exists",
                    )
                effect = {
                    "effect_id": arguments["effect_id"],
                    "effect_key": arguments["effect_key"],
                    "work_id": work["work_id"],
                    "claim_id": claim["claim_id"],
                    "status": "authorized",
                    "operation": arguments["operation"],
                    "scope_ref": copy.deepcopy(arguments["scope_ref"]),
                    "expected_project_revision": arguments["expected_revision"] + 1,
                    "sequence_no": max(
                        (item["sequence_no"] for item in snapshot["effects"]),
                        default=0,
                    )
                    + 1,
                    "evidence_ids": copy.deepcopy(arguments["evidence_ids"]),
                    "result_ref": None,
                    "requested_at": observed_at,
                    "completed_at": None,
                }
                if snapshot["schema_version"] == "context.typed-state/v3alpha1":
                    effect["attempt_id"] = (
                        arguments["attempt_id"]
                        if tool == EXPERIMENT_EFFECT_TOOL
                        else None
                    )
            else:
                if existing is None:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="not_found",
                        error_message="effect was not found",
                    )
                if (
                    existing["effect_key"] != arguments["effect_key"]
                    or existing["work_id"] != work["work_id"]
                    or existing["claim_id"] != claim["claim_id"]
                    or existing["operation"] != arguments["operation"]
                    or existing["scope_ref"] != arguments["scope_ref"]
                    or existing["status"] not in {"authorized", "started"}
                    or (
                        snapshot["schema_version"] == "context.typed-state/v3alpha1"
                        and existing.get("attempt_id")
                        != (
                            arguments["attempt_id"]
                            if tool == EXPERIMENT_EFFECT_TOOL
                            else None
                        )
                    )
                ):
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="integrity",
                        error_message="effect provenance does not match the active claim",
                    )
                effect = copy.deepcopy(existing)
                effect.update(
                    {
                        "status": "succeeded",
                        "evidence_ids": copy.deepcopy(arguments["evidence_ids"]),
                        "result_ref": arguments["result_ref"],
                        "completed_at": observed_at,
                    }
                )

            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {"collection": "effects", "object_id": effect["effect_id"], "value": effect},
            )
            changes = [
                {"collection": "effects", "object_id": effect["effect_id"], "value": effect}
            ]
            if arguments["action"] == "complete":
                completed_claim = next(
                    item
                    for item in candidate["claims"]
                    if item["claim_id"] == claim["claim_id"]
                )
                lease_expires_at = datetime.fromisoformat(
                    completed_claim["lease_expires_at"].replace("Z", "+00:00")
                )
                completed_at = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
                if completed_at >= lease_expires_at:
                    completed_claim.update(
                        {"status": "expired", "released_at": observed_at}
                    )
                    _upsert_change(
                        changes, collection="claims", value=completed_claim
                    )
                    recovery_work = next(
                        item
                        for item in candidate["works"]
                        if item["work_id"] == completed_claim["work_id"]
                    )
                    recovery_work.update(
                        {
                            "status": "verifying",
                            "revision": recovery_work["revision"] + 1,
                        }
                    )
                    _upsert_change(changes, collection="works", value=recovery_work)
            revision_after = arguments["expected_revision"] + 1
            for existing_claim in candidate["claims"]:
                if existing_claim["status"] == "active":
                    existing_claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=existing_claim)
            for existing_effect in candidate["effects"]:
                if existing_effect["status"] in {"authorized", "started"}:
                    existing_effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=existing_effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=observed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=candidate["project"]["updated_at"],
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
                prior_events=(
                    events if event["supersedes_event_id"] is not None else None
                ),
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=tool,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=tool,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(tool, arguments, context, response)
        return response

    def _claim(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=CLAIM_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )
            observed_at = self._clock()
            work = next(
                (
                    item
                    for item in snapshot["works"]
                    if item["work_id"] == arguments["work_id"]
                ),
                None,
            )
            if work is not None and work["kind"] == "experiment":
                lifecycle_gate = evaluate_experiment_activation_gate(
                    snapshot,
                    work_id=work["work_id"],
                    observed_at=observed_at,
                )
                if lifecycle_gate["decision"] != "allow":
                    return _response(
                        tool=CLAIM_TOOL,
                        request_id=request_id,
                        error_code="integrity",
                        error_message=(
                            "experiment activation gate denied read-only: "
                            + lifecycle_gate["reason"]
                        ),
                    )
            gate = evaluate_claim_scope_gate(
                snapshot,
                actor_ref=context.subject_ref,
                work_id=arguments["work_id"],
                expected_revision=arguments["expected_revision"],
                requested_scopes=arguments["scope_owners"],
            )
            if gate["decision"] != "allow":
                return _response(
                    tool=CLAIM_TOOL,
                    request_id=request_id,
                    error_code=(
                        "conflict"
                        if gate["reason"] in {"stale_revision", "scope_conflict"}
                        else "integrity"
                    ),
                    error_message=f"claim scope gate denied read-only: {gate['reason']}",
                )
            assert work is not None
            if any(
                claim["claim_id"] == arguments["claim_id"]
                for claim in snapshot["claims"]
            ):
                return _response(
                    tool=CLAIM_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="claim identity already exists",
                )
            if any(
                claim["work_id"] == work["work_id"] and claim["status"] == "active"
                for claim in snapshot["claims"]
            ):
                return _response(
                    tool=CLAIM_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="Work already has an active claim",
                )

            claimed_at = observed_at
            work = copy.deepcopy(work)
            work["status"] = "active"
            work["revision"] += 1
            claim = {
                "claim_id": arguments["claim_id"],
                "work_id": work["work_id"],
                "actor_ref": context.subject_ref,
                "status": "active",
                "expected_project_revision": arguments["expected_revision"] + 1,
                "claimed_at": claimed_at,
                "lease_expires_at": arguments["lease_expires_at"],
                "released_at": None,
                "scope_owners": copy.deepcopy(arguments["scope_owners"]),
            }
            candidate = copy.deepcopy(snapshot)
            _replace_or_append(
                candidate,
                {"collection": "works", "object_id": work["work_id"], "value": work},
            )
            _replace_or_append(
                candidate,
                {"collection": "claims", "object_id": claim["claim_id"], "value": claim},
            )
            changes = [
                {"collection": "works", "object_id": work["work_id"], "value": work},
                {"collection": "claims", "object_id": claim["claim_id"], "value": claim},
            ]
            for existing_claim in candidate["claims"]:
                if existing_claim["status"] == "active":
                    existing_claim["expected_project_revision"] = arguments["expected_revision"] + 1
                    _upsert_change(changes, collection="claims", value=existing_claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = arguments["expected_revision"] + 1
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=arguments["expected_revision"] + 1,
                updated_at=claimed_at,
            )
            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type="state-transition",
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=claimed_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=None,
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
                prior_events=(
                    events if event["supersedes_event_id"] is not None else None
                ),
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=CLAIM_TOOL,
                request_id=request_id,
                error=exc,
            )
        response = _response(
            tool=CLAIM_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(CLAIM_TOOL, arguments, context, response)
        return response

    def _commit(
        self,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        request_id = arguments["request_id"]
        project_id = arguments["project_id"]
        try:
            snapshot = invoke_state_store(self._store, "read_project", project_id)
            events = invoke_state_store(self._store, "read_events", project_id)
            if snapshot["project"]["revision"] != arguments["expected_revision"]:
                return _response(
                    tool=COMMIT_TOOL,
                    request_id=request_id,
                    error_code="conflict",
                    error_message="expected revision is stale",
                )

            changes = copy.deepcopy(arguments["changes"])
            candidate = copy.deepcopy(snapshot)
            for change in changes:
                _replace_or_append(candidate, change)

            revision_after = arguments["expected_revision"] + 1
            occurred_at = self._clock()
            for claim in candidate["claims"]:
                if claim["status"] == "active":
                    claim["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="claims", value=claim)
            for effect in candidate["effects"]:
                if effect["status"] in {"authorized", "started"}:
                    effect["expected_project_revision"] = revision_after
                    _upsert_change(changes, collection="effects", value=effect)
            _derive_project_projection(
                candidate,
                revision=revision_after,
                updated_at=occurred_at,
            )

            previous_hash = events[-1]["event_sha256"] if events else None
            sequence_no = events[-1]["sequence_no"] + 1 if events else 1
            event = build_state_event(
                event_id=self._event_id_factory(request_id),
                event_type=(
                    "correction"
                    if arguments["supersedes_event_id"] is not None
                    else "state-transition"
                ),
                project_id=project_id,
                sequence_no=sequence_no,
                revision_before=arguments["expected_revision"],
                occurred_at=occurred_at,
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=previous_hash,
                supersedes_event_id=arguments["supersedes_event_id"],
                changes=changes,
                project_after=candidate["project"],
                schema_version=_event_schema_version(snapshot),
            )
            expected_snapshot = replay_state_events(
                snapshot,
                [event],
                starting_sequence_no=sequence_no,
                previous_event_sha256=previous_hash,
                prior_events=(
                    events if event["supersedes_event_id"] is not None else None
                ),
            )
            invoke_state_store(
                self._store,
                "commit_event",
                project_id=project_id,
                expected_revision=arguments["expected_revision"],
                event=event,
                expected_snapshot=expected_snapshot,
            )
        except (
            StateStoreCapabilityError,
            StateStoreNotFound,
            StateStoreConflict,
            StateStoreBusy,
            StateStoreIntegrityError,
            StateEventError,
            TypedStateError,
        ) as exc:
            return _backend_error_response(
                tool=COMMIT_TOOL,
                request_id=request_id,
                error=exc,
            )
        except (KeyError, TypeError):
            return _response(
                tool=COMMIT_TOOL,
                request_id=request_id,
                error_code="integrity",
                error_message="state intent contains an invalid nested value",
            )

        response = _response(
            tool=COMMIT_TOOL,
            request_id=request_id,
            result={
                "snapshot": expected_snapshot,
                "revision": expected_snapshot["project"]["revision"],
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
                "registry_digest": self._registry_hash_value,
                "capabilities": capability_manifest_to_document(self._manifest),
            },
        )
        self._remember_request(COMMIT_TOOL, arguments, context, response)
        return response
