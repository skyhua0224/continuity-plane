"""Provider-neutral durable operation records and recovery decisions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from itertools import pairwise
from typing import Any

from .artifact_store import ArtifactRef
from .durable_continuation import (
    DurableContinuationError,
    advance_durable_continuation,
    canonical_durable_continuation_bytes,
    validate_durable_continuation,
)
from .effect_scope_gate import operation_scope_kinds, validate_scope

OPERATION_SCHEMA_VERSION = "context.durable-operation/v1alpha1"
RECOVERY_SCHEMA_VERSION = "context.durable-operation-recovery/v1alpha1"
MAX_OPERATION_BYTES = 64 * 1024

_PHASES = {
    "prepared",
    "intent-committed",
    "effect-in-flight",
    "outcome-unknown",
    "effect-settled",
    "response-committed",
    "terminal",
    "quarantined",
}
_PHASE_TRANSITIONS = {
    "prepared": {"intent-committed", "quarantined"},
    "intent-committed": {"effect-in-flight", "quarantined"},
    "effect-in-flight": {"outcome-unknown", "effect-settled", "quarantined"},
    "outcome-unknown": {"effect-in-flight", "effect-settled", "quarantined"},
    "effect-settled": {"response-committed", "quarantined"},
    "response-committed": {"terminal", "quarantined"},
    "terminal": set(),
    "quarantined": set(),
}
_REPLAY_POLICIES = {"safe", "never"}
_IDEMPOTENCY_MODES = {"effect-key", "none"}
_STATUS_LOOKUPS = {"none", "supported", "required"}
_OPERATION_FIELDS = {
    "schema_version",
    "operation_id",
    "project_id",
    "work_id",
    "claim_id",
    "authority",
    "effect",
    "checkpoint_ref",
    "continuation_sha256",
    "trace_binding",
    "phase",
    "record_revision",
    "previous_record_sha256",
    "attempt_count",
    "intent_ref",
    "start_ref",
    "settlement_ref",
    "result_ref",
    "state_commit_ref",
    "reconciliation_reason",
    "created_at",
    "updated_at",
    "state_write_authority",
    "provider_native_authority",
    "record_sha256",
}
_AUTHORITY_FIELDS = {"project_revision", "event_head"}
_EVENT_HEAD_FIELDS = {"sequence_no", "event_sha256"}
_EFFECT_FIELDS = {
    "effect_id",
    "effect_key",
    "operation",
    "scope_ref",
    "adapter_id",
    "request_sha256",
    "replay_policy",
    "idempotency_mode",
    "status_lookup",
}
_TRACE_FIELDS = {"trace_id", "span_id", "run_id", "correlation_id"}
_IMMUTABLE_FIELDS = {
    "schema_version",
    "operation_id",
    "project_id",
    "work_id",
    "claim_id",
    "authority",
    "effect",
    "checkpoint_ref",
    "trace_binding",
    "created_at",
    "state_write_authority",
    "provider_native_authority",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_TRACE_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_RE = re.compile(r"^[0-9a-f]{16}$")
_SAFE_TEXT_RE = re.compile(r"^[^\r\n\x00]+$")
_CONTINUATION_PHASE = {
    "prepared": "prepared",
    "intent-committed": "intent-committed",
    "effect-in-flight": "effect-in-flight",
    "outcome-unknown": "effect-in-flight",
    "effect-settled": "effect-settled",
    "response-committed": "response-committed",
    "terminal": "terminal",
}
_CONTINUATION_EFFECT_STATUS = {
    "prepared": "reserved",
    "intent-committed": "reserved",
    "effect-in-flight": "started",
    "outcome-unknown": "started",
    "effect-settled": "settled",
    "response-committed": "settled",
    "terminal": "settled",
}


class DurableOperationError(ValueError):
    """Raised when a durable operation or recovery decision is unsafe."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DurableOperationError("durable operation must be canonical JSON") from exc


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise DurableOperationError(f"{field} fields are invalid")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DurableOperationError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, *, maximum: int = 2048) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or _SAFE_TEXT_RE.fullmatch(value) is None
    ):
        raise DurableOperationError(f"{field} is invalid")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is not None:
        _text(value, field)
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise DurableOperationError(f"{field} must be a lowercase SHA-256")
    return value


def _uint(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise DurableOperationError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: Any, field: str) -> str:
    _text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DurableOperationError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DurableOperationError(f"{field} requires a timezone")
    return value


