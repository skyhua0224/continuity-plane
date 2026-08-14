"""Provider-neutral durable continuation state and anti-reset recovery gate."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any


STATE_SCHEMA_VERSION = "context.durable-continuation/v1alpha1"
RECOVERY_SCHEMA_VERSION = "context.durable-continuation-recovery/v1alpha1"
MAX_RECOVERY_BYTES = 64 * 1024
CONTINUATION_FIELD_COUNT = 10

_PHASES = {
    "prepared",
    "intent-committed",
    "effect-in-flight",
    "effect-settled",
    "response-committed",
    "terminal",
}
_PHASE_TRANSITIONS = {
    "prepared": {"intent-committed"},
    "intent-committed": {"effect-in-flight", "response-committed"},
    "effect-in-flight": {"effect-settled"},
    "effect-settled": {"response-committed"},
    "response-committed": {"terminal"},
    "terminal": set(),
}
_RESPONSE_MODES = {
    "continue-silently",
    "answer-pending-input",
    "await-effect",
    "terminal",
}
_EFFECT_REPLAY_POLICIES = {"safe", "never"}
_EFFECT_STATUSES = {"reserved", "started", "settled"}
_EFFECT_STATUS_TRANSITIONS = {
    "reserved": {"reserved", "started"},
    "started": {"started", "settled"},
    "settled": {"settled"},
}
_STATE_FIELDS = {
    "schema_version",
    "operation_id",
    "project_id",
    "project_revision",
    "task_id",
    "task_revision",
    "event_head",
    "phase",
    "last_durable_action",
    "next_action",
    "acknowledged_input_ids",
    "reserved_effects",
    "response_mode",
    "state_write_authority",
    "provider_native_authority",
    "state_sha256",
}
_AUTHORITY_FIELDS = {
    "operation_id",
    "project_id",
    "project_revision",
    "task_id",
    "task_revision",
    "event_head",
}
_EVENT_HEAD_FIELDS = {"sequence_no", "event_sha256"}
_EFFECT_FIELDS = {"effect_id", "replay_policy", "status"}
_READ_FIELDS = {"source_ref", "content_sha256", "bytes_read"}
_READ_RECEIPT_FIELDS = {
    "read_count",
    "bytes_read",
    "budget_bytes",
    "source_refs",
    "reads_sha256",
}
_RECOVERY_FIELDS = {
    "schema_version",
    "operation_id",
    "project_id",
    "project_revision",
    "task_id",
    "task_revision",
    "event_head",
    "durable_state_sha256",
    "phase",
    "last_durable_action",
    "first_action",
    "acknowledged_input_ids",
    "reserved_effect_ids",
    "response_mode",
    "continuation_fields_total",
    "continuation_fields_recovered",
    "acknowledged_input_replays",
    "execution_gate",
    "recovery_read_receipt",
    "observed_at",
    "state_write_authority",
    "provider_native_authority",
    "receipt_sha256",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_TEXT_RE = re.compile(r"^[^\r\n\x00]+$")


class DurableContinuationError(ValueError):
    """Raised when continuation state cannot authorize an exact recovery action."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise DurableContinuationError(f"{field} fields are invalid")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DurableContinuationError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, *, max_length: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or _SAFE_TEXT_RE.fullmatch(value) is None
    ):
        raise DurableContinuationError(f"{field} is invalid")
    return value


