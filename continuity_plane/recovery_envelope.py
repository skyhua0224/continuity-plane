"""Checkpoint-bound, bounded recovery envelope for provider lifecycle adapters."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any


SCHEMA_VERSION = "context.recovery-envelope/v1alpha1"
INTERACTION_CURSOR_SCHEMA_VERSION = "context.interaction-cursor/v1alpha1"
MAX_ENVELOPE_BYTES = 12 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_CURSOR_FIELDS = {
    "schema_version",
    "current_input_ref",
    "current_input_sha256",
    "current_turn_sha256",
    "confirmed_input_refs",
    "visible_output_high_watermark_sha256",
    "visible_output_phase",
    "response_mode",
    "no_restate",
    "raw_transcript_admission",
    "state_write_authority",
    "completion_authority",
    "cursor_sha256",
}
_ENVELOPE_FIELDS = {
    "schema_version",
    "project_id",
    "revision",
    "event_head",
    "checkpoint_ref",
    "checkpoint_verified",
    "active_work",
    "claim",
    "current_decisions",
    "current_constraints",
    "open_blockers",
    "return_point_work_id",
    "effect_high_watermark",
    "proposal_sha256",
    "source_fresh",
    "lease_valid",
    "read_only",
    "next_action",
    "first_permitted_action",
    "interaction_cursor",
    "skill_lock",
    "recovery_read_budget_bytes",
    "state_write_authority",
    "completion_authority",
    "packet_sha256",
}
_EVENT_HEAD_FIELDS = {"sequence_no", "event_sha256"}
_ARTIFACT_FIELDS = {"schema_version", "artifact_uri", "digest_algorithm", "digest", "size_bytes"}
_ACTIVE_WORK_FIELDS = {"work_id", "title", "status", "revision", "scope_refs", "evidence_ids"}
_CLAIM_FIELDS = {"claim_id", "actor_ref", "status", "lease_expires_at", "scope_owners"}
_SCOPE_FIELDS = {"scope_kind", "scope_ref"}
_DECISION_FIELDS = {"decision_id", "statement", "evidence_ids"}
_CONSTRAINT_FIELDS = {"constraint_id", "statement", "scope_work_ids", "evidence_ids"}
_BLOCKER_FIELDS = {"blocker_id", "reason", "blocked_work_ids", "evidence_ids"}
_ACTION_FIELDS = {"kind", "target", "request_sha256"}
_SKILL_LOCK_FIELDS = {
    "status",
    "selected_rule_ids",
    "compiled_packet_sha256",
    "unavailable_reason",
}


class RecoveryEnvelopeError(ValueError):
    """Raised when recovery context is stale, unbounded, or self-authorizing."""


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
        raise RecoveryEnvelopeError("recovery value is not canonical JSON") from exc


def _digest(document: dict[str, Any], digest_field: str) -> str:
    body = copy.deepcopy(document)
    body.pop(digest_field, None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RecoveryEnvelopeError(f"{field} fields are invalid")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise RecoveryEnvelopeError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise RecoveryEnvelopeError(f"{field} is invalid")
    return value


def _uint(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise RecoveryEnvelopeError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, *, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(character in value for character in "\r\n\x00")
    ):
        raise RecoveryEnvelopeError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RecoveryEnvelopeError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RecoveryEnvelopeError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecoveryEnvelopeError(f"{field} requires timezone")
    return value


def _ids(value: Any, field: str, *, maximum: int = 256) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise RecoveryEnvelopeError(f"{field} is invalid")
    normalized = [_id(item, field) for item in value]
    if len(normalized) != len(set(normalized)):
        raise RecoveryEnvelopeError(f"{field} must contain unique values")
    return sorted(normalized)


def validate_interaction_cursor(value: Any, *, verify_digest: bool = True) -> None:
    cursor = _object(value, _CURSOR_FIELDS, "interaction cursor")
    if cursor["schema_version"] != INTERACTION_CURSOR_SCHEMA_VERSION:
        raise RecoveryEnvelopeError("interaction cursor schema_version is unsupported")
    _id(cursor["current_input_ref"], "current_input_ref")
    _sha(cursor["current_input_sha256"], "current_input_sha256")
    if cursor["current_turn_sha256"] is not None:
        _sha(cursor["current_turn_sha256"], "current_turn_sha256")
    confirmed = _ids(cursor["confirmed_input_refs"], "confirmed_input_refs")
    if cursor["visible_output_high_watermark_sha256"] is not None:
        _sha(
            cursor["visible_output_high_watermark_sha256"],
            "visible_output_high_watermark_sha256",
        )
    if cursor["visible_output_phase"] not in {None, "commentary", "final_answer", "other"}:
        raise RecoveryEnvelopeError("visible output phase is invalid")
    if cursor["response_mode"] not in {
        "answer-current-input",
        "continue-without-restatement",
        "continue-silently",
    }:
        raise RecoveryEnvelopeError("interaction response mode is invalid")
    if type(cursor["no_restate"]) is not bool:
        raise RecoveryEnvelopeError("no_restate is invalid")
    if cursor["no_restate"] != (
        cursor["visible_output_high_watermark_sha256"] is not None
    ):
        raise RecoveryEnvelopeError("no_restate conflicts with visible output watermark")
    if cursor["response_mode"] == "continue-silently" and cursor["current_input_ref"] not in confirmed:
        raise RecoveryEnvelopeError("silent continuation requires confirmed input")
    if cursor["response_mode"] != "continue-silently" and cursor["current_input_ref"] in confirmed:
        raise RecoveryEnvelopeError("confirmed input requires silent continuation")
    for field in (
        "raw_transcript_admission",
        "state_write_authority",
        "completion_authority",
    ):
        if cursor[field] is not False:
            raise RecoveryEnvelopeError(f"interaction cursor {field} must be false")
    _sha(cursor["cursor_sha256"], "cursor_sha256")
    if verify_digest and cursor["cursor_sha256"] != _digest(cursor, "cursor_sha256"):
        raise RecoveryEnvelopeError("interaction cursor digest mismatch")


def load_interaction_cursor(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            cursor = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryEnvelopeError("interaction cursor is unavailable or invalid") from exc
    validate_interaction_cursor(cursor)
    return cursor


def validate_recovery_skill_lock(value: Any) -> None:
    skill = _object(value, _SKILL_LOCK_FIELDS, "skill_lock")
    _ids(skill["selected_rule_ids"], "skill_lock.selected_rule_ids")
    if skill["status"] == "measured":
        _sha(skill["compiled_packet_sha256"], "compiled_packet_sha256")
        if not skill["selected_rule_ids"] or skill["unavailable_reason"] is not None:
            raise RecoveryEnvelopeError("measured skill lock is incomplete")
    elif skill["status"] == "unavailable":
        if skill["compiled_packet_sha256"] is not None or skill["selected_rule_ids"]:
            raise RecoveryEnvelopeError("unavailable skill lock carries selected assets")
        _text(skill["unavailable_reason"], "skill_lock.unavailable_reason", maximum=512)
    else:
        raise RecoveryEnvelopeError("skill lock status is invalid")


def load_recovery_skill_lock(path: str | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as source:
            skill_lock = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryEnvelopeError("Skill lock is unavailable or invalid") from exc
    validate_recovery_skill_lock(skill_lock)
    return skill_lock


def _validate_scope(value: Any) -> None:
    scope = _object(value, _SCOPE_FIELDS, "scope")
    _text(scope["scope_kind"], "scope_kind", maximum=64)
    _text(scope["scope_ref"], "scope_ref", maximum=512)


def _validate_scopes(value: Any, field: str) -> None:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise RecoveryEnvelopeError(f"{field} is invalid")
    for item in value:
        _validate_scope(item)


def _validate_checkpoint_ref(value: Any) -> None:
    ref = _object(value, _ARTIFACT_FIELDS, "checkpoint_ref")
    if ref["schema_version"] != "context.artifact-ref/v1alpha1":
        raise RecoveryEnvelopeError("checkpoint artifact schema is unsupported")
    if ref["digest_algorithm"] != "sha-256":
        raise RecoveryEnvelopeError("checkpoint digest algorithm is unsupported")
    digest = _sha(ref["digest"], "checkpoint digest")
    if ref["artifact_uri"] != f"artifact://sha256/{digest}":
        raise RecoveryEnvelopeError("checkpoint artifact URI does not match digest")
    _uint(ref["size_bytes"], "checkpoint size_bytes", positive=True)


def _expected_action_sha(envelope: dict[str, Any]) -> str:
    body = {
        "project_id": envelope["project_id"],
        "revision": envelope["revision"],
        "event_head": envelope["event_head"],
        "checkpoint_digest": envelope["checkpoint_ref"]["digest"],
        "work_id": envelope["active_work"]["work_id"],
        "work_revision": envelope["active_work"]["revision"],
        "claim_id": envelope["claim"]["claim_id"],
        "next_action": envelope["next_action"],
        "cursor_sha256": (
            envelope["interaction_cursor"]["cursor_sha256"]
            if envelope["interaction_cursor"] is not None
            else None
        ),
    }
    return hashlib.sha256(_canonical(body)).hexdigest()


def _validate_summary_items(value: Any, kind: str) -> None:
    fields = {
        "decision": _DECISION_FIELDS,
        "constraint": _CONSTRAINT_FIELDS,
        "blocker": _BLOCKER_FIELDS,
    }[kind]
    if not isinstance(value, list) or len(value) > 32:
        raise RecoveryEnvelopeError(f"current {kind} list is invalid")
    identifiers = []
    for item in value:
        document = _object(item, fields, kind)
        identifier_field = f"{kind}_id"
        identifiers.append(_id(document[identifier_field], identifier_field))
        _text(
            document["reason" if kind == "blocker" else "statement"],
            f"{kind} text",
        )
        _ids(document["evidence_ids"], f"{kind}.evidence_ids")
        if kind == "constraint":
            _ids(document["scope_work_ids"], "constraint.scope_work_ids")
        if kind == "blocker":
            _ids(document["blocked_work_ids"], "blocker.blocked_work_ids")
    if len(identifiers) != len(set(identifiers)):
        raise RecoveryEnvelopeError(f"current {kind} IDs must be unique")


def validate_recovery_envelope(value: Any, *, verify_digest: bool = True) -> None:
    envelope = _object(value, _ENVELOPE_FIELDS, "recovery envelope")
    if envelope["schema_version"] != SCHEMA_VERSION:
        raise RecoveryEnvelopeError("recovery envelope schema_version is unsupported")
    _id(envelope["project_id"], "project_id")
    _uint(envelope["revision"], "revision", positive=True)
    head = _object(envelope["event_head"], _EVENT_HEAD_FIELDS, "event_head")
    _uint(head["sequence_no"], "event_head.sequence_no", positive=True)
    _sha(head["event_sha256"], "event_head.event_sha256")
    _validate_checkpoint_ref(envelope["checkpoint_ref"])
    if envelope["checkpoint_verified"] is not True:
        raise RecoveryEnvelopeError("recovery envelope requires a verified checkpoint")
    work = _object(envelope["active_work"], _ACTIVE_WORK_FIELDS, "active_work")
    _id(work["work_id"], "active_work.work_id")
    _text(work["title"], "active_work.title", maximum=512)
    if work["status"] != "active":
        raise RecoveryEnvelopeError("active_work status is invalid")
    _uint(work["revision"], "active_work.revision", positive=True)
    _validate_scopes(work["scope_refs"], "active_work.scope_refs")
    _ids(work["evidence_ids"], "active_work.evidence_ids")
    claim = _object(envelope["claim"], _CLAIM_FIELDS, "claim")
    for field in ("claim_id", "actor_ref"):
        _id(claim[field], f"claim.{field}")
    if claim["status"] != "active":
        raise RecoveryEnvelopeError("claim status is invalid")
    _timestamp(claim["lease_expires_at"], "claim.lease_expires_at")
    _validate_scopes(claim["scope_owners"], "claim.scope_owners")
    _validate_summary_items(envelope["current_decisions"], "decision")
    _validate_summary_items(envelope["current_constraints"], "constraint")
    _validate_summary_items(envelope["open_blockers"], "blocker")
    if envelope["return_point_work_id"] is not None:
        _id(envelope["return_point_work_id"], "return_point_work_id")
    _uint(envelope["effect_high_watermark"], "effect_high_watermark")
    _sha(envelope["proposal_sha256"], "proposal_sha256")
    for field in ("source_fresh", "lease_valid", "read_only"):
        if type(envelope[field]) is not bool:
            raise RecoveryEnvelopeError(f"{field} is invalid")
    if envelope["read_only"] != (
        not envelope["source_fresh"] or not envelope["lease_valid"]
    ):
        raise RecoveryEnvelopeError("read_only does not match source and lease status")
    _text(envelope["next_action"], "next_action")
    action = _object(envelope["first_permitted_action"], _ACTION_FIELDS, "first action")
    if action["kind"] != "continuation" or action["target"] != envelope["next_action"]:
        raise RecoveryEnvelopeError("first permitted action does not match next_action")
    _sha(action["request_sha256"], "first action request_sha256")
    if action["request_sha256"] != _expected_action_sha(envelope):
        raise RecoveryEnvelopeError("first permitted action digest mismatch")
    if envelope["interaction_cursor"] is not None:
        validate_interaction_cursor(envelope["interaction_cursor"])
    validate_recovery_skill_lock(envelope["skill_lock"])
    if envelope["recovery_read_budget_bytes"] != MAX_ENVELOPE_BYTES:
        raise RecoveryEnvelopeError("recovery read budget is invalid")
    if envelope["state_write_authority"] is not False or envelope["completion_authority"] is not False:
        raise RecoveryEnvelopeError("recovery envelopes have no authority")
    _sha(envelope["packet_sha256"], "packet_sha256")
    if verify_digest and envelope["packet_sha256"] != _digest(envelope, "packet_sha256"):
        raise RecoveryEnvelopeError("recovery envelope digest mismatch")
    if len(_canonical(envelope)) > MAX_ENVELOPE_BYTES:
        raise RecoveryEnvelopeError("recovery envelope exceeds the 12 KiB budget")


def compose_recovery_envelope(
    *,
    project_id: str,
    revision: int,
    event_head: dict[str, Any],
    checkpoint_ref: dict[str, Any],
    active_work: dict[str, Any],
    claim: dict[str, Any],
    current_decisions: list[dict[str, Any]],
    current_constraints: list[dict[str, Any]],
    open_blockers: list[dict[str, Any]],
    return_point_work_id: str | None,
    effect_high_watermark: int,
    proposal_sha256: str,
    source_fresh: bool,
    lease_valid: bool,
    next_action: str,
    interaction_cursor: dict[str, Any] | None,
    skill_lock: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the exact context allowed after a verified checkpoint restore."""
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "revision": revision,
        "event_head": copy.deepcopy(event_head),
        "checkpoint_ref": copy.deepcopy(checkpoint_ref),
        "checkpoint_verified": True,
        "active_work": copy.deepcopy(active_work),
        "claim": copy.deepcopy(claim),
        "current_decisions": copy.deepcopy(current_decisions),
        "current_constraints": copy.deepcopy(current_constraints),
        "open_blockers": copy.deepcopy(open_blockers),
        "return_point_work_id": return_point_work_id,
        "effect_high_watermark": effect_high_watermark,
        "proposal_sha256": proposal_sha256,
        "source_fresh": source_fresh,
        "lease_valid": lease_valid,
        "read_only": not source_fresh or not lease_valid,
        "next_action": next_action,
        "first_permitted_action": {
            "kind": "continuation",
            "target": next_action,
            "request_sha256": "",
        },
        "interaction_cursor": copy.deepcopy(interaction_cursor),
        "skill_lock": copy.deepcopy(skill_lock)
        if skill_lock is not None
        else {
            "status": "unavailable",
            "selected_rule_ids": [],
            "compiled_packet_sha256": None,
            "unavailable_reason": "no active compiled Skill lock is stored in local State",
        },
        "recovery_read_budget_bytes": MAX_ENVELOPE_BYTES,
        "state_write_authority": False,
        "completion_authority": False,
        "packet_sha256": "",
    }
    envelope["first_permitted_action"]["request_sha256"] = _expected_action_sha(envelope)
    envelope["packet_sha256"] = _digest(envelope, "packet_sha256")
    validate_recovery_envelope(envelope)
    return envelope


__all__ = [
    "MAX_ENVELOPE_BYTES",
    "RecoveryEnvelopeError",
    "compose_recovery_envelope",
    "load_interaction_cursor",
    "load_recovery_skill_lock",
    "validate_interaction_cursor",
    "validate_recovery_envelope",
    "validate_recovery_skill_lock",
]
