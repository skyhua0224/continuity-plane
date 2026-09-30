"""Deterministic typed-state v5/v6 shared-work migration boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .typed_state import TypedStateError, validate_typed_state

V5_SCHEMA_VERSION = "context.typed-state/v5alpha1"
V6_SCHEMA_VERSION = "context.typed-state/v6alpha1"
MIGRATION_RECEIPT_SCHEMA_VERSION = (
    "context.typed-state-v5-v6-migration-receipt/v1alpha1"
)
MIGRATION_ALGORITHM_ID = "context.typed-state.shared-work-lease.v5-v6/v1"
MIGRATION_ALGORITHM_SHA256 = hashlib.sha256(
    MIGRATION_ALGORITHM_ID.encode("ascii")
).hexdigest()

_WORK_DEFAULTS = {
    "work_source_ref": None,
    "source_revision": 0,
    "work_identity_sha256": None,
    "dedupe_receipt_sha256": None,
}
_CLAIM_DEFAULTS = {
    "claim_revision": 0,
    "lease_epoch": 0,
    "last_heartbeat_at": None,
    "closed_at": None,
    "closed_by_ref": None,
    "close_reason": None,
    "reclaimed_from_claim_id": None,
}
_EFFECT_DEFAULTS = {
    "lease_epoch": 0,
    "dispatch_receipt_sha256": None,
    "dispatch_started_at": None,
}
_CLOSE_REASONS = {
    "worker_release",
    "lease_expired",
    "administrative_revoke",
}
_TERMINAL_CLOSE_REASON = {
    "released": "worker_release",
    "expired": "lease_expired",
    "revoked": "administrative_revoke",
}
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


class DurableStateV6MigrationError(ValueError):
    """Raised when the v5/v6 boundary cannot be proven losslessly."""


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
        raise DurableStateV6MigrationError(
            "migration data is not canonical JSON"
        ) from exc


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise DurableStateV6MigrationError(f"{field} must be lowercase SHA-256")
    return value


def _nullable_sha256(value: Any, field: str) -> None:
    if value is not None:
        _sha256(value, field)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DurableStateV6MigrationError(f"{field} is invalid")
    return value


def _reference(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 2048
        or any(character in value for character in "\r\n\x00")
    ):
        raise DurableStateV6MigrationError(f"{field} is invalid")
    return value


def _nullable_reference(value: Any, field: str) -> None:
    if value is not None:
        _reference(value, field)


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise DurableStateV6MigrationError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DurableStateV6MigrationError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DurableStateV6MigrationError(f"{field} must include a timezone")
    return value


def _nullable_timestamp(value: Any, field: str) -> None:
    if value is not None:
        _timestamp(value, field)


def _non_negative_integer(value: Any, field: str) -> None:
    if type(value) is not int or value < 0:
        raise DurableStateV6MigrationError(f"{field} must be a non-negative integer")


def _event_head(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _EVENT_HEAD_FIELDS:
        raise DurableStateV6MigrationError("source_event_head fields are invalid")
    sequence_no = value["sequence_no"]
    event_sha256 = value["event_sha256"]
    _non_negative_integer(sequence_no, "source_event_head.sequence_no")
    if sequence_no == 0:
        if event_sha256 is not None:
            raise DurableStateV6MigrationError(
                "genesis source_event_head cannot contain an event digest"
            )
    elif event_sha256 is None:
        raise DurableStateV6MigrationError(
            "non-genesis source_event_head requires an event digest"
        )
    else:
        _sha256(event_sha256, "source_event_head.event_sha256")
    return copy.deepcopy(value)


def _validate_v5(snapshot: Any) -> dict[str, Any]:
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != V5_SCHEMA_VERSION
    ):
        raise DurableStateV6MigrationError(
            "migration source must be typed-state v5alpha1"
        )
    try:
        validate_typed_state(snapshot)
    except (TypedStateError, TypeError, ValueError) as exc:
        raise DurableStateV6MigrationError(
            "typed-state v5 snapshot is invalid"
        ) from exc
    return copy.deepcopy(snapshot)


def _records(snapshot: dict[str, Any], collection: str) -> list[dict[str, Any]]:
    records = snapshot.get(collection)
    if not isinstance(records, list) or any(
        not isinstance(item, dict) for item in records
    ):
        raise DurableStateV6MigrationError(
            f"typed-state v6 {collection} projection is invalid"
        )
    return records


def _require_fields(record: dict[str, Any], fields: set[str], label: str) -> None:
    missing = fields - set(record)
    if missing:
        raise DurableStateV6MigrationError(
            f"typed-state v6 {label} is missing shared-state fields"
        )


def _validate_v6(snapshot: Any) -> dict[str, Any]:
    if (
        not isinstance(snapshot, dict)
        or snapshot.get("schema_version") != V6_SCHEMA_VERSION
    ):
        raise DurableStateV6MigrationError(
            "migration target must be typed-state v6alpha1"
        )
    current = copy.deepcopy(snapshot)
    downgraded = copy.deepcopy(snapshot)
    downgraded["schema_version"] = V5_SCHEMA_VERSION

    works = _records(current, "works")
    downgraded_works = _records(downgraded, "works")
    for work, base_work in zip(works, downgraded_works, strict=True):
        _require_fields(work, set(_WORK_DEFAULTS), "Work")
        _nullable_reference(work["work_source_ref"], "work.work_source_ref")
        _non_negative_integer(work["source_revision"], "work.source_revision")
        _nullable_sha256(work["work_identity_sha256"], "work.work_identity_sha256")
        _nullable_sha256(work["dedupe_receipt_sha256"], "work.dedupe_receipt_sha256")
        for field in _WORK_DEFAULTS:
            base_work.pop(field)

    claims = _records(current, "claims")
    downgraded_claims = _records(downgraded, "claims")
    for claim, base_claim in zip(claims, downgraded_claims, strict=True):
        _require_fields(claim, set(_CLAIM_DEFAULTS), "Claim")
        _non_negative_integer(claim["claim_revision"], "claim.claim_revision")
        _non_negative_integer(claim["lease_epoch"], "claim.lease_epoch")
        _nullable_timestamp(claim["last_heartbeat_at"], "claim.last_heartbeat_at")
        _nullable_timestamp(claim["closed_at"], "claim.closed_at")
        _nullable_reference(claim["closed_by_ref"], "claim.closed_by_ref")
        close_reason = claim["close_reason"]
        if close_reason is not None and close_reason not in _CLOSE_REASONS:
            raise DurableStateV6MigrationError("claim.close_reason is invalid")
        _nullable_reference(
            claim["reclaimed_from_claim_id"], "claim.reclaimed_from_claim_id"
        )
        for field in _CLAIM_DEFAULTS:
            base_claim.pop(field)

    effects = _records(current, "effects")
    downgraded_effects = _records(downgraded, "effects")
    for effect, base_effect in zip(effects, downgraded_effects, strict=True):
        _require_fields(effect, set(_EFFECT_DEFAULTS), "Effect")
        _non_negative_integer(effect["lease_epoch"], "effect.lease_epoch")
        _nullable_sha256(
            effect["dispatch_receipt_sha256"], "effect.dispatch_receipt_sha256"
        )
        _nullable_timestamp(effect["dispatch_started_at"], "effect.dispatch_started_at")
        for field in _EFFECT_DEFAULTS:
            base_effect.pop(field)

    try:
        validate_typed_state(downgraded)
    except (TypedStateError, TypeError, ValueError) as exc:
        raise DurableStateV6MigrationError(
            "typed-state v6 snapshot is invalid"
        ) from exc
    try:
        validate_typed_state(current)
    except (TypedStateError, TypeError, ValueError) as exc:
        raise DurableStateV6MigrationError(
            "typed-state v6 shared fields are invalid"
        ) from exc
    return current


def canonical_shared_state_migration_bytes(snapshot: dict[str, Any]) -> bytes:
    """Return canonical bytes for a validated v5 or v6 snapshot."""
    version = snapshot.get("schema_version") if isinstance(snapshot, dict) else None
    if version == V5_SCHEMA_VERSION:
        normalized = _validate_v5(snapshot)
    elif version == V6_SCHEMA_VERSION:
        normalized = _validate_v6(snapshot)
    else:
        raise DurableStateV6MigrationError(
            "typed-state migration version is unsupported"
        )
    return _canonical(normalized)


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_shared_state_migration_bytes(snapshot)).hexdigest()


def migrate_typed_state_v5_to_v6(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Add shared-work fields and deterministically fence active legacy claims."""
    if (
        isinstance(snapshot, dict)
        and snapshot.get("schema_version") == V6_SCHEMA_VERSION
    ):
        return json.loads(canonical_shared_state_migration_bytes(snapshot))
    migrated = _validate_v5(snapshot)
    migrated["schema_version"] = V6_SCHEMA_VERSION
    for work in migrated["works"]:
        work.update(copy.deepcopy(_WORK_DEFAULTS))
    active_claim_ids = sorted(
        claim["claim_id"] for claim in migrated["claims"] if claim["status"] == "active"
    )
    active_epochs = {
        claim_id: index for index, claim_id in enumerate(active_claim_ids, start=1)
    }
    for claim in migrated["claims"]:
        if claim["status"] == "active":
            claim.update(
                {
                    "claim_revision": 1,
                    "lease_epoch": active_epochs[claim["claim_id"]],
                    "last_heartbeat_at": claim["claimed_at"],
                    "closed_at": None,
                    "closed_by_ref": None,
                    "close_reason": None,
                    "reclaimed_from_claim_id": None,
                }
            )
        else:
            claim.update(
                {
                    "claim_revision": 1,
                    "lease_epoch": 0,
                    "last_heartbeat_at": None,
                    "closed_at": claim["released_at"],
                    "closed_by_ref": "system/v5-v6-migration",
                    "close_reason": _TERMINAL_CLOSE_REASON[claim["status"]],
                    "reclaimed_from_claim_id": None,
                }
            )
    claim_epochs = {
        claim["claim_id"]: claim["lease_epoch"] for claim in migrated["claims"]
    }
    for effect in migrated["effects"]:
        effect.update(
            {
                "lease_epoch": claim_epochs.get(effect["claim_id"], 0),
                "dispatch_receipt_sha256": None,
                "dispatch_started_at": (
                    effect["requested_at"]
                    if effect["status"]
                    in {"started", "succeeded", "failed", "compensated"}
                    else None
                ),
            }
        )
    return json.loads(canonical_shared_state_migration_bytes(migrated))


