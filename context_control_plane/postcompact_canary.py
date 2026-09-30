"""Deterministic PostCompact write gate bound to M5-01 and M5-02 artifacts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .artifact_store import ArtifactRef, ArtifactStoreError, LocalArtifactStore
from .checkpoint import CheckpointError, restore_checkpoint
from .compaction_checkpoint import (
    CompactionCheckpointError,
    validate_material_event_delta,
    validate_provider_compaction_hook_receipt,
)
from .execution_packet import (
    ExecutionPacketError,
    canonical_execution_packet_bytes,
    validate_execution_packet,
)
from .typed_state import canonical_state_bytes

SCHEMA_VERSION = "context.postcompact-canary/v1alpha1"
CANARY_VERSION = "context.postcompact-canary-evaluator/v1alpha1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_EVENT_HEAD_FIELDS = {"sequence_no", "event_sha256"}
_FIELDS = {
    "schema_version",
    "canary_version",
    "canary_id",
    "provider_id",
    "project_id",
    "project_revision",
    "expected_packet_sha256",
    "restored_packet_sha256",
    "delta_id",
    "delta_sha256",
    "event_head",
    "active_work_id",
    "task_revision",
    "effect_high_watermark",
    "decision_ids",
    "constraint_ids",
    "host_metadata_sha256",
    "authority_binding_sha256",
    "checkpoint_sha256",
    "expected_packet_artifact_sha256",
    "status",
    "execution_gate",
    "state_write_authority",
    "provider_native_authority",
    "observed_at",
    "canary_sha256",
}
_BINDING_FIELDS = {
    "schema_version",
    "project_id",
    "project_revision",
    "event_head",
    "governance_ref",
    "canonical_plan_sha256",
    "registry_digest",
    "state_sha256",
    "checkpoint_ref",
    "expected_packet_ref",
    "active_work_id",
    "task_revision",
    "effect_high_watermark",
}
BINDING_SCHEMA_VERSION = "context.postcompact-authority-binding/v1alpha1"


class PostCompactCanaryError(ValueError):
    """Raised when restored context cannot safely reopen the execution gate."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise PostCompactCanaryError(f"{field} must be lowercase SHA-256")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise PostCompactCanaryError(f"{field} is invalid")
    return value


