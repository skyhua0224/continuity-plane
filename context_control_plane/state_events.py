"""M2-02 append-only state events and deterministic snapshot replay."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .typed_state import validate_typed_state


EVENT_SCHEMA_VERSION = "context.state-event/v1alpha1"

_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "event_type",
    "project_id",
    "sequence_no",
    "revision_before",
    "revision_after",
    "occurred_at",
    "actor_ref",
    "causation_ref",
    "correlation_ref",
    "previous_event_sha256",
    "supersedes_event_id",
    "changes",
    "project_after",
    "event_sha256",
}
_CHANGE_FIELDS = {"collection", "object_id", "value"}
_EVENT_TYPES = {"state-transition", "correction"}
_COLLECTION_ID_FIELDS = {
    "works": "work_id",
    "claims": "claim_id",
    "ideas": "idea_id",
    "decisions": "decision_id",
    "constraints": "constraint_id",
    "evidence": "evidence_id",
    "blockers": "blocker_id",
    "effects": "effect_id",
}
_TERMINAL_DECISION_STATUSES = {"rejected", "reverted", "superseded"}
_REVIVABLE_DECISION_STATUSES = {"proposed", "accepted"}
_TERMINAL_WORK_STATUSES = {"completed", "rejected", "reverted", "superseded"}
_REVIVABLE_WORK_STATUSES = {"proposed", "blocked", "ready", "active", "verifying"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StateEventError(ValueError):
    """Raised when an M2-02 event or replay stream is invalid."""


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StateEventError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _non_empty_string(value, field)


def _positive_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise StateEventError(f"{field} must be a positive integer")
    return value


def _non_negative_integer(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise StateEventError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: Any, field: str) -> None:
    value = _non_empty_string(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StateEventError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise StateEventError(f"{field} must include timezone")


def _sha256(value: Any, field: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise StateEventError(f"{field} must be lowercase SHA-256")
    return value


def _event_payload_bytes(event: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in event.items() if key != "event_sha256"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _computed_event_sha256(event: dict[str, Any]) -> str:
    return hashlib.sha256(_event_payload_bytes(event)).hexdigest()


def validate_state_event(event: dict[str, Any]) -> None:
    """Validate one event independently of its position in a stream."""
    if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
        raise StateEventError("event fields do not match the contract")
    if event["schema_version"] != EVENT_SCHEMA_VERSION:
        raise StateEventError("unsupported schema_version")

    _non_empty_string(event["event_id"], "event.event_id")
    event_type = event["event_type"]
    if event_type not in _EVENT_TYPES:
        raise StateEventError("unsupported event_type")
    project_id = _non_empty_string(event["project_id"], "event.project_id")
    _positive_integer(event["sequence_no"], "event.sequence_no")
    revision_before = _non_negative_integer(
        event["revision_before"], "event.revision_before"
    )
    revision_after = _positive_integer(event["revision_after"], "event.revision_after")
    if revision_after != revision_before + 1:
        raise StateEventError("event revisions must advance by one")
    _timestamp(event["occurred_at"], "event.occurred_at")
    _non_empty_string(event["actor_ref"], "event.actor_ref")
    _optional_string(event["causation_ref"], "event.causation_ref")
    _optional_string(event["correlation_ref"], "event.correlation_ref")
    _sha256(
        event["previous_event_sha256"],
        "event.previous_event_sha256",
        optional=True,
    )
    supersedes_event_id = _optional_string(
        event["supersedes_event_id"], "event.supersedes_event_id"
    )
    if event_type == "state-transition" and supersedes_event_id is not None:
        raise StateEventError("state-transition cannot supersede an event")
    if event_type == "correction" and supersedes_event_id is None:
        raise StateEventError("correction requires supersedes_event_id")

    changes = event["changes"]
    if not isinstance(changes, list) or not changes:
        raise StateEventError("event.changes must be a non-empty list")
    seen_changes: set[tuple[str, str]] = set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != _CHANGE_FIELDS:
            raise StateEventError("change fields do not match the contract")
        collection = change["collection"]
        if collection not in _COLLECTION_ID_FIELDS:
            raise StateEventError("change.collection is unsupported")
        object_id = _non_empty_string(change["object_id"], "change.object_id")
        value = change["value"]
        if not isinstance(value, dict):
            raise StateEventError("change.value must be an object")
        id_field = _COLLECTION_ID_FIELDS[collection]
        if value.get(id_field) != object_id:
            raise StateEventError("change.object_id must match change.value")
        change_key = (collection, object_id)
        if change_key in seen_changes:
            raise StateEventError("event.changes must target unique objects")
        seen_changes.add(change_key)

    project_after = event["project_after"]
    if not isinstance(project_after, dict):
        raise StateEventError("event.project_after must be an object")
    if project_after.get("project_id") != project_id:
        raise StateEventError("event.project_after project_id mismatch")
    if project_after.get("revision") != revision_after:
        raise StateEventError("event.project_after revision mismatch")

    event_sha256 = _sha256(event["event_sha256"], "event.event_sha256")
    if event_sha256 != _computed_event_sha256(event):
        raise StateEventError("event.event_sha256 mismatch")


def build_state_event(
    *,
    event_id: str,
    event_type: str,
    project_id: str,
    sequence_no: int,
    revision_before: int,
    occurred_at: str,
    actor_ref: str,
    causation_ref: str | None,
    correlation_ref: str | None,
    previous_event_sha256: str | None,
    supersedes_event_id: str | None,
    changes: list[dict[str, Any]],
    project_after: dict[str, Any],
) -> dict[str, Any]:
    """Build a canonical event and bind its content hash."""
    event = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "event_type": event_type,
        "project_id": project_id,
        "sequence_no": sequence_no,
        "revision_before": revision_before,
        "revision_after": revision_before + 1,
        "occurred_at": occurred_at,
        "actor_ref": actor_ref,
        "causation_ref": causation_ref,
        "correlation_ref": correlation_ref,
        "previous_event_sha256": previous_event_sha256,
        "supersedes_event_id": supersedes_event_id,
        "changes": copy.deepcopy(changes),
        "project_after": copy.deepcopy(project_after),
        "event_sha256": "0" * 64,
    }
    event["event_sha256"] = _computed_event_sha256(event)
    validate_state_event(event)
    return event


def canonical_event_bytes(event: dict[str, Any]) -> bytes:
    """Return deterministic JSON bytes for a validated event."""
    validate_state_event(event)
    return json.dumps(
        event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _put_change(state: dict[str, Any], change: dict[str, Any]) -> None:
    collection = change["collection"]
    object_id = change["object_id"]
    id_field = _COLLECTION_ID_FIELDS[collection]
    values = state[collection]
    existing_index = next(
        (index for index, item in enumerate(values) if item[id_field] == object_id),
        None,
    )
    if existing_index is not None:
        previous = values[existing_index]
        current = change["value"]
        if (
            collection == "decisions"
            and previous["status"] in _TERMINAL_DECISION_STATUSES
            and current["status"] in _REVIVABLE_DECISION_STATUSES
        ):
            raise StateEventError("terminal Decision cannot be revived")
        if (
            collection == "works"
            and previous["status"] in _TERMINAL_WORK_STATUSES
            and current["status"] in _REVIVABLE_WORK_STATUSES
        ):
            raise StateEventError("terminal Work cannot be revived")
        values[existing_index] = copy.deepcopy(current)
    else:
        values.append(copy.deepcopy(change["value"]))


def replay_state_events(
    initial_state: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    starting_sequence_no: int = 1,
    previous_event_sha256: str | None = None,
) -> dict[str, Any]:
    """Replay a contiguous event segment from an M2-01 snapshot."""
    if not isinstance(events, list):
        raise StateEventError("events must be a list")
    state = copy.deepcopy(initial_state)
    validate_typed_state(state)
    project_id = state["project"]["project_id"]
    expected_revision = state["project"]["revision"]
    expected_sequence = _positive_integer(
        starting_sequence_no,
        "starting_sequence_no",
    )
    previous_event_sha256 = _sha256(
        previous_event_sha256,
        "previous_event_sha256",
        optional=True,
    )
    seen_event_ids: set[str] = set()

    for event in events:
        validate_state_event(event)
        event_id = event["event_id"]
        if event_id in seen_event_ids:
            raise StateEventError("event_id must be unique in a stream")
        if event["sequence_no"] != expected_sequence:
            raise StateEventError("event sequence is not contiguous")
        if event["project_id"] != project_id:
            raise StateEventError("event project_id does not match the snapshot")
        if event["revision_before"] != expected_revision:
            raise StateEventError("event revision does not match the snapshot")
        if event["previous_event_sha256"] != previous_event_sha256:
            raise StateEventError("event hash chain is not contiguous")
        supersedes_event_id = event["supersedes_event_id"]
        if supersedes_event_id is not None and supersedes_event_id not in seen_event_ids:
            raise StateEventError("supersedes_event_id must reference an earlier event")

        for change in event["changes"]:
            _put_change(state, change)
        state["project"] = copy.deepcopy(event["project_after"])
        validate_typed_state(state)

        seen_event_ids.add(event_id)
        previous_event_sha256 = event["event_sha256"]
        expected_revision = event["revision_after"]
        expected_sequence += 1

    return state