def _authority(value: Any) -> dict[str, Any]:
    authority = _object(value, _AUTHORITY_FIELDS, "authority")
    _uint(authority["project_revision"], "authority.project_revision")
    head = _object(authority["event_head"], _EVENT_HEAD_FIELDS, "authority.event_head")
    _uint(head["sequence_no"], "authority.event_head.sequence_no")
    _sha256(head["event_sha256"], "authority.event_head.event_sha256")
    return copy.deepcopy(authority)


def _effect(value: Any) -> dict[str, Any]:
    effect = _object(value, _EFFECT_FIELDS, "effect")
    for field in ("effect_id", "effect_key", "operation", "adapter_id"):
        _identifier(effect[field], f"effect.{field}")
    try:
        scope = validate_scope(copy.deepcopy(effect["scope_ref"]))
    except (TypeError, ValueError) as exc:
        raise DurableOperationError("effect scope is invalid") from exc
    allowed_kinds = operation_scope_kinds(effect["operation"])
    if allowed_kinds is None or scope["scope_kind"] not in allowed_kinds:
        raise DurableOperationError("effect scope does not match its operation")
    _sha256(effect["request_sha256"], "effect.request_sha256")
    if effect["replay_policy"] not in _REPLAY_POLICIES:
        raise DurableOperationError("effect replay policy is invalid")
    if effect["idempotency_mode"] not in _IDEMPOTENCY_MODES:
        raise DurableOperationError("effect idempotency mode is invalid")
    if effect["status_lookup"] not in _STATUS_LOOKUPS:
        raise DurableOperationError("effect status lookup is invalid")
    if effect["replay_policy"] == "safe" and effect["idempotency_mode"] != "effect-key":
        raise DurableOperationError("safe replay requires effect-key idempotency")
    normalized = copy.deepcopy(effect)
    normalized["scope_ref"] = scope
    return normalized


def _artifact_ref(value: Any) -> dict[str, Any]:
    try:
        return ArtifactRef.from_document(copy.deepcopy(value)).to_document()
    except (TypeError, ValueError) as exc:
        raise DurableOperationError("checkpoint_ref is invalid") from exc


def _trace_binding(value: Any) -> dict[str, str]:
    trace = _object(value, _TRACE_FIELDS, "trace binding")
    if (
        not isinstance(trace["trace_id"], str)
        or _TRACE_RE.fullmatch(trace["trace_id"]) is None
        or set(trace["trace_id"]) == {"0"}
    ):
        raise DurableOperationError("trace_id is invalid")
    if (
        not isinstance(trace["span_id"], str)
        or _SPAN_RE.fullmatch(trace["span_id"]) is None
        or set(trace["span_id"]) == {"0"}
    ):
        raise DurableOperationError("span_id is invalid")
    _identifier(trace["run_id"], "trace_binding.run_id")
    _identifier(trace["correlation_id"], "trace_binding.correlation_id")
    return copy.deepcopy(trace)


