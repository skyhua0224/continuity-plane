"""Deterministic M3-07 Idea review and correction-protection operations."""

from __future__ import annotations

import copy
import hashlib
import re
import unicodedata
from datetime import datetime
from typing import Any

from .typed_state import (
    DURABLE_EFFECT_SCHEMA_VERSION,
    EXPERIMENT_LIFECYCLE_SCHEMA_VERSION,
    IDEA_REVIEW_SCHEMA_VERSION,
    TypedStateError,
    canonical_state_bytes,
    validate_typed_state,
)

_SOURCE_REF_RE = re.compile(r"^rng_[a-z2-7]{26}$")
_URGENCIES = {"immediate", "next", "later", "review-date"}
_IMPACTS = {"none", "low", "medium", "high"}
_REVIEW_DECISIONS = {"keep", "park", "reject", "supersede", "approve"}
_RELATIONSHIP_KINDS = {"related", "depends-on", "duplicates", "supersedes", "blocks"}
MIGRATION_RECEIPT_SCHEMA_VERSION = "context.typed-state-migration-receipt/v1alpha1"
MIGRATION_ALGORITHM_ID = "context.idea-review.v3-to-v4/v1"
MIGRATION_ALGORITHM_SHA256 = hashlib.sha256(
    MIGRATION_ALGORITHM_ID.encode("ascii")
).hexdigest()
_MIGRATION_RECEIPT_FIELDS = {
    "schema_version",
    "migration_id",
    "project_id",
    "from_schema_version",
    "to_schema_version",
    "source_revision",
    "source_event_head_sha256",
    "source_snapshot_sha256",
    "target_snapshot_sha256",
    "algorithm_id",
    "algorithm_sha256",
    "registry_digest",
    "authorization_ref",
    "status",
    "migrated_at",
}


class IdeaReviewError(ValueError):
    """Raised when an M3-07 Idea operation violates typed state authority."""


def _require_v4(snapshot: dict[str, Any]) -> None:
    if snapshot.get("schema_version") not in {
        IDEA_REVIEW_SCHEMA_VERSION,
        DURABLE_EFFECT_SCHEMA_VERSION,
    }:
        raise IdeaReviewError("M3-07 requires typed-state v4alpha1 or later")
    try:
        validate_typed_state(snapshot)
    except TypedStateError as exc:
        raise IdeaReviewError("typed state is invalid") from exc


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise IdeaReviewError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IdeaReviewError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise IdeaReviewError(f"{field} must include a timezone")
    return parsed


def _idea(snapshot: dict[str, Any], idea_id: str) -> dict[str, Any]:
    idea = next((item for item in snapshot["ideas"] if item["idea_id"] == idea_id), None)
    if idea is None:
        raise IdeaReviewError("unknown Idea")
    return idea


