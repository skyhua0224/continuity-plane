"""Deterministic typed-state v4/v5 migration and rollback receipts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .typed_state import TypedStateError, validate_typed_state

V4_SCHEMA_VERSION = "context.typed-state/v4alpha1"
V5_SCHEMA_VERSION = "context.typed-state/v5alpha1"
MIGRATION_RECEIPT_SCHEMA_VERSION = (
    "context.typed-state-v4-v5-migration-receipt/v1alpha1"
)
MIGRATION_ALGORITHM_ID = "context.typed-state.effect-request-digest.v4-v5/v1"
MIGRATION_ALGORITHM_SHA256 = hashlib.sha256(
    MIGRATION_ALGORITHM_ID.encode("ascii")
).hexdigest()

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_EVENT_HEAD_FIELDS = {"sequence_no", "event_sha256"}
_RECEIPT_FIELDS = {
    "schema_version",
    "migration_id",
    "project_id",
    "direction",
    "from_schema_version",
    "to_schema_version",
    "source_revision",
    "source_event_head",
    "source_snapshot_sha256",
    "target_snapshot_sha256",
    "algorithm_id",
    "algorithm_sha256",
    "registry_digest",
    "authorization_ref",
    "status",
    "migrated_at",
    "receipt_sha256",
}


class DurableStateMigrationError(ValueError):
    """Raised when the v4/v5 state boundary cannot be proven losslessly."""


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
        raise DurableStateMigrationError("migration data is not canonical JSON") from exc


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise DurableStateMigrationError(f"{field} must be lowercase SHA-256")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DurableStateMigrationError(f"{field} is invalid")
    return value


def _reference(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2048
        or any(character in value for character in "\r\n\x00")
    ):
        raise DurableStateMigrationError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise DurableStateMigrationError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DurableStateMigrationError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DurableStateMigrationError(f"{field} must include a timezone")
    return value


def _event_head(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _EVENT_HEAD_FIELDS:
        raise DurableStateMigrationError("source_event_head fields are invalid")
    sequence_no = value["sequence_no"]
    event_sha256 = value["event_sha256"]
    if type(sequence_no) is not int or sequence_no < 0:
        raise DurableStateMigrationError("source_event_head sequence is invalid")
    if sequence_no == 0:
        if event_sha256 is not None:
            raise DurableStateMigrationError(
                "genesis source_event_head cannot contain an event digest"
            )
    elif event_sha256 is None:
        raise DurableStateMigrationError(
            "non-genesis source_event_head requires an event digest"
        )
    else:
        _sha256(event_sha256, "source_event_head.event_sha256")
    return copy.deepcopy(value)


def _validate_v4(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != V4_SCHEMA_VERSION:
        raise DurableStateMigrationError("migration source must be typed-state v4alpha1")
    effects = snapshot.get("effects")
    if not isinstance(effects, list) or any(
        not isinstance(effect, dict) or "request_sha256" in effect for effect in effects
    ):
        raise DurableStateMigrationError(
            "typed-state v4 effects cannot contain request_sha256"
        )
    try:
        validate_typed_state(snapshot)
    except (TypedStateError, TypeError, ValueError) as exc:
        raise DurableStateMigrationError("typed-state v4 snapshot is invalid") from exc
    return copy.deepcopy(snapshot)


def _validate_v5(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != V5_SCHEMA_VERSION:
        raise DurableStateMigrationError("migration target must be typed-state v5alpha1")
    effects = snapshot.get("effects")
    if not isinstance(effects, list):
        raise DurableStateMigrationError("typed-state v5 effects are invalid")
    downgraded = copy.deepcopy(snapshot)
    downgraded["schema_version"] = V4_SCHEMA_VERSION
    for source_effect, downgraded_effect in zip(effects, downgraded["effects"], strict=True):
        if not isinstance(source_effect, dict) or "request_sha256" not in source_effect:
            raise DurableStateMigrationError(
                "typed-state v5 effect requires request_sha256"
            )
        request_sha256 = source_effect["request_sha256"]
        if request_sha256 is not None:
            _sha256(request_sha256, "effect.request_sha256")
        downgraded_effect.pop("request_sha256")
    try:
        validate_typed_state(downgraded)
    except (TypedStateError, TypeError, ValueError) as exc:
        raise DurableStateMigrationError("typed-state v5 snapshot is invalid") from exc
    return copy.deepcopy(snapshot)


def canonical_typed_state_migration_bytes(snapshot: dict[str, Any]) -> bytes:
    """Return canonical bytes for a validated v4 or locally validated v5 snapshot."""
    version = snapshot.get("schema_version") if isinstance(snapshot, dict) else None
    if version == V4_SCHEMA_VERSION:
        normalized = _validate_v4(snapshot)
    elif version == V5_SCHEMA_VERSION:
        normalized = _validate_v5(snapshot)
    else:
        raise DurableStateMigrationError("typed-state migration version is unsupported")
    return _canonical(normalized)


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_typed_state_migration_bytes(snapshot)).hexdigest()


def migrate_typed_state_v4_to_v5(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Add a nullable request digest to every Effect without inferring payloads."""
    if isinstance(snapshot, dict) and snapshot.get("schema_version") == V5_SCHEMA_VERSION:
        return json.loads(canonical_typed_state_migration_bytes(snapshot))
    migrated = _validate_v4(snapshot)
    migrated["schema_version"] = V5_SCHEMA_VERSION
    for effect in migrated["effects"]:
        effect["request_sha256"] = None
    return json.loads(canonical_typed_state_migration_bytes(migrated))