def rollback_typed_state_v6_to_v5(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Remove v6 fields only while they match the deterministic migration image."""
    if (
        isinstance(snapshot, dict)
        and snapshot.get("schema_version") == V5_SCHEMA_VERSION
    ):
        return json.loads(canonical_shared_state_migration_bytes(snapshot))
    try:
        current = _validate_v6(snapshot)
    except DurableStateV6MigrationError as exc:
        raise DurableStateV6MigrationError(
            "typed-state v6 rollback source is invalid"
        ) from exc
    downgraded = copy.deepcopy(current)
    downgraded["schema_version"] = V5_SCHEMA_VERSION
    for collection, defaults in (
        ("works", _WORK_DEFAULTS),
        ("claims", _CLAIM_DEFAULTS),
        ("effects", _EFFECT_DEFAULTS),
    ):
        for record in downgraded[collection]:
            for field in defaults:
                del record[field]
    downgraded = _validate_v5(downgraded)
    expected = migrate_typed_state_v5_to_v6(downgraded)
    if canonical_shared_state_migration_bytes(expected) != (
        canonical_shared_state_migration_bytes(current)
    ):
        raise DurableStateV6MigrationError(
            "typed-state v6 rollback would discard shared-state facts"
        )
    for collection, defaults in (
        ("works", _WORK_DEFAULTS),
        ("claims", _CLAIM_DEFAULTS),
        ("effects", _EFFECT_DEFAULTS),
    ):
        for record in current[collection]:
            for field in defaults:
                del record[field]
    current["schema_version"] = V5_SCHEMA_VERSION
    return json.loads(canonical_shared_state_migration_bytes(current))


def _expected_target(
    source: dict[str, Any], target: dict[str, Any]
) -> tuple[str, str, str]:
    source_version = source.get("schema_version") if isinstance(source, dict) else None
    target_version = target.get("schema_version") if isinstance(target, dict) else None
    if (source_version, target_version) == (V5_SCHEMA_VERSION, V6_SCHEMA_VERSION):
        expected = migrate_typed_state_v5_to_v6(source)
        direction = "upgrade"
    elif (source_version, target_version) == (V6_SCHEMA_VERSION, V5_SCHEMA_VERSION):
        expected = rollback_typed_state_v6_to_v5(source)
        direction = "rollback"
    else:
        raise DurableStateV6MigrationError(
            "typed-state migration direction is unsupported"
        )
    if canonical_shared_state_migration_bytes(
        expected
    ) != canonical_shared_state_migration_bytes(target):
        raise DurableStateV6MigrationError(
            "migration target does not match the deterministic result"
        )
    return direction, source_version, target_version


def _receipt_digest(receipt: dict[str, Any]) -> str:
    body = copy.deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def build_typed_state_v5_to_v6_migration_receipt(
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    migration_id: str,
    source_event_head: dict[str, Any],
    registry_digest: str,
    authorization_ref: str,
    migrated_at: str,
) -> dict[str, Any]:
    """Bind a deterministic v5/v6 migration to its authority cursor."""
    direction, source_version, target_version = _expected_target(source, target)
    migration_id = _identifier(migration_id, "migration_id")
    event_head = _event_head(source_event_head)
    registry_digest = _sha256(registry_digest, "registry_digest")
    authorization_ref = _reference(authorization_ref, "authorization_ref")
    migrated_at = _timestamp(migrated_at, "migrated_at")
    project = source.get("project")
    target_project = target.get("project")
    if not isinstance(project, dict) or not isinstance(target_project, dict):
        raise DurableStateV6MigrationError("migration project projection is invalid")
    revision = project.get("revision")
    if (
        type(revision) is not int
        or revision < 0
        or target_project.get("revision") != revision
    ):
        raise DurableStateV6MigrationError("migration project revision is invalid")
    project_id = _identifier(project.get("project_id"), "project_id")
    if target_project.get("project_id") != project_id:
        raise DurableStateV6MigrationError("migration project identity changed")
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


def validate_typed_state_v5_to_v6_migration_receipt(
    receipt: Any,
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    expected_source_event_head: dict[str, Any],
    expected_registry_digest: str,
    expected_authorization_ref: str,
) -> dict[str, Any]:
    """Validate a receipt against caller-supplied authority values."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise DurableStateV6MigrationError("migration receipt fields are invalid")
    if receipt["schema_version"] != MIGRATION_RECEIPT_SCHEMA_VERSION:
        raise DurableStateV6MigrationError("migration receipt version is unsupported")
    _sha256(receipt["receipt_sha256"], "receipt_sha256")
    if receipt["receipt_sha256"] != _receipt_digest(receipt):
        raise DurableStateV6MigrationError("migration receipt digest mismatch")
    expected = build_typed_state_v5_to_v6_migration_receipt(
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
            raise DurableStateV6MigrationError(f"migration receipt {field} mismatch")
    return copy.deepcopy(receipt)
