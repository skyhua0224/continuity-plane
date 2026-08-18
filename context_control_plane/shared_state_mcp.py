"""Provider-neutral State MCP v3 boundary for the shared Work ledger."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

from .claim_evidence_gate import (
    ClaimEvidenceError,
    validate_claim_evidence_claim,
    validate_claim_evidence_verdict,
)
from .shared_work_ledger import ClaimLifecycleError, WorkLedger
from .sqlite_state_store import SQLiteStateConflict, SQLiteStateIntegrityError
from .sqlite_work_ledger import SQLiteWorkLedgerConflict, SQLiteWorkLedgerError
from .state_mcp import RequestContext
from .verification_profile import (
    VerificationProfileError,
    validate_verification_decision,
)

REQUEST_SCHEMA_VERSION = "context.state-mcp-request/v3alpha1"
RESPONSE_SCHEMA_VERSION = "context.state-mcp-response/v3alpha1"
WORK_COMPLETION_REQUEST_SCHEMA_VERSION = "context.state-work-completion-request/v1alpha1"
WORK_COMPLETION_RESPONSE_SCHEMA_VERSION = "context.state-work-completion-response/v1alpha1"

CLAIM_LIFECYCLE_TOOL = "context.state.claim.lifecycle"
EFFECT_DISPATCH_TOOL = "context.state.effect.dispatch"
WORK_COMPLETION_TOOL = "context.state.work.complete"
SHARED_STATE_MCP_TOOLS = (
    CLAIM_LIFECYCLE_TOOL,
    EFFECT_DISPATCH_TOOL,
    WORK_COMPLETION_TOOL,
)

_ACTIONS = ("acquire", "heartbeat", "release", "revoke", "expire", "reclaim")
_COMMON_FIELDS = {"schema_version", "request_id", "project_id"}
_TOKEN_FIELDS = {
    "expected_project_revision",
    "expected_claim_revision",
    "lease_epoch",
    "fence",
}
_LIFECYCLE_FIELDS = {
    "acquire": _COMMON_FIELDS
    | {
        "action",
        "expected_project_revision",
        "work_id",
        "claim_id",
        "requested_ttl_ms",
        "scope_owners",
    },
    "heartbeat": _COMMON_FIELDS
    | _TOKEN_FIELDS
    | {"action", "claim_id", "requested_ttl_ms"},
    "release": _COMMON_FIELDS | _TOKEN_FIELDS | {"action", "claim_id"},
    "revoke": _COMMON_FIELDS | _TOKEN_FIELDS | {"action", "claim_id", "reason"},
    "expire": _COMMON_FIELDS | _TOKEN_FIELDS | {"action", "claim_id"},
    "reclaim": _COMMON_FIELDS
    | _TOKEN_FIELDS
    | {
        "action",
        "old_claim_id",
        "new_claim_id",
        "requested_ttl_ms",
        "scope_owners",
    },
}
_EFFECT_FIELDS = (
    _COMMON_FIELDS
    | _TOKEN_FIELDS
    | {
        "effect_id",
        "effect_key",
        "request_sha256",
        "claim_id",
        "work_id",
        "operation",
        "scope_ref",
    }
)
_WORK_COMPLETION_FIELDS = (
    _COMMON_FIELDS
    | _TOKEN_FIELDS
    | {
        "work_id",
        "claim_id",
        "expected_work_revision",
        "evidence_ids",
        "verification_decision_sha256",
        "claim_evidence_verdict_sha256",
    }
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SCOPE_KINDS = {"repo", "directory", "file", "symbol", "capability", "effect"}


class SharedStateMCPAuthorizer(Protocol):
    """Authorize a trusted request context for one project action."""

    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool: ...


class CompletionEvidenceResolver(Protocol):
    """Resolve immutable verifier and claim-evidence records by digest."""

    def resolve_verification_decision(self, digest: str) -> dict[str, Any] | None: ...

    def resolve_claim_evidence_verdict(self, digest: str) -> dict[str, Any] | None: ...


class _DenyAllAuthorizer:
    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        return False


class _TrustedClockError(RuntimeError):
    """Raised when the provider cannot supply a valid authority timestamp."""


def _scope_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["scope_kind", "scope_ref"],
        "properties": {
            "scope_kind": {"enum": sorted(_SCOPE_KINDS)},
            "scope_ref": {"type": "string", "minLength": 1, "maxLength": 1_000},
        },
    }


def _common_properties(schema_version: str = REQUEST_SCHEMA_VERSION) -> dict[str, Any]:
    return {
        "schema_version": {"const": schema_version},
        "request_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "project_id": {"type": "string", "minLength": 1, "maxLength": 256},
    }


def _token_properties() -> dict[str, Any]:
    return {
        "expected_project_revision": {"type": "integer", "minimum": 0},
        "expected_claim_revision": {"type": "integer", "minimum": 1},
        "lease_epoch": {"type": "integer", "minimum": 1},
        "fence": {"type": "integer", "minimum": 1},
    }


def _object_schema(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": copy.deepcopy(properties),
    }


def _lifecycle_schema(action: str) -> dict[str, Any]:
    properties = {
        **_common_properties(),
        "action": {"const": action},
        "expected_project_revision": {"type": "integer", "minimum": 0},
    }
    if action == "acquire":
        properties.update(
            {
                "work_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "claim_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "requested_ttl_ms": {"type": "integer", "minimum": 1},
                "scope_owners": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 128,
                    "uniqueItems": True,
                    "items": _scope_schema(),
                },
            }
        )
    else:
        properties.update(
            {
                key: value
                for key, value in _token_properties().items()
                if key != "expected_project_revision"
            }
        )
        if action == "reclaim":
            properties.update(
                {
                    "old_claim_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                    },
                    "new_claim_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                    },
                    "requested_ttl_ms": {"type": "integer", "minimum": 1},
                    "scope_owners": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 128,
                        "uniqueItems": True,
                        "items": _scope_schema(),
                    },
                }
            )
        else:
            properties["claim_id"] = {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
            }
        if action == "heartbeat":
            properties["requested_ttl_ms"] = {"type": "integer", "minimum": 1}
        if action == "revoke":
            properties["reason"] = {
                "type": "string",
                "minLength": 1,
                "maxLength": 512,
            }
    return _object_schema(properties)


def _effect_schema() -> dict[str, Any]:
    properties = {
        **_common_properties(),
        **_token_properties(),
        "effect_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "effect_key": {"type": "string", "minLength": 1, "maxLength": 256},
        "request_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "claim_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "work_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "operation": {"type": "string", "minLength": 1, "maxLength": 256},
        "scope_ref": _scope_schema(),
    }
    return _object_schema(properties)


def _work_completion_schema() -> dict[str, Any]:
    properties = {
        **_common_properties(WORK_COMPLETION_REQUEST_SCHEMA_VERSION),
        **_token_properties(),
        "expected_work_revision": {"type": "integer", "minimum": 0},
        "work_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "claim_id": {"type": "string", "minLength": 1, "maxLength": 256},
        "evidence_ids": {
            "type": "array",
            "minItems": 1,
            "maxItems": 256,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 256},
        },
        "verification_decision_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
        "claim_evidence_verdict_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$",
        },
    }
    return _object_schema(properties)


def shared_state_mcp_tool_definitions() -> list[dict[str, Any]]:
    """Return strict provider-neutral input schemas for the v3 tools."""
    return [
        {
            "name": CLAIM_LIFECYCLE_TOOL,
            "description": "Apply one fenced claim lifecycle transition.",
            "inputSchema": {"oneOf": [_lifecycle_schema(item) for item in _ACTIONS]},
        },
        {
            "name": EFFECT_DISPATCH_TOOL,
            "description": "Fence and persist the start of one external effect.",
            "inputSchema": _effect_schema(),
        },
        {
            "name": WORK_COMPLETION_TOOL,
            "description": "Atomically complete verified Work and release its fenced Claim.",
            "inputSchema": _work_completion_schema(),
        },
    ]


def _response(
    *,
    tool: str,
    request_id: str | None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_reason: str | None = None,
) -> dict[str, Any]:
    ok = error_code is None
    return {
        "schema_version": (
            WORK_COMPLETION_RESPONSE_SCHEMA_VERSION
            if tool == WORK_COMPLETION_TOOL
            else RESPONSE_SCHEMA_VERSION
        ),
        "request_id": request_id,
        "tool": tool,
        "ok": ok,
        "result": copy.deepcopy(result) if ok else None,
        "error": (
            None
            if ok
            else {
                "code": error_code,
                "reason": error_reason or "request_failed",
            }
        ),
    }


def _request_id(arguments: Any) -> str | None:
    if isinstance(arguments, dict) and isinstance(arguments.get("request_id"), str):
        return arguments["request_id"]
    return None


def _rejection_code(tool: str) -> str:
    if tool == CLAIM_LIFECYCLE_TOOL:
        return "claim_rejected"
    if tool == EFFECT_DISPATCH_TOOL:
        return "dispatch_rejected"
    return "completion_rejected"


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value) is not None


def _scope_valid(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"scope_kind", "scope_ref"}
        and value["scope_kind"] in _SCOPE_KINDS
        and isinstance(value["scope_ref"], str)
        and bool(value["scope_ref"].strip())
        and len(value["scope_ref"]) <= 1_000
    )


def _positive_integer(value: Any, *, minimum: int = 1) -> bool:
    return type(value) is int and value >= minimum


def _validate_common(
    arguments: dict[str, Any], *, schema_version: str = REQUEST_SCHEMA_VERSION
) -> str | None:
    if arguments.get("schema_version") != schema_version:
        return "unsupported_schema_version"
    if not _identifier(arguments.get("request_id")):
        return "invalid_request_id"
    if not _identifier(arguments.get("project_id")):
        return "invalid_project_id"
    return None


def _validate_tokens(arguments: dict[str, Any], *, claim_tokens: bool) -> str | None:
    if not _positive_integer(arguments.get("expected_project_revision"), minimum=0):
        return "invalid_expected_project_revision"
    if claim_tokens:
        for field in ("expected_claim_revision", "lease_epoch", "fence"):
            if not _positive_integer(arguments.get(field)):
                return f"invalid_{field}"
    return None


def _validate_scopes(value: Any) -> bool:
    if not isinstance(value, list) or not value or len(value) > 128:
        return False
    canonical: set[str] = set()
    for scope in value:
        if not _scope_valid(scope):
            return False
        encoded = json.dumps(scope, sort_keys=True, separators=(",", ":"))
        if encoded in canonical:
            return False
        canonical.add(encoded)
    return True


def _validate_lifecycle(arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return "request_must_be_object"
    action = arguments.get("action")
    if action not in _LIFECYCLE_FIELDS:
        return "unsupported_action"
    expected = _LIFECYCLE_FIELDS[action]
    if set(arguments) != expected:
        return "unexpected_fields" if set(arguments) - expected else "missing_fields"
    common_error = _validate_common(arguments)
    if common_error:
        return common_error
    token_error = _validate_tokens(arguments, claim_tokens=action != "acquire")
    if token_error:
        return token_error
    identifiers = (
        ("work_id", "claim_id")
        if action == "acquire"
        else (
            ("old_claim_id", "new_claim_id") if action == "reclaim" else ("claim_id",)
        )
    )
    for field in identifiers:
        if not _identifier(arguments[field]):
            return f"invalid_{field}"
    if action in {"acquire", "heartbeat", "reclaim"} and not _positive_integer(
        arguments["requested_ttl_ms"]
    ):
        return "invalid_requested_ttl_ms"
    if action in {"acquire", "reclaim"} and not _validate_scopes(
        arguments["scope_owners"]
    ):
        return "invalid_scope_owners"
    if action == "revoke":
        reason = arguments["reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            return "invalid_reason"
    return None


def _validate_effect(arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return "request_must_be_object"
    if set(arguments) != _EFFECT_FIELDS:
        return (
            "unexpected_fields" if set(arguments) - _EFFECT_FIELDS else "missing_fields"
        )
    common_error = _validate_common(arguments)
    if common_error:
        return common_error
    token_error = _validate_tokens(arguments, claim_tokens=True)
    if token_error:
        return token_error
    for field in ("effect_id", "effect_key", "claim_id", "work_id", "operation"):
        if not _identifier(arguments[field]):
            return f"invalid_{field}"
    if (
        not isinstance(arguments["request_sha256"], str)
        or _SHA256_RE.fullmatch(arguments["request_sha256"]) is None
    ):
        return "invalid_request_sha256"
    if not _scope_valid(arguments["scope_ref"]):
        return "invalid_scope_ref"
    return None


def _validate_work_completion(arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return "request_must_be_object"
    if set(arguments) != _WORK_COMPLETION_FIELDS:
        return (
            "unexpected_fields"
            if set(arguments) - _WORK_COMPLETION_FIELDS
            else "missing_fields"
        )
    common_error = _validate_common(
        arguments, schema_version=WORK_COMPLETION_REQUEST_SCHEMA_VERSION
    )
    if common_error:
        return common_error
    token_error = _validate_tokens(arguments, claim_tokens=True)
    if token_error:
        return token_error
    if not _positive_integer(arguments["expected_work_revision"], minimum=0):
        return "invalid_expected_work_revision"
    for field in ("work_id", "claim_id"):
        if not _identifier(arguments[field]):
            return f"invalid_{field}"
    evidence_ids = arguments["evidence_ids"]
    if (
        not isinstance(evidence_ids, list)
        or not evidence_ids
        or len(evidence_ids) > 256
        or len(evidence_ids) != len(set(evidence_ids))
        or any(not _identifier(item) for item in evidence_ids)
    ):
        return "invalid_evidence_ids"
    for field in (
        "verification_decision_sha256",
        "claim_evidence_verdict_sha256",
    ):
        value = arguments[field]
        if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
            return f"invalid_{field}"
    return None


class SharedStateMCPService:
    """Authorize and atomically dispatch v3 writes to one shared Work ledger."""

    def __init__(
        self,
        ledger: Any,
        *,
        authorizer: SharedStateMCPAuthorizer | None = None,
        clock: Callable[[], str],
        completion_evidence_resolver: CompletionEvidenceResolver | None = None,
    ) -> None:
        persistent_ledger = all(
            callable(getattr(ledger, operation, None))
            for operation in ("execute_work_ledger", "read_work_ledger")
        )
        if not isinstance(ledger, WorkLedger) and not persistent_ledger:
            raise TypeError("ledger must provide the Work Ledger authority contract")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._ledger = ledger
        self._persistent_ledger = persistent_ledger
        self._authorizer = authorizer or _DenyAllAuthorizer()
        self._clock = clock
        self._completion_evidence_resolver = completion_evidence_resolver
        self._request_receipts: dict[
            tuple[str, str], tuple[str, dict[str, Any]]
        ] = {}
        self._mutation_lock = threading.Lock()

    def resolve_committed_effect_dispatch(
        self, project_id: str, request_id: str
    ) -> dict[str, Any]:
        """Resolve one effect dispatch previously committed by this authority."""
        if not _identifier(project_id) or not _identifier(request_id):
            raise LookupError("committed effect dispatch identity is invalid")
        with self._mutation_lock:
            stored = self._request_receipts.get(
                (project_id, request_id)
            )
            if stored is not None:
                response = stored[1]
                result = response.get("result") if response.get("ok") is True else None
            elif self._persistent_ledger and callable(
                getattr(self._ledger, "read_work_ledger_receipt", None)
            ):
                try:
                    result = self._ledger.read_work_ledger_receipt(
                        project_id,
                        "start_effect_dispatch",
                        request_id,
                    )
                except Exception as exc:
                    raise LookupError(
                        "committed effect dispatch is unavailable"
                    ) from exc
            else:
                result = None
            if result is None:
                raise LookupError("committed effect dispatch is unavailable")
            if not isinstance(result, dict):
                raise TypeError("committed effect dispatch receipt is invalid")
            receipt = result.get("receipt")
            if not isinstance(receipt, dict) or receipt.get("project_id") != project_id:
                raise LookupError("committed effect dispatch project does not match")
            return copy.deepcopy(result)

    @staticmethod
    def _fingerprint(
        tool: str,
        arguments: dict[str, Any],
        context: RequestContext,
    ) -> str:
        body = {
            "tool": tool,
            "arguments": arguments,
            "subject_ref": context.subject_ref,
            "authorization_ref": context.authorization_ref,
        }
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _authorized(
        self,
        *,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        try:
            return self._authorizer.authorize(context, action, project_id) is True
        except Exception:  # noqa: BLE001
            return False

    def _trusted_timestamp(self) -> str:
        try:
            observed_at = self._clock()
        except Exception as exc:
            raise _TrustedClockError("trusted_clock_failed") from exc
        if not isinstance(observed_at, str):
            raise _TrustedClockError("trusted_clock_failed")
        try:
            parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _TrustedClockError("trusted_clock_failed") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise _TrustedClockError("trusted_clock_failed")
        return observed_at

    def _dispatch_lifecycle(
        self,
        arguments: dict[str, Any],
        *,
        actor_ref: str,
        observed_at: str,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        action = arguments["action"]
        common = {
            "expected_project_revision": arguments["expected_project_revision"],
            "observed_at": observed_at,
        }
        if action == "acquire":
            operation_arguments = {
                **common,
                "work_id": arguments["work_id"],
                "actor_ref": actor_ref,
                "requested_ttl_ms": arguments["requested_ttl_ms"],
                "claim_id": arguments["claim_id"],
                "scope_owners": arguments["scope_owners"],
            }
            if self._persistent_ledger:
                return self._execute_persistent(
                    operation="acquire_claim",
                    arguments=arguments,
                    operation_arguments=operation_arguments,
                    request_fingerprint=request_fingerprint,
                )
            return self._ledger.acquire_claim(**operation_arguments)
        tokens = {
            **common,
            "expected_claim_revision": arguments["expected_claim_revision"],
            "lease_epoch": arguments["lease_epoch"],
            "fence": arguments["fence"],
        }
        if action == "heartbeat":
            operation_arguments = {
                **tokens,
                "claim_id": arguments["claim_id"],
                "actor_ref": actor_ref,
                "requested_ttl_ms": arguments["requested_ttl_ms"],
            }
            operation = "heartbeat_claim"
        elif action == "release":
            operation_arguments = {
                **tokens,
                "claim_id": arguments["claim_id"],
                "actor_ref": actor_ref,
            }
            operation = "release_claim"
        elif action == "revoke":
            operation_arguments = {
                **tokens,
                "claim_id": arguments["claim_id"],
                "revoker_ref": actor_ref,
                "reason": arguments["reason"],
            }
            operation = "revoke_claim"
        elif action == "expire":
            operation_arguments = {
                **tokens,
                "claim_id": arguments["claim_id"],
            }
            operation = "expire_claim"
        else:
            operation_arguments = {
                **tokens,
                "old_claim_id": arguments["old_claim_id"],
                "new_claim_id": arguments["new_claim_id"],
                "new_actor_ref": actor_ref,
                "requested_ttl_ms": arguments["requested_ttl_ms"],
                "scope_owners": arguments["scope_owners"],
            }
            operation = "reclaim_claim"
        if self._persistent_ledger:
            return self._execute_persistent(
                operation=operation,
                arguments=arguments,
                operation_arguments=operation_arguments,
                request_fingerprint=request_fingerprint,
            )
        return getattr(self._ledger, operation)(**operation_arguments)

    def _execute_persistent(
        self,
        *,
        operation: str,
        arguments: dict[str, Any],
        operation_arguments: dict[str, Any],
        request_fingerprint: str,
    ) -> dict[str, Any]:
        return self._ledger.execute_work_ledger(
            project_id=arguments["project_id"],
            operation=operation,
            request_id=arguments["request_id"],
            arguments=operation_arguments,
            request_payload={"mcp_request_sha256": request_fingerprint},
        )

    def _dispatch_effect(
        self,
        arguments: dict[str, Any],
        *,
        actor_ref: str,
        observed_at: str,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        operation_arguments = {
            "request_id": arguments["request_id"],
            "effect_id": arguments["effect_id"],
            "effect_key": arguments["effect_key"],
            "request_sha256": arguments["request_sha256"],
            "claim_id": arguments["claim_id"],
            "work_id": arguments["work_id"],
            "actor_ref": actor_ref,
            "expected_project_revision": arguments["expected_project_revision"],
            "expected_claim_revision": arguments["expected_claim_revision"],
            "lease_epoch": arguments["lease_epoch"],
            "fence": arguments["fence"],
            "observed_at": observed_at,
            "operation": arguments["operation"],
            "scope_ref": arguments["scope_ref"],
        }
        if self._persistent_ledger:
            return self._execute_persistent(
                operation="start_effect_dispatch",
                arguments=arguments,
                operation_arguments=operation_arguments,
                request_fingerprint=request_fingerprint,
            )
        return self._ledger.start_effect_dispatch(**operation_arguments)

    def _dispatch_work_completion(
        self,
        arguments: dict[str, Any],
        *,
        actor_ref: str,
        observed_at: str,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        operation_arguments = {
            "work_id": arguments["work_id"],
            "claim_id": arguments["claim_id"],
            "actor_ref": actor_ref,
            "expected_project_revision": arguments["expected_project_revision"],
            "expected_work_revision": arguments["expected_work_revision"],
            "expected_claim_revision": arguments["expected_claim_revision"],
            "lease_epoch": arguments["lease_epoch"],
            "fence": arguments["fence"],
            "observed_at": observed_at,
            "evidence_ids": arguments["evidence_ids"],
            "verification_decision_sha256": arguments[
                "verification_decision_sha256"
            ],
            "claim_evidence_verdict_sha256": arguments[
                "claim_evidence_verdict_sha256"
            ],
        }
        if self._persistent_ledger and callable(
            getattr(self._ledger, "read_work_ledger_receipt", None)
        ):
            committed = self._ledger.read_work_ledger_receipt(
                arguments["project_id"],
                "complete_work",
                arguments["request_id"],
            )
            if committed is not None:
                return self._execute_persistent(
                    operation="complete_work",
                    arguments=arguments,
                    operation_arguments=operation_arguments,
                    request_fingerprint=request_fingerprint,
                )
        current_revision = (
            self._ledger.read_work_ledger(arguments["project_id"])[
                "project_revision"
            ]
            if self._persistent_ledger
            else self._ledger.project_revision
        )
        if current_revision != arguments["expected_project_revision"]:
            raise ClaimLifecycleError("stale_revision")
        self._validate_completion_evidence(arguments, observed_at)
        if self._persistent_ledger:
            return self._execute_persistent(
                operation="complete_work",
                arguments=arguments,
                operation_arguments=operation_arguments,
                request_fingerprint=request_fingerprint,
            )
        return self._ledger.complete_work(**operation_arguments)

    def _validate_completion_evidence(
        self, arguments: dict[str, Any], observed_at: str
    ) -> None:
        resolver = self._completion_evidence_resolver
        if resolver is None:
            raise ClaimLifecycleError("completion_evidence_resolver_required")
        try:
            resolved_decision = resolver.resolve_verification_decision(
                arguments["verification_decision_sha256"]
            )
            resolved_verdict = resolver.resolve_claim_evidence_verdict(
                arguments["claim_evidence_verdict_sha256"]
            )
        except Exception as exc:  # noqa: BLE001
            raise ClaimLifecycleError("completion_evidence_unavailable") from exc
        if not isinstance(resolved_decision, dict) or not isinstance(
            resolved_verdict, dict
        ):
            raise ClaimLifecycleError("completion_evidence_unavailable")
        try:
            decision = resolved_decision["decision"]
            profile = resolved_decision["profile"]
            adapter = resolved_decision["adapter"]
            verdict = resolved_verdict["verdict"]
            claim = resolved_verdict["claim"]
            validate_verification_decision(decision, profile=profile, adapter=adapter)
            validate_claim_evidence_claim(claim)
            validate_claim_evidence_verdict(verdict)
        except (KeyError, TypeError, VerificationProfileError, ClaimEvidenceError) as exc:
            raise ClaimLifecycleError("completion_evidence_invalid") from exc
        if decision["decision_sha256"] != arguments["verification_decision_sha256"]:
            raise ClaimLifecycleError("verification_digest_mismatch")
        if verdict["verdict_sha256"] != arguments["claim_evidence_verdict_sha256"]:
            raise ClaimLifecycleError("claim_evidence_digest_mismatch")
        if decision["overall_status"] != "satisfied":
            raise ClaimLifecycleError("verification_not_satisfied")
        if (
            decision["project_id"] != arguments["project_id"]
            or decision["work_id"] != arguments["work_id"]
            or decision["project_revision"] != arguments["expected_project_revision"]
            or decision["completion_authority"] is not False
            or decision["state_write_authority"] is not False
        ):
            raise ClaimLifecycleError("verification_binding_mismatch")
        claim_body = json.dumps(
            claim,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if verdict["claim_sha256"] != hashlib.sha256(claim_body).hexdigest():
            raise ClaimLifecycleError("claim_evidence_binding_mismatch")
        if (
            verdict["decision"] != "allow"
            or verdict["claim_id"] != arguments["claim_id"]
            or claim["claim_id"] != arguments["claim_id"]
            or claim["work_id"] != arguments["work_id"]
            or claim["claim_kind"] != "completion"
            or sorted(verdict["evidence_ids"])
            != sorted(arguments["evidence_ids"])
            or sorted(claim["evidence_assertion_ids"])
            != sorted(arguments["evidence_ids"])
        ):
            raise ClaimLifecycleError("claim_evidence_binding_mismatch")
        try:
            observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
            evaluated = (
                datetime.fromisoformat(decision["evaluated_at"].replace("Z", "+00:00")),
                datetime.fromisoformat(verdict["evaluated_at"].replace("Z", "+00:00")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ClaimLifecycleError("completion_evidence_timestamp_invalid") from exc
        if any(item > observed for item in evaluated):
            raise ClaimLifecycleError("completion_evidence_from_future")

    def call_tool(
        self,
        tool: str,
        arguments: Any,
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Validate, authorize, replay, and execute one shared-state write."""
        request_id = _request_id(arguments)
        if tool not in SHARED_STATE_MCP_TOOLS:
            return _response(
                tool=str(tool),
                request_id=request_id,
                error_code="unsupported",
                error_reason="unsupported_tool",
            )
        if tool == CLAIM_LIFECYCLE_TOOL:
            validation_error = _validate_lifecycle(arguments)
        elif tool == EFFECT_DISPATCH_TOOL:
            validation_error = _validate_effect(arguments)
        else:
            validation_error = _validate_work_completion(arguments)
        if validation_error:
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="invalid_request",
                error_reason=validation_error,
            )
        if not isinstance(context, RequestContext):
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="permission_denied",
                error_reason="trusted_context_required",
            )
        authorization_action = (
            f"state.claim.{arguments['action']}"
            if tool == CLAIM_LIFECYCLE_TOOL
            else (
                "state.effect.dispatch"
                if tool == EFFECT_DISPATCH_TOOL
                else "state.work.complete"
            )
        )
        if not self._authorized(
            context=context,
            action=authorization_action,
            project_id=arguments["project_id"],
        ):
            return _response(
                tool=tool,
                request_id=request_id,
                error_code="permission_denied",
                error_reason="authorization_denied",
            )

        fingerprint = self._fingerprint(tool, arguments, context)
        with self._mutation_lock:
            cache_key = (
                arguments["project_id"],
                arguments["request_id"],
            )
            previous = self._request_receipts.get(cache_key)
            if previous is not None:
                if previous[0] != fingerprint:
                    return _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_reason="request_id_reused",
                    )
                return copy.deepcopy(previous[1])
            if (
                not self._persistent_ledger
                and arguments["project_id"] != self._ledger.project_id
            ):
                return _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="not_found",
                    error_reason="project_not_found",
                )
            try:
                observed_at = self._trusted_timestamp()
                if tool == CLAIM_LIFECYCLE_TOOL:
                    result = self._dispatch_lifecycle(
                        arguments,
                        actor_ref=context.subject_ref,
                        observed_at=observed_at,
                        request_fingerprint=fingerprint,
                    )
                elif tool == EFFECT_DISPATCH_TOOL:
                    result = self._dispatch_effect(
                        arguments,
                        actor_ref=context.subject_ref,
                        observed_at=observed_at,
                        request_fingerprint=fingerprint,
                    )
                else:
                    result = self._dispatch_work_completion(
                        arguments,
                        actor_ref=context.subject_ref,
                        observed_at=observed_at,
                        request_fingerprint=fingerprint,
                    )
            except ClaimLifecycleError as exc:
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    error_code=_rejection_code(tool),
                    error_reason=exc.code,
                )
            except (SQLiteWorkLedgerConflict, SQLiteStateConflict) as exc:
                reason = str(exc)
                if reason == "project does not exist":
                    response = _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="not_found",
                        error_reason="project_not_found",
                    )
                elif reason == "request_id was reused for a different payload":
                    response = _response(
                        tool=tool,
                        request_id=request_id,
                        error_code="conflict",
                        error_reason="request_id_reused",
                    )
                else:
                    response = _response(
                        tool=tool,
                        request_id=request_id,
                        error_code=_rejection_code(tool),
                        error_reason=reason,
                    )
            except SQLiteStateIntegrityError as exc:
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    error_code=_rejection_code(tool),
                    error_reason=(
                        "state_integrity_rejected"
                        if tool == WORK_COMPLETION_TOOL
                        else str(exc)
                    ),
                )
            except SQLiteWorkLedgerError:
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="service_unavailable",
                    error_reason="ledger_unavailable",
                )
            except _TrustedClockError as exc:
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="service_unavailable",
                    error_reason=str(exc),
                )
            except Exception:  # noqa: BLE001
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    error_code="service_unavailable",
                    error_reason="ledger_unavailable",
                )
            else:
                response = _response(
                    tool=tool,
                    request_id=request_id,
                    result=result,
                )
            if response["ok"]:
                self._request_receipts[cache_key] = (
                    fingerprint,
                    copy.deepcopy(response),
                )
            return response


__all__ = [
    "CLAIM_LIFECYCLE_TOOL",
    "EFFECT_DISPATCH_TOOL",
    "REQUEST_SCHEMA_VERSION",
    "RESPONSE_SCHEMA_VERSION",
    "WORK_COMPLETION_REQUEST_SCHEMA_VERSION",
    "WORK_COMPLETION_RESPONSE_SCHEMA_VERSION",
    "SHARED_STATE_MCP_TOOLS",
    "WORK_COMPLETION_TOOL",
    "CompletionEvidenceResolver",
    "SharedStateMCPAuthorizer",
    "SharedStateMCPService",
    "shared_state_mcp_tool_definitions",
]
