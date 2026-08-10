"""Immutable checkpoint publication and deterministic restore canary."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .artifact_store import ArtifactRef, ArtifactStoreError, LocalArtifactStore
from .state_store import StateStoreCapabilityError, capability_manifest_from_document
from .typed_state import TypedStateError, canonical_state_bytes, validate_typed_state


CHECKPOINT_SCHEMA_VERSION = "context.checkpoint-manifest/v1alpha1"
DEFAULT_MAX_MANIFEST_BYTES = 64 * 1024
DEFAULT_MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_READ_RESULT_FIELDS = {
    "snapshot",
    "revision",
    "event_head",
    "registry_digest",
    "capabilities",
}
_EVENT_HEAD_FIELDS = {"sequence_no", "event_sha256"}
_PROJECTION_FIELDS = {
    "project_id",
    "revision",
    "governance_ref",
    "canonical_plan_sha256",
    "registry_digest",
    "event_head",
    "active_work_ids",
    "primary_work_id",
    "terminal_work_ids",
    "accepted_decision_ids",
    "terminal_decision_ids",
    "active_constraint_ids",
    "open_blocker_ids",
    "active_claim_ids",
    "in_flight_effect_ids",
    "terminal_effect_ids",
    "effect_high_watermark",
    "captured_state_updated_at",
}
_MANIFEST_FIELDS = {
    "schema_version",
    "snapshot_ref",
    *_PROJECTION_FIELDS,
    "critical_projection_sha256",
}
_TERMINAL_WORK_STATUSES = {"completed", "rejected", "reverted", "superseded"}
_TERMINAL_DECISION_STATUSES = {"rejected", "reverted", "superseded"}


class CheckpointError(RuntimeError):
    """Base error for checkpoint publication and restore."""


class CheckpointInputError(CheckpointError):
    """Raised when a checkpoint source or expected authority is invalid."""


class CheckpointIntegrityError(CheckpointError):
    """Raised when checkpoint content is missing, changed, or inconsistent."""


class CheckpointStaleError(CheckpointError):
    """Raised when a checkpoint no longer matches current trusted authority."""


class CheckpointSizeError(CheckpointIntegrityError):
    """Raised before reading an artifact outside configured restore bounds."""


@dataclass(frozen=True, slots=True)
class RestoredCheckpoint:
    """One verified checkpoint and its restored typed-state snapshot."""

    checkpoint_ref: ArtifactRef
    manifest: dict[str, Any]
    snapshot: dict[str, Any]


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any, field: str, error_type: type[CheckpointError]) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise error_type(f"{field} must be lowercase SHA-256")
    return value


def _event_head(
    value: Any,
    *,
    error_type: type[CheckpointError],
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _EVENT_HEAD_FIELDS:
        raise error_type("event head fields are invalid")
    sequence_no = value["sequence_no"]
    if type(sequence_no) is not int or sequence_no <= 0:
        raise error_type("event head sequence_no must be positive")
    _sha256(value["event_sha256"], "event head hash", error_type)
    return {
        "sequence_no": sequence_no,
        "event_sha256": value["event_sha256"],
    }


def _sorted_ids(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
        or value != sorted(value)
    ):
        raise CheckpointIntegrityError(f"{field} must contain sorted unique IDs")
    return list(value)


def _critical_projection(
    snapshot: dict[str, Any],
    *,
    canonical_plan_sha256: str,
    registry_digest: str,
    event_head: dict[str, Any] | None,
) -> dict[str, Any]:
    project = snapshot["project"]
    return {
        "project_id": project["project_id"],
        "revision": project["revision"],
        "governance_ref": project["governance_ref"],
        "canonical_plan_sha256": canonical_plan_sha256,
        "registry_digest": registry_digest,
        "event_head": copy.deepcopy(event_head),
        "active_work_ids": sorted(project["active_work_ids"]),
        "primary_work_id": project["primary_work_id"],
        "terminal_work_ids": sorted(
            item["work_id"]
            for item in snapshot["works"]
            if item["status"] in _TERMINAL_WORK_STATUSES
        ),
        "accepted_decision_ids": sorted(
            item["decision_id"]
            for item in snapshot["decisions"]
            if item["status"] == "accepted"
        ),
        "terminal_decision_ids": sorted(
            item["decision_id"]
            for item in snapshot["decisions"]
            if item["status"] in _TERMINAL_DECISION_STATUSES
        ),
        "active_constraint_ids": sorted(project["active_constraint_ids"]),
        "open_blocker_ids": sorted(project["open_blocker_ids"]),
        "active_claim_ids": sorted(
            item["claim_id"]
            for item in snapshot["claims"]
            if item["status"] == "active"
        ),
        "in_flight_effect_ids": sorted(
            item["effect_id"]
            for item in snapshot["effects"]
            if item["status"] in {"authorized", "started"}
        ),
        "terminal_effect_ids": sorted(
            item["effect_id"]
            for item in snapshot["effects"]
            if item["status"] in {"succeeded", "failed", "compensated"}
        ),
        "effect_high_watermark": project["effect_high_watermark"],
        "captured_state_updated_at": project["updated_at"],
    }


def _projection_digest(projection: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(projection)).hexdigest()


def _strict_json_document(payload: bytes, field: str) -> dict[str, Any]:
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CheckpointIntegrityError(f"{field} is not strict JSON") from exc
    if not isinstance(document, dict):
        raise CheckpointIntegrityError(f"{field} must be a JSON object")
    return document


def _validate_manifest(document: dict[str, Any]) -> ArtifactRef:
    if set(document) != _MANIFEST_FIELDS:
        raise CheckpointIntegrityError("checkpoint manifest fields are invalid")
    if document["schema_version"] != CHECKPOINT_SCHEMA_VERSION:
        raise CheckpointIntegrityError("unsupported checkpoint schema_version")
    try:
        snapshot_ref = ArtifactRef.from_document(document["snapshot_ref"])
    except (TypeError, ValueError) as exc:
        raise CheckpointIntegrityError("checkpoint snapshot_ref is invalid") from exc
    if not isinstance(document["project_id"], str) or not document["project_id"]:
        raise CheckpointIntegrityError("checkpoint project_id is invalid")
    if type(document["revision"]) is not int or document["revision"] < 0:
        raise CheckpointIntegrityError("checkpoint revision is invalid")
    if not isinstance(document["governance_ref"], str) or not document["governance_ref"]:
        raise CheckpointIntegrityError("checkpoint governance_ref is invalid")
    _sha256(
        document["canonical_plan_sha256"],
        "canonical_plan_sha256",
        CheckpointIntegrityError,
    )
    _sha256(document["registry_digest"], "registry_digest", CheckpointIntegrityError)
    _event_head(document["event_head"], error_type=CheckpointIntegrityError)
    for field in (
        "active_work_ids",
        "terminal_work_ids",
        "accepted_decision_ids",
        "terminal_decision_ids",
        "active_constraint_ids",
        "open_blocker_ids",
        "active_claim_ids",
        "in_flight_effect_ids",
        "terminal_effect_ids",
    ):
        _sorted_ids(document[field], field)
    if document["primary_work_id"] is not None and (
        not isinstance(document["primary_work_id"], str)
        or not document["primary_work_id"]
    ):
        raise CheckpointIntegrityError("checkpoint primary_work_id is invalid")
    if (
        type(document["effect_high_watermark"]) is not int
        or document["effect_high_watermark"] < 0
    ):
        raise CheckpointIntegrityError("checkpoint effect_high_watermark is invalid")
    if (
        not isinstance(document["captured_state_updated_at"], str)
        or not document["captured_state_updated_at"]
    ):
        raise CheckpointIntegrityError("checkpoint captured timestamp is invalid")
    expected_projection_digest = _projection_digest(
        {field: copy.deepcopy(document[field]) for field in _PROJECTION_FIELDS}
    )
    if document["critical_projection_sha256"] != expected_projection_digest:
        raise CheckpointIntegrityError("checkpoint critical projection digest mismatch")
    return snapshot_ref


def _read_artifact(
    store: LocalArtifactStore,
    ref: ArtifactRef,
    *,
    maximum_bytes: int,
    field: str,
) -> bytes:
    if type(maximum_bytes) is not int or maximum_bytes <= 0:
        raise CheckpointInputError(f"{field} maximum must be a positive integer")
    if ref.size_bytes > maximum_bytes:
        raise CheckpointSizeError(f"{field} exceeds configured restore bound")
    try:
        return store.read(ref)
    except ArtifactStoreError as exc:
        raise CheckpointIntegrityError(f"{field} artifact failed integrity verification") from exc


def publish_checkpoint(
    read_result: dict[str, Any],
    artifact_store: LocalArtifactStore,
    *,
    canonical_plan_sha256: str,
) -> ArtifactRef:
    """Publish a typed snapshot and its strict manifest as immutable artifacts."""
    if not isinstance(read_result, dict) or set(read_result) != _READ_RESULT_FIELDS:
        raise CheckpointInputError("State MCP read result fields are invalid")
    _sha256(
        canonical_plan_sha256,
        "canonical_plan_sha256",
        CheckpointInputError,
    )
    registry_digest = _sha256(
        read_result["registry_digest"],
        "registry_digest",
        CheckpointInputError,
    )
    event_head = _event_head(
        read_result["event_head"],
        error_type=CheckpointInputError,
    )
    try:
        capability_manifest_from_document(read_result["capabilities"])
    except StateStoreCapabilityError as exc:
        raise CheckpointInputError(
            "State MCP read result capabilities are invalid"
        ) from exc
    snapshot = read_result["snapshot"]
    try:
        snapshot_payload = canonical_state_bytes(snapshot)
    except (TypeError, TypedStateError) as exc:
        raise CheckpointInputError("State MCP read result contains invalid typed state") from exc
    if read_result["revision"] != snapshot["project"]["revision"]:
        raise CheckpointInputError("State MCP read result revision is torn")
    try:
        snapshot_ref = artifact_store.put_bytes(snapshot_payload)
    except ArtifactStoreError as exc:
        raise CheckpointIntegrityError("checkpoint snapshot publication failed") from exc
    projection = _critical_projection(
        snapshot,
        canonical_plan_sha256=canonical_plan_sha256,
        registry_digest=registry_digest,
        event_head=event_head,
    )
    manifest = {
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "snapshot_ref": snapshot_ref.to_document(),
        **projection,
        "critical_projection_sha256": _projection_digest(projection),
    }
    _validate_manifest(manifest)
    try:
        return artifact_store.put_bytes(_canonical_json_bytes(manifest))
    except ArtifactStoreError as exc:
        raise CheckpointIntegrityError("checkpoint manifest publication failed") from exc


def restore_checkpoint(
    checkpoint_ref: ArtifactRef,
    artifact_store: LocalArtifactStore,
    *,
    expected_project_id: str,
    expected_revision: int,
    expected_event_head: dict[str, Any] | None,
    expected_governance_ref: str,
    expected_plan_sha256: str,
    expected_registry_digest: str,
    max_manifest_bytes: int = DEFAULT_MAX_MANIFEST_BYTES,
    max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
) -> RestoredCheckpoint:
    """Restore only when immutable content and trusted authority still agree."""
    if not isinstance(checkpoint_ref, ArtifactRef):
        raise CheckpointInputError("checkpoint_ref must be an ArtifactRef")
    if not isinstance(expected_project_id, str) or not expected_project_id:
        raise CheckpointInputError("expected_project_id is invalid")
    if type(expected_revision) is not int or expected_revision < 0:
        raise CheckpointInputError("expected_revision is invalid")
    expected_head = _event_head(
        expected_event_head,
        error_type=CheckpointInputError,
    )
    if not isinstance(expected_governance_ref, str) or not expected_governance_ref:
        raise CheckpointInputError("expected_governance_ref is invalid")
    _sha256(expected_plan_sha256, "expected_plan_sha256", CheckpointInputError)
    _sha256(expected_registry_digest, "expected_registry_digest", CheckpointInputError)

    manifest_payload = _read_artifact(
        artifact_store,
        checkpoint_ref,
        maximum_bytes=max_manifest_bytes,
        field="checkpoint manifest",
    )
    manifest = _strict_json_document(manifest_payload, "checkpoint manifest")
    snapshot_ref = _validate_manifest(manifest)
    snapshot_payload = _read_artifact(
        artifact_store,
        snapshot_ref,
        maximum_bytes=max_snapshot_bytes,
        field="checkpoint snapshot",
    )
    snapshot = _strict_json_document(snapshot_payload, "checkpoint snapshot")
    try:
        validate_typed_state(snapshot)
    except (TypeError, TypedStateError) as exc:
        raise CheckpointIntegrityError("checkpoint snapshot violates typed state") from exc

    derived_projection = _critical_projection(
        snapshot,
        canonical_plan_sha256=manifest["canonical_plan_sha256"],
        registry_digest=manifest["registry_digest"],
        event_head=manifest["event_head"],
    )
    manifest_projection = {
        field: copy.deepcopy(manifest[field]) for field in _PROJECTION_FIELDS
    }
    if derived_projection != manifest_projection:
        raise CheckpointIntegrityError("checkpoint manifest and snapshot projection drift")

    current_authority = {
        "project_id": expected_project_id,
        "revision": expected_revision,
        "event_head": expected_head,
        "governance_ref": expected_governance_ref,
        "canonical_plan_sha256": expected_plan_sha256,
        "registry_digest": expected_registry_digest,
    }
    for field, expected in current_authority.items():
        if manifest[field] != expected:
            raise CheckpointStaleError(f"checkpoint {field} is stale")

    return RestoredCheckpoint(
        checkpoint_ref=checkpoint_ref,
        manifest=copy.deepcopy(manifest),
        snapshot=copy.deepcopy(snapshot),
    )
