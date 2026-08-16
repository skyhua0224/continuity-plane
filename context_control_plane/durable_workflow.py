"""Durable workflow identity, history replay, patching, and rollover contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

WORKFLOW_RUN_SCHEMA_VERSION = "context.workflow-run/v1alpha1"
WORKFLOW_EVENT_SCHEMA_VERSION = "context.workflow-history-event/v1alpha1"
WORKFLOW_DEFINITION_SCHEMA_VERSION = "context.workflow-definition/v1alpha1"
WORKFLOW_REPLAY_SCHEMA_VERSION = "context.workflow-replay-receipt/v1alpha1"
WORKFLOW_ROLLOVER_SCHEMA_VERSION = "context.workflow-rollover-receipt/v1alpha1"
MAX_PENDING_INPUTS = 128
MAX_ACTIVE_EFFECTS = 128

_RUN_FIELDS = {
    "schema_version",
    "workflow_id",
    "chain_id",
    "run_id",
    "generation",
    "previous_run_id",
    "previous_run_event_sha256",
    "project_id",
    "root_work_id",
    "request_id",
    "workflow_type",
    "definition_version",
    "implementation_sha256",
    "authority",
    "checkpoint_ref",
    "checkpoint_sha256",
    "continuation_sha256",
    "active_patch_ids",
    "pending_inputs",
    "input_ack_count",
    "input_ack_sha256",
    "effects",
    "effect_settlement_count",
    "effect_settlement_sha256",
    "history",
    "phase",
    "created_at",
    "updated_at",
    "state_write_authority",
    "effect_dispatch_authority",
    "provider_native_authority",
    "run_sha256",
}
_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "sequence_no",
    "event_type",
    "workflow_id",
    "chain_id",
    "run_id",
    "generation",
    "occurred_at",
    "payload",
    "previous_event_sha256",
    "event_sha256",
}
_DEFINITION_FIELDS = {
    "schema_version",
    "workflow_type",
    "definition_version",
    "implementation_sha256",
    "supported_history_versions",
    "patches",
    "state_write_authority",
    "effect_dispatch_authority",
    "provider_native_authority",
    "manifest_sha256",
}
_PATCH_FIELDS = {
    "patch_id",
    "introduced_in_version",
    "deprecated_in_version",
    "semantic_sha256",
}
_INPUT_FIELDS = {"input_id", "input_sha256", "status", "last_event_sha256"}
_EFFECT_FIELDS = {
    "effect_id",
    "effect_key",
    "request_sha256",
    "status",
    "last_event_sha256",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_INPUT_TRANSITIONS = {None: {"received"}, "received": {"acknowledged"}}
_EFFECT_TRANSITIONS = {
    None: {"started"},
    "started": {"outcome-unknown", "settled"},
    "outcome-unknown": {"settled"},
}


class DurableWorkflowError(ValueError):
    """Raised when workflow history cannot be replayed safely."""


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
        raise DurableWorkflowError("workflow data must be canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _body(value: dict[str, Any], digest_field: str) -> dict[str, Any]:
    body = copy.deepcopy(value)
    body.pop(digest_field, None)
    return body


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DurableWorkflowError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise DurableWorkflowError(f"{field} must be a lowercase SHA-256")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise DurableWorkflowError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DurableWorkflowError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DurableWorkflowError(f"{field} requires a timezone")
    return value


def _instant(value: Any, field: str) -> datetime:
    return datetime.fromisoformat(_timestamp(value, field).replace("Z", "+00:00"))


def _positive(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise DurableWorkflowError(f"{field} must be a positive integer")
    return value


def _non_negative(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise DurableWorkflowError(f"{field} must be a non-negative integer")
    return value


def _authority(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"project_revision", "event_head"}:
        raise DurableWorkflowError("authority fields are invalid")
    _non_negative(value["project_revision"], "authority.project_revision")
    head = value["event_head"]
    if not isinstance(head, dict) or set(head) != {"sequence_no", "event_sha256"}:
        raise DurableWorkflowError("authority event head fields are invalid")
    _non_negative(head["sequence_no"], "authority.event_head.sequence_no")
    _sha256(head["event_sha256"], "authority.event_head.event_sha256")
    return copy.deepcopy(value)


def _stable_id(prefix: str, *parts: Any) -> str:
    return prefix + _digest(list(parts))[:32]


def _run_id(chain_id: str, generation: int, previous_run_id: str | None) -> str:
    return _stable_id("wfr_", chain_id, generation, previous_run_id)


def _watermark(previous: str, identity: str, content_sha256: str) -> str:
    return _digest(
        {
            "previous_sha256": previous,
            "identity": identity,
            "content_sha256": content_sha256,
        }
    )


def _validate_id_set(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or value != sorted(set(value))
        or any(
            not isinstance(item, str) or _ID_RE.fullmatch(item) is None
            for item in value
        )
    ):
        raise DurableWorkflowError(f"{field} is invalid")
    return copy.deepcopy(value)


def _validate_inputs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DurableWorkflowError("pending_inputs must be a list")
    if len(value) > MAX_PENDING_INPUTS:
        raise DurableWorkflowError("workflow pending input limit exceeded")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != _INPUT_FIELDS:
            raise DurableWorkflowError("pending input fields are invalid")
        input_id = _identifier(item["input_id"], "input_id")
        if input_id in seen:
            raise DurableWorkflowError("input identity is duplicated")
        seen.add(input_id)
        _sha256(item["input_sha256"], "input.input_sha256")
        if item["status"] != "received":
            raise DurableWorkflowError("pending input status is invalid")
        _sha256(item["last_event_sha256"], "input.last_event_sha256")
        normalized.append(copy.deepcopy(item))
    if [item["input_id"] for item in normalized] != sorted(seen):
        raise DurableWorkflowError("pending inputs must be sorted")
    return normalized


def _validate_effects(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise DurableWorkflowError("effects must be a list")
    if len(value) > MAX_ACTIVE_EFFECTS:
        raise DurableWorkflowError("workflow active effect limit exceeded")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for effect in value:
        if not isinstance(effect, dict) or set(effect) != _EFFECT_FIELDS:
            raise DurableWorkflowError("effect fields are invalid")
        effect_id = _identifier(effect["effect_id"], "effect_id")
        if effect_id in seen:
            raise DurableWorkflowError("effect identity is duplicated")
        seen.add(effect_id)
        _identifier(effect["effect_key"], "effect_key")
        _sha256(effect["request_sha256"], "effect.request_sha256")
        if effect["status"] not in {"started", "outcome-unknown"}:
            raise DurableWorkflowError("active effect status is invalid")
        _sha256(effect["last_event_sha256"], "effect.last_event_sha256")
        normalized.append(copy.deepcopy(effect))
    if [item["effect_id"] for item in normalized] != sorted(seen):
        raise DurableWorkflowError("effects must be sorted")
    return normalized


def _event(
    run: dict[str, Any], event_type: str, payload: dict[str, Any], occurred_at: str
) -> dict[str, Any]:
    _timestamp(occurred_at, "occurred_at")
    sequence_no = len(run.get("history", [])) + 1
    previous = run["history"][-1]["event_sha256"] if run.get("history") else None
    event = {
        "schema_version": WORKFLOW_EVENT_SCHEMA_VERSION,
        "event_id": _stable_id(
            "wfe_", run["run_id"], sequence_no, event_type, _digest(payload)
        ),
        "sequence_no": sequence_no,
        "event_type": event_type,
        "workflow_id": run["workflow_id"],
        "chain_id": run["chain_id"],
        "run_id": run["run_id"],
        "generation": run["generation"],
        "occurred_at": occurred_at,
        "payload": copy.deepcopy(payload),
        "previous_event_sha256": previous,
        "event_sha256": "",
    }
    event["event_sha256"] = _digest(_body(event, "event_sha256"))
    return event


def _validate_event(
    event: Any,
    *,
    run: dict[str, Any],
    expected_sequence: int,
    previous_event_sha256: str | None,
) -> dict[str, Any]:
    if not isinstance(event, dict) or set(event) != _EVENT_FIELDS:
        raise DurableWorkflowError("event fields are invalid")
    if event["schema_version"] != WORKFLOW_EVENT_SCHEMA_VERSION:
        raise DurableWorkflowError("event schema version is invalid")
    _identifier(event["event_id"], "event_id")
    if event["sequence_no"] != expected_sequence:
        raise DurableWorkflowError("event sequence is invalid")
    _identifier(event["event_type"], "event_type")
    for field in ("workflow_id", "chain_id", "run_id", "generation"):
        if event[field] != run[field]:
            raise DurableWorkflowError("event identity does not match run")
    _timestamp(event["occurred_at"], "event.occurred_at")
    if not isinstance(event["payload"], dict):
        raise DurableWorkflowError("event payload must be an object")
    if event["previous_event_sha256"] != previous_event_sha256:
        raise DurableWorkflowError("event history chain is invalid")
    expected_event_id = _stable_id(
        "wfe_",
        run["run_id"],
        event["sequence_no"],
        event["event_type"],
        _digest(event["payload"]),
    )
    if event["event_id"] != expected_event_id:
        raise DurableWorkflowError("workflow event identity is invalid")
    if event["event_sha256"] != _digest(_body(event, "event_sha256")):
        raise DurableWorkflowError("event hash mismatch")
    return copy.deepcopy(event)


def _rehash_run(run: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(run)
    normalized["run_sha256"] = _digest(_body(normalized, "run_sha256"))
    return normalized


def _append(
    run: dict[str, Any], event_type: str, payload: dict[str, Any], occurred_at: str
) -> dict[str, Any]:
    current = validate_workflow_run(run)
    if current["phase"] != "running":
        raise DurableWorkflowError("terminal workflow run cannot append history")
    if _instant(occurred_at, "occurred_at") < _instant(
        current["history"][-1]["occurred_at"], "previous occurred_at"
    ):
        raise DurableWorkflowError("workflow event time moved backwards")
    event = _event(current, event_type, payload, occurred_at)
    advanced = copy.deepcopy(current)
    advanced["history"].append(event)
    advanced["updated_at"] = occurred_at
    return _rehash_run(advanced)


def create_workflow_run(
    *,
    project_id: str,
    root_work_id: str,
    request_id: str,
    workflow_type: str,
    definition_version: int,
    implementation_sha256: str,
    authority: dict[str, Any],
    checkpoint_ref: str,
    checkpoint_sha256: str,
    continuation_sha256: str,
    started_at: str,
) -> dict[str, Any]:
    """Create a deterministic first run for one workflow execution chain."""
    for field, value in (
        ("project_id", project_id),
        ("root_work_id", root_work_id),
        ("request_id", request_id),
        ("workflow_type", workflow_type),
    ):
        _identifier(value, field)
    _positive(definition_version, "definition_version")
    _sha256(implementation_sha256, "implementation_sha256")
    validated_authority = _authority(authority)
    _identifier(checkpoint_ref, "checkpoint_ref")
    _sha256(checkpoint_sha256, "checkpoint_sha256")
    _sha256(continuation_sha256, "continuation_sha256")
    _timestamp(started_at, "started_at")
    workflow_id = _stable_id("wf_", project_id, root_work_id, workflow_type, request_id)
    chain_id = _stable_id("wfc_", workflow_id, request_id)
    run_id = _run_id(chain_id, 1, None)
    empty_watermark = _digest([])
    run = {
        "schema_version": WORKFLOW_RUN_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "chain_id": chain_id,
        "run_id": run_id,
        "generation": 1,
        "previous_run_id": None,
        "previous_run_event_sha256": None,
        "project_id": project_id,
        "root_work_id": root_work_id,
        "request_id": request_id,
        "workflow_type": workflow_type,
        "definition_version": definition_version,
        "implementation_sha256": implementation_sha256,
        "authority": validated_authority,
        "checkpoint_ref": checkpoint_ref,
        "checkpoint_sha256": checkpoint_sha256,
        "continuation_sha256": continuation_sha256,
        "active_patch_ids": [],
        "pending_inputs": [],
        "input_ack_count": 0,
        "input_ack_sha256": empty_watermark,
        "effects": [],
        "effect_settlement_count": 0,
        "effect_settlement_sha256": empty_watermark,
        "history": [],
        "phase": "running",
        "created_at": started_at,
        "updated_at": started_at,
        "state_write_authority": False,
        "effect_dispatch_authority": False,
        "provider_native_authority": False,
        "run_sha256": "",
    }
    started = _event(run, "started", _started_payload(run), started_at)
    run["history"] = [started]
    return validate_workflow_run(_rehash_run(run))


def _started_payload(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "definition_version": run["definition_version"],
        "implementation_sha256": run["implementation_sha256"],
        "checkpoint_ref": run["checkpoint_ref"],
        "checkpoint_sha256": run["checkpoint_sha256"],
        "continuation_sha256": run["continuation_sha256"],
        "active_patch_ids": copy.deepcopy(run["active_patch_ids"]),
        "pending_inputs": copy.deepcopy(run["pending_inputs"]),
        "input_ack_count": run["input_ack_count"],
        "input_ack_sha256": run["input_ack_sha256"],
        "effects": copy.deepcopy(run["effects"]),
        "effect_settlement_count": run["effect_settlement_count"],
        "effect_settlement_sha256": run["effect_settlement_sha256"],
        "previous_run_event_sha256": run["previous_run_event_sha256"],
    }


def validate_workflow_run(run: Any) -> dict[str, Any]:
    """Validate one bounded run and its append-only history."""
    if not isinstance(run, dict) or set(run) != _RUN_FIELDS:
        raise DurableWorkflowError("workflow run fields are invalid")
    if run["schema_version"] != WORKFLOW_RUN_SCHEMA_VERSION:
        raise DurableWorkflowError("workflow run schema version is invalid")
    for field in (
        "workflow_id",
        "chain_id",
        "run_id",
        "project_id",
        "root_work_id",
        "request_id",
        "workflow_type",
        "checkpoint_ref",
    ):
        _identifier(run[field], field)
    _positive(run["generation"], "generation")
    if run["previous_run_id"] is not None:
        _identifier(run["previous_run_id"], "previous_run_id")
    if run["previous_run_event_sha256"] is not None:
        _sha256(run["previous_run_event_sha256"], "previous_run_event_sha256")
    first_generation = run["generation"] == 1
    if first_generation != (
        run["previous_run_id"] is None and run["previous_run_event_sha256"] is None
    ):
        raise DurableWorkflowError("workflow run predecessor is invalid")
    expected_workflow_id = _stable_id(
        "wf_",
        run["project_id"],
        run["root_work_id"],
        run["workflow_type"],
        run["request_id"],
    )
    if run["workflow_id"] != expected_workflow_id:
        raise DurableWorkflowError("workflow identity is invalid")
    if run["chain_id"] != _stable_id("wfc_", run["workflow_id"], run["request_id"]):
        raise DurableWorkflowError("workflow chain identity is invalid")
    if run["run_id"] != _run_id(
        run["chain_id"], run["generation"], run["previous_run_id"]
    ):
        raise DurableWorkflowError("workflow run identity is invalid")
    _positive(run["definition_version"], "definition_version")
    _sha256(run["implementation_sha256"], "implementation_sha256")
    _authority(run["authority"])
    _sha256(run["checkpoint_sha256"], "checkpoint_sha256")
    _sha256(run["continuation_sha256"], "continuation_sha256")
    patches = _validate_id_set(run["active_patch_ids"], "active patches")
    pending_inputs = _validate_inputs(run["pending_inputs"])
    input_ack_count = _non_negative(run["input_ack_count"], "input_ack_count")
    input_ack_sha256 = _sha256(run["input_ack_sha256"], "input_ack_sha256")
    effects = _validate_effects(run["effects"])
    effect_settlement_count = _non_negative(
        run["effect_settlement_count"], "effect_settlement_count"
    )
    effect_settlement_sha256 = _sha256(
        run["effect_settlement_sha256"], "effect_settlement_sha256"
    )
    if not isinstance(run["history"], list) or not run["history"]:
        raise DurableWorkflowError("workflow history is empty")
    history: list[dict[str, Any]] = []
    previous_hash: str | None = None
    previous_time: datetime | None = None
    for sequence, event in enumerate(run["history"], start=1):
        validated = _validate_event(
            event,
            run=run,
            expected_sequence=sequence,
            previous_event_sha256=previous_hash,
        )
        current_time = _instant(validated["occurred_at"], "event.occurred_at")
        if previous_time is not None and current_time < previous_time:
            raise DurableWorkflowError("workflow event time moved backwards")
        history.append(validated)
        previous_hash = validated["event_sha256"]
        previous_time = current_time
    if history[0]["event_type"] != "started":
        raise DurableWorkflowError("workflow run must start with a started event")
    started = history[0]["payload"]
    if not isinstance(started, dict) or set(started) != set(_started_payload(run)):
        raise DurableWorkflowError("started event payload is invalid")
    for field in (
        "definition_version",
        "implementation_sha256",
        "checkpoint_ref",
        "checkpoint_sha256",
        "continuation_sha256",
        "previous_run_event_sha256",
    ):
        if started[field] != run[field]:
            raise DurableWorkflowError("started event does not match run")
    active_patches = _validate_id_set(started["active_patch_ids"], "started patches")
    started_inputs = _validate_inputs(started["pending_inputs"])
    started_input_count = _non_negative(
        started["input_ack_count"], "started input_ack_count"
    )
    started_input_sha = _sha256(started["input_ack_sha256"], "started input_ack_sha256")
    started_effects = _validate_effects(started["effects"])
    started_effect_count = _non_negative(
        started["effect_settlement_count"], "started effect_settlement_count"
    )
    started_effect_sha = _sha256(
        started["effect_settlement_sha256"], "started effect_settlement_sha256"
    )
    if first_generation:
        empty_watermark = _digest([])
        if (
            active_patches
            or started_inputs
            or started_effects
            or started_input_count != 0
            or started_effect_count != 0
            or started_input_sha != empty_watermark
            or started_effect_sha != empty_watermark
        ):
            raise DurableWorkflowError(
                "first workflow run must start with empty projections"
            )
    current_inputs = {item["input_id"]: copy.deepcopy(item) for item in started_inputs}
    current_input_count = started_input_count
    current_input_sha = started_input_sha
    current_effects = {
        item["effect_id"]: copy.deepcopy(item) for item in started_effects
    }
    current_effect_count = started_effect_count
    current_effect_sha = started_effect_sha
    for event in history[1:]:
        payload = event["payload"]
        event_type = event["event_type"]
        if event_type == "patch-marker":
            required = {"patch_id", "semantic_sha256", "activated"}
            if set(payload) != required or payload["activated"] is not True:
                raise DurableWorkflowError("patch marker payload is invalid")
            patch_id = _identifier(payload["patch_id"], "patch_id")
            _sha256(payload["semantic_sha256"], "patch.semantic_sha256")
            if patch_id in active_patches:
                raise DurableWorkflowError("patch marker is duplicated")
            active_patches.append(patch_id)
            active_patches.sort()
        elif event_type == "input-status":
            required = {"input_id", "input_sha256", "status"}
            if set(payload) != required:
                raise DurableWorkflowError("input event payload is invalid")
            input_id = _identifier(payload["input_id"], "input_id")
            input_sha = _sha256(payload["input_sha256"], "input_sha256")
            prior = current_inputs.get(input_id)
            prior_status = prior["status"] if prior else None
            if payload["status"] not in _INPUT_TRANSITIONS.get(prior_status, set()):
                raise DurableWorkflowError("input status transition is invalid")
            if prior and prior["input_sha256"] != input_sha:
                raise DurableWorkflowError("input identity changed")
            if payload["status"] == "received":
                current_inputs[input_id] = {
                    **copy.deepcopy(payload),
                    "last_event_sha256": event["event_sha256"],
                }
            else:
                current_inputs.pop(input_id)
                current_input_count += 1
                current_input_sha = _watermark(current_input_sha, input_id, input_sha)
        elif event_type == "effect-status":
            required = {"effect_id", "effect_key", "request_sha256", "status"}
            if set(payload) != required:
                raise DurableWorkflowError("effect event payload is invalid")
            effect_id = _identifier(payload["effect_id"], "effect_id")
            effect_key = _identifier(payload["effect_key"], "effect_key")
            request_sha = _sha256(payload["request_sha256"], "request_sha256")
            prior = current_effects.get(effect_id)
            prior_status = prior["status"] if prior else None
            if payload["status"] not in _EFFECT_TRANSITIONS.get(prior_status, set()):
                raise DurableWorkflowError("effect status transition is invalid")
            if prior and (
                prior["effect_key"] != effect_key
                or prior["request_sha256"] != request_sha
            ):
                raise DurableWorkflowError("effect identity changed")
            if payload["status"] == "settled":
                current_effects.pop(effect_id)
                current_effect_count += 1
                current_effect_sha = _watermark(
                    current_effect_sha, effect_id, request_sha
                )
            else:
                current_effects[effect_id] = {
                    **copy.deepcopy(payload),
                    "last_event_sha256": event["event_sha256"],
                }
        elif event_type == "step-committed":
            required = {
                "step_id",
                "input_sha256",
                "result_sha256",
                "active_patch_ids",
            }
            if set(payload) != required:
                raise DurableWorkflowError("step event payload is invalid")
            _identifier(payload["step_id"], "step_id")
            _sha256(payload["input_sha256"], "step.input_sha256")
            _sha256(payload["result_sha256"], "step.result_sha256")
            if payload["active_patch_ids"] != active_patches:
                raise DurableWorkflowError("step patch binding is invalid")
        elif event_type == "continued-as-new":
            if event is not history[-1]:
                raise DurableWorkflowError("continued event must close the run")
            _validate_continuation_payload(
                payload,
                active_patch_ids=active_patches,
                pending_inputs=current_inputs,
                input_ack_count=current_input_count,
                input_ack_sha256=current_input_sha,
                effects=current_effects,
                effect_settlement_count=current_effect_count,
                effect_settlement_sha256=current_effect_sha,
            )
            expected_next_generation = run["generation"] + 1
            expected_next_run_id = _run_id(
                run["chain_id"], expected_next_generation, run["run_id"]
            )
            if (
                payload["next_generation"] != expected_next_generation
                or payload["next_run_id"] != expected_next_run_id
            ):
                raise DurableWorkflowError("continued event target identity is invalid")
            if (
                payload["authority"]["project_revision"]
                < run["authority"]["project_revision"]
            ):
                raise DurableWorkflowError(
                    "continued authority revision moved backwards"
                )
        else:
            raise DurableWorkflowError("workflow event type is unsupported")
    if active_patches != patches:
        raise DurableWorkflowError("active patch projection mismatch")
    if [current_inputs[key] for key in sorted(current_inputs)] != pending_inputs:
        raise DurableWorkflowError("pending input projection mismatch")
    if current_input_count != input_ack_count or current_input_sha != input_ack_sha256:
        raise DurableWorkflowError("input acknowledgement projection mismatch")
    if [current_effects[key] for key in sorted(current_effects)] != effects:
        raise DurableWorkflowError("effect projection mismatch")
    if (
        current_effect_count != effect_settlement_count
        or current_effect_sha != effect_settlement_sha256
    ):
        raise DurableWorkflowError("effect settlement projection mismatch")
    if run["phase"] not in {"running", "continued-as-new"}:
        raise DurableWorkflowError("workflow phase is invalid")
    if (run["phase"] == "continued-as-new") != (
        history[-1]["event_type"] == "continued-as-new"
    ):
        raise DurableWorkflowError("workflow phase does not match history")
    created = _instant(run["created_at"], "created_at")
    updated = _instant(run["updated_at"], "updated_at")
    if created != _instant(history[0]["occurred_at"], "started occurred_at"):
        raise DurableWorkflowError("workflow created_at does not match history")
    if updated != _instant(history[-1]["occurred_at"], "updated occurred_at"):
        raise DurableWorkflowError("workflow updated_at does not match history")
    for field in (
        "state_write_authority",
        "effect_dispatch_authority",
        "provider_native_authority",
    ):
        if run[field] is not False:
            raise DurableWorkflowError("workflow cannot claim execution authority")
    _sha256(run["run_sha256"], "run_sha256")
    if run["run_sha256"] != _digest(_body(run, "run_sha256")):
        raise DurableWorkflowError("run hash mismatch")
    return copy.deepcopy(run)


def _validate_continuation_payload(
    payload: Any,
    *,
    active_patch_ids: list[str],
    pending_inputs: dict[str, dict[str, Any]],
    input_ack_count: int,
    input_ack_sha256: str,
    effects: dict[str, dict[str, Any]],
    effect_settlement_count: int,
    effect_settlement_sha256: str,
) -> None:
    required = {
        "next_run_id",
        "next_generation",
        "authority",
        "checkpoint_ref",
        "checkpoint_sha256",
        "continuation_sha256",
        "active_patch_ids",
        "pending_inputs",
        "input_ack_count",
        "input_ack_sha256",
        "effects",
        "effect_settlement_count",
        "effect_settlement_sha256",
    }
    if not isinstance(payload, dict) or set(payload) != required:
        raise DurableWorkflowError("continued event payload is invalid")
    _identifier(payload["next_run_id"], "next_run_id")
    _positive(payload["next_generation"], "next_generation")
    _authority(payload["authority"])
    _identifier(payload["checkpoint_ref"], "checkpoint_ref")
    _sha256(payload["checkpoint_sha256"], "checkpoint_sha256")
    _sha256(payload["continuation_sha256"], "continuation_sha256")
    expected = {
        "active_patch_ids": active_patch_ids,
        "pending_inputs": [pending_inputs[key] for key in sorted(pending_inputs)],
        "input_ack_count": input_ack_count,
        "input_ack_sha256": input_ack_sha256,
        "effects": [effects[key] for key in sorted(effects)],
        "effect_settlement_count": effect_settlement_count,
        "effect_settlement_sha256": effect_settlement_sha256,
    }
    for field, value in expected.items():
        if payload[field] != value:
            raise DurableWorkflowError("continued event projection mismatch")


def activate_workflow_patch(
    run: dict[str, Any],
    *,
    definition: dict[str, Any],
    patch_id: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Record one version-compatible patch decision before patched commands execute."""
    _identifier(patch_id, "patch_id")
    current = validate_workflow_run(run)
    current_definition = _validate_definition(definition)
    if current["workflow_type"] != current_definition["workflow_type"]:
        raise DurableWorkflowError("workflow definition type mismatch")
    patch = next(
        (
            item
            for item in current_definition["patches"]
            if item["patch_id"] == patch_id
        ),
        None,
    )
    if patch is None:
        raise DurableWorkflowError("workflow patch is unknown")
    _require_patch_version(patch, current["definition_version"])
    if patch_id in current["active_patch_ids"]:
        raise DurableWorkflowError("patch marker is duplicated")
    advanced = _append(
        current,
        "patch-marker",
        {
            "patch_id": patch_id,
            "semantic_sha256": patch["semantic_sha256"],
            "activated": True,
        },
        occurred_at,
    )
    advanced["active_patch_ids"] = sorted([*advanced["active_patch_ids"], patch_id])
    return validate_workflow_run(_rehash_run(advanced))


