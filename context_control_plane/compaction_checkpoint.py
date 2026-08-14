"""Provider-neutral PreCompact material-event delta and shadow hook adapters."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .artifact_store import ArtifactRef, ArtifactStoreError, LocalArtifactStore
from .execution_packet import ExecutionPacketError, validate_execution_packet
from .typed_state import TypedStateError, canonical_state_bytes, validate_typed_state

MATERIAL_EVENT_DELTA_SCHEMA_VERSION = "context.material-event-delta/v1alpha1"
PROVIDER_COMPACTION_HOOK_SCHEMA_VERSION = "context.provider-compaction-hook/v1alpha1"
ADAPTER_VERSION = "context.provider-compaction-hook-adapter/v1alpha1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SAFE_TEXT_RE = re.compile(r"^[^\r\n\x00]+$")
_EVENT_HEAD_FIELDS = {"sequence_no", "event_sha256"}
_MATERIAL_EVENT_FIELDS = {
    "event_id",
    "event_type",
    "sequence_no",
    "revision_before",
    "revision_after",
    "event_sha256",
}
_DELTA_FIELDS = {
    "schema_version",
    "delta_id",
    "project_id",
    "base_checkpoint_ref",
    "base_revision",
    "base_event_head",
    "project_revision",
    "event_head",
    "material_events",
    "state_sha256",
    "execution_packet_sha256",
    "canonical_plan_sha256",
    "active_work_id",
    "task_revision",
    "effect_high_watermark",
    "observed_at",
    "state_write_authority",
    "provider_native_authority",
    "delta_sha256",
}
_HOOK_FIELDS = {
    "schema_version",
    "provider_id",
    "adapter_version",
    "operation",
    "delta_id",
    "delta_sha256",
    "project_id",
    "project_revision",
    "event_head",
    "task_revision",
    "effect_high_watermark",
    "host_metadata",
    "provider_native_authority",
    "state_write_authority",
    "observed_at",
    "hook_sha256",
}


class CompactionCheckpointError(ValueError):
    """Raised when a PreCompact delta or provider hook receipt is unsafe."""


class MaterialEventDeltaError(CompactionCheckpointError):
    """Raised when a rolling material-event delta is invalid or stale."""


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CompactionCheckpointError(f"{field} must be lowercase SHA-256")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise CompactionCheckpointError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int = 256) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or _SAFE_TEXT_RE.fullmatch(value) is None
    ):
        raise CompactionCheckpointError(f"{field} is invalid")
    return value


def _uint(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise CompactionCheckpointError(f"{field} must be a {'positive' if positive else 'non-negative'} integer")
    return value


def _timestamp(value: Any, field: str) -> str:
    _text(value, field, 64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CompactionCheckpointError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CompactionCheckpointError(f"{field} must include timezone")
    return value


def _event_head(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _EVENT_HEAD_FIELDS:
        raise CompactionCheckpointError(f"{field} fields are invalid")
    _uint(value["sequence_no"], f"{field}.sequence_no", positive=True)
    _sha(value["event_sha256"], f"{field}.event_sha256")
    return {"sequence_no": value["sequence_no"], "event_sha256": value["event_sha256"]}


def _artifact_ref(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    try:
        return ArtifactRef.from_document(value).to_document()
    except (TypeError, ValueError) as exc:
        raise CompactionCheckpointError(f"{field} is invalid") from exc


def _delta_body(delta: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(delta)
    body.pop("delta_sha256", None)
    return body


def canonical_material_event_delta_bytes(delta: dict[str, Any]) -> bytes:
    return _canonical(delta)


def _event_summary(event: Any, field: str) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise MaterialEventDeltaError(f"{field} is invalid")
    required = {
        "event_id",
        "event_type",
        "sequence_no",
        "revision_before",
        "revision_after",
        "event_sha256",
    }
    if not required.issubset(event):
        raise MaterialEventDeltaError(f"{field} fields are incomplete")
    _id(event["event_id"], f"{field}.event_id")
    _text(event["event_type"], f"{field}.event_type", 64)
    _uint(event["sequence_no"], f"{field}.sequence_no", positive=True)
    _uint(event["revision_before"], f"{field}.revision_before")
    _uint(event["revision_after"], f"{field}.revision_after")
    if event["revision_after"] < event["revision_before"]:
        raise MaterialEventDeltaError(f"{field}.revision_after is behind revision_before")
    _sha(event["event_sha256"], f"{field}.event_sha256")
    return {
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "sequence_no": event["sequence_no"],
        "revision_before": event["revision_before"],
        "revision_after": event["revision_after"],
        "event_sha256": event["event_sha256"],
    }


def _validate_material_events(
    events: Any,
    *,
    base_event_head: dict[str, Any] | None,
    project_revision: int,
) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        raise MaterialEventDeltaError("material_events must be a list")
    summaries = [_event_summary(event, f"material_events[{index}]") for index, event in enumerate(events)]
    sequence_floor = base_event_head["sequence_no"] if base_event_head is not None else 0
    expected_sequence = sequence_floor + 1
    seen_ids: set[str] = set()
    seen_sequences: set[int] = set()
    for event in summaries:
        if event["event_id"] in seen_ids or event["sequence_no"] in seen_sequences:
            raise MaterialEventDeltaError("material_events must contain unique event identities")
        seen_ids.add(event["event_id"])
        seen_sequences.add(event["sequence_no"])
        if event["sequence_no"] != expected_sequence:
            raise MaterialEventDeltaError("material_events sequence has a gap")
        if event["revision_after"] > project_revision:
            raise MaterialEventDeltaError("material event revision is ahead of project revision")
        expected_sequence += 1
    if summaries and summaries != sorted(summaries, key=lambda item: item["sequence_no"]):
        raise MaterialEventDeltaError("material_events must be ordered by sequence")
    return summaries


def validate_material_event_delta(delta: dict[str, Any]) -> None:
    if not isinstance(delta, dict) or set(delta) != _DELTA_FIELDS:
        raise MaterialEventDeltaError("material event delta fields are invalid")
    if delta["schema_version"] != MATERIAL_EVENT_DELTA_SCHEMA_VERSION:
        raise MaterialEventDeltaError("material event delta schema_version is unsupported")
    _id(delta["delta_id"], "delta_id")
    _id(delta["project_id"], "project_id")
    _artifact_ref(delta["base_checkpoint_ref"], "base_checkpoint_ref")
    _uint(delta["base_revision"], "base_revision")
    base_head = _event_head(delta["base_event_head"], "base_event_head")
    project_revision = _uint(delta["project_revision"], "project_revision")
    if delta["base_revision"] > project_revision:
        raise MaterialEventDeltaError("base revision is ahead of project revision")
    head = _event_head(delta["event_head"], "event_head")
    summaries = _validate_material_events(
        delta["material_events"],
        base_event_head=base_head,
        project_revision=project_revision,
    )
    expected_head = (
        {"sequence_no": summaries[-1]["sequence_no"], "event_sha256": summaries[-1]["event_sha256"]}
        if summaries
        else base_head
    )
    if head != expected_head:
        raise MaterialEventDeltaError("event_head does not match material events")
    _sha(delta["state_sha256"], "state_sha256")
    _sha(delta["execution_packet_sha256"], "execution_packet_sha256")
    _sha(delta["canonical_plan_sha256"], "canonical_plan_sha256")
    if delta["active_work_id"] is not None:
        _id(delta["active_work_id"], "active_work_id")
    _uint(delta["task_revision"], "task_revision")
    _uint(delta["effect_high_watermark"], "effect_high_watermark")
    _timestamp(delta["observed_at"], "observed_at")
    if delta["state_write_authority"] is not False or delta["provider_native_authority"] is not False:
        raise MaterialEventDeltaError("material event delta authority boundary is invalid")
    expected_digest = hashlib.sha256(_canonical(_delta_body(delta))).hexdigest()
    if delta["delta_sha256"] != expected_digest:
        raise MaterialEventDeltaError("material event delta digest mismatch")


def build_material_event_delta(
    *,
    snapshot: dict[str, Any],
    base_checkpoint_ref: dict[str, Any] | None,
    base_revision: int,
    base_event_head: dict[str, Any] | None,
    events: list[dict[str, Any]],
    execution_packet: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    try:
        validate_typed_state(snapshot)
        validate_execution_packet(execution_packet)
    except (TypeError, TypedStateError, ExecutionPacketError) as exc:
        raise MaterialEventDeltaError("PreCompact inputs are invalid") from exc
    project = snapshot["project"]
    if project["project_id"] != execution_packet["project_id"]:
        raise MaterialEventDeltaError("packet project does not match snapshot")
    if project["revision"] != execution_packet["project_revision"]:
        raise MaterialEventDeltaError("packet revision does not match snapshot")
    if execution_packet["state_sha256"] != hashlib.sha256(canonical_state_bytes(snapshot)).hexdigest():
        raise MaterialEventDeltaError("packet state digest does not match snapshot")
    if type(base_revision) is not int or base_revision != project["revision"]:
        raise MaterialEventDeltaError("base revision must match current project revision")
    normalized_base_head = _event_head(base_event_head, "base_event_head")
    summaries = _validate_material_events(
        events,
        base_event_head=normalized_base_head,
        project_revision=project["revision"],
    )
    event_head = (
        {"sequence_no": summaries[-1]["sequence_no"], "event_sha256": summaries[-1]["event_sha256"]}
        if summaries
        else normalized_base_head
    )
    normalized = {
        "schema_version": MATERIAL_EVENT_DELTA_SCHEMA_VERSION,
        "delta_id": "pending",
        "project_id": project["project_id"],
        "base_checkpoint_ref": _artifact_ref(base_checkpoint_ref, "base_checkpoint_ref"),
        "base_revision": base_revision,
        "base_event_head": normalized_base_head,
        "project_revision": project["revision"],
        "event_head": event_head,
        "material_events": summaries,
        "state_sha256": hashlib.sha256(canonical_state_bytes(snapshot)).hexdigest(),
        "execution_packet_sha256": execution_packet["packet_sha256"],
        "canonical_plan_sha256": execution_packet["canonical_plan_sha256"],
        "active_work_id": execution_packet["active_leaf"]["work_id"],
        "task_revision": execution_packet["active_leaf"]["revision"],
        "effect_high_watermark": project["effect_high_watermark"],
        "observed_at": _timestamp(observed_at, "observed_at"),
        "state_write_authority": False,
        "provider_native_authority": False,
        "delta_sha256": "pending",
    }
    body_without_id = _delta_body(normalized)
    delta_digest = hashlib.sha256(_canonical(body_without_id)).hexdigest()
    normalized["delta_id"] = f"delta-{delta_digest[:32]}"
    normalized["delta_sha256"] = hashlib.sha256(_canonical(_delta_body(normalized))).hexdigest()
    validate_material_event_delta(normalized)
    return normalized


def publish_material_event_delta(
    delta: dict[str, Any], artifact_store: LocalArtifactStore
) -> ArtifactRef:
    validate_material_event_delta(delta)
    try:
        return artifact_store.put_bytes(canonical_material_event_delta_bytes(delta))
    except ArtifactStoreError as exc:
        raise CompactionCheckpointError("material event delta publication failed") from exc


def _hook_body(receipt: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(receipt)
    body.pop("hook_sha256", None)
    return body


def _validate_provider_metadata(provider_id: str, metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        raise CompactionCheckpointError("provider metadata is invalid")
    expected = (
        {"cut_point", "split_turn", "usage_tokens"}
        if provider_id == "pi"
        else {"checkpoint_kind", "checkpoint_sequence"}
    )
    if set(metadata) != expected:
        raise CompactionCheckpointError("provider metadata fields are invalid")
    if provider_id == "pi":
        _uint(metadata["cut_point"], "provider metadata.cut_point")
        if type(metadata["split_turn"]) is not bool:
            raise CompactionCheckpointError("provider metadata.split_turn is invalid")
        _uint(metadata["usage_tokens"], "provider metadata.usage_tokens")
    else:
        if metadata["checkpoint_kind"] != "semantic":
            raise CompactionCheckpointError("provider metadata.checkpoint_kind is invalid")
        _uint(metadata["checkpoint_sequence"], "provider metadata.checkpoint_sequence", positive=True)
    return copy.deepcopy(metadata)


def validate_provider_compaction_hook_receipt(receipt: dict[str, Any]) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _HOOK_FIELDS:
        raise CompactionCheckpointError("provider compaction hook fields are invalid")
    if receipt["schema_version"] != PROVIDER_COMPACTION_HOOK_SCHEMA_VERSION:
        raise CompactionCheckpointError("provider compaction hook schema_version is unsupported")
    if receipt["provider_id"] not in {"pi", "deepseek"} or receipt["adapter_version"] != ADAPTER_VERSION:
        raise CompactionCheckpointError("provider compaction hook identity is invalid")
    if receipt["operation"] != "precompact":
        raise CompactionCheckpointError("provider compaction hook operation is invalid")
    _id(receipt["delta_id"], "delta_id")
    _sha(receipt["delta_sha256"], "delta_sha256")
    _id(receipt["project_id"], "project_id")
    _uint(receipt["project_revision"], "project_revision")
    _event_head(receipt["event_head"], "event_head")
    _uint(receipt["task_revision"], "task_revision")
    _uint(receipt["effect_high_watermark"], "effect_high_watermark")
    _validate_provider_metadata(receipt["provider_id"], receipt["host_metadata"])
    if receipt["provider_native_authority"] is not False or receipt["state_write_authority"] is not False:
        raise CompactionCheckpointError("provider authority boundary is invalid")
    _timestamp(receipt["observed_at"], "observed_at")
    expected_digest = hashlib.sha256(_canonical(_hook_body(receipt))).hexdigest()
    if receipt["hook_sha256"] != expected_digest:
        raise CompactionCheckpointError("provider compaction hook digest mismatch")


class _CompactionHookAdapter:
    provider_id: str

    def capture(
        self,
        delta: dict[str, Any],
        *,
        metadata: dict[str, Any],
        observed_at: str,
    ) -> dict[str, Any]:
        validate_material_event_delta(delta)
        host_metadata = _validate_provider_metadata(self.provider_id, metadata)
        receipt = {
            "schema_version": PROVIDER_COMPACTION_HOOK_SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "adapter_version": ADAPTER_VERSION,
            "operation": "precompact",
            "delta_id": delta["delta_id"],
            "delta_sha256": delta["delta_sha256"],
            "project_id": delta["project_id"],
            "project_revision": delta["project_revision"],
            "event_head": copy.deepcopy(delta["event_head"]),
            "task_revision": delta["task_revision"],
            "effect_high_watermark": delta["effect_high_watermark"],
            "host_metadata": host_metadata,
            "provider_native_authority": False,
            "state_write_authority": False,
            "observed_at": _timestamp(observed_at, "observed_at"),
            "hook_sha256": "pending",
        }
        receipt["hook_sha256"] = hashlib.sha256(_canonical(_hook_body(receipt))).hexdigest()
        validate_provider_compaction_hook_receipt(receipt)
        return receipt


class PiCompactionHookAdapter(_CompactionHookAdapter):
    provider_id = "pi"


class DeepSeekCompactionHookAdapter(_CompactionHookAdapter):
    provider_id = "deepseek"
