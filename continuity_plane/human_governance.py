"""Transport-neutral controlled entry for human governance actions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
from typing import Any, Protocol

from .authorization_audit import (
    GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION,
    AuthorizationAuditError,
    validate_authorization_audit_event,
)
from .state_events import StateEventError, validate_state_event
from .state_mcp import RequestContext

HUMAN_GOVERNANCE_ACTIONS = frozenset(
    {
        "context.experiment.promotion.propose",
        "context.experiment.promotion.approve",
        "context.idea.correction.protect",
        "context.idea.correction.release",
    }
)
REQUEST_SCHEMA_VERSION = "context.human-governance-request/v1alpha1"
RESPONSE_SCHEMA_VERSION = "context.human-governance-response/v1alpha1"
MAX_REQUEST_BYTES = 32 * 1024
MAX_LIST_ITEMS = 256
MAX_CRITERIA = 16
MAX_CRITERION_EVIDENCE = 64
MAX_TOTAL_CRITERION_EVIDENCE = 1024
DEFAULT_REQUEST_CACHE_ENTRIES = 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,1023}$")
_PRINCIPAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "project_id",
    "expected_revision",
    "action",
    "payload",
}
_AUTHORITY = {
    "state_write_authority": False,
    "completion_authority": False,
    "approval_authority": False,
    "provider_native_authority": False,
    "external_effect_authority": False,
}
_PAYLOAD_FIELDS = {
    "context.experiment.promotion.propose": {
        "work_id",
        "expected_work_revision",
        "expected_target_work_revision",
        "attempt_id",
        "proposal_id",
        "criterion_evidence",
        "causation_ref",
        "correlation_ref",
    },
    "context.experiment.promotion.approve": {
        "work_id",
        "expected_work_revision",
        "expected_target_work_revision",
        "proposal_id",
        "approval_id",
        "causation_ref",
        "correlation_ref",
    },
    "context.idea.correction.protect": {
        "idea_id",
        "protection_id",
        "affected_work_ids",
        "affected_scope_refs",
        "reason",
        "evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
    "context.idea.correction.release": {
        "idea_id",
        "protection_id",
        "release_reason",
        "release_evidence_ids",
        "causation_ref",
        "correlation_ref",
    },
}
_STATE_SCHEMA_VERSIONS = {
    "context.experiment.promotion.propose": (
        "context.experiment-promotion-proposal-request/v1alpha1"
    ),
    "context.experiment.promotion.approve": (
        "context.experiment-promotion-approval-request/v1alpha1"
    ),
    "context.idea.correction.protect": (
        "context.idea-correction-protection-request/v1alpha1"
    ),
    "context.idea.correction.release": (
        "context.idea-correction-release-request/v1alpha1"
    ),
}
_RECORD_KINDS = {
    "context.experiment.promotion.propose": "promotion-proposal",
    "context.experiment.promotion.approve": "promotion-approval",
    "context.idea.correction.protect": "correction-protection",
    "context.idea.correction.release": "correction-release",
}
_RECORD_ID_FIELDS = {
    "context.experiment.promotion.propose": "proposal_id",
    "context.experiment.promotion.approve": "approval_id",
    "context.idea.correction.protect": "protection_id",
    "context.idea.correction.release": "protection_id",
}
_AUTHORIZATION_ACTIONS = {
    action: f"state.{action.removeprefix('context.')}"
    for action in HUMAN_GOVERNANCE_ACTIONS
}
_ERROR_MESSAGES = {
    "invalid_request": "request failed validation",
    "permission_denied": "request is not authorized",
    "conflict": "request conflicts with current state",
    "integrity": "request failed State validation",
    "unavailable": "governance service unavailable",
}


class HumanGovernanceError(ValueError):
    """Raised when a human governance contract is malformed."""


class HumanGovernanceSessionResolver(Protocol):
    """Resolve transport session identity inside the trusted service boundary."""

    def resolve(
        self, session_id: str, project_id: str, action: str
    ) -> RequestContext | None: ...


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise HumanGovernanceError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
    ):
        raise HumanGovernanceError(f"{field} is invalid")
    return value


def _ref(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum=500)


def _revision(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise HumanGovernanceError(f"{field} is invalid")
    return value


def _string_list(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    maximum: int = MAX_LIST_ITEMS,
) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise HumanGovernanceError(f"{field} is invalid")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = _text(item, field, maximum=500)
        if text in seen:
            raise HumanGovernanceError(f"{field} is invalid")
        seen.add(text)
        normalized.append(text)
    return normalized


def _validate_payload(action: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _PAYLOAD_FIELDS[action]:
        raise HumanGovernanceError("payload fields are invalid")
    for field in ("causation_ref", "correlation_ref"):
        _ref(payload[field], field)
    if action.startswith("context.experiment.promotion"):
        _id(payload["work_id"], "work_id")
        _revision(payload["expected_work_revision"], "expected_work_revision")
        _revision(
            payload["expected_target_work_revision"],
            "expected_target_work_revision",
        )
        _id(payload["proposal_id"], "proposal_id")
        if action.endswith("propose"):
            _id(payload["attempt_id"], "attempt_id")
            criteria = payload["criterion_evidence"]
            if (
                not isinstance(criteria, dict)
                or not 1 <= len(criteria) <= MAX_CRITERIA
            ):
                raise HumanGovernanceError("criterion_evidence is invalid")
            total = 0
            for criterion, evidence_ids in criteria.items():
                _text(criterion, "criterion", maximum=500)
                values = _string_list(
                    evidence_ids,
                    "criterion_evidence",
                    minimum=1,
                    maximum=MAX_CRITERION_EVIDENCE,
                )
                total += len(values)
            if total > MAX_TOTAL_CRITERION_EVIDENCE:
                raise HumanGovernanceError("criterion_evidence is too large")
        else:
            _id(payload["approval_id"], "approval_id")
    else:
        _id(payload["idea_id"], "idea_id")
        _id(payload["protection_id"], "protection_id")
        if action.endswith("protect"):
            _string_list(
                payload["affected_work_ids"], "affected_work_ids", minimum=1
            )
            _string_list(
                payload["affected_scope_refs"],
                "affected_scope_refs",
                minimum=1,
            )
            _text(payload["reason"], "reason", maximum=2000)
            _string_list(payload["evidence_ids"], "evidence_ids")
        else:
            _text(payload["release_reason"], "release_reason", maximum=2000)
            _string_list(
                payload["release_evidence_ids"],
                "release_evidence_ids",
                minimum=1,
            )
    return copy.deepcopy(payload)


def validate_human_governance_request(request: Any) -> dict[str, Any]:
    """Validate one bounded request without resolving identity or reading State."""
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise HumanGovernanceError("request fields are invalid")
    if request["schema_version"] != REQUEST_SCHEMA_VERSION:
        raise HumanGovernanceError("request schema_version is unsupported")
    _id(request["request_id"], "request_id")
    _id(request["project_id"], "project_id")
    _revision(request["expected_revision"], "expected_revision")
    action = request["action"]
    if not isinstance(action, str) or action not in HUMAN_GOVERNANCE_ACTIONS:
        raise HumanGovernanceError("governance action is unsupported")
    _validate_payload(action, request["payload"])
    if len(_canonical(request)) > MAX_REQUEST_BYTES:
        raise HumanGovernanceError("request exceeds the byte limit")
    return copy.deepcopy(request)


def _receipt_digest(response: dict[str, Any]) -> str:
    body = {key: value for key, value in response.items() if key != "receipt_sha256"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def validate_human_governance_response(response: Any) -> dict[str, Any]:
    """Validate a minimal, zero-authority governance action receipt."""
    fields = {
        "schema_version",
        "request_id",
        "project_id",
        "action",
        "ok",
        "result",
        "error",
        "authority",
        "receipt_sha256",
    }
    if not isinstance(response, dict) or set(response) != fields:
        raise HumanGovernanceError("response fields are invalid")
    if response["schema_version"] != RESPONSE_SCHEMA_VERSION:
        raise HumanGovernanceError("response schema_version is unsupported")
    for field in ("request_id", "project_id"):
        value = response[field]
        if value is not None:
            _id(value, field)
    action = response["action"]
    if action is not None and action not in HUMAN_GOVERNANCE_ACTIONS:
        raise HumanGovernanceError("response action is unsupported")
    if type(response["ok"]) is not bool or response["authority"] != _AUTHORITY:
        raise HumanGovernanceError("response authority or status is invalid")
    if response["ok"]:
        result = response["result"]
        expected_result_fields = {
            "state_revision",
            "state_mcp_tool",
            "record_kind",
            "record_id",
            "event_id",
            "event_sha256",
            "actor_ref",
            "state_event_binding_sha256",
            "authorization_audit",
        }
        if (
            response["error"] is not None
            or not isinstance(result, dict)
            or set(result) != expected_result_fields
            or action is None
            or result["state_mcp_tool"] != action
            or result["record_kind"] != _RECORD_KINDS[action]
        ):
            raise HumanGovernanceError("successful response is invalid")
        _revision(result["state_revision"], "state_revision")
        _id(result["record_id"], "record_id")
        if (
            not isinstance(result["event_id"], str)
            or _EVENT_ID_RE.fullmatch(result["event_id"]) is None
        ):
            raise HumanGovernanceError("event_id is invalid")
        if (
            not isinstance(result["actor_ref"], str)
            or _PRINCIPAL_RE.fullmatch(result["actor_ref"]) is None
        ):
            raise HumanGovernanceError("actor_ref is invalid")
        for field in ("event_sha256", "state_event_binding_sha256"):
            if not isinstance(result[field], str) or _SHA_RE.fullmatch(result[field]) is None:
                raise HumanGovernanceError(f"{field} is invalid")
        audit = result["authorization_audit"]
        if not isinstance(audit, dict) or set(audit) != {
            "event_schema_version",
            "policy_sha256",
            "event_id",
            "event_sha256",
            "request_sha256",
        }:
            raise HumanGovernanceError("authorization_audit is invalid")
        if (
            audit["event_schema_version"]
            != GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
            or not isinstance(audit["event_id"], str)
            or _EVENT_ID_RE.fullmatch(audit["event_id"]) is None
        ):
            raise HumanGovernanceError("authorization_audit identity is invalid")
        for field in ("policy_sha256", "event_sha256", "request_sha256"):
            if not isinstance(audit[field], str) or _SHA_RE.fullmatch(audit[field]) is None:
                raise HumanGovernanceError(f"authorization_audit.{field} is invalid")
    else:
        error = response["error"]
        if (
            response["result"] is not None
            or not isinstance(error, dict)
            or set(error) != {"code", "message"}
            or error["code"] not in _ERROR_MESSAGES
            or error["message"] != _ERROR_MESSAGES[error["code"]]
        ):
            raise HumanGovernanceError("error response is invalid")
    digest = response["receipt_sha256"]
    if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
        raise HumanGovernanceError("response digest is invalid")
    if digest != _receipt_digest(response):
        raise HumanGovernanceError("response digest does not match content")
    return copy.deepcopy(response)


def _response(
    *,
    request_id: str | None,
    project_id: str | None,
    action: str | None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> dict[str, Any]:
    response = {
        "schema_version": RESPONSE_SCHEMA_VERSION,
        "request_id": request_id,
        "project_id": project_id,
        "action": action,
        "ok": error_code is None,
        "result": copy.deepcopy(result) if error_code is None else None,
        "error": (
            None
            if error_code is None
            else {"code": error_code, "message": _ERROR_MESSAGES[error_code]}
        ),
        "authority": copy.deepcopy(_AUTHORITY),
        "receipt_sha256": "0" * 64,
    }
    response["receipt_sha256"] = _receipt_digest(response)
    validate_human_governance_response(response)
    return response


def _request_identity(request: Any) -> tuple[str | None, str | None, str | None]:
    if not isinstance(request, dict):
        return None, None, None
    request_id = request.get("request_id")
    project_id = request.get("project_id")
    action = request.get("action")
    return (
        request_id if isinstance(request_id, str) and _ID_RE.fullmatch(request_id) else None,
        project_id if isinstance(project_id, str) and _ID_RE.fullmatch(project_id) else None,
        (
            action
            if isinstance(action, str) and action in HUMAN_GOVERNANCE_ACTIONS
            else None
        ),
    )


def _state_request(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": _STATE_SCHEMA_VERSIONS[request["action"]],
        "request_id": request["request_id"],
        "project_id": request["project_id"],
        "expected_revision": request["expected_revision"],
        **copy.deepcopy(request["payload"]),
    }


def _state_request_sha256(
    request: dict[str, Any], context: RequestContext
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "tool": request["action"],
                "arguments": _state_request(request),
                "subject_ref": context.subject_ref,
                "authorization_ref": context.authorization_ref,
            }
        )
    ).hexdigest()


def _state_event_binds_request(
    *,
    request: dict[str, Any],
    context: RequestContext,
    event_head: Any,
    event: Any,
) -> bool:
    if not isinstance(event_head, dict) or set(event_head) != {
        "sequence_no",
        "event_sha256",
    }:
        return False
    try:
        validate_state_event(event)
    except (StateEventError, TypeError, ValueError):
        return False
    payload = request["payload"]
    action = request["action"]
    expected_record_id = payload[_RECORD_ID_FIELDS[action]]
    if (
        event["project_id"] != request["project_id"]
        or event["revision_before"] != request["expected_revision"]
        or event["revision_after"] != request["expected_revision"] + 1
        or event["actor_ref"] != context.subject_ref
        or event["causation_ref"] != payload["causation_ref"]
        or event["correlation_ref"] != payload["correlation_ref"]
        or event_head["sequence_no"] != event["sequence_no"]
        or event_head["event_sha256"] != event["event_sha256"]
    ):
        return False
    expected_request_sha256 = _state_request_sha256(request, context)
    if action.startswith("context.experiment.promotion"):
        transition = event.get("experiment_transition")
        expected_operation = (
            "promotion-proposed" if action.endswith("propose") else "promotion-approved"
        )
        if (
            not isinstance(transition, dict)
            or transition.get("operation") != expected_operation
            or transition.get("request_sha256") != expected_request_sha256
            or transition.get("promotion_id") != expected_record_id
            or transition.get("proposal_id") != payload["proposal_id"]
        ):
            return False
        collection = "experiment_promotions"
    else:
        transition = event.get("idea_transition")
        expected_operation = (
            "correction-guarded" if action.endswith("protect") else "correction-released"
        )
        if (
            not isinstance(transition, dict)
            or transition.get("operation") != expected_operation
            or transition.get("request_sha256") != expected_request_sha256
            or transition.get("canonical_idea_id") != payload["idea_id"]
            or transition.get("protection_id") != expected_record_id
        ):
            return False
        collection = "correction_protections"
    return sum(
        change.get("collection") == collection
        and change.get("object_id") == expected_record_id
        for change in event["changes"]
    ) == 1


def _state_event_binding_sha256(state_response: dict[str, Any]) -> str:
    result = state_response["result"]
    return hashlib.sha256(
        _canonical(
            {
                "schema_version": state_response["schema_version"],
                "request_id": state_response["request_id"],
                "tool": state_response["tool"],
                "ok": state_response["ok"],
                "result": {
                    "revision": result["revision"],
                    "event_head": result["event_head"],
                    "event": result["event"],
                },
                "error": state_response["error"],
            }
        )
    ).hexdigest()


def _authorization_audit_binding(
    *,
    request: dict[str, Any],
    context: RequestContext,
    audit_event: Any,
) -> dict[str, Any] | None:
    try:
        event = validate_authorization_audit_event(audit_event)
    except (AuthorizationAuditError, TypeError, ValueError):
        return None
    expected_request_sha256 = _state_request_sha256(request, context)
    if (
        event["schema_version"]
        != GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
        or event["request_id"] != request["request_id"]
        or event["request_sha256"] != expected_request_sha256
        or event["project_id"] != request["project_id"]
        or event["action"] != _AUTHORIZATION_ACTIONS[request["action"]]
        or event["decision"] != "allow"
        or event["reason_code"] != "grant_matched"
        or event["subject_ref"] != context.subject_ref
        or event["authorization_ref"] != context.authorization_ref
    ):
        return None
    return {
        "event_schema_version": event["schema_version"],
        "policy_sha256": event["policy_sha256"],
        "event_id": event["event_id"],
        "event_sha256": event["event_sha256"],
        "request_sha256": event["request_sha256"],
    }


class HumanGovernanceFacade:
    """Expose four governed State writes without accepting caller-built trust context."""

    def __init__(
        self,
        state_mcp: Any,
        *,
        session_resolver: HumanGovernanceSessionResolver,
    ) -> None:
        if not callable(getattr(state_mcp, "call_tool", None)):
            raise TypeError("state_mcp must provide call_tool")
        if not callable(getattr(state_mcp, "authorization_receipt", None)):
            raise TypeError("state_mcp must provide authorization_receipt")
        if not callable(getattr(session_resolver, "resolve", None)):
            raise TypeError("session_resolver must provide resolve")
        self._state_mcp = state_mcp
        self._session_resolver = session_resolver
        self._request_fingerprints: dict[tuple[str, str, str], str] = {}
        self._receipts: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._request_cache_limit = DEFAULT_REQUEST_CACHE_ENTRIES
        self._request_lock = threading.Lock()

    def _remember_fingerprint(
        self,
        request_key: tuple[str, str, str],
        fingerprint: str,
    ) -> None:
        self._request_fingerprints.pop(request_key, None)
        self._request_fingerprints[request_key] = fingerprint
        while len(self._request_fingerprints) > self._request_cache_limit:
            evicted_key = next(iter(self._request_fingerprints))
            del self._request_fingerprints[evicted_key]
            self._receipts.pop(evicted_key, None)

    def submit(self, request: Any, *, session_id: str) -> dict[str, Any]:
        """Validate and submit one controlled action through the State MCP."""
        request_id, project_id, action = _request_identity(request)
        try:
            validated = validate_human_governance_request(request)
        except HumanGovernanceError:
            return _response(
                request_id=request_id,
                project_id=project_id,
                action=action,
                error_code="invalid_request",
            )
        if not isinstance(session_id, str) or not 1 <= len(session_id) <= 1024:
            return _response(
                request_id=request_id,
                project_id=project_id,
                action=action,
                error_code="permission_denied",
            )
        try:
            context = self._session_resolver.resolve(
                session_id, validated["project_id"], validated["action"]
            )
        except Exception:  # noqa: BLE001
            context = None
        if (
            not isinstance(context, RequestContext)
            or not isinstance(context.subject_ref, str)
            or not isinstance(context.authorization_ref, str)
            or _PRINCIPAL_RE.fullmatch(context.subject_ref) is None
            or _PRINCIPAL_RE.fullmatch(context.authorization_ref) is None
        ):
            return _response(
                request_id=request_id,
                project_id=project_id,
                action=action,
                error_code="permission_denied",
            )

        fingerprint = hashlib.sha256(
            _canonical(
                {
                    "request": validated,
                    "subject_ref": context.subject_ref,
                    "authorization_ref": context.authorization_ref,
                }
            )
        ).hexdigest()
        request_key = (
            validated["project_id"],
            validated["action"],
            validated["request_id"],
        )
        with self._request_lock:
            previous_fingerprint = self._request_fingerprints.get(
                request_key
            )
            if previous_fingerprint is not None and previous_fingerprint != fingerprint:
                return _response(
                    request_id=request_id,
                    project_id=project_id,
                    action=action,
                    error_code="conflict",
                )
            previous = self._receipts.get(request_key)
            if previous is not None:
                return copy.deepcopy(previous)
            self._remember_fingerprint(request_key, fingerprint)

            state_request = _state_request(validated)
            try:
                state_response = self._state_mcp.call_tool(
                    validated["action"], state_request, context=context
                )
            except Exception:  # noqa: BLE001
                state_response = None
            try:
                audit_event = self._state_mcp.authorization_receipt(
                    validated["action"], state_request, context=context
                )
            except Exception:  # noqa: BLE001
                audit_event = None
            receipt = self._translate_state_response(
                request=validated,
                context=context,
                state_response=state_response,
                audit_event=audit_event,
            )
            if receipt["ok"]:
                self._receipts[request_key] = copy.deepcopy(receipt)
            return receipt

    @staticmethod
    def _translate_state_response(
        *,
        request: dict[str, Any],
        context: RequestContext,
        state_response: Any,
        audit_event: Any,
    ) -> dict[str, Any]:
        identity = {
            "request_id": request["request_id"],
            "project_id": request["project_id"],
            "action": request["action"],
        }
        expected_fields = {
            "schema_version",
            "request_id",
            "tool",
            "ok",
            "result",
            "error",
        }
        if (
            not isinstance(state_response, dict)
            or set(state_response) != expected_fields
            or state_response["schema_version"]
            != "context.state-mcp-response/v1alpha1"
            or state_response["request_id"] != request["request_id"]
            or state_response["tool"] != request["action"]
            or type(state_response["ok"]) is not bool
        ):
            return _response(**identity, error_code="unavailable")
        if not state_response["ok"]:
            error = state_response["error"]
            code = error.get("code") if isinstance(error, dict) else None
            if code not in {"permission_denied", "conflict", "integrity"}:
                code = "unavailable"
            return _response(**identity, error_code=code)
        result = state_response["result"]
        if not isinstance(result, dict) or state_response["error"] is not None:
            return _response(**identity, error_code="unavailable")
        revision = result.get("revision")
        event_head = result.get("event_head")
        event = result.get("event")
        if (
            type(revision) is not int
            or revision != request["expected_revision"] + 1
            or not _state_event_binds_request(
                request=request,
                context=context,
                event_head=event_head,
                event=event,
            )
            or _EVENT_ID_RE.fullmatch(event["event_id"]) is None
        ):
            return _response(**identity, error_code="unavailable")
        payload = request["payload"]
        authorization_audit = _authorization_audit_binding(
            request=request,
            context=context,
            audit_event=audit_event,
        )
        if authorization_audit is None:
            return _response(**identity, error_code="unavailable")
        translated = {
            "state_revision": revision,
            "state_mcp_tool": request["action"],
            "record_kind": _RECORD_KINDS[request["action"]],
            "record_id": payload[_RECORD_ID_FIELDS[request["action"]]],
            "event_id": event["event_id"],
            "event_sha256": event["event_sha256"],
            "actor_ref": event["actor_ref"],
            "state_event_binding_sha256": _state_event_binding_sha256(
                state_response
            ),
            "authorization_audit": authorization_audit,
        }
        return _response(**identity, result=translated)


__all__ = [
    "HUMAN_GOVERNANCE_ACTIONS",
    "MAX_CRITERIA",
    "MAX_CRITERION_EVIDENCE",
    "MAX_LIST_ITEMS",
    "MAX_REQUEST_BYTES",
    "MAX_TOTAL_CRITERION_EVIDENCE",
    "REQUEST_SCHEMA_VERSION",
    "RESPONSE_SCHEMA_VERSION",
    "HumanGovernanceError",
    "HumanGovernanceFacade",
    "HumanGovernanceSessionResolver",
    "validate_human_governance_request",
    "validate_human_governance_response",
]