def append_workflow_step(
    run: dict[str, Any],
    *,
    step_id: str,
    input_sha256: str,
    result_sha256: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Append one deterministic command result to workflow history."""
    _identifier(step_id, "step_id")
    _sha256(input_sha256, "input_sha256")
    _sha256(result_sha256, "result_sha256")
    current = validate_workflow_run(run)
    return validate_workflow_run(
        _append(
            current,
            "step-committed",
            {
                "step_id": step_id,
                "input_sha256": input_sha256,
                "result_sha256": result_sha256,
                "active_patch_ids": copy.deepcopy(current["active_patch_ids"]),
            },
            occurred_at,
        )
    )


def record_workflow_input(
    run: dict[str, Any],
    *,
    input_id: str,
    input_sha256: str,
    status: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Record input receipt and acknowledgement with a bounded carry watermark."""
    _identifier(input_id, "input_id")
    _sha256(input_sha256, "input_sha256")
    current = validate_workflow_run(run)
    prior = next(
        (item for item in current["pending_inputs"] if item["input_id"] == input_id),
        None,
    )
    prior_status = prior["status"] if prior else None
    if status not in _INPUT_TRANSITIONS.get(prior_status, set()):
        raise DurableWorkflowError("input status transition is invalid")
    if prior and prior["input_sha256"] != input_sha256:
        raise DurableWorkflowError("input identity changed")
    advanced = _append(
        current,
        "input-status",
        {"input_id": input_id, "input_sha256": input_sha256, "status": status},
        occurred_at,
    )
    projected = {
        item["input_id"]: copy.deepcopy(item) for item in advanced["pending_inputs"]
    }
    if status == "received":
        projected[input_id] = {
            "input_id": input_id,
            "input_sha256": input_sha256,
            "status": status,
            "last_event_sha256": advanced["history"][-1]["event_sha256"],
        }
    else:
        projected.pop(input_id)
        advanced["input_ack_count"] += 1
        advanced["input_ack_sha256"] = _watermark(
            advanced["input_ack_sha256"], input_id, input_sha256
        )
    advanced["pending_inputs"] = [projected[key] for key in sorted(projected)]
    return validate_workflow_run(_rehash_run(advanced))


def record_workflow_effect(
    run: dict[str, Any],
    *,
    effect_id: str,
    effect_key: str,
    request_sha256: str,
    status: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Record orchestration evidence without granting effect dispatch authority."""
    _identifier(effect_id, "effect_id")
    _identifier(effect_key, "effect_key")
    _sha256(request_sha256, "request_sha256")
    current = validate_workflow_run(run)
    prior = next(
        (item for item in current["effects"] if item["effect_id"] == effect_id), None
    )
    prior_status = prior["status"] if prior else None
    if status not in _EFFECT_TRANSITIONS.get(prior_status, set()):
        raise DurableWorkflowError("effect status transition is invalid")
    if prior and (
        prior["effect_key"] != effect_key or prior["request_sha256"] != request_sha256
    ):
        raise DurableWorkflowError("effect identity changed")
    advanced = _append(
        current,
        "effect-status",
        {
            "effect_id": effect_id,
            "effect_key": effect_key,
            "request_sha256": request_sha256,
            "status": status,
        },
        occurred_at,
    )
    projected = {item["effect_id"]: copy.deepcopy(item) for item in advanced["effects"]}
    if status == "settled":
        projected.pop(effect_id)
        advanced["effect_settlement_count"] += 1
        advanced["effect_settlement_sha256"] = _watermark(
            advanced["effect_settlement_sha256"], effect_id, request_sha256
        )
    else:
        projected[effect_id] = {
            "effect_id": effect_id,
            "effect_key": effect_key,
            "request_sha256": request_sha256,
            "status": status,
            "last_event_sha256": advanced["history"][-1]["event_sha256"],
        }
    advanced["effects"] = [projected[key] for key in sorted(projected)]
    return validate_workflow_run(_rehash_run(advanced))


def build_workflow_definition(
    *,
    workflow_type: str,
    definition_version: int,
    implementation_sha256: str,
    supported_history_versions: list[int],
    patches: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build an immutable replay and patch compatibility manifest."""
    _identifier(workflow_type, "workflow_type")
    _positive(definition_version, "definition_version")
    _sha256(implementation_sha256, "implementation_sha256")
    if (
        not isinstance(supported_history_versions, list)
        or supported_history_versions != sorted(set(supported_history_versions))
        or any(
            type(value) is not int or value <= 0 for value in supported_history_versions
        )
        or definition_version not in supported_history_versions
    ):
        raise DurableWorkflowError("supported history versions are invalid")
    normalized_patches: list[dict[str, Any]] = []
    for patch in patches:
        if not isinstance(patch, dict) or set(patch) != _PATCH_FIELDS:
            raise DurableWorkflowError("patch definition fields are invalid")
        patch_id = _identifier(patch["patch_id"], "patch_id")
        introduced = _positive(patch["introduced_in_version"], "introduced_in_version")
        deprecated = patch["deprecated_in_version"]
        if deprecated is not None and (
            type(deprecated) is not int or deprecated <= introduced
        ):
            raise DurableWorkflowError("patch deprecation version is invalid")
        semantic_sha256 = _sha256(patch["semantic_sha256"], "semantic_sha256")
        normalized_patches.append(
            {
                "patch_id": patch_id,
                "introduced_in_version": introduced,
                "deprecated_in_version": deprecated,
                "semantic_sha256": semantic_sha256,
            }
        )
    normalized_patches.sort(key=lambda item: item["patch_id"])
    if len({item["patch_id"] for item in normalized_patches}) != len(
        normalized_patches
    ):
        raise DurableWorkflowError("patch identity is duplicated")
    manifest = {
        "schema_version": WORKFLOW_DEFINITION_SCHEMA_VERSION,
        "workflow_type": workflow_type,
        "definition_version": definition_version,
        "implementation_sha256": implementation_sha256,
        "supported_history_versions": copy.deepcopy(supported_history_versions),
        "patches": normalized_patches,
        "state_write_authority": False,
        "effect_dispatch_authority": False,
        "provider_native_authority": False,
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = _digest(_body(manifest, "manifest_sha256"))
    return copy.deepcopy(manifest)


def _validate_definition(definition: Any) -> dict[str, Any]:
    if not isinstance(definition, dict) or set(definition) != _DEFINITION_FIELDS:
        raise DurableWorkflowError("workflow definition fields are invalid")
    for field in (
        "state_write_authority",
        "effect_dispatch_authority",
        "provider_native_authority",
    ):
        if definition[field] is not False:
            raise DurableWorkflowError("workflow definition cannot claim authority")
    rebuilt = build_workflow_definition(
        workflow_type=definition["workflow_type"],
        definition_version=definition["definition_version"],
        implementation_sha256=definition["implementation_sha256"],
        supported_history_versions=definition["supported_history_versions"],
        patches=definition["patches"],
    )
    if rebuilt != definition:
        raise DurableWorkflowError("workflow definition hash mismatch")
    return copy.deepcopy(definition)


def validate_workflow_definition(definition: Any) -> dict[str, Any]:
    """Validate an immutable replay and patch compatibility manifest."""
    return _validate_definition(definition)


def _require_patch_version(patch: dict[str, Any], definition_version: int) -> None:
    if definition_version < patch["introduced_in_version"]:
        raise DurableWorkflowError(
            "workflow patch was activated before introduced version"
        )
    deprecated = patch["deprecated_in_version"]
    if deprecated is not None and definition_version >= deprecated:
        raise DurableWorkflowError(
            "workflow patch was activated after deprecated version"
        )


def continue_workflow_as_new(
    run: dict[str, Any],
    *,
    authority: dict[str, Any],
    checkpoint_ref: str,
    checkpoint_sha256: str,
    continuation_sha256: str,
    occurred_at: str,
) -> dict[str, Any]:
    """Close one quiescent run and start the next bounded generation."""
    current = validate_workflow_run(run)
    if current["pending_inputs"]:
        raise DurableWorkflowError("input is not acknowledged at rollover")
    if current["effects"]:
        raise DurableWorkflowError("effect is not settled at rollover")
    next_authority = _authority(authority)
    if next_authority["project_revision"] < current["authority"]["project_revision"]:
        raise DurableWorkflowError("rollover authority revision moved backwards")
    _identifier(checkpoint_ref, "checkpoint_ref")
    _sha256(checkpoint_sha256, "checkpoint_sha256")
    _sha256(continuation_sha256, "continuation_sha256")
    next_generation = current["generation"] + 1
    next_run_id = _run_id(current["chain_id"], next_generation, current["run_id"])
    continuation_payload = {
        "next_run_id": next_run_id,
        "next_generation": next_generation,
        "authority": next_authority,
        "checkpoint_ref": checkpoint_ref,
        "checkpoint_sha256": checkpoint_sha256,
        "continuation_sha256": continuation_sha256,
        "active_patch_ids": copy.deepcopy(current["active_patch_ids"]),
        "pending_inputs": copy.deepcopy(current["pending_inputs"]),
        "input_ack_count": current["input_ack_count"],
        "input_ack_sha256": current["input_ack_sha256"],
        "effects": copy.deepcopy(current["effects"]),
        "effect_settlement_count": current["effect_settlement_count"],
        "effect_settlement_sha256": current["effect_settlement_sha256"],
    }
    closed = _append(current, "continued-as-new", continuation_payload, occurred_at)
    closed["phase"] = "continued-as-new"
    closed = validate_workflow_run(_rehash_run(closed))
    previous_event_sha256 = closed["history"][-1]["event_sha256"]
    next_run = {
        **copy.deepcopy(current),
        "run_id": next_run_id,
        "generation": next_generation,
        "previous_run_id": current["run_id"],
        "previous_run_event_sha256": previous_event_sha256,
        "authority": next_authority,
        "checkpoint_ref": checkpoint_ref,
        "checkpoint_sha256": checkpoint_sha256,
        "continuation_sha256": continuation_sha256,
        "history": [],
        "phase": "running",
        "created_at": occurred_at,
        "updated_at": occurred_at,
        "run_sha256": "",
    }
    next_run["history"] = [
        _event(next_run, "started", _started_payload(next_run), occurred_at)
    ]
    next_run = validate_workflow_run(_rehash_run(next_run))
    lost_inputs = len(current["pending_inputs"])
    effect_ids = [item["effect_id"] for item in current["effects"]]
    duplicate_effects = len(effect_ids) - len(set(effect_ids))
    receipt = {
        "schema_version": WORKFLOW_ROLLOVER_SCHEMA_VERSION,
        "workflow_id": current["workflow_id"],
        "chain_id": current["chain_id"],
        "from_run_id": current["run_id"],
        "to_run_id": next_run_id,
        "from_generation": current["generation"],
        "to_generation": next_generation,
        "continued_from_event_sha256": previous_event_sha256,
        "checkpoint_sha256": checkpoint_sha256,
        "continuation_sha256": continuation_sha256,
        "authority": next_authority,
        "carried_patch_ids": copy.deepcopy(current["active_patch_ids"]),
        "input_ack_count": current["input_ack_count"],
        "input_ack_sha256": current["input_ack_sha256"],
        "effect_settlement_count": current["effect_settlement_count"],
        "effect_settlement_sha256": current["effect_settlement_sha256"],
        "safe_point": lost_inputs == 0 and duplicate_effects == 0,
        "lost_inputs": lost_inputs,
        "duplicate_effects": duplicate_effects,
        "state_write_authority": False,
        "effect_dispatch_authority": False,
        "provider_native_authority": False,
        "rolled_over_at": occurred_at,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(_body(receipt, "receipt_sha256"))
    return {"closed_run": closed, "next_run": next_run, "receipt": receipt}


def validate_rollover_receipt(receipt: Any) -> dict[str, Any]:
    """Validate the safe-point proof consumed by a workflow backend."""
    fields = {
        "schema_version",
        "workflow_id",
        "chain_id",
        "from_run_id",
        "to_run_id",
        "from_generation",
        "to_generation",
        "continued_from_event_sha256",
        "checkpoint_sha256",
        "continuation_sha256",
        "authority",
        "carried_patch_ids",
        "input_ack_count",
        "input_ack_sha256",
        "effect_settlement_count",
        "effect_settlement_sha256",
        "safe_point",
        "lost_inputs",
        "duplicate_effects",
        "state_write_authority",
        "effect_dispatch_authority",
        "provider_native_authority",
        "rolled_over_at",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise DurableWorkflowError("rollover receipt fields are invalid")
    if receipt["schema_version"] != WORKFLOW_ROLLOVER_SCHEMA_VERSION:
        raise DurableWorkflowError("rollover receipt version is invalid")
    for field in ("workflow_id", "chain_id", "from_run_id", "to_run_id"):
        _identifier(receipt[field], field)
    _positive(receipt["from_generation"], "from_generation")
    if receipt["to_generation"] != receipt["from_generation"] + 1:
        raise DurableWorkflowError("rollover generation is invalid")
    for field in (
        "continued_from_event_sha256",
        "checkpoint_sha256",
        "continuation_sha256",
        "input_ack_sha256",
        "effect_settlement_sha256",
    ):
        _sha256(receipt[field], field)
    _authority(receipt["authority"])
    _validate_id_set(receipt["carried_patch_ids"], "carried_patch_ids")
    _non_negative(receipt["input_ack_count"], "input_ack_count")
    _non_negative(receipt["effect_settlement_count"], "effect_settlement_count")
    if receipt["safe_point"] is not True:
        raise DurableWorkflowError("rollover safe point is missing")
    if receipt["lost_inputs"] != 0 or receipt["duplicate_effects"] != 0:
        raise DurableWorkflowError("rollover loss metrics are non-zero")
    for field in (
        "state_write_authority",
        "effect_dispatch_authority",
        "provider_native_authority",
    ):
        if receipt[field] is not False:
            raise DurableWorkflowError("rollover receipt cannot claim authority")
    _timestamp(receipt["rolled_over_at"], "rolled_over_at")
    _sha256(receipt["receipt_sha256"], "receipt_sha256")
    if receipt["receipt_sha256"] != _digest(_body(receipt, "receipt_sha256")):
        raise DurableWorkflowError("rollover receipt hash mismatch")
    return copy.deepcopy(receipt)


def _validate_run_transition(previous: dict[str, Any], current: dict[str, Any]) -> None:
    if previous["phase"] != "continued-as-new":
        raise DurableWorkflowError("workflow chain predecessor is still open")
    if current["previous_run_id"] != previous["run_id"]:
        raise DurableWorkflowError("workflow predecessor run mismatch")
    previous_event = previous["history"][-1]
    if current["previous_run_event_sha256"] != previous_event["event_sha256"]:
        raise DurableWorkflowError("workflow predecessor event mismatch")
    if _instant(
        current["history"][0]["occurred_at"], "current generation occurred_at"
    ) < _instant(previous_event["occurred_at"], "previous generation occurred_at"):
        raise DurableWorkflowError("workflow generation time moved backwards")
    payload = previous_event["payload"]
    started = current["history"][0]["payload"]
    expected = {
        "next_run_id": current["run_id"],
        "next_generation": current["generation"],
        "authority": current["authority"],
        "checkpoint_ref": started["checkpoint_ref"],
        "checkpoint_sha256": started["checkpoint_sha256"],
        "continuation_sha256": started["continuation_sha256"],
        "active_patch_ids": started["active_patch_ids"],
        "pending_inputs": started["pending_inputs"],
        "input_ack_count": started["input_ack_count"],
        "input_ack_sha256": started["input_ack_sha256"],
        "effects": started["effects"],
        "effect_settlement_count": started["effect_settlement_count"],
        "effect_settlement_sha256": started["effect_settlement_sha256"],
    }
    if payload != expected:
        raise DurableWorkflowError("workflow continuation payload mismatch")


def validate_workflow_rollover(
    receipt: Any,
    *,
    closed_run: dict[str, Any],
    next_run: dict[str, Any],
) -> dict[str, Any]:
    """Validate a rollover receipt against both linked workflow generations."""
    current_receipt = validate_rollover_receipt(receipt)
    closed = validate_workflow_run(closed_run)
    successor = validate_workflow_run(next_run)
    for field in (
        "workflow_id",
        "chain_id",
        "project_id",
        "root_work_id",
        "request_id",
        "workflow_type",
    ):
        if successor[field] != closed[field]:
            raise DurableWorkflowError("workflow rollover identity changed")
    if successor["generation"] != closed["generation"] + 1:
        raise DurableWorkflowError("workflow rollover generation is not contiguous")
    if (
        closed["pending_inputs"]
        or closed["effects"]
        or successor["pending_inputs"]
        or successor["effects"]
    ):
        raise DurableWorkflowError("workflow rollover safe point is not quiescent")
    _validate_run_transition(closed, successor)
    continued_event = closed["history"][-1]
    expected = {
        "workflow_id": closed["workflow_id"],
        "chain_id": closed["chain_id"],
        "from_run_id": closed["run_id"],
        "to_run_id": successor["run_id"],
        "from_generation": closed["generation"],
        "to_generation": successor["generation"],
        "continued_from_event_sha256": continued_event["event_sha256"],
        "checkpoint_sha256": successor["checkpoint_sha256"],
        "continuation_sha256": successor["continuation_sha256"],
        "authority": successor["authority"],
        "carried_patch_ids": successor["active_patch_ids"],
        "input_ack_count": successor["input_ack_count"],
        "input_ack_sha256": successor["input_ack_sha256"],
        "effect_settlement_count": successor["effect_settlement_count"],
        "effect_settlement_sha256": successor["effect_settlement_sha256"],
        "rolled_over_at": continued_event["occurred_at"],
    }
    if any(current_receipt[field] != value for field, value in expected.items()):
        raise DurableWorkflowError("workflow rollover receipt does not match run chain")
    return current_receipt


def replay_workflow_chain(
    runs: list[dict[str, Any]],
    *,
    definition: dict[str, Any],
    step_resolver: Callable[[str, str, frozenset[str]], str],
) -> dict[str, Any]:
    """Replay a workflow chain without provider, State, or effect calls."""
    current_definition = _validate_definition(definition)
    if not isinstance(runs, list) or not runs:
        raise DurableWorkflowError("workflow replay requires at least one run")
    if not callable(step_resolver):
        raise TypeError("step_resolver must be callable")
    validated = [validate_workflow_run(run) for run in runs]
    first = validated[0]
    patches = {item["patch_id"]: item for item in current_definition["patches"]}
    command_count = 0
    for index, run in enumerate(validated):
        if run["workflow_type"] != current_definition["workflow_type"]:
            raise DurableWorkflowError("workflow definition type mismatch")
        if (
            run["definition_version"]
            not in current_definition["supported_history_versions"]
        ):
            raise DurableWorkflowError("history version is unsupported")
        for field in (
            "workflow_id",
            "chain_id",
            "project_id",
            "root_work_id",
            "request_id",
            "workflow_type",
        ):
            if run[field] != first[field]:
                raise DurableWorkflowError("workflow chain identity changed")
        if run["generation"] != index + 1:
            raise DurableWorkflowError("workflow generation is not contiguous")
        if index:
            _validate_run_transition(validated[index - 1], run)
        active = set(run["history"][0]["payload"]["active_patch_ids"])
        for patch_id in active:
            patch = patches.get(patch_id)
            if patch is None:
                raise DurableWorkflowError("workflow history contains an unknown patch")
            _require_patch_version(patch, run["definition_version"])
        for event in run["history"][1:]:
            if event["event_type"] == "patch-marker":
                patch_id = event["payload"]["patch_id"]
                patch = patches.get(patch_id)
                if patch is None:
                    raise DurableWorkflowError(
                        "workflow history contains an unknown patch"
                    )
                _require_patch_version(patch, run["definition_version"])
                if event["payload"]["semantic_sha256"] != patch["semantic_sha256"]:
                    raise DurableWorkflowError("workflow patch semantic hash mismatch")
                active.add(patch_id)
            elif event["event_type"] == "step-committed":
                command_count += 1
                expected = step_resolver(
                    event["payload"]["step_id"],
                    event["payload"]["input_sha256"],
                    frozenset(active),
                )
                _sha256(expected, "replayed result_sha256")
                if expected != event["payload"]["result_sha256"]:
                    raise DurableWorkflowError("workflow replay command mismatch")
    run_sha256s = [run["run_sha256"] for run in validated]
    final_history_event_sha256 = validated[-1]["history"][-1]["event_sha256"]
    chain_sha256 = _digest(
        {
            "run_sha256s": run_sha256s,
            "final_history_event_sha256": final_history_event_sha256,
        }
    )
    receipt = {
        "schema_version": WORKFLOW_REPLAY_SCHEMA_VERSION,
        "workflow_id": first["workflow_id"],
        "chain_id": first["chain_id"],
        "definition_manifest_sha256": current_definition["manifest_sha256"],
        "run_count": len(validated),
        "generation_count": validated[-1]["generation"],
        "history_event_count": sum(len(run["history"]) for run in validated),
        "command_count": command_count,
        "command_mismatches": 0,
        "run_sha256s": run_sha256s,
        "final_history_event_sha256": final_history_event_sha256,
        "chain_sha256": chain_sha256,
        "external_calls": 0,
        "state_writes": 0,
        "effect_calls": 0,
        "state_write_authority": False,
        "effect_dispatch_authority": False,
        "provider_native_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(_body(receipt, "receipt_sha256"))
    return receipt


def validate_workflow_replay_receipt(
    receipt: Any,
    *,
    runs: list[dict[str, Any]],
    definition: dict[str, Any],
    step_resolver: Callable[[str, str, frozenset[str]], str],
) -> dict[str, Any]:
    """Recompute replay semantics before accepting a replay receipt."""
    if not isinstance(receipt, dict):
        raise DurableWorkflowError("workflow replay receipt is invalid")
    expected = replay_workflow_chain(
        runs,
        definition=definition,
        step_resolver=step_resolver,
    )
    if receipt != expected:
        raise DurableWorkflowError("workflow replay receipt does not match replay")
    return copy.deepcopy(expected)