def _uint(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise PostCompactCanaryError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise PostCompactCanaryError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PostCompactCanaryError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PostCompactCanaryError(f"{field} must include timezone")
    return value


def _event_head(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _EVENT_HEAD_FIELDS:
        raise PostCompactCanaryError("event_head fields are invalid")
    if type(value["sequence_no"]) is not int or value["sequence_no"] <= 0:
        raise PostCompactCanaryError("event_head.sequence_no must be positive")
    _sha(value["event_sha256"], "event_head.event_sha256")
    return copy.deepcopy(value)


def _ids(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or _ID_RE.fullmatch(item) is None for item in value)
        or value != sorted(value)
        or len(value) != len(set(value))
    ):
        raise PostCompactCanaryError(f"{field} must contain sorted unique IDs")
    return list(value)


def _artifact_ref(value: Any, field: str) -> ArtifactRef:
    try:
        return ArtifactRef.from_document(value)
    except (TypeError, ValueError) as exc:
        raise PostCompactCanaryError(f"{field} is invalid") from exc


def _validate_binding(binding: Any) -> tuple[dict[str, Any], ArtifactRef, ArtifactRef]:
    if not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS:
        raise PostCompactCanaryError("trusted authority binding fields are invalid")
    if binding["schema_version"] != BINDING_SCHEMA_VERSION:
        raise PostCompactCanaryError("trusted authority binding version is invalid")
    _id(binding["project_id"], "trusted project_id")
    _uint(binding["project_revision"], "trusted project_revision")
    _event_head(binding["event_head"])
    if not isinstance(binding["governance_ref"], str) or not binding["governance_ref"]:
        raise PostCompactCanaryError("trusted governance_ref is invalid")
    for field in ("canonical_plan_sha256", "registry_digest", "state_sha256"):
        _sha(binding[field], f"trusted {field}")
    checkpoint_ref = _artifact_ref(binding["checkpoint_ref"], "trusted checkpoint_ref")
    packet_ref = _artifact_ref(binding["expected_packet_ref"], "trusted expected_packet_ref")
    _id(binding["active_work_id"], "trusted active_work_id")
    _uint(binding["task_revision"], "trusted task_revision")
    _uint(binding["effect_high_watermark"], "trusted effect_high_watermark")
    return copy.deepcopy(binding), checkpoint_ref, packet_ref


def _strict_json(payload: bytes, field: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PostCompactCanaryError(f"{field} contains duplicate fields")
            result[key] = value
        return result

    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PostCompactCanaryError(f"{field} is not strict JSON") from exc
    if not isinstance(value, dict):
        raise PostCompactCanaryError(f"{field} must be an object")
    return value


def _packet_matches_checkpoint(packet: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    project = snapshot["project"]
    active = next(
        (item for item in snapshot["works"] if item["work_id"] == project["primary_work_id"]),
        None,
    )
    if active is None:
        return False
    expected_leaf = {
        "work_id": active["work_id"],
        "kind": active["kind"],
        "title": active["title"],
        "status": active["status"],
        "revision": active["revision"],
        "parent_work_id": active.get("parent_work_id"),
        "dependency_ids": sorted(active["dependency_ids"]),
        "owner_refs": sorted(active["owner_refs"]),
        "scope_refs": sorted(
            (copy.deepcopy(item) for item in active["scope_refs"]),
            key=lambda item: (item["scope_kind"], item["scope_ref"]),
        ),
        "return_point_work_id": active.get("return_point_work_id"),
        "exit_criteria": list(active.get("exit_criteria", [])),
        "promotion_target_work_id": active.get("promotion_target_work_id"),
        "mainline_authority": active.get("mainline_authority", True),
    }
    if packet["active_leaf"] != expected_leaf:
        return False
    expected_decisions = sorted(
        (
            {
                "decision_id": item["decision_id"],
                "work_id": item["work_id"],
                "status": item["status"],
                "statement": item["statement"],
                "evidence_ids": sorted(item["evidence_ids"]),
            }
            for item in snapshot["decisions"]
            if item["decision_id"] in project["current_decision_ids"]
            and item["status"] == "accepted"
            and item["work_id"] == active["work_id"]
        ),
        key=lambda item: item["decision_id"],
    )
    expected_constraints = sorted(
        (
            {
                "constraint_id": item["constraint_id"],
                "status": item["status"],
                "statement": item["statement"],
                "scope_work_ids": sorted(item["scope_work_ids"]),
                "evidence_ids": sorted(item["evidence_ids"]),
            }
            for item in snapshot["constraints"]
            if item["constraint_id"] in project["active_constraint_ids"]
            and item["status"] == "active"
            and (not item["scope_work_ids"] or active["work_id"] in item["scope_work_ids"])
        ),
        key=lambda item: item["constraint_id"],
    )
    return packet["decisions"] == expected_decisions and packet["constraints"] == expected_constraints


def _body(receipt: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(receipt)
    result.pop("canary_sha256", None)
    return result


def canonical_postcompact_canary_bytes(receipt: dict[str, Any]) -> bytes:
    """Return canonical receipt bytes after strict validation."""
    validate_postcompact_canary_receipt(receipt)
    return _canonical(receipt)


def validate_postcompact_canary_receipt(receipt: dict[str, Any]) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS:
        raise PostCompactCanaryError("PostCompact canary fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION or receipt["canary_version"] != CANARY_VERSION:
        raise PostCompactCanaryError("PostCompact canary identity is invalid")
    _id(receipt["canary_id"], "canary_id")
    if receipt["provider_id"] not in {"pi", "deepseek"}:
        raise PostCompactCanaryError("provider_id is invalid")
    _id(receipt["project_id"], "project_id")
    _uint(receipt["project_revision"], "project_revision")
    _sha(receipt["expected_packet_sha256"], "expected_packet_sha256")
    _sha(receipt["restored_packet_sha256"], "restored_packet_sha256")
    if receipt["expected_packet_sha256"] != receipt["restored_packet_sha256"]:
        raise PostCompactCanaryError("restored packet digest mismatch")
    _id(receipt["delta_id"], "delta_id")
    _sha(receipt["delta_sha256"], "delta_sha256")
    _event_head(receipt["event_head"])
    _id(receipt["active_work_id"], "active_work_id")
    _uint(receipt["task_revision"], "task_revision")
    _uint(receipt["effect_high_watermark"], "effect_high_watermark")
    _ids(receipt["decision_ids"], "decision_ids")
    _ids(receipt["constraint_ids"], "constraint_ids")
    _sha(receipt["host_metadata_sha256"], "host_metadata_sha256")
    _sha(receipt["authority_binding_sha256"], "authority_binding_sha256")
    _sha(receipt["checkpoint_sha256"], "checkpoint_sha256")
    _sha(receipt["expected_packet_artifact_sha256"], "expected_packet_artifact_sha256")
    if receipt["status"] != "pass" or receipt["execution_gate"] != "allow":
        raise PostCompactCanaryError("PostCompact canary gate is invalid")
    if receipt["state_write_authority"] is not False:
        raise PostCompactCanaryError("PostCompact canary cannot grant State write authority")
    if receipt["provider_native_authority"] is not False:
        raise PostCompactCanaryError("PostCompact canary cannot grant provider-native authority")
    _timestamp(receipt["observed_at"], "observed_at")
    expected = hashlib.sha256(_canonical(_body(receipt))).hexdigest()
    if receipt["canary_sha256"] != expected:
        raise PostCompactCanaryError("PostCompact canary digest mismatch")


def evaluate_postcompact_canary(
    *,
    artifact_store: LocalArtifactStore,
    trusted_binding: dict[str, Any],
    restored_packet: dict[str, Any],
    delta: dict[str, Any],
    hook_receipt: dict[str, Any],
    observed_host_metadata: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Open the execution gate only for an exact, authority-bound restore."""
    if not isinstance(artifact_store, LocalArtifactStore):
        raise PostCompactCanaryError("artifact_store is invalid")
    binding, checkpoint_ref, packet_ref = _validate_binding(trusted_binding)
    try:
        checkpoint = restore_checkpoint(
            checkpoint_ref,
            artifact_store,
            expected_project_id=binding["project_id"],
            expected_revision=binding["project_revision"],
            expected_event_head=binding["event_head"],
            expected_governance_ref=binding["governance_ref"],
            expected_plan_sha256=binding["canonical_plan_sha256"],
            expected_registry_digest=binding["registry_digest"],
        )
        expected_payload = artifact_store.read(packet_ref)
    except (CheckpointError, ArtifactStoreError) as exc:
        raise PostCompactCanaryError("trusted PostCompact artifacts failed verification") from exc
    expected_packet = _strict_json(expected_payload, "expected packet artifact")
    try:
        validate_execution_packet(expected_packet)
        validate_execution_packet(restored_packet)
        validate_material_event_delta(delta)
        validate_provider_compaction_hook_receipt(hook_receipt)
    except (ExecutionPacketError, CompactionCheckpointError, TypeError) as exc:
        raise PostCompactCanaryError("PostCompact input validation failed") from exc
    state_sha256 = hashlib.sha256(canonical_state_bytes(checkpoint.snapshot)).hexdigest()
    if state_sha256 != binding["state_sha256"]:
        raise PostCompactCanaryError("checkpoint state digest differs from trusted authority")
    binding_projection = {
        "project_id": checkpoint.snapshot["project"]["project_id"],
        "project_revision": checkpoint.snapshot["project"]["revision"],
        "active_work_id": checkpoint.snapshot["project"]["primary_work_id"],
        "effect_high_watermark": checkpoint.snapshot["project"]["effect_high_watermark"],
    }
    for field, actual in binding_projection.items():
        if binding[field] != actual:
            raise PostCompactCanaryError(f"trusted {field} differs from checkpoint")
    active = next(
        item
        for item in checkpoint.snapshot["works"]
        if item["work_id"] == binding["active_work_id"]
    )
    if binding["task_revision"] != active["revision"]:
        raise PostCompactCanaryError("trusted task_revision differs from checkpoint")
    expected_binding = {
        "project_id": binding["project_id"],
        "project_revision": binding["project_revision"],
        "governance_ref": binding["governance_ref"],
        "canonical_plan_sha256": binding["canonical_plan_sha256"],
        "state_sha256": binding["state_sha256"],
    }
    for field, expected in expected_binding.items():
        if expected_packet[field] != expected:
            raise PostCompactCanaryError(f"expected packet {field} differs from trusted authority")
    if not _packet_matches_checkpoint(expected_packet, checkpoint.snapshot):
        raise PostCompactCanaryError("expected packet projection differs from checkpoint truth")
    expected_bytes = canonical_execution_packet_bytes(expected_packet)
    restored_bytes = canonical_execution_packet_bytes(restored_packet)
    if expected_bytes != restored_bytes:
        raise PostCompactCanaryError("restored packet differs from expected packet")
    if not isinstance(observed_host_metadata, dict):
        raise PostCompactCanaryError("observed host metadata is invalid")
    if observed_host_metadata != hook_receipt["host_metadata"]:
        raise PostCompactCanaryError("host compaction metadata mismatch")
    if delta["base_checkpoint_ref"] != checkpoint_ref.to_document():
        raise PostCompactCanaryError("material delta is not bound to the trusted checkpoint")

    packet_digest = expected_packet["packet_sha256"]
    active_leaf = expected_packet["active_leaf"]
    bindings = {
        "project_id": expected_packet["project_id"],
        "project_revision": expected_packet["project_revision"],
        "execution_packet_sha256": packet_digest,
        "canonical_plan_sha256": expected_packet["canonical_plan_sha256"],
        "active_work_id": active_leaf["work_id"],
        "task_revision": active_leaf["revision"],
    }
    for field, expected in bindings.items():
        if delta[field] != expected:
            raise PostCompactCanaryError(f"material delta {field} mismatch")
    hook_bindings = {
        "delta_id": delta["delta_id"],
        "delta_sha256": delta["delta_sha256"],
        "project_id": delta["project_id"],
        "project_revision": delta["project_revision"],
        "event_head": delta["event_head"],
        "task_revision": delta["task_revision"],
        "effect_high_watermark": delta["effect_high_watermark"],
    }
    for field, expected in hook_bindings.items():
        if hook_receipt[field] != expected:
            raise PostCompactCanaryError(f"provider hook {field} mismatch")

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "canary_version": CANARY_VERSION,
        "canary_id": "pending",
        "provider_id": hook_receipt["provider_id"],
        "project_id": expected_packet["project_id"],
        "project_revision": expected_packet["project_revision"],
        "expected_packet_sha256": packet_digest,
        "restored_packet_sha256": restored_packet["packet_sha256"],
        "delta_id": delta["delta_id"],
        "delta_sha256": delta["delta_sha256"],
        "event_head": copy.deepcopy(delta["event_head"]),
        "active_work_id": active_leaf["work_id"],
        "task_revision": active_leaf["revision"],
        "effect_high_watermark": delta["effect_high_watermark"],
        "decision_ids": sorted(item["decision_id"] for item in expected_packet["decisions"]),
        "constraint_ids": sorted(
            item["constraint_id"] for item in expected_packet["constraints"]
        ),
        "host_metadata_sha256": hashlib.sha256(_canonical(observed_host_metadata)).hexdigest(),
        "authority_binding_sha256": hashlib.sha256(_canonical(binding)).hexdigest(),
        "checkpoint_sha256": checkpoint_ref.digest,
        "expected_packet_artifact_sha256": packet_ref.digest,
        "status": "pass",
        "execution_gate": "allow",
        "state_write_authority": False,
        "provider_native_authority": False,
        "observed_at": _timestamp(observed_at, "observed_at"),
        "canary_sha256": "pending",
    }
    identity = hashlib.sha256(_canonical(_body(receipt))).hexdigest()
    receipt["canary_id"] = f"canary-{identity[:32]}"
    receipt["canary_sha256"] = hashlib.sha256(_canonical(_body(receipt))).hexdigest()
    validate_postcompact_canary_receipt(receipt)
    return receipt
