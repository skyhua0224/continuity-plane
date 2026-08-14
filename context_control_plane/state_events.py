"""M2-02 append-only state events and deterministic snapshot replay."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .typed_state import TypedStateError, validate_typed_state

LEGACY_EVENT_SCHEMA_VERSION = "context.state-event/v1alpha1"
EVENT_SCHEMA_VERSION = "context.state-event/v2alpha1"
EVENT_SCHEMA_VERSION_V3 = "context.state-event/v3alpha1"
EVENT_SCHEMA_VERSION_V4 = "context.state-event/v4alpha1"
IDEA_EVENT_SCHEMA_VERSION = "context.idea-event/v1alpha1"
IDEA_EVENT_SCHEMA_VERSION_V2 = "context.idea-event/v2alpha1"
SUPPORTED_EVENT_SCHEMA_VERSIONS = {
    LEGACY_EVENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION_V3,
    EVENT_SCHEMA_VERSION_V4,
    IDEA_EVENT_SCHEMA_VERSION,
    IDEA_EVENT_SCHEMA_VERSION_V2,
}

_BASE_EVENT_FIELDS = {
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
_V3_EVENT_FIELDS = _BASE_EVENT_FIELDS | {"task_transition"}
_V4_EVENT_FIELDS = _BASE_EVENT_FIELDS | {"task_transition", "experiment_transition"}
_IDEA_EVENT_FIELDS = _BASE_EVENT_FIELDS | {
    "task_transition",
    "experiment_transition",
    "idea_transition",
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
    "experiment_attempts": "attempt_id",
    "experiment_promotions": "promotion_id",
    "idea_relationships": "relationship_id",
    "idea_occurrences": "occurrence_id",
    "idea_reviews": "review_id",
    "correction_protections": "protection_id",
}
_TERMINAL_DECISION_STATUSES = {"rejected", "reverted", "superseded"}
_REVIVABLE_DECISION_STATUSES = {"proposed", "accepted"}
_TERMINAL_WORK_STATUSES = {"completed", "rejected", "reverted", "superseded"}
_REVIVABLE_WORK_STATUSES = {"proposed", "blocked", "ready", "active", "verifying"}
_APPEND_ONLY_COLLECTIONS = {
    "experiment_attempts",
    "experiment_promotions",
    "idea_relationships",
    "idea_occurrences",
    "idea_reviews",
}
_EXPERIMENT_APPEND_ONLY_COLLECTIONS = {
    "experiment_attempts",
    "experiment_promotions",
}
_EXPERIMENT_CONTRACT_FIELDS = {
    "return_point_work_id",
    "exit_criteria",
    "attempt_budget",
    "expires_at",
    "promotion_target_work_id",
    "mainline_authority",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OPAQUE_RANGE_REF_RE = re.compile(r"^rng_[a-z2-7]{26}$")
_TASK_TRANSITION_FIELDS = {
    "route_decision_sha256",
    "route_apply_request_sha256",
    "route_kind",
    "task_events",
}
_TASK_EVENT_FIELDS = {
    "task_event_id",
    "event_kind",
    "work_id",
    "work_revision",
    "return_work_id",
    "checkpoint_ref",
    "related_event_id",
}
_EXPERIMENT_TRANSITION_FIELDS = {
    "operation",
    "request_sha256",
    "attempt_id",
    "promotion_id",
    "proposal_id",
}
_IDEA_TRANSITION_FIELDS = {
    "operation",
    "request_sha256",
    "idea_id",
    "parent_work_id",
    "return_work_id",
    "switch_target_work_id",
}
_IDEA_REVIEW_TRANSITION_FIELDS = {
    "operation",
    "request_sha256",
    "canonical_idea_id",
    "submitted_idea_id",
    "occurrence_id",
    "review_id",
    "protection_id",
}
_ARTIFACT_REF_FIELDS = {
    "schema_version",
    "digest_algorithm",
    "digest",
    "size_bytes",
    "artifact_uri",
}


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


def _validate_artifact_ref(value: Any, field: str) -> None:
    if not isinstance(value, dict) or set(value) != _ARTIFACT_REF_FIELDS:
        raise StateEventError(f"{field} fields do not match ArtifactRef")
    if value["schema_version"] != "context.artifact-ref/v1alpha1":
        raise StateEventError(f"{field}.schema_version is unsupported")
    if value["digest_algorithm"] != "sha-256":
        raise StateEventError(f"{field}.digest_algorithm must be sha-256")
    digest = _sha256(value["digest"], f"{field}.digest")
    _non_negative_integer(value["size_bytes"], f"{field}.size_bytes")
    if value["artifact_uri"] != f"artifact://sha256/{digest}":
        raise StateEventError(f"{field}.artifact_uri does not match digest")


def _validate_task_transition(
    transition: Any,
    *,
    event_type: str,
    supersedes_event_id: str | None,
) -> None:
    if transition is None:
        return
    if not isinstance(transition, dict) or set(transition) != _TASK_TRANSITION_FIELDS:
        raise StateEventError("task_transition fields do not match the contract")
    _sha256(
        transition["route_decision_sha256"],
        "task_transition.route_decision_sha256",
    )
    _sha256(
        transition["route_apply_request_sha256"],
        "task_transition.route_apply_request_sha256",
    )
    route_kind = transition["route_kind"]
    if route_kind not in {"child", "interrupt", "switch", "correction"}:
        raise StateEventError("task_transition.route_kind is unsupported")
    task_events = transition["task_events"]
    if not isinstance(task_events, list) or not task_events:
        raise StateEventError("task_transition.task_events must be non-empty")

    seen_task_event_ids: set[str] = set()
    for task_event in task_events:
        if not isinstance(task_event, dict) or set(task_event) != _TASK_EVENT_FIELDS:
            raise StateEventError("task event fields do not match the contract")
        task_event_id = _non_empty_string(
            task_event["task_event_id"], "task_event.task_event_id"
        )
        if task_event_id in seen_task_event_ids:
            raise StateEventError("task_event_id must be unique")
        seen_task_event_ids.add(task_event_id)
        if task_event["event_kind"] not in {
            "child_proposed",
            "task_suspended",
            "task_activated",
            "correction_applied",
        }:
            raise StateEventError("task_event.event_kind is unsupported")
        _non_empty_string(task_event["work_id"], "task_event.work_id")
        _non_negative_integer(task_event["work_revision"], "task_event.work_revision")
        _optional_string(task_event["return_work_id"], "task_event.return_work_id")
        checkpoint_ref = task_event["checkpoint_ref"]
        if checkpoint_ref is not None:
            _validate_artifact_ref(checkpoint_ref, "task_event.checkpoint_ref")
        _optional_string(task_event["related_event_id"], "task_event.related_event_id")

    kinds = [task_event["event_kind"] for task_event in task_events]
    work_ids = [task_event["work_id"] for task_event in task_events]
    if route_kind == "child":
        if kinds != ["child_proposed"] or event_type != "state-transition":
            raise StateEventError("child route requires one child_proposed task event")
    elif route_kind in {"interrupt", "switch"}:
        if (
            kinds != ["task_suspended", "task_activated"]
            or len(work_ids) != 2
            or work_ids[0] == work_ids[1]
            or event_type != "state-transition"
        ):
            raise StateEventError(
                "task_events must contain suspended old work then activated target"
            )
    elif route_kind == "correction" and (
        kinds != ["correction_applied"]
        or event_type != "correction"
        or supersedes_event_id is None
    ):
        raise StateEventError(
            "correction route requires one correction_applied task event and supersedes"
        )


def _validate_experiment_transition(
    transition: Any,
    *,
    changes: list[dict[str, Any]],
) -> None:
    if not isinstance(transition, dict) or set(transition) != _EXPERIMENT_TRANSITION_FIELDS:
        raise StateEventError("experiment_transition fields do not match the contract")
    operation = transition["operation"]
    if operation not in {"attempt-started", "promotion-proposed", "promotion-approved"}:
        raise StateEventError("experiment_transition operation is unsupported")
    _sha256(transition["request_sha256"], "experiment_transition.request_sha256")
    for field in ("attempt_id", "promotion_id", "proposal_id"):
        _optional_string(transition[field], f"experiment_transition.{field}")
    if operation == "attempt-started":
        if transition["attempt_id"] is None or any(
            transition[field] is not None for field in ("promotion_id", "proposal_id")
        ):
            raise StateEventError("attempt transition identity is invalid")
        if {
            (change["collection"], change["object_id"])
            for change in changes
        } & {("experiment_attempts", transition["attempt_id"])} != {
            ("experiment_attempts", transition["attempt_id"])
        }:
            raise StateEventError("attempt transition requires its ledger change")
    elif transition["attempt_id"] is not None or transition["promotion_id"] is None:
        raise StateEventError("promotion transition identity is invalid")
    elif operation == "promotion-proposed" and transition["proposal_id"] != transition["promotion_id"]:
        raise StateEventError("promotion proposal must self-identify")
    elif operation == "promotion-approved" and transition["proposal_id"] is None:
        raise StateEventError("promotion approval requires proposal identity")
    if operation in {"promotion-proposed", "promotion-approved"} and (
        ("experiment_promotions", transition["promotion_id"])
        not in {
            (change["collection"], change["object_id"])
            for change in changes
        }
    ):
        raise StateEventError("promotion transition requires its ledger change")


def _validate_idea_transition(
    transition: Any,
    *,
    changes: list[dict[str, Any]],
) -> None:
    if not isinstance(transition, dict) or set(transition) != _IDEA_TRANSITION_FIELDS:
        raise StateEventError("idea_transition fields do not match the contract")
    operation = transition["operation"]
    if operation not in {"capture-and-continue", "park", "propose-switch"}:
        raise StateEventError("idea_transition operation is unsupported")
    _sha256(transition["request_sha256"], "idea_transition.request_sha256")
    for field in ("idea_id", "parent_work_id", "return_work_id"):
        _non_empty_string(transition[field], f"idea_transition.{field}")
    switch_target = _optional_string(
        transition["switch_target_work_id"], "idea_transition.switch_target_work_id"
    )
    if (operation == "propose-switch") != (switch_target is not None):
        raise StateEventError("idea transition switch target is invalid")
    idea_changes = [change for change in changes if change["collection"] == "ideas"]
    if (
        len(idea_changes) != 1
        or idea_changes[0]["object_id"] != transition["idea_id"]
    ):
        raise StateEventError("idea transition requires exactly one Idea change")
    idea = idea_changes[0]["value"]
    if (
        idea.get("parent_work_id") != transition["parent_work_id"]
        or idea.get("return_work_id") != transition["return_work_id"]
    ):
        raise StateEventError("idea transition does not match Idea binding")
    expected_status = {
        "capture-and-continue": "candidate",
        "park": "parked",
        "propose-switch": "proposed",
    }[operation]
    if idea.get("status") != expected_status or idea.get("promotion_target") != switch_target:
        raise StateEventError("idea transition does not match candidate state")


def _validate_idea_review_transition(
    transition: Any,
    *,
    changes: list[dict[str, Any]],
) -> None:
    if not isinstance(transition, dict) or set(transition) != _IDEA_REVIEW_TRANSITION_FIELDS:
        raise StateEventError("idea v2 transition fields do not match the contract")
    operation = transition["operation"]
    if operation not in {
        "capture-created",
        "capture-merged",
        "review-updated",
        "correction-guarded",
        "correction-released",
    }:
        raise StateEventError("idea v2 transition operation is unsupported")
    _sha256(transition["request_sha256"], "idea_transition.request_sha256")
    _non_empty_string(
        transition["canonical_idea_id"], "idea_transition.canonical_idea_id"
    )
    for field in ("submitted_idea_id", "occurrence_id", "review_id", "protection_id"):
        _optional_string(transition[field], f"idea_transition.{field}")
    changed = {(change["collection"], change["object_id"]) for change in changes}
    canonical_id = transition["canonical_idea_id"]
    if operation == "capture-created":
        if (
            transition["submitted_idea_id"] != canonical_id
            or transition["occurrence_id"] is None
            or transition["review_id"] is not None
            or transition["protection_id"] is not None
            or ("ideas", canonical_id) not in changed
            or ("idea_occurrences", transition["occurrence_id"]) not in changed
        ):
            raise StateEventError("idea v2 capture-created transition is invalid")
    elif operation == "capture-merged":
        if (
            transition["submitted_idea_id"] is None
            or transition["occurrence_id"] is None
            or transition["review_id"] is not None
            or transition["protection_id"] is not None
            or ("ideas", canonical_id) in changed
            or ("idea_occurrences", transition["occurrence_id"]) not in changed
        ):
            raise StateEventError("idea v2 capture-merged transition is invalid")
    elif operation == "review-updated":
        if (
            transition["submitted_idea_id"] is not None
            or transition["occurrence_id"] is not None
            or transition["review_id"] is None
            or transition["protection_id"] is not None
            or ("ideas", canonical_id) not in changed
            or ("idea_reviews", transition["review_id"]) not in changed
        ):
            raise StateEventError("idea v2 review transition is invalid")
    elif operation == "correction-guarded":
        if (
            transition["submitted_idea_id"] is not None
            or transition["occurrence_id"] is not None
            or transition["review_id"] is not None
            or transition["protection_id"] is None
            or ("correction_protections", transition["protection_id"]) not in changed
        ):
            raise StateEventError("idea v2 correction guard transition is invalid")
    elif (
        transition["submitted_idea_id"] is not None
        or transition["occurrence_id"] is not None
        or transition["review_id"] is not None
        or transition["protection_id"] is None
        or ("correction_protections", transition["protection_id"]) not in changed
    ):
        raise StateEventError("idea v2 correction release transition is invalid")


def validate_state_event(event: dict[str, Any]) -> None:
    """Validate one event independently of its position in a stream."""
    if not isinstance(event, dict) or set(event) not in (
        _BASE_EVENT_FIELDS,
        _V3_EVENT_FIELDS,
        _V4_EVENT_FIELDS,
        _IDEA_EVENT_FIELDS,
    ):
        raise StateEventError("event fields do not match the contract")
    schema_version = event["schema_version"]
    if schema_version not in SUPPORTED_EVENT_SCHEMA_VERSIONS:
        raise StateEventError("unsupported schema_version")
    if schema_version in {IDEA_EVENT_SCHEMA_VERSION, IDEA_EVENT_SCHEMA_VERSION_V2}:
        if set(event) != _IDEA_EVENT_FIELDS:
            raise StateEventError("Idea event fields do not match the contract")
    elif schema_version == EVENT_SCHEMA_VERSION_V4:
        if set(event) != _V4_EVENT_FIELDS:
            raise StateEventError("v4 event fields do not match the contract")
    elif schema_version == EVENT_SCHEMA_VERSION_V3:
        if set(event) != _V3_EVENT_FIELDS:
            raise StateEventError("v3 event fields do not match the contract")
    elif set(event) != _BASE_EVENT_FIELDS:
        raise StateEventError("legacy event fields do not match the contract")

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
    if schema_version not in {
        EVENT_SCHEMA_VERSION_V3,
        EVENT_SCHEMA_VERSION_V4,
        IDEA_EVENT_SCHEMA_VERSION,
        IDEA_EVENT_SCHEMA_VERSION_V2,
    }:
        if "task_transition" in event:
            raise StateEventError("v1/v2 events cannot contain task_transition")
    else:
        _validate_task_transition(
            event["task_transition"],
            event_type=event_type,
            supersedes_event_id=supersedes_event_id,
        )
    if schema_version in {
        EVENT_SCHEMA_VERSION_V4,
        IDEA_EVENT_SCHEMA_VERSION,
        IDEA_EVENT_SCHEMA_VERSION_V2,
    }:
        transition = event["experiment_transition"]
        if transition is not None:
            _validate_experiment_transition(transition, changes=event["changes"])
            if event["task_transition"] is not None:
                raise StateEventError("experiment transition cannot mix with route transition")
    if schema_version == IDEA_EVENT_SCHEMA_VERSION:
        transition = event["idea_transition"]
        if transition is None:
            raise StateEventError("Idea event requires idea_transition")
        _validate_idea_transition(transition, changes=event["changes"])
        if (
            event["task_transition"] is not None
            or event["experiment_transition"] is not None
        ):
            raise StateEventError("idea transition cannot mix with task or experiment transition")
    if schema_version == IDEA_EVENT_SCHEMA_VERSION_V2:
        transition = event["idea_transition"]
        if transition is None:
            raise StateEventError("Idea v2 event requires idea_transition")
        _validate_idea_review_transition(transition, changes=event["changes"])
        if (
            event["task_transition"] is not None
            or event["experiment_transition"] is not None
        ):
            raise StateEventError("idea v2 transition cannot mix with task or experiment transition")

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
    task_transition: dict[str, Any] | None = None,
    experiment_transition: dict[str, Any] | None = None,
    idea_transition: dict[str, Any] | None = None,
    schema_version: str | None = None,
) -> dict[str, Any]:
    """Build a canonical event and bind its content hash."""
    if schema_version is None:
        if idea_transition is not None:
            schema_version = IDEA_EVENT_SCHEMA_VERSION
        elif experiment_transition is not None:
            schema_version = EVENT_SCHEMA_VERSION_V4
        elif task_transition is not None:
            schema_version = EVENT_SCHEMA_VERSION_V3
        elif any(
            change.get("collection") == "works"
            and "return_point_work_id" in change.get("value", {})
            for change in changes
        ):
            schema_version = EVENT_SCHEMA_VERSION
        else:
            schema_version = LEGACY_EVENT_SCHEMA_VERSION
    if schema_version not in SUPPORTED_EVENT_SCHEMA_VERSIONS:
        raise StateEventError("unsupported event schema_version")
    if task_transition is not None and schema_version not in {
        EVENT_SCHEMA_VERSION_V3,
        EVENT_SCHEMA_VERSION_V4,
    }:
        raise StateEventError("task_transition requires v3 event schema")
    if experiment_transition is not None and schema_version != EVENT_SCHEMA_VERSION_V4:
        raise StateEventError("experiment_transition requires v4 event schema")
    if idea_transition is not None and schema_version not in {
        IDEA_EVENT_SCHEMA_VERSION,
        IDEA_EVENT_SCHEMA_VERSION_V2,
    }:
        raise StateEventError("idea_transition requires Idea event schema")
    event = {
        "schema_version": schema_version,
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
    if schema_version in {
        EVENT_SCHEMA_VERSION_V3,
        EVENT_SCHEMA_VERSION_V4,
        IDEA_EVENT_SCHEMA_VERSION,
        IDEA_EVENT_SCHEMA_VERSION_V2,
    }:
        event["task_transition"] = copy.deepcopy(task_transition)
    if schema_version in {
        EVENT_SCHEMA_VERSION_V4,
        IDEA_EVENT_SCHEMA_VERSION,
        IDEA_EVENT_SCHEMA_VERSION_V2,
    }:
        event["experiment_transition"] = copy.deepcopy(experiment_transition)
    if schema_version in {IDEA_EVENT_SCHEMA_VERSION, IDEA_EVENT_SCHEMA_VERSION_V2}:
        event["idea_transition"] = copy.deepcopy(idea_transition)
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
        if collection in _APPEND_ONLY_COLLECTIONS:
            raise StateEventError(f"{collection} are append-only")
        if (
            collection == "works"
            and previous.get("kind") == "experiment"
            and any(
                attempt["work_id"] == object_id
                for attempt in state.get("experiment_attempts", [])
            )
            and any(previous[field] != current[field] for field in _EXPERIMENT_CONTRACT_FIELDS)
        ):
            raise StateEventError("attempt-bound Experiment contract is immutable")
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
        if collection == "works" and any(
            previous[field] != current[field]
            for field in ("kind", "parent_work_id", "dependency_ids")
        ) and current["revision"] != previous["revision"] + 1:
            raise StateEventError(
                "Work revision must advance by one for a graph change"
            )
        values[existing_index] = copy.deepcopy(current)
    else:
        values.append(copy.deepcopy(change["value"]))


def replay_state_events(
    initial_state: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    starting_sequence_no: int = 1,
    previous_event_sha256: str | None = None,
    known_event_ids: set[str] | None = None,
    prior_events: list[dict[str, Any]] | None = None,
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
    if known_event_ids is not None and prior_events is not None:
        raise StateEventError("known_event_ids and prior_events are mutually exclusive")
    if known_event_ids is None:
        seen_event_ids: set[str] = set()
    elif not isinstance(known_event_ids, set) or any(
        not isinstance(event_id, str) or not event_id.strip()
        for event_id in known_event_ids
    ):
        raise StateEventError("known_event_ids must be a set of non-empty strings")
    else:
        seen_event_ids = set(known_event_ids)
    prior_event_by_id: dict[str, dict[str, Any]] = {}
    superseded_event_ids: set[str] = set()
    if prior_events is not None:
        if not isinstance(prior_events, list):
            raise StateEventError("prior_events must be a list")
        for prior_event in prior_events:
            validate_state_event(prior_event)
            if prior_event["project_id"] != project_id:
                raise StateEventError("prior Event project_id does not match the snapshot")
            prior_event_id = prior_event["event_id"]
            if prior_event_id in seen_event_ids:
                raise StateEventError("prior Event identities must be unique")
            seen_event_ids.add(prior_event_id)
            prior_event_by_id[prior_event_id] = copy.deepcopy(prior_event)
            if prior_event["supersedes_event_id"] is not None:
                superseded_event_ids.add(prior_event["supersedes_event_id"])
        if prior_events:
            if prior_events[-1]["sequence_no"] != expected_sequence - 1:
                raise StateEventError("prior Event sequence does not precede replay segment")
            if prior_events[-1]["event_sha256"] != previous_event_sha256:
                raise StateEventError("prior Event head does not match replay segment")
            if prior_events[-1]["revision_after"] != expected_revision:
                raise StateEventError("prior Event revision does not match snapshot")
        elif expected_sequence != 1 or previous_event_sha256 is not None:
            raise StateEventError("empty prior Event history does not match replay cursor")

    for event in events:
        validate_state_event(event)
        occurred_at = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
        snapshot_updated_at = datetime.fromisoformat(
            state["project"]["updated_at"].replace("Z", "+00:00")
        )
        if occurred_at < snapshot_updated_at:
            raise StateEventError("event occurred_at regresses project.updated_at")
        idea_transition = event.get("idea_transition")
        if idea_transition is not None:
            if event["schema_version"] == IDEA_EVENT_SCHEMA_VERSION:
                _validate_idea_replay_gate(
                    state,
                    event=event,
                    occurred_at=occurred_at,
                )
            elif event["schema_version"] == IDEA_EVENT_SCHEMA_VERSION_V2:
                _validate_idea_review_replay_gate(
                    state,
                    event=event,
                    occurred_at=occurred_at,
                )
        lifecycle_changes = {
            change["collection"]
            for change in event["changes"]
            if change["collection"] in _EXPERIMENT_APPEND_ONLY_COLLECTIONS
        }
        if lifecycle_changes and event.get("experiment_transition") is None:
            lifecycle_ids = {
                (change["collection"], change["object_id"])
                for change in event["changes"]
                if change["collection"] in _EXPERIMENT_APPEND_ONLY_COLLECTIONS
            }
            existing_lifecycle_ids = {
                (collection, item["attempt_id"] if collection == "experiment_attempts" else item["promotion_id"])
                for collection in _EXPERIMENT_APPEND_ONLY_COLLECTIONS
                for item in state[collection]
            }
            if not lifecycle_ids.issubset(existing_lifecycle_ids):
                raise StateEventError("lifecycle change requires experiment transition")
        if lifecycle_changes:
            lifecycle_work_ids = {
                change["value"]["work_id"] for change in event["changes"]
                if change["collection"] in _EXPERIMENT_APPEND_ONLY_COLLECTIONS
            }
            work_by_id = {work["work_id"]: work for work in state["works"]}
            for work_id in lifecycle_work_ids:
                work = work_by_id.get(work_id)
                if work is None:
                    raise StateEventError("lifecycle change references unknown Experiment")
                expiry = datetime.fromisoformat(work["expires_at"].replace("Z", "+00:00"))
                if occurred_at >= expiry:
                    raise StateEventError("lifecycle event occurs after Experiment expiry")
        allowed_event_versions = {
            "context.typed-state/v2alpha1": {
                EVENT_SCHEMA_VERSION,
                EVENT_SCHEMA_VERSION_V3,
            },
            "context.typed-state/v3alpha1": {
                EVENT_SCHEMA_VERSION,
                EVENT_SCHEMA_VERSION_V3,
                EVENT_SCHEMA_VERSION_V4,
                IDEA_EVENT_SCHEMA_VERSION,
            },
            "context.typed-state/v4alpha1": {
                EVENT_SCHEMA_VERSION_V4,
                IDEA_EVENT_SCHEMA_VERSION_V2,
            },
        }.get(state["schema_version"], {LEGACY_EVENT_SCHEMA_VERSION})
        if event["schema_version"] not in allowed_event_versions:
            raise StateEventError("event wire version does not match typed state")
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
        if supersedes_event_id is not None:
            if supersedes_event_id in superseded_event_ids:
                raise StateEventError("correction lineage fork: target already superseded")
            target_event = prior_event_by_id.get(supersedes_event_id)
            if target_event is None:
                raise StateEventError(
                    "correction requires the superseded prior Event envelope"
                )
            target_keys = {
                (change["collection"], change["object_id"])
                for change in target_event["changes"]
            }
            changed_keys = {
                (change["collection"], change["object_id"])
                for change in event["changes"]
            }
            if not target_keys.intersection(changed_keys):
                raise StateEventError(
                    "correction changes must intersect superseded event changed keys"
                )
            superseded_event_ids.add(supersedes_event_id)

        for change in event["changes"]:
            _put_change(state, change)
        state["project"] = copy.deepcopy(event["project_after"])
        try:
            validate_typed_state(state)
        except TypedStateError as exc:
            raise StateEventError("event creates an invalid typed state graph") from exc

        seen_event_ids.add(event_id)
        prior_event_by_id[event_id] = copy.deepcopy(event)
        previous_event_sha256 = event["event_sha256"]
        expected_revision = event["revision_after"]
        expected_sequence += 1

    return state


def _validate_idea_replay_gate(
    state: dict[str, Any],
    *,
    event: dict[str, Any],
    occurred_at: datetime,
) -> None:
    """Keep an Idea Event within candidate-only execution authority."""
    transition = event["idea_transition"]
    assert isinstance(transition, dict)
    project = state["project"]
    active_work_id = project["primary_work_id"]
    if (
        transition["parent_work_id"] != active_work_id
        or transition["return_work_id"] != active_work_id
    ):
        raise StateEventError("Idea event parent or return point does not match active Work")
    idea_change = next(
        change
        for change in event["changes"]
        if change["collection"] == "ideas"
        and change["object_id"] == transition["idea_id"]
    )
    idea = idea_change["value"]
    if any(item["idea_id"] == transition["idea_id"] for item in state["ideas"]):
        raise StateEventError("Idea capture cannot replace an existing Idea")
    if not isinstance(idea.get("source_ref"), str) or not _OPAQUE_RANGE_REF_RE.fullmatch(
        idea["source_ref"]
    ):
        raise StateEventError("Idea event source_ref is not an opaque range")
    if (
        not isinstance(idea.get("summary"), str)
        or not idea["summary"].strip()
        or len(idea["summary"]) > 2000
    ):
        raise StateEventError("Idea event summary is not bounded")
    expiry = idea.get("expiry")
    if expiry is not None:
        try:
            expires_at = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise StateEventError("Idea event expiry is invalid") from exc
        if expires_at.tzinfo is None or expires_at <= occurred_at:
            raise StateEventError("Idea event expiry is not in the future")
    project_after = event["project_after"]
    immutable_project_fields = set(project) - {"revision", "updated_at"}
    if (
        any(project_after[field] != project[field] for field in immutable_project_fields)
        or project_after["updated_at"] != event["occurred_at"]
    ):
        raise StateEventError("Idea event cannot change active execution authority")
    changed_collections = {change["collection"] for change in event["changes"]}
    if changed_collections - {"ideas", "claims"}:
        raise StateEventError("Idea event can change only Idea and claim revision")
    previous_claims = {item["claim_id"]: item for item in state["claims"]}
    active_claims = [
        claim
        for claim in previous_claims.values()
        if claim["work_id"] == active_work_id and claim["status"] == "active"
    ]
    if len(active_claims) != 1 or active_claims[0]["actor_ref"] != event["actor_ref"]:
        raise StateEventError("Idea event actor does not own the active claim")
    target_id = transition["switch_target_work_id"]
    if target_id is not None:
        target = next(
            (work for work in state["works"] if work["work_id"] == target_id), None
        )
        if target is None or target["status"] != "ready" or target_id == active_work_id:
            raise StateEventError("Idea switch proposal target is invalid")
    for change in event["changes"]:
        if change["collection"] != "claims":
            continue
        previous = previous_claims.get(change["object_id"])
        current = change["value"]
        if previous is None:
            raise StateEventError("Idea event cannot add a claim")
        if any(
            current[field] != previous[field]
            for field in current
            if field != "expected_project_revision"
        ) or current["expected_project_revision"] != event["revision_after"]:
            raise StateEventError("Idea event cannot change claim authority")


def _validate_idea_review_replay_gate(
    state: dict[str, Any],
    *,
    event: dict[str, Any],
    occurred_at: datetime,
) -> None:
    """Keep v2 Idea events candidate-only while allowing bounded review facts."""
    transition = event["idea_transition"]
    assert isinstance(transition, dict)
    project = state["project"]
    immutable_project_fields = set(project) - {"revision", "updated_at"}
    project_after = event["project_after"]
    if (
        any(project_after[field] != project[field] for field in immutable_project_fields)
        or project_after["updated_at"] != event["occurred_at"]
    ):
        raise StateEventError("Idea v2 event cannot change execution authority")
    allowed_collections = {
        "ideas",
        "claims",
        "idea_relationships",
        "idea_occurrences",
        "idea_reviews",
        "correction_protections",
        "effects",
    }
    changed_collections = {change["collection"] for change in event["changes"]}
    if changed_collections - allowed_collections:
        raise StateEventError("Idea v2 event changes an unauthorized collection")

    active_work_id = project["primary_work_id"]
    active_claims = [
        claim
        for claim in state["claims"]
        if claim["work_id"] == active_work_id and claim["status"] == "active"
    ]
    operation = transition["operation"]
    if len(active_claims) != 1:
        raise StateEventError("Idea v2 event requires one active claim")
    if (
        operation in {"capture-created", "capture-merged"}
        and active_claims[0]["actor_ref"] != event["actor_ref"]
    ):
        raise StateEventError("Idea v2 capture actor does not own the active claim")
    previous_claims = {claim["claim_id"]: claim for claim in state["claims"]}
    for change in event["changes"]:
        if change["collection"] != "claims":
            continue
        previous = previous_claims.get(change["object_id"])
        current = change["value"]
        if previous is None or any(
            current[field] != previous[field]
            for field in current
            if field != "expected_project_revision"
        ) or current["expected_project_revision"] != event["revision_after"]:
            raise StateEventError("Idea v2 event cannot change claim authority")

    previous_effects = {effect["effect_id"]: effect for effect in state["effects"]}
    for change in event["changes"]:
        if change["collection"] != "effects":
            continue
        previous = previous_effects.get(change["object_id"])
        current = change["value"]
        if (
            previous is None
            or previous["status"] not in {"authorized", "started"}
            or any(
                current[field] != previous[field]
                for field in current
                if field != "expected_project_revision"
            )
            or current["expected_project_revision"] != event["revision_after"]
        ):
            raise StateEventError("Idea v2 event cannot change effect authority")

    canonical_id = transition["canonical_idea_id"]
    operation_changes = {
        (change["collection"], change["object_id"])
        for change in event["changes"]
        if change["collection"] not in {"claims", "effects"}
    }
    expected_delta = {
        "capture-created": {
            ("ideas", canonical_id),
            ("idea_occurrences", transition["occurrence_id"]),
        },
        "capture-merged": {
            ("idea_occurrences", transition["occurrence_id"]),
        },
        "review-updated": {
            ("ideas", canonical_id),
            ("idea_reviews", transition["review_id"]),
        },
        "correction-guarded": {
            ("correction_protections", transition["protection_id"]),
        },
        "correction-released": {
            ("correction_protections", transition["protection_id"]),
        },
    }[operation]
    if operation_changes != expected_delta:
        raise StateEventError("Idea v2 operation delta is not exact")
    existing_ideas = {idea["idea_id"]: idea for idea in state["ideas"]}
    occurrence = next(
        (
            change["value"]
            for change in event["changes"]
            if change["collection"] == "idea_occurrences"
            and change["object_id"] == transition["occurrence_id"]
        ),
        None,
    )
    if operation in {"capture-created", "capture-merged"}:
        if not isinstance(occurrence, dict):
            raise StateEventError("Idea v2 capture requires an occurrence")
        if (
            occurrence.get("idea_id") != canonical_id
            or occurrence.get("submitted_idea_id") != transition["submitted_idea_id"]
            or occurrence.get("origin") != "capture"
            or occurrence.get("request_sha256") != transition["request_sha256"]
            or not isinstance(occurrence.get("source_ref"), str)
            or not _OPAQUE_RANGE_REF_RE.fullmatch(occurrence["source_ref"])
        ):
            raise StateEventError("Idea v2 capture occurrence is invalid")
        if operation == "capture-created":
            idea = next(
                (
                    change["value"]
                    for change in event["changes"]
                    if change["collection"] == "ideas" and change["object_id"] == canonical_id
                ),
                None,
            )
            if canonical_id in existing_ideas or not isinstance(idea, dict):
                raise StateEventError("Idea v2 capture-created canonical Idea is invalid")
            if (
                idea.get("parent_work_id") != active_work_id
                or idea.get("return_work_id") != active_work_id
                or idea.get("dedupe_key") != occurrence.get("dedupe_key")
            ):
                raise StateEventError("Idea v2 capture-created binding is invalid")
        elif canonical_id not in existing_ideas:
            raise StateEventError("Idea v2 capture-merged canonical Idea is unknown")
        elif occurrence.get("dedupe_key") != existing_ideas[canonical_id].get("dedupe_key"):
            raise StateEventError("Idea v2 capture-merged dedupe key is invalid")
    elif operation == "review-updated":
        previous_idea = existing_ideas.get(canonical_id)
        if previous_idea is None:
            raise StateEventError("Idea v2 review canonical Idea is unknown")
        if previous_idea["status"] in {"rejected", "superseded", "expired"}:
            raise StateEventError("Idea v2 review cannot revive a terminal Idea")
        idea = next(
            change["value"]
            for change in event["changes"]
            if change["collection"] == "ideas"
        )
        review = next(
            change["value"]
            for change in event["changes"]
            if change["collection"] == "idea_reviews"
        )
        allowed_idea_fields = {"status", "urgency", "review_at", "revision"}
        if any(
            idea[field] != previous_idea[field]
            for field in previous_idea
            if field not in allowed_idea_fields
        ) or idea["revision"] != previous_idea["revision"] + 1:
            raise StateEventError("Idea v2 review delta is invalid")
        expected_status = {
            "keep": "candidate",
            "park": "parked",
            "reject": "rejected",
            "supersede": "superseded",
            "approve": "approved",
        }.get(review.get("decision"))
        if (
            review.get("idea_id") != canonical_id
            or review.get("reviewer_ref") != event["actor_ref"]
            or idea["status"] != expected_status
            or idea["urgency"] != review.get("urgency")
            or idea["review_at"] != review.get("review_at")
            or review.get("reviewed_at") != event["occurred_at"]
        ):
            raise StateEventError("Idea v2 review delta is invalid")
    elif operation == "correction-guarded":
        if canonical_id not in existing_ideas:
            raise StateEventError("Idea v2 correction canonical Idea is unknown")
        protections = {
            item["protection_id"]: item for item in state["correction_protections"]
        }
        protection = next(
            change["value"]
            for change in event["changes"]
            if change["collection"] == "correction_protections"
        )
        if (
            transition["protection_id"] in protections
            or protection.get("idea_id") != canonical_id
            or protection.get("status") != "active"
            or protection.get("opened_by_ref") != event["actor_ref"]
            or protection.get("opened_at") != event["occurred_at"]
            or protection.get("released_by_ref") is not None
            or protection.get("release_reason") is not None
            or protection.get("release_evidence_ids") != []
            or protection.get("released_at") is not None
        ):
            raise StateEventError("Idea v2 correction guard delta is invalid")
    else:
        if canonical_id not in existing_ideas:
            raise StateEventError("Idea v2 correction canonical Idea is unknown")
        protections = {
            item["protection_id"]: item for item in state["correction_protections"]
        }
        previous = protections.get(transition["protection_id"])
        released = next(
            change["value"]
            for change in event["changes"]
            if change["collection"] == "correction_protections"
        )
        immutable_fields = set(previous or ()) - {
            "status",
            "released_by_ref",
            "release_reason",
            "release_evidence_ids",
            "released_at",
        }
        evidence_by_id = {item["evidence_id"]: item for item in state["evidence"]}
        release_evidence_ids = released.get("release_evidence_ids")
        if (
            previous is None
            or previous["idea_id"] != canonical_id
            or previous["status"] != "active"
            or any(released[field] != previous[field] for field in immutable_fields)
            or released.get("status") != "released"
            or released.get("released_by_ref") != event["actor_ref"]
            or not isinstance(released.get("release_reason"), str)
            or not released["release_reason"].strip()
            or not isinstance(release_evidence_ids, list)
            or not release_evidence_ids
            or any(
                evidence_id not in evidence_by_id
                or evidence_by_id[evidence_id]["validity"] != "verified"
                or evidence_by_id[evidence_id]["verified_at"] is None
                for evidence_id in release_evidence_ids
            )
            or released.get("released_at") != event["occurred_at"]
        ):
            raise StateEventError("Idea v2 correction release delta is invalid")