def _body(operation: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(operation)
    body.pop("record_sha256", None)
    return body


def _digest(operation: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(_body(operation))).hexdigest()


def validate_operation_continuation_binding(
    operation: dict[str, Any],
    continuation: dict[str, Any],
    *,
    operation_phase: str | None = None,
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Bind one M5 continuation state to a durable operation phase."""
    current = validate_durable_operation(copy.deepcopy(operation))
    phase = operation_phase or current["phase"]
    if phase not in _CONTINUATION_PHASE:
        raise DurableOperationError("operation phase has no M5 continuation mapping")
    try:
        state = validate_durable_continuation(copy.deepcopy(continuation))
    except DurableContinuationError as exc:
        raise DurableOperationError("durable continuation is invalid") from exc
    expected_identity = {
        "operation_id": current["operation_id"],
        "project_id": current["project_id"],
        "project_revision": current["authority"]["project_revision"],
        "task_id": current["work_id"],
        "event_head": current["authority"]["event_head"],
    }
    for field, expected in expected_identity.items():
        if state[field] != expected:
            raise DurableOperationError(
                f"durable continuation {field} does not match operation"
            )
    if state["phase"] != _CONTINUATION_PHASE[phase]:
        raise DurableOperationError("durable continuation phase does not match operation")
    effects = [
        item
        for item in state["reserved_effects"]
        if item["effect_id"] == current["effect"]["effect_id"]
    ]
    if len(effects) != 1 or len(state["reserved_effects"]) != 1:
        raise DurableOperationError("durable continuation effect reservation is invalid")
    if effects[0] != {
        "effect_id": current["effect"]["effect_id"],
        "replay_policy": current["effect"]["replay_policy"],
        "status": _CONTINUATION_EFFECT_STATUS[phase],
    }:
        raise DurableOperationError("durable continuation effect does not match operation")
    if expected_sha256 is not None and state["state_sha256"] != expected_sha256:
        raise DurableOperationError("durable continuation digest does not match operation")
    return state


def validate_operation_continuation_transition(
    operation: dict[str, Any],
    current_state: dict[str, Any],
    next_state: dict[str, Any],
    *,
    operation_phase: str,
) -> dict[str, Any]:
    """Prove that a proposed M5 cursor is the only adjacent cursor allowed."""
    current = validate_operation_continuation_binding(
        operation,
        current_state,
        expected_sha256=operation["continuation_sha256"],
    )
    candidate = validate_operation_continuation_binding(
        operation,
        next_state,
        operation_phase=operation_phase,
    )
    if candidate["phase"] == current["phase"]:
        if canonical_durable_continuation_bytes(candidate) != canonical_durable_continuation_bytes(
            current
        ):
            raise DurableOperationError(
                "durable continuation cannot change within an extended operation phase"
            )
        return candidate
    try:
        expected = advance_durable_continuation(
            current,
            phase=candidate["phase"],
            last_durable_action=candidate["last_durable_action"],
            next_action=candidate["next_action"],
            reserved_effects=candidate["reserved_effects"],
            response_mode=candidate["response_mode"],
            acknowledged_input_ids=candidate["acknowledged_input_ids"],
        )
    except DurableContinuationError as exc:
        raise DurableOperationError(
            "durable continuation phase transition is invalid"
        ) from exc
    if canonical_durable_continuation_bytes(candidate) != canonical_durable_continuation_bytes(
        expected
    ):
        raise DurableOperationError("durable continuation transition does not match")
    return candidate


def _validate_phase_consistency(operation: dict[str, Any]) -> None:
    phase = operation["phase"]
    revision = operation["record_revision"]
    previous = operation["previous_record_sha256"]
    attempts = operation["attempt_count"]
    intent = operation["intent_ref"]
    start = operation["start_ref"]
    settlement = operation["settlement_ref"]
    result = operation["result_ref"]
    state_commit = operation["state_commit_ref"]
    reason = operation["reconciliation_reason"]
    if phase == "prepared":
        if revision != 0 or previous is not None or attempts != 0 or any(
            value is not None
            for value in (intent, start, settlement, result, state_commit, reason)
        ):
            raise DurableOperationError("prepared operation fields are inconsistent")
        return
    if revision <= 0 or previous is None:
        raise DurableOperationError("advanced operation requires a prior record")
    if phase == "intent-committed":
        valid = intent is not None and attempts == 0 and all(
            value is None for value in (start, settlement, result, state_commit, reason)
        )
    elif phase == "effect-in-flight":
        valid = intent is not None and start is not None and attempts > 0 and all(
            value is None for value in (settlement, result, state_commit, reason)
        )
    elif phase == "outcome-unknown":
        valid = intent is not None and start is not None and attempts > 0 and all(
            value is None for value in (settlement, result, state_commit)
        ) and reason is not None
    elif phase == "effect-settled":
        valid = all(value is not None for value in (intent, start, settlement, result)) and (
            attempts > 0 and state_commit is None
        )
    elif phase in {"response-committed", "terminal"}:
        valid = all(
            value is not None for value in (intent, start, settlement, result, state_commit)
        ) and attempts > 0
    else:
        valid = reason is not None
    if not valid:
        raise DurableOperationError(f"{phase} fields are inconsistent")


def validate_durable_operation(
    operation: Any, *, verify_digest: bool = True
) -> dict[str, Any]:
    """Validate one complete durable operation record."""
    operation = _object(operation, _OPERATION_FIELDS, "durable operation")
    if operation["schema_version"] != OPERATION_SCHEMA_VERSION:
        raise DurableOperationError("unsupported durable operation schema_version")
    for field in ("operation_id", "project_id", "work_id", "claim_id"):
        _identifier(operation[field], field)
    authority = _authority(operation["authority"])
    effect = _effect(operation["effect"])
    checkpoint_ref = _artifact_ref(operation["checkpoint_ref"])
    _sha256(operation["continuation_sha256"], "continuation_sha256")
    trace_binding = _trace_binding(operation["trace_binding"])
    if operation["phase"] not in _PHASES:
        raise DurableOperationError("durable operation phase is invalid")
    _uint(operation["record_revision"], "record_revision")
    if operation["previous_record_sha256"] is not None:
        _sha256(operation["previous_record_sha256"], "previous_record_sha256")
    _uint(operation["attempt_count"], "attempt_count")
    for field in (
        "intent_ref",
        "start_ref",
        "settlement_ref",
        "result_ref",
        "state_commit_ref",
        "reconciliation_reason",
    ):
        _optional_text(operation[field], field)
    _timestamp(operation["created_at"], "created_at")
    _timestamp(operation["updated_at"], "updated_at")
    if operation["state_write_authority"] is not False:
        raise DurableOperationError("durable operation cannot claim State authority")
    if operation["provider_native_authority"] is not False:
        raise DurableOperationError("durable operation cannot claim provider authority")
    _sha256(operation["record_sha256"], "record_sha256")
    normalized = copy.deepcopy(operation)
    normalized["authority"] = authority
    normalized["effect"] = effect
    normalized["checkpoint_ref"] = checkpoint_ref
    normalized["trace_binding"] = trace_binding
    _validate_phase_consistency(normalized)
    if len(_canonical(normalized)) > MAX_OPERATION_BYTES:
        raise DurableOperationError("durable operation exceeds the size bound")
    if verify_digest and operation["record_sha256"] != _digest(normalized):
        raise DurableOperationError("durable operation digest mismatch")
    return normalized


def compose_durable_operation(
    *,
    operation_id: str,
    project_id: str,
    work_id: str,
    claim_id: str,
    authority: dict[str, Any],
    effect: dict[str, Any],
    checkpoint_ref: dict[str, Any],
    continuation_sha256: str,
    trace_binding: dict[str, str],
    observed_at: str,
) -> dict[str, Any]:
    """Compose one prepared local operation with no State authority."""
    operation = {
        "schema_version": OPERATION_SCHEMA_VERSION,
        "operation_id": operation_id,
        "project_id": project_id,
        "work_id": work_id,
        "claim_id": claim_id,
        "authority": copy.deepcopy(authority),
        "effect": copy.deepcopy(effect),
        "checkpoint_ref": copy.deepcopy(checkpoint_ref),
        "continuation_sha256": continuation_sha256,
        "trace_binding": copy.deepcopy(trace_binding),
        "phase": "prepared",
        "record_revision": 0,
        "previous_record_sha256": None,
        "attempt_count": 0,
        "intent_ref": None,
        "start_ref": None,
        "settlement_ref": None,
        "result_ref": None,
        "state_commit_ref": None,
        "reconciliation_reason": None,
        "created_at": observed_at,
        "updated_at": observed_at,
        "state_write_authority": False,
        "provider_native_authority": False,
        "record_sha256": "0" * 64,
    }
    normalized = validate_durable_operation(operation, verify_digest=False)
    normalized["record_sha256"] = _digest(normalized)
    return json.loads(_canonical(validate_durable_operation(normalized)))


def _assert_immutable(previous: dict[str, Any], current: dict[str, Any]) -> None:
    for field in _IMMUTABLE_FIELDS:
        if current.get(field) != previous[field]:
            raise DurableOperationError(f"immutable operation field changed: {field}")


def advance_durable_operation(
    operation: dict[str, Any],
    *,
    phase: str,
    observed_at: str,
    continuation_sha256: str,
    intent_ref: str | None = None,
    start_ref: str | None = None,
    settlement_ref: str | None = None,
    result_ref: str | None = None,
    state_commit_ref: str | None = None,
    reconciliation_reason: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance exactly one operation phase and extend its hash chain."""
    if previous is not None:
        trusted_previous = validate_durable_operation(copy.deepcopy(previous))
        _assert_immutable(trusted_previous, operation)
    current = validate_durable_operation(copy.deepcopy(operation))
    if phase not in _PHASE_TRANSITIONS[current["phase"]]:
        raise DurableOperationError("durable operation phase transition is invalid")
    if current["phase"] in {"terminal", "quarantined"}:
        raise DurableOperationError(f"{current['phase']} operation cannot be reopened")
    _sha256(continuation_sha256, "continuation_sha256")
    _timestamp(observed_at, "observed_at")
    current_time = datetime.fromisoformat(current["updated_at"].replace("Z", "+00:00"))
    next_time = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if next_time < current_time:
        raise DurableOperationError("updated_at cannot regress")

    if current["phase"] == "outcome-unknown" and phase == "effect-in-flight":
        if current["effect"]["replay_policy"] != "safe":
            raise DurableOperationError("never-replay effect requires reconciliation")
        if current["effect"]["idempotency_mode"] != "effect-key":
            raise DurableOperationError("effect replay lacks idempotency")

    advanced = copy.deepcopy(current)
    advanced.update(
        {
            "phase": phase,
            "record_revision": current["record_revision"] + 1,
            "previous_record_sha256": current["record_sha256"],
            "continuation_sha256": continuation_sha256,
            "updated_at": observed_at,
            "record_sha256": "0" * 64,
        }
    )
    if phase == "intent-committed":
        advanced["intent_ref"] = intent_ref
    elif phase == "effect-in-flight":
        advanced["start_ref"] = start_ref
        advanced["attempt_count"] += 1
        advanced["reconciliation_reason"] = None
    elif phase == "outcome-unknown":
        advanced["reconciliation_reason"] = reconciliation_reason
    elif phase == "effect-settled":
        advanced["settlement_ref"] = settlement_ref
        advanced["result_ref"] = result_ref
        advanced["reconciliation_reason"] = reconciliation_reason
    elif phase == "response-committed":
        advanced["state_commit_ref"] = state_commit_ref
    elif phase == "quarantined":
        advanced["reconciliation_reason"] = reconciliation_reason

    normalized = validate_durable_operation(advanced, verify_digest=False)
    normalized["record_sha256"] = _digest(normalized)
    return json.loads(_canonical(validate_durable_operation(normalized)))


def validate_durable_operation_chain(chain: Any) -> list[dict[str, Any]]:
    """Validate an ordered, immutable, append-only operation history."""
    if not isinstance(chain, list) or not chain:
        raise DurableOperationError("durable operation chain is empty")
    normalized = [validate_durable_operation(copy.deepcopy(item)) for item in chain]
    if normalized[0]["phase"] != "prepared" or normalized[0]["record_revision"] != 0:
        raise DurableOperationError("durable operation chain must start prepared")
    for previous, current in pairwise(normalized):
        validate_durable_operation_transition(previous, current)
    return normalized


def validate_durable_operation_transition(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any]:
    """Validate one adjacent operation transition without requiring genesis."""
    previous = validate_durable_operation(copy.deepcopy(previous))
    current = validate_durable_operation(copy.deepcopy(current))
    _assert_immutable(previous, current)
    if current["phase"] not in _PHASE_TRANSITIONS[previous["phase"]]:
        raise DurableOperationError("durable operation chain transition is invalid")
    if current["record_revision"] != previous["record_revision"] + 1:
        raise DurableOperationError("durable operation chain revision is not contiguous")
    if current["previous_record_sha256"] != previous["record_sha256"]:
        raise DurableOperationError("durable operation hash chain is broken")
    expected_attempts = previous["attempt_count"] + (
        1 if current["phase"] == "effect-in-flight" else 0
    )
    if current["attempt_count"] != expected_attempts:
        raise DurableOperationError("durable operation attempt count is invalid")
    return current


def evaluate_durable_operation_recovery(operation: dict[str, Any]) -> dict[str, Any]:
    """Return the only recovery action permitted by one durable phase."""
    current = validate_durable_operation(copy.deepcopy(operation))
    phase = current["phase"]
    replay = False
    if phase == "prepared":
        action, reason = "commit-intent", "intent-not-committed"
    elif phase == "intent-committed":
        action, reason = "dispatch-effect", "effect-not-started"
    elif phase in {"effect-in-flight", "outcome-unknown"}:
        effect = current["effect"]
        replay = (
            effect["replay_policy"] == "safe"
            and effect["idempotency_mode"] == "effect-key"
        )
        if replay:
            action, reason = "verify-or-retry-idempotent", "safe-effect-outcome-unknown"
        else:
            action, reason = "manual", "never-effect-outcome-unknown"
    elif phase == "effect-settled":
        action, reason = "commit-state", "external-effect-already-settled"
    elif phase == "response-committed":
        action, reason = "finalize", "state-already-committed"
    elif phase == "terminal":
        action, reason = "terminal", "operation-complete"
    else:
        action, reason = "manual", "operation-quarantined"
    return {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "operation_id": current["operation_id"],
        "operation_sha256": current["record_sha256"],
        "phase": phase,
        "recovery_action": action,
        "reason": reason,
        "effect_id": current["effect"]["effect_id"],
        "effect_key": current["effect"]["effect_key"],
        "automatic_effect_replay": replay,
        "state_write_authority": False,
        "provider_native_authority": False,
    }