def _all_ids(snapshot: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for collection in (
        "works",
        "claims",
        "ideas",
        "decisions",
        "constraints",
        "evidence",
        "blockers",
        "effects",
        "experiment_attempts",
        "experiment_promotions",
        "idea_relationships",
        "idea_occurrences",
        "idea_reviews",
        "correction_protections",
    ):
        ids.update(
            item[field]
            for item in snapshot.get(collection, [])
            for field in (
                "work_id",
                "claim_id",
                "idea_id",
                "decision_id",
                "constraint_id",
                "evidence_id",
                "blocker_id",
                "effect_id",
                "attempt_id",
                "promotion_id",
                "relationship_id",
                "occurrence_id",
                "review_id",
                "protection_id",
            )
            if field in item
        )
    return ids


def _normalize_summary(summary: str) -> str:
    if not isinstance(summary, str) or not summary.strip():
        raise IdeaReviewError("summary must be non-empty")
    normalized = unicodedata.normalize("NFKC", summary)
    normalized = " ".join(normalized.casefold().split())
    return normalized.rstrip(".?!。！？")


def compute_idea_dedupe_key(*, parent_work_id: str, scope_ref: str, summary: str) -> str:
    """Return a deterministic exact-match key for one Idea envelope."""
    if not isinstance(parent_work_id, str) or not parent_work_id.strip():
        raise IdeaReviewError("parent_work_id must be non-empty")
    if not isinstance(scope_ref, str) or not scope_ref.strip():
        raise IdeaReviewError("scope_ref must be non-empty")
    canonical = "\x1f".join(
        (parent_work_id.strip(), scope_ref.strip(), _normalize_summary(summary))
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_source_ref(source_ref: str) -> None:
    if not isinstance(source_ref, str) or not _SOURCE_REF_RE.fullmatch(source_ref):
        raise IdeaReviewError("source_ref must be an opaque source range")


def _validate_urgency(urgency: str, review_at: str | None) -> None:
    if urgency not in _URGENCIES:
        raise IdeaReviewError("urgency is unsupported")
    if urgency == "review-date" and review_at is None:
        raise IdeaReviewError("review-date urgency requires review_at")
    if review_at is not None:
        _timestamp(review_at, "review_at")


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_state_bytes(snapshot)).hexdigest()


def _validate_sha256(value: Any, field: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise IdeaReviewError(f"{field} must be lowercase SHA-256")


def migrate_typed_state_v3_to_v4(
    snapshot: dict[str, Any], *, migrated_at: str
) -> dict[str, Any]:
    """Add deterministic Idea review collections to a v3 snapshot."""
    if snapshot.get("schema_version") != EXPERIMENT_LIFECYCLE_SCHEMA_VERSION:
        raise IdeaReviewError("migration source must be typed-state v3alpha1")
    _timestamp(migrated_at, "migrated_at")
    validate_typed_state(snapshot)
    migrated = copy.deepcopy(snapshot)
    migrated["schema_version"] = IDEA_REVIEW_SCHEMA_VERSION
    migrated.update(
        {
            "idea_relationships": [],
            "idea_occurrences": [],
            "idea_reviews": [],
            "correction_protections": [],
        }
    )
    for idea in migrated["ideas"]:
        # v3 has no capture basis that can safely be merged. Keep every legacy
        # Idea distinct until an explicit v4 observation establishes a match.
        idea["dedupe_key"] = "sha256:" + hashlib.sha256(
            f"legacy-v3\x1f{idea['idea_id']}".encode()
        ).hexdigest()
        idea["scope_ref"] = f"work://{idea['parent_work_id']}"
        idea["urgency"] = "later"
        idea["review_at"] = None
        idea["created_at"] = None
        idea["revision"] = 1
        occurrence_id = "occ_" + hashlib.sha256(
            f"{idea['idea_id']}\x1f{idea['source_ref']}".encode()
        ).hexdigest()[:26]
        migrated["idea_occurrences"].append(
            {
                "occurrence_id": occurrence_id,
                "idea_id": idea["idea_id"],
                "submitted_idea_id": idea["idea_id"],
                "source_ref": idea["source_ref"],
                "dedupe_key": idea["dedupe_key"],
                "observed_at": None,
                "actor_ref": "migration://legacy-v3",
                "request_sha256": None,
                "origin": "legacy-v3",
            }
        )
    try:
        validate_typed_state(migrated)
    except TypedStateError as exc:
        raise IdeaReviewError("v3 to v4 migration produced invalid state") from exc
    return migrated


def build_typed_state_v3_to_v4_migration_receipt(
    *,
    source: dict[str, Any],
    target: dict[str, Any],
    migration_id: str,
    source_event_head_sha256: str | None,
    registry_digest: str,
    authorization_ref: str,
    migrated_at: str,
) -> dict[str, Any]:
    """Bind a v3-to-v4 snapshot boundary to its authority and inputs."""
    if source.get("schema_version") != EXPERIMENT_LIFECYCLE_SCHEMA_VERSION:
        raise IdeaReviewError("migration source must be typed-state v3alpha1")
    if target.get("schema_version") != IDEA_REVIEW_SCHEMA_VERSION:
        raise IdeaReviewError("migration target must be typed-state v4alpha1")
    validate_typed_state(source)
    _require_v4(target)
    _timestamp(migrated_at, "migrated_at")
    if not isinstance(migration_id, str) or not migration_id.strip():
        raise IdeaReviewError("migration_id must be non-empty")
    if not isinstance(authorization_ref, str) or not authorization_ref.strip():
        raise IdeaReviewError("authorization_ref must be non-empty")
    _validate_sha256(source_event_head_sha256, "source_event_head_sha256", optional=True)
    _validate_sha256(registry_digest, "registry_digest")
    expected_target = migrate_typed_state_v3_to_v4(source, migrated_at=migrated_at)
    if canonical_state_bytes(expected_target) != canonical_state_bytes(target):
        raise IdeaReviewError("migration target does not match deterministic v3 to v4 result")
    return {
        "schema_version": MIGRATION_RECEIPT_SCHEMA_VERSION,
        "migration_id": migration_id,
        "project_id": source["project"]["project_id"],
        "from_schema_version": EXPERIMENT_LIFECYCLE_SCHEMA_VERSION,
        "to_schema_version": IDEA_REVIEW_SCHEMA_VERSION,
        "source_revision": source["project"]["revision"],
        "source_event_head_sha256": source_event_head_sha256,
        "source_snapshot_sha256": _snapshot_sha256(source),
        "target_snapshot_sha256": _snapshot_sha256(target),
        "algorithm_id": MIGRATION_ALGORITHM_ID,
        "algorithm_sha256": MIGRATION_ALGORITHM_SHA256,
        "registry_digest": registry_digest,
        "authorization_ref": authorization_ref,
        "status": "committed",
        "migrated_at": migrated_at,
    }


def validate_typed_state_v3_to_v4_migration_receipt(
    receipt: dict[str, Any], *, source: dict[str, Any], target: dict[str, Any]
) -> None:
    """Reject a receipt that does not exactly bind the migration boundary."""
    if not isinstance(receipt, dict) or set(receipt) != _MIGRATION_RECEIPT_FIELDS:
        raise IdeaReviewError("migration receipt fields do not match the contract")
    if receipt["schema_version"] != MIGRATION_RECEIPT_SCHEMA_VERSION:
        raise IdeaReviewError("migration receipt schema_version is unsupported")
    expected = build_typed_state_v3_to_v4_migration_receipt(
        source=source,
        target=target,
        migration_id=receipt["migration_id"],
        source_event_head_sha256=receipt["source_event_head_sha256"],
        registry_digest=receipt["registry_digest"],
        authorization_ref=receipt["authorization_ref"],
        migrated_at=receipt["migrated_at"],
    )
    for field, value in expected.items():
        if receipt[field] != value:
            label = "target snapshot" if field == "target_snapshot_sha256" else field
            raise IdeaReviewError(f"migration receipt {label} mismatch")


def rollback_typed_state_v4_to_v3(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Downgrade only a v4 snapshot with no M3-07 records."""
    _require_v4(snapshot)
    has_non_legacy_occurrence = any(
        occurrence["origin"] != "legacy-v3"
        for occurrence in snapshot["idea_occurrences"]
    )
    if has_non_legacy_occurrence or any(
        snapshot[collection]
        for collection in (
            "idea_relationships",
            "idea_reviews",
            "correction_protections",
        )
    ):
        raise IdeaReviewError("v4 rollback would discard Idea review records")
    downgraded = copy.deepcopy(snapshot)
    downgraded["schema_version"] = EXPERIMENT_LIFECYCLE_SCHEMA_VERSION
    for idea in downgraded["ideas"]:
        for field in ("dedupe_key", "scope_ref", "urgency", "review_at", "created_at", "revision"):
            idea.pop(field, None)
    for collection in (
        "idea_relationships",
        "idea_occurrences",
        "idea_reviews",
        "correction_protections",
    ):
        downgraded.pop(collection, None)
    validate_typed_state(downgraded)
    return downgraded


def upsert_idea_observation(
    snapshot: dict[str, Any],
    *,
    idea_id: str,
    parent_work_id: str,
    return_work_id: str,
    source_ref: str,
    summary: str,
    scope_ref: str,
    urgency: str,
    review_at: str | None,
    occurrence_id: str,
    observed_at: str,
    actor_ref: str = "actor://local",
    request_sha256: str | None = None,
) -> dict[str, Any]:
    """Create one Idea or append an immutable occurrence to its dedupe match."""
    _require_v4(snapshot)
    _validate_source_ref(source_ref)
    observed = _timestamp(observed_at, "observed_at")
    _validate_urgency(urgency, review_at)
    if return_work_id not in {item["work_id"] for item in snapshot["works"]}:
        raise IdeaReviewError("return_work_id is unknown")
    if parent_work_id not in {item["work_id"] for item in snapshot["works"]}:
        raise IdeaReviewError("parent_work_id is unknown")
    dedupe_key = compute_idea_dedupe_key(
        parent_work_id=parent_work_id, scope_ref=scope_ref, summary=summary
    )
    if not isinstance(actor_ref, str) or not actor_ref.strip():
        raise IdeaReviewError("actor_ref must be non-empty")
    if request_sha256 is None:
        request_sha256 = hashlib.sha256(
            f"{idea_id}\x1f{occurrence_id}\x1f{source_ref}\x1f{observed.isoformat()}".encode()
        ).hexdigest()
    _validate_sha256(request_sha256, "request_sha256")
    candidate = copy.deepcopy(snapshot)
    occurrence = {
        "occurrence_id": occurrence_id,
        "idea_id": idea_id,
        "submitted_idea_id": idea_id,
        "source_ref": source_ref,
        "dedupe_key": dedupe_key,
        "observed_at": observed.isoformat(),
        "actor_ref": actor_ref,
        "request_sha256": request_sha256,
        "origin": "capture",
    }
    existing_occurrence = next(
        (item for item in candidate["idea_occurrences"] if item["occurrence_id"] == occurrence_id),
        None,
    )
    if existing_occurrence is not None:
        if existing_occurrence != occurrence:
            raise IdeaReviewError("occurrence is immutable")
        return candidate
    existing = next(
        (item for item in candidate["ideas"] if item["dedupe_key"] == dedupe_key),
        None,
    )
    if existing is None:
        if idea_id in _all_ids(candidate):
            raise IdeaReviewError("Idea identity already exists")
        idea = {
            "idea_id": idea_id,
            "parent_work_id": parent_work_id,
            "source_ref": source_ref,
            "summary": summary.strip(),
            "status": "candidate",
            "return_work_id": return_work_id,
            "expiry": None,
            "attempt_budget": None,
            "promotion_target": None,
            "evidence_ids": [],
            "dedupe_key": dedupe_key,
            "scope_ref": scope_ref.strip(),
            "urgency": urgency,
            "review_at": review_at,
            "created_at": observed.isoformat(),
            "revision": 1,
        }
        candidate["ideas"].append(idea)
        occurrence["idea_id"] = idea_id
    else:
        occurrence["idea_id"] = existing["idea_id"]
        occurrence["dedupe_key"] = existing["dedupe_key"]
    if occurrence["occurrence_id"] in _all_ids(candidate):
        raise IdeaReviewError("occurrence identity already exists")
    candidate["idea_occurrences"].append(occurrence)
    validate_typed_state(candidate)
    return candidate


def append_idea_occurrence(
    snapshot: dict[str, Any],
    *,
    occurrence_id: str,
    idea_id: str,
    source_ref: str,
    observed_at: str,
    submitted_idea_id: str | None = None,
    actor_ref: str = "actor://local",
    request_sha256: str | None = None,
) -> dict[str, Any]:
    _require_v4(snapshot)
    _idea(snapshot, idea_id)
    _validate_source_ref(source_ref)
    observed = _timestamp(observed_at, "observed_at")
    if submitted_idea_id is None:
        submitted_idea_id = idea_id
    if not isinstance(submitted_idea_id, str) or not submitted_idea_id.strip():
        raise IdeaReviewError("submitted_idea_id must be non-empty")
    if not isinstance(actor_ref, str) or not actor_ref.strip():
        raise IdeaReviewError("actor_ref must be non-empty")
    if request_sha256 is None:
        request_sha256 = hashlib.sha256(
            f"{submitted_idea_id}\x1f{occurrence_id}\x1f{source_ref}\x1f{observed.isoformat()}".encode()
        ).hexdigest()
    _validate_sha256(request_sha256, "request_sha256")
    idea = _idea(snapshot, idea_id)
    occurrence = {
        "occurrence_id": occurrence_id,
        "idea_id": idea_id,
        "submitted_idea_id": submitted_idea_id,
        "source_ref": source_ref,
        "dedupe_key": idea["dedupe_key"],
        "observed_at": observed.isoformat(),
        "actor_ref": actor_ref,
        "request_sha256": request_sha256,
        "origin": "capture",
    }
    candidate = copy.deepcopy(snapshot)
    existing = next(
        (item for item in candidate["idea_occurrences"] if item["occurrence_id"] == occurrence_id),
        None,
    )
    if existing is not None:
        if existing != occurrence:
            raise IdeaReviewError("occurrence is immutable")
        return candidate
    if occurrence_id in _all_ids(candidate):
        raise IdeaReviewError("occurrence identity already exists")
    candidate["idea_occurrences"].append(occurrence)
    validate_typed_state(candidate)
    return candidate


def _relationship_graph(snapshot: dict[str, Any]) -> dict[str, set[str]]:
    graph = {item["idea_id"]: set() for item in snapshot["ideas"]}
    for relationship in snapshot["idea_relationships"]:
        graph[relationship["source_idea_id"]].add(relationship["target_idea_id"])
    return graph


def add_idea_relationship(
    snapshot: dict[str, Any],
    *,
    relationship_id: str,
    source_idea_id: str,
    target_idea_id: str,
    relationship_kind: str,
    evidence_ids: list[str],
    created_at: str,
) -> dict[str, Any]:
    _require_v4(snapshot)
    if source_idea_id == target_idea_id:
        raise IdeaReviewError("relationship self edge is invalid")
    _idea(snapshot, source_idea_id)
    _idea(snapshot, target_idea_id)
    if relationship_kind not in _RELATIONSHIP_KINDS:
        raise IdeaReviewError("relationship kind is unsupported")
    _timestamp(created_at, "created_at")
    if relationship_id in _all_ids(snapshot):
        raise IdeaReviewError("duplicate relationship")
    key = (source_idea_id, target_idea_id, relationship_kind)
    if any(
        (item["source_idea_id"], item["target_idea_id"], item["relationship_kind"]) == key
        for item in snapshot["idea_relationships"]
    ):
        raise IdeaReviewError("duplicate relationship")
    candidate = copy.deepcopy(snapshot)
    candidate["idea_relationships"].append(
        {
            "relationship_id": relationship_id,
            "source_idea_id": source_idea_id,
            "target_idea_id": target_idea_id,
            "relationship_kind": relationship_kind,
            "evidence_ids": list(evidence_ids),
            "created_at": created_at,
        }
    )
    graph = _relationship_graph(candidate)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise IdeaReviewError("relationship cycle is invalid")
        if node in visited:
            return
        visiting.add(node)
        for target in graph[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for idea_id in graph:
        visit(idea_id)
    validate_typed_state(candidate)
    return candidate


def apply_idea_review(
    snapshot: dict[str, Any],
    *,
    idea_id: str,
    review_id: str,
    reviewer_ref: str,
    decision: str,
    urgency: str,
    impact: str,
    review_at: str | None,
    evidence_ids: list[str],
    reviewed_at: str,
) -> dict[str, Any]:
    _require_v4(snapshot)
    idea = _idea(snapshot, idea_id)
    if idea["status"] in {"rejected", "superseded", "expired"}:
        raise IdeaReviewError("terminal Idea cannot be reviewed or reopened")
    if review_id in _all_ids(snapshot):
        raise IdeaReviewError("duplicate review")
    if decision not in _REVIEW_DECISIONS:
        raise IdeaReviewError("review decision is unsupported")
    if not isinstance(reviewer_ref, str) or not reviewer_ref.strip():
        raise IdeaReviewError("reviewer_ref must be non-empty")
    if impact not in _IMPACTS:
        raise IdeaReviewError("impact is unsupported")
    _validate_urgency(urgency, review_at)
    reviewed = _timestamp(reviewed_at, "reviewed_at")
    candidate = copy.deepcopy(snapshot)
    current = next(item for item in candidate["ideas"] if item["idea_id"] == idea_id)
    current["urgency"] = urgency
    current["review_at"] = review_at
    current["revision"] += 1
    current["status"] = {
        "keep": "candidate",
        "park": "parked",
        "reject": "rejected",
        "supersede": "superseded",
        "approve": "approved",
    }[decision]
    candidate["idea_reviews"].append(
        {
            "review_id": review_id,
            "idea_id": idea_id,
            "reviewer_ref": reviewer_ref,
            "decision": decision,
            "urgency": urgency,
            "impact": impact,
            "review_at": review_at,
            "evidence_ids": list(evidence_ids),
            "reviewed_at": reviewed.isoformat(),
        }
    )
    validate_typed_state(candidate)
    return candidate


def open_correction_protection(
    snapshot: dict[str, Any],
    *,
    protection_id: str,
    idea_id: str,
    affected_work_ids: list[str],
    affected_scope_refs: list[str],
    reason: str,
    evidence_ids: list[str],
    opened_at: str,
    opened_by_ref: str = "actor://local",
) -> dict[str, Any]:
    _require_v4(snapshot)
    _idea(snapshot, idea_id)
    if protection_id in _all_ids(snapshot):
        raise IdeaReviewError("duplicate correction protection")
    if not affected_work_ids or not affected_scope_refs:
        raise IdeaReviewError("correction protection scope is empty")
    known_work_ids = {item["work_id"] for item in snapshot["works"]}
    if any(work_id not in known_work_ids for work_id in affected_work_ids):
        raise IdeaReviewError("correction protection work is unknown")
    if not isinstance(opened_by_ref, str) or not opened_by_ref.strip():
        raise IdeaReviewError("opened_by_ref must be non-empty")
    _timestamp(opened_at, "opened_at")
    candidate = copy.deepcopy(snapshot)
    candidate["correction_protections"].append(
        {
            "protection_id": protection_id,
            "idea_id": idea_id,
            "status": "active",
            "affected_work_ids": list(affected_work_ids),
            "affected_scope_refs": list(affected_scope_refs),
            "reason": reason,
            "evidence_ids": list(evidence_ids),
            "opened_by_ref": opened_by_ref,
            "opened_at": opened_at,
            "released_by_ref": None,
            "release_reason": None,
            "release_evidence_ids": [],
            "released_at": None,
        }
    )
    validate_typed_state(candidate)
    return candidate


def release_correction_protection(
    snapshot: dict[str, Any],
    *,
    protection_id: str,
    idea_id: str,
    released_by_ref: str,
    release_reason: str,
    release_evidence_ids: list[str],
    released_at: str,
) -> dict[str, Any]:
    _require_v4(snapshot)
    _idea(snapshot, idea_id)
    protection = next(
        (
            item
            for item in snapshot["correction_protections"]
            if item["protection_id"] == protection_id
        ),
        None,
    )
    if protection is None or protection["idea_id"] != idea_id:
        raise IdeaReviewError("correction protection is unknown")
    if protection["status"] != "active":
        raise IdeaReviewError("correction protection is not active")
    if not isinstance(released_by_ref, str) or not released_by_ref.strip():
        raise IdeaReviewError("released_by_ref must be non-empty")
    if not isinstance(release_reason, str) or not release_reason.strip():
        raise IdeaReviewError("release_reason must be non-empty")
    if not release_evidence_ids:
        raise IdeaReviewError("release requires verified evidence")
    released = _timestamp(released_at, "released_at")
    evidence_by_id = {item["evidence_id"]: item for item in snapshot["evidence"]}
    if any(
        evidence_id not in evidence_by_id
        or evidence_by_id[evidence_id]["validity"] != "verified"
        or evidence_by_id[evidence_id]["verified_at"] is None
        or _timestamp(
            evidence_by_id[evidence_id]["observed_at"], "evidence.observed_at"
        )
        > released
        or _timestamp(
            evidence_by_id[evidence_id]["verified_at"], "evidence.verified_at"
        )
        > released
        for evidence_id in release_evidence_ids
    ):
        raise IdeaReviewError("release requires verified evidence")
    if released < _timestamp(protection["opened_at"], "opened_at"):
        raise IdeaReviewError("release precedes correction protection")
    candidate = copy.deepcopy(snapshot)
    current = next(
        item
        for item in candidate["correction_protections"]
        if item["protection_id"] == protection_id
    )
    current.update(
        {
            "status": "released",
            "released_by_ref": released_by_ref,
            "release_reason": release_reason.strip(),
            "release_evidence_ids": list(release_evidence_ids),
            "released_at": released.isoformat(),
        }
    )
    validate_typed_state(candidate)
    return candidate


def evaluate_correction_write_gate(
    snapshot: dict[str, Any], changes: list[dict[str, Any]]
) -> dict[str, Any]:
    """Deny only changes intersecting an active correction protection."""
    _require_v4(snapshot)
    protected_work_ids = {
        work_id
        for protection in snapshot["correction_protections"]
        if protection["status"] == "active"
        for work_id in protection["affected_work_ids"]
    }
    protected_scope_refs = {
        scope_ref
        for protection in snapshot["correction_protections"]
        if protection["status"] == "active"
        for scope_ref in protection["affected_scope_refs"]
    }
    work_ref_fields = {
        "work_id",
        "parent_work_id",
        "return_work_id",
        "return_point_work_id",
        "promotion_target_work_id",
        "target_work_id",
    }
    work_ref_list_fields = {
        "active_work_ids",
        "affected_work_ids",
        "blocked_work_ids",
        "dependency_ids",
        "scope_work_ids",
    }

    def scope_key(value: Any) -> str | None:
        if isinstance(value, str) and ":" in value:
            return value
        if isinstance(value, dict):
            kind = value.get("scope_kind")
            ref = value.get("scope_ref")
            if isinstance(kind, str) and kind and isinstance(ref, str) and ref:
                return f"{kind}:{ref}"
        return None

    for change in changes:
        value = change.get("value", {})
        if change.get("collection") == "works" and change.get("object_id") in protected_work_ids:
            return {"decision": "deny", "reason": "correction_protection"}
        direct_work_refs = {
            value.get(field)
            for field in work_ref_fields
            if isinstance(value.get(field), str)
        }
        listed_work_refs: set[str] = set()
        for field in work_ref_list_fields:
            field_value = value.get(field, [])
            if isinstance(field_value, list):
                listed_work_refs.update(
                    work_id for work_id in field_value if isinstance(work_id, str)
                )
        if protected_work_ids.intersection(direct_work_refs | listed_work_refs):
            return {"decision": "deny", "reason": "correction_protection"}
        scopes = [value.get("scope_ref")]
        for field in ("scope_refs", "scope_owners", "affected_scope_refs"):
            field_value = value.get(field, [])
            if isinstance(field_value, list):
                scopes.extend(field_value)
        scope_refs = {key for item in scopes if (key := scope_key(item)) is not None}
        if protected_scope_refs.intersection(scope_refs):
            return {"decision": "deny", "reason": "correction_protection"}
    return {"decision": "allow", "reason": "no_affected_protection"}


def packet_eligible_ideas(snapshot: dict[str, Any], *, now: str) -> list[str]:
    _require_v4(snapshot)
    current = _timestamp(now, "now")
    eligible: list[str] = []
    for idea in snapshot["ideas"]:
        if idea["status"] not in {"candidate", "proposed", "approved"}:
            continue
        if idea["expiry"] is not None and _timestamp(idea["expiry"], "idea.expiry") <= current:
            continue
        if (
            idea["urgency"] == "review-date"
            and idea["review_at"] is not None
            and _timestamp(idea["review_at"], "idea.review_at") > current
        ):
            continue
        eligible.append(idea["idea_id"])
    return sorted(eligible)