def _uint(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise DurableContinuationError(f"{field} must be a non-negative integer")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise DurableContinuationError(f"{field} must be a lowercase SHA-256")
    return value


def _timestamp(value: Any, field: str) -> str:
    _text(value, field, max_length=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DurableContinuationError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DurableContinuationError(f"{field} requires a timezone")
    return value


def _event_head(value: Any, field: str = "event_head") -> dict[str, Any]:
    head = _object(value, _EVENT_HEAD_FIELDS, field)
    _uint(head["sequence_no"], f"{field}.sequence_no")
    _sha(head["event_sha256"], f"{field}.event_sha256")
    return head


def _ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise DurableContinuationError(f"{field} must be an ID list")
    result = [_id(item, field) for item in value]
    if len(result) != len(set(result)):
        raise DurableContinuationError(f"{field} must contain unique IDs")
    return sorted(result)


def _effects(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise DurableContinuationError("reserved_effects must be an object list")
    result: list[dict[str, str]] = []
    for item in value:
        effect = _object(item, _EFFECT_FIELDS, "reserved effect")
        effect_id = _id(effect["effect_id"], "reserved_effect.effect_id")
        if effect["replay_policy"] not in _EFFECT_REPLAY_POLICIES:
            raise DurableContinuationError("reserved effect replay policy is invalid")
        if effect["status"] not in _EFFECT_STATUSES:
            raise DurableContinuationError("reserved effect status is invalid")
        result.append(
            {
                "effect_id": effect_id,
                "replay_policy": effect["replay_policy"],
                "status": effect["status"],
            }
        )
    identifiers = [item["effect_id"] for item in result]
    if len(identifiers) != len(set(identifiers)):
        raise DurableContinuationError("reserved effect IDs must be unique")
    return sorted(result, key=lambda item: item["effect_id"])


def _state_body(state: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(state)
    body.pop("state_sha256", None)
    return body


def _state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(_state_body(state))).hexdigest()


def _validate_phase_consistency(state: dict[str, Any]) -> None:
    phase = state["phase"]
    effects = state["reserved_effects"]
    statuses = {item["status"] for item in effects}
    if phase in {"prepared", "intent-committed"} and statuses - {"reserved"}:
        raise DurableContinuationError(f"reserved effect status is invalid for phase {phase}")
    if phase == "effect-in-flight" and "started" not in statuses:
        raise DurableContinuationError("effect-in-flight phase requires a started effect")
    if phase in {"effect-settled", "response-committed", "terminal"} and any(
        item["status"] != "settled" for item in effects
    ):
        raise DurableContinuationError(f"reserved effect status is invalid for phase {phase}")
    if phase == "effect-settled" and not effects:
        raise DurableContinuationError("effect-settled phase requires a reserved effect")
    if phase == "terminal":
        if state["next_action"] is not None or state["response_mode"] != "terminal":
            raise DurableContinuationError("terminal phase must clear next action and response")
    elif state["next_action"] is None:
        raise DurableContinuationError("non-terminal phase requires a next action")
    if phase != "terminal" and state["response_mode"] == "terminal":
        raise DurableContinuationError("terminal response mode requires terminal phase")


def validate_durable_continuation(
    state: Any, *, verify_digest: bool = True
) -> dict[str, Any]:
    """Validate the total provider-neutral operation program counter."""
    state = _object(state, _STATE_FIELDS, "durable continuation")
    if state["schema_version"] != STATE_SCHEMA_VERSION:
        raise DurableContinuationError("unsupported durable continuation schema_version")
    for field in ("operation_id", "project_id", "task_id"):
        _id(state[field], field)
    for field in ("project_revision", "task_revision"):
        _uint(state[field], field)
    _event_head(state["event_head"])
    if state["phase"] not in _PHASES:
        raise DurableContinuationError("durable continuation phase is invalid")
    _text(state["last_durable_action"], "last_durable_action")
    if state["next_action"] is not None:
        _text(state["next_action"], "next_action")
    acknowledged = _ids(state["acknowledged_input_ids"], "acknowledged_input_ids")
    effects = _effects(state["reserved_effects"])
    if state["response_mode"] not in _RESPONSE_MODES:
        raise DurableContinuationError("response mode is invalid")
    if state["state_write_authority"] is not False:
        raise DurableContinuationError("durable continuation cannot claim State authority")
    if state["provider_native_authority"] is not False:
        raise DurableContinuationError("durable continuation cannot claim provider authority")
    _sha(state["state_sha256"], "state_sha256")
    normalized = copy.deepcopy(state)
    normalized["acknowledged_input_ids"] = acknowledged
    normalized["reserved_effects"] = effects
    _validate_phase_consistency(normalized)
    if verify_digest and state["state_sha256"] != _state_digest(normalized):
        raise DurableContinuationError("durable continuation digest mismatch")
    return normalized


def compose_durable_continuation(
    *,
    operation_id: str,
    project_id: str,
    project_revision: int,
    task_id: str,
    task_revision: int,
    event_head: dict[str, Any],
    phase: str,
    last_durable_action: str,
    next_action: str | None,
    acknowledged_input_ids: list[str],
    reserved_effects: list[dict[str, str]],
    response_mode: str,
) -> dict[str, Any]:
    """Compose a content-addressed durable continuation state with no write authority."""
    normalized_acknowledgements = _ids(
        copy.deepcopy(acknowledged_input_ids), "acknowledged_input_ids"
    )
    normalized_effects = _effects(copy.deepcopy(reserved_effects))
    state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "operation_id": operation_id,
        "project_id": project_id,
        "project_revision": project_revision,
        "task_id": task_id,
        "task_revision": task_revision,
        "event_head": copy.deepcopy(event_head),
        "phase": phase,
        "last_durable_action": last_durable_action,
        "next_action": next_action,
        "acknowledged_input_ids": normalized_acknowledgements,
        "reserved_effects": normalized_effects,
        "response_mode": response_mode,
        "state_write_authority": False,
        "provider_native_authority": False,
        "state_sha256": "0" * 64,
    }
    normalized = validate_durable_continuation(state, verify_digest=False)
    normalized["state_sha256"] = _state_digest(normalized)
    validate_durable_continuation(normalized)
    return json.loads(_canonical(normalized))


def canonical_durable_continuation_bytes(state: dict[str, Any]) -> bytes:
    """Return canonical bytes for a validated durable state."""
    normalized = validate_durable_continuation(state)
    return _canonical(normalized)


def advance_durable_continuation(
    state: dict[str, Any],
    *,
    phase: str,
    last_durable_action: str,
    next_action: str | None,
    reserved_effects: list[dict[str, str]],
    response_mode: str,
    acknowledged_input_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Advance exactly one durable phase while preserving reserved identities."""
    current = validate_durable_continuation(state)
    if current["phase"] == "terminal":
        raise DurableContinuationError("terminal durable continuation cannot be reopened")
    if phase not in _PHASE_TRANSITIONS[current["phase"]]:
        raise DurableContinuationError("durable phase transition is invalid")
    proposed_effects = _effects(copy.deepcopy(reserved_effects))
    current_by_id = {item["effect_id"]: item for item in current["reserved_effects"]}
    proposed_by_id = {item["effect_id"]: item for item in proposed_effects}
    if set(current_by_id) != set(proposed_by_id):
        raise DurableContinuationError("reserved effect IDs cannot change after creation")
    for effect_id, before in current_by_id.items():
        after = proposed_by_id[effect_id]
        if after["replay_policy"] != before["replay_policy"]:
            raise DurableContinuationError("reserved effect replay policy is immutable")
        if after["status"] not in _EFFECT_STATUS_TRANSITIONS[before["status"]]:
            raise DurableContinuationError("reserved effect status transition is invalid")
    acknowledgements = (
        current["acknowledged_input_ids"]
        if acknowledged_input_ids is None
        else _ids(acknowledged_input_ids, "acknowledged_input_ids")
    )
    if not set(current["acknowledged_input_ids"]).issubset(acknowledgements):
        raise DurableContinuationError("acknowledged input IDs cannot be removed")
    return compose_durable_continuation(
        operation_id=current["operation_id"],
        project_id=current["project_id"],
        project_revision=current["project_revision"],
        task_id=current["task_id"],
        task_revision=current["task_revision"],
        event_head=current["event_head"],
        phase=phase,
        last_durable_action=last_durable_action,
        next_action=next_action,
        acknowledged_input_ids=acknowledgements,
        reserved_effects=proposed_effects,
        response_mode=response_mode,
    )


def _validate_authority(value: Any) -> dict[str, Any]:
    authority = _object(value, _AUTHORITY_FIELDS, "trusted authority")
    for field in ("operation_id", "project_id", "task_id"):
        _id(authority[field], f"trusted_authority.{field}")
    for field in ("project_revision", "task_revision"):
        _uint(authority[field], f"trusted_authority.{field}")
    _event_head(authority["event_head"], "trusted_authority.event_head")
    return authority


def _recovery_read_receipt(
    recovery_reads: Any, *, recovery_budget_bytes: int
) -> dict[str, Any]:
    if (
        type(recovery_budget_bytes) is not int
        or recovery_budget_bytes <= 0
        or recovery_budget_bytes > MAX_RECOVERY_BYTES
    ):
        raise DurableContinuationError("recovery read budget is invalid")
    if not isinstance(recovery_reads, list) or not recovery_reads:
        raise DurableContinuationError("recovery reads must be a non-empty list")
    reads = []
    for item in recovery_reads:
        read = _object(item, _READ_FIELDS, "recovery read")
        source_ref = _text(read["source_ref"], "recovery_read.source_ref", max_length=2048)
        content_sha256 = _sha(read["content_sha256"], "recovery_read.content_sha256")
        bytes_read = _uint(read["bytes_read"], "recovery_read.bytes_read")
        reads.append(
            {
                "source_ref": source_ref,
                "content_sha256": content_sha256,
                "bytes_read": bytes_read,
            }
        )
    source_refs = [item["source_ref"] for item in reads]
    if len(source_refs) != len(set(source_refs)):
        raise DurableContinuationError("recovery read source refs must be unique")
    reads.sort(key=lambda item: item["source_ref"])
    total = sum(item["bytes_read"] for item in reads)
    if total > recovery_budget_bytes:
        raise DurableContinuationError("recovery reads exceed the bounded budget")
    return {
        "read_count": len(reads),
        "bytes_read": total,
        "budget_bytes": recovery_budget_bytes,
        "source_refs": sorted(source_refs),
        "reads_sha256": hashlib.sha256(_canonical(reads)).hexdigest(),
    }


def _receipt_body(receipt: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return body


def _receipt_digest(receipt: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(_receipt_body(receipt))).hexdigest()


def evaluate_durable_recovery(
    *,
    trusted_state: dict[str, Any],
    restored_state: dict[str, Any],
    trusted_authority: dict[str, Any],
    proposed_first_action: str | None,
    response_input_id: str | None,
    requested_effect_id: str | None,
    replay_requested: bool,
    recovery_reads: list[dict[str, Any]],
    recovery_budget_bytes: int,
    observed_at: str,
) -> dict[str, Any]:
    """Fail closed unless recovery resumes the exact durable atomic action."""
    expected = validate_durable_continuation(copy.deepcopy(trusted_state))
    restored = validate_durable_continuation(copy.deepcopy(restored_state))
    authority = _validate_authority(copy.deepcopy(trusted_authority))
    if canonical_durable_continuation_bytes(restored) != canonical_durable_continuation_bytes(
        expected
    ):
        raise DurableContinuationError("restored state does not match trusted durable state")
    for field in ("operation_id", "project_id", "project_revision", "task_id", "task_revision"):
        if expected[field] != authority[field]:
            raise DurableContinuationError(f"{field} does not match trusted authority")
    if expected["event_head"] != authority["event_head"]:
        raise DurableContinuationError("event head does not match trusted authority")

    if expected["phase"] == "terminal":
        if proposed_first_action is not None:
            raise DurableContinuationError("terminal operation cannot have a first action")
    else:
        _text(proposed_first_action, "proposed_first_action")
        if proposed_first_action != expected["next_action"]:
            raise DurableContinuationError("post-restore first action does not match durable cursor")

    if response_input_id is not None:
        response_id = _id(response_input_id, "response_input_id")
        if response_id in expected["acknowledged_input_ids"]:
            raise DurableContinuationError("acknowledged input cannot be answered again")
        if expected["response_mode"] != "answer-pending-input":
            raise DurableContinuationError("response mode does not permit an answer")
    if type(replay_requested) is not bool:
        raise DurableContinuationError("replay_requested must be boolean")
    effects = {item["effect_id"]: item for item in expected["reserved_effects"]}
    if requested_effect_id is not None:
        effect_id = _id(requested_effect_id, "requested_effect_id")
        effect = effects.get(effect_id)
        if effect is None:
            raise DurableContinuationError("requested effect is not reserved")
        if replay_requested:
            if effect["replay_policy"] == "never":
                raise DurableContinuationError("never-replay effect cannot be invoked again")
            if effect["status"] == "settled":
                raise DurableContinuationError("settled effect cannot be replayed")
    elif replay_requested:
        raise DurableContinuationError("effect replay requires a reserved effect ID")

    read_receipt = _recovery_read_receipt(
        copy.deepcopy(recovery_reads), recovery_budget_bytes=recovery_budget_bytes
    )
    receipt = {
        "schema_version": RECOVERY_SCHEMA_VERSION,
        "operation_id": expected["operation_id"],
        "project_id": expected["project_id"],
        "project_revision": expected["project_revision"],
        "task_id": expected["task_id"],
        "task_revision": expected["task_revision"],
        "event_head": copy.deepcopy(expected["event_head"]),
        "durable_state_sha256": expected["state_sha256"],
        "phase": expected["phase"],
        "last_durable_action": expected["last_durable_action"],
        "first_action": expected["next_action"],
        "acknowledged_input_ids": list(expected["acknowledged_input_ids"]),
        "reserved_effect_ids": [item["effect_id"] for item in expected["reserved_effects"]],
        "response_mode": expected["response_mode"],
        "continuation_fields_total": CONTINUATION_FIELD_COUNT,
        "continuation_fields_recovered": CONTINUATION_FIELD_COUNT,
        "acknowledged_input_replays": 0,
        "execution_gate": "allow",
        "recovery_read_receipt": read_receipt,
        "observed_at": _timestamp(observed_at, "observed_at"),
        "state_write_authority": False,
        "provider_native_authority": False,
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    validate_durable_recovery_receipt(receipt)
    return json.loads(_canonical(receipt))


def validate_durable_recovery_receipt(receipt: Any) -> dict[str, Any]:
    """Validate a bounded recovery decision receipt."""
    receipt = _object(receipt, _RECOVERY_FIELDS, "durable recovery receipt")
    if receipt["schema_version"] != RECOVERY_SCHEMA_VERSION:
        raise DurableContinuationError("unsupported durable recovery schema_version")
    for field in ("operation_id", "project_id", "task_id"):
        _id(receipt[field], field)
    for field in ("project_revision", "task_revision"):
        _uint(receipt[field], field)
    _event_head(receipt["event_head"])
    _sha(receipt["durable_state_sha256"], "durable_state_sha256")
    if receipt["phase"] not in _PHASES:
        raise DurableContinuationError("durable recovery phase is invalid")
    _text(receipt["last_durable_action"], "last_durable_action")
    if receipt["first_action"] is not None:
        _text(receipt["first_action"], "first_action")
    _ids(receipt["acknowledged_input_ids"], "acknowledged_input_ids")
    _ids(receipt["reserved_effect_ids"], "reserved_effect_ids")
    if receipt["response_mode"] not in _RESPONSE_MODES:
        raise DurableContinuationError("durable recovery response mode is invalid")
    for field in (
        "continuation_fields_total",
        "continuation_fields_recovered",
        "acknowledged_input_replays",
    ):
        _uint(receipt[field], field)
    if (
        receipt["continuation_fields_total"] != CONTINUATION_FIELD_COUNT
        or receipt["continuation_fields_recovered"] != CONTINUATION_FIELD_COUNT
        or receipt["acknowledged_input_replays"] != 0
    ):
        raise DurableContinuationError("durable continuation fields were not recovered exactly")
    if receipt["execution_gate"] != "allow":
        raise DurableContinuationError("durable recovery execution gate is invalid")
    read_receipt = _object(
        receipt["recovery_read_receipt"], _READ_RECEIPT_FIELDS, "recovery read receipt"
    )
    for field in ("read_count", "bytes_read", "budget_bytes"):
        _uint(read_receipt[field], f"recovery_read_receipt.{field}")
    refs = _ids(read_receipt["source_refs"], "recovery_read_receipt.source_refs")
    # Artifact and state refs contain only the same safe ID character set.
    if read_receipt["read_count"] != len(refs):
        raise DurableContinuationError("recovery read count is invalid")
    if (
        read_receipt["budget_bytes"] <= 0
        or read_receipt["budget_bytes"] > MAX_RECOVERY_BYTES
        or read_receipt["bytes_read"] > read_receipt["budget_bytes"]
    ):
        raise DurableContinuationError("recovery read budget is invalid")
    _sha(read_receipt["reads_sha256"], "recovery_read_receipt.reads_sha256")
    _timestamp(receipt["observed_at"], "observed_at")
    if receipt["state_write_authority"] is not False:
        raise DurableContinuationError("recovery receipt cannot claim State authority")
    if receipt["provider_native_authority"] is not False:
        raise DurableContinuationError("recovery receipt cannot claim provider authority")
    _sha(receipt["receipt_sha256"], "receipt_sha256")
    if receipt["receipt_sha256"] != _receipt_digest(receipt):
        raise DurableContinuationError("durable recovery receipt digest mismatch")
    return copy.deepcopy(receipt)


def canonical_durable_recovery_bytes(receipt: dict[str, Any]) -> bytes:
    """Return canonical bytes for a validated recovery receipt."""
    return _canonical(validate_durable_recovery_receipt(receipt))