def rollback_typed_state_v5_to_v4(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Remove the digest field only while no Effect has acquired a payload binding."""
    if isinstance(snapshot, dict) and snapshot.get("schema_version") == V4_SCHEMA_VERSION:
        return json.loads(canonical_typed_state_migration_bytes(snapshot))
    current = _validate_v5(snapshot)
    if any(effect["request_sha256"] is not None for effect in current["effects"]):
        raise DurableStateMigrationError(
            "typed-state v5 rollback would discard an Effect request digest"
        )
    current["schema_version"] = V4_SCHEMA_VERSION
    for effect in current["effects"]:
        del effect["request_sha256"]
    return json.loads(canonical_typed_state_migration_bytes(current))


def _expected_target(source: dict[str, Any], target: dict[str, Any]) -> tuple[str, str, str]:
    source_version = source.get("schema_version") if isinstance(source, dict) else None
    target_version = target.get("schema_version") if isinstance(target, dict) else None
    if (source_version, target_version) == (V4_SCHEMA_VERSION, V5_SCHEMA_VERSION):
        expected = migrate_typed_state_v4_to_v5(source)
        direction = "upgrade"
    elif (source_version, target_version) == (V5_SCHEMA_VERSION, V4_SCHEMA_VERSION):
        expected = rollback_typed_state_v5_to_v4(source)
        direction = "rollback"
    else:
        raise DurableStateMigrationError("typed-state migration direction is unsupported")
    if canonical_typed_state_migration_bytes(expected) != canonical_typed_state_migration_bytes(
        target
    ):
        raise DurableStateMigrationError(
            "migration target does not match the deterministic result"
        )
    return direction, source_version, target_version


def _receipt_digest(receipt: dict[str, Any]) -> str:
    body = copy.deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def build_durable_state_migration_receipt(
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    migration_id: str,
    source_event_head: dict[str, Any],
    registry_digest: str,
    authorization_ref: str,
    migrated_at: str,
) -> dict[str, Any]:
    """Bind one deterministic upgrade or rollback to its external authority cursor."""
    direction, source_version, target_version = _expected_target(source, target)
    migration_id = _identifier(migration_id, "migration_id")
    event_head = _event_head(source_event_head)
    registry_digest = _sha256(registry_digest, "registry_digest")
    authorization_ref = _reference(authorization_ref, "authorization_ref")
    migrated_at = _timestamp(migrated_at, "migrated_at")
    project = source.get("project")
    target_project = target.get("project")
    if not isinstance(project, dict) or not isinstance(target_project, dict):
        raise DurableStateMigrationError("migration project projection is invalid")
    revision = project.get("revision")
    if type(revision) is not int or revision < 0 or target_project.get("revision") != revision:
        raise DurableStateMigrationError("migration project revision is invalid")
    project_id = _identifier(project.get("project_id"), "project_id")
    if target_project.get("project_id") != project_id:
        raise DurableStateMigrationError("migration project identity changed")
    receipt = {
        "schema_version": MIGRATION_RECEIPT_SCHEMA_VERSION,
        "migration_id": migration_id,
        "project_id": project_id,
        "direction": direction,
        "from_schema_version": source_version,
        "to_schema_version": target_version,
        "source_revision": revision,
        "source_event_head": event_head,
        "source_snapshot_sha256": _snapshot_sha256(source),
        "target_snapshot_sha256": _snapshot_sha256(target),
        "algorithm_id": MIGRATION_ALGORITHM_ID,
        "algorithm_sha256": MIGRATION_ALGORITHM_SHA256,
        "registry_digest": registry_digest,
        "authorization_ref": authorization_ref,
        "status": "committed",
        "migrated_at": migrated_at,
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    return receipt


def validate_durable_state_migration_receipt(
    receipt: Any,
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    expected_source_event_head: dict[str, Any],
    expected_registry_digest: str,
    expected_authorization_ref: str,
) -> dict[str, Any]:
    """Validate a receipt against caller-supplied authority, not self-signed fields."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise DurableStateMigrationError("migration receipt fields are invalid")
    if receipt["schema_version"] != MIGRATION_RECEIPT_SCHEMA_VERSION:
        raise DurableStateMigrationError("migration receipt version is unsupported")
    _sha256(receipt["receipt_sha256"], "receipt_sha256")
    if receipt["receipt_sha256"] != _receipt_digest(receipt):
        raise DurableStateMigrationError("migration receipt digest mismatch")
    expected = build_durable_state_migration_receipt(
        source=source,
        target=target,
        migration_id=receipt["migration_id"],
        source_event_head=expected_source_event_head,
        registry_digest=expected_registry_digest,
        authorization_ref=expected_authorization_ref,
        migrated_at=receipt["migrated_at"],
    )
    for field, value in expected.items():
        if receipt[field] != value:
            raise DurableStateMigrationError(f"migration receipt {field} mismatch")
    return copy.deepcopy(receipt)
