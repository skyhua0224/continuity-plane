"""M2-01 typed state contract validation and canonical round trips."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any

from .effect_scope_gate import (
    any_scope_covers,
    scopes_overlap,
    validate_scope,
)
from .experiment_lifecycle import experiment_contract_sha256

LEGACY_SCHEMA_VERSION = "context.typed-state/v1alpha1"
SCHEMA_VERSION = "context.typed-state/v2alpha1"
EXPERIMENT_LIFECYCLE_SCHEMA_VERSION = "context.typed-state/v3alpha1"
IDEA_REVIEW_SCHEMA_VERSION = "context.typed-state/v4alpha1"
DURABLE_EFFECT_SCHEMA_VERSION = "context.typed-state/v5alpha1"
SHARED_WORK_SCHEMA_VERSION = "context.typed-state/v6alpha1"
SUPPORTED_SCHEMA_VERSIONS = {
    LEGACY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    EXPERIMENT_LIFECYCLE_SCHEMA_VERSION,
    IDEA_REVIEW_SCHEMA_VERSION,
    DURABLE_EFFECT_SCHEMA_VERSION,
    SHARED_WORK_SCHEMA_VERSION,
}

_DOCUMENT_FIELDS = {
    "schema_version",
    "project",
    "works",
    "claims",
    "ideas",
    "decisions",
    "constraints",
    "evidence",
    "blockers",
    "effects",
}
_DOCUMENT_FIELDS_V3 = _DOCUMENT_FIELDS | {
    "experiment_attempts",
    "experiment_promotions",
}
_DOCUMENT_FIELDS_V4 = _DOCUMENT_FIELDS_V3 | {
    "idea_relationships",
    "idea_occurrences",
    "idea_reviews",
    "correction_protections",
}
_PROJECT_FIELDS = {
    "project_id",
    "revision",
    "governance_ref",
    "active_work_ids",
    "primary_work_id",
    "current_decision_ids",
    "active_constraint_ids",
    "open_blocker_ids",
    "effect_high_watermark",
    "updated_at",
}
_WORK_FIELDS_V1 = {
    "work_id",
    "kind",
    "title",
    "status",
    "parent_work_id",
    "dependency_ids",
    "owner_refs",
    "scope_refs",
    "overlap_candidate_ids",
    "dedupe_status",
    "supersedes_work_id",
    "evidence_ids",
    "blocker_ids",
    "revision",
}
_WORK_FIELDS_V2 = _WORK_FIELDS_V1 | {
    "return_point_work_id",
    "exit_criteria",
    "attempt_budget",
    "expires_at",
    "promotion_target_work_id",
    "mainline_authority",
}
_WORK_FIELDS_V3 = _WORK_FIELDS_V2 | {
    "work_source_ref",
    "source_revision",
    "work_identity_sha256",
    "dedupe_receipt_sha256",
}
_CLAIM_FIELDS = {
    "claim_id",
    "work_id",
    "actor_ref",
    "status",
    "expected_project_revision",
    "claimed_at",
    "lease_expires_at",
    "released_at",
    "scope_owners",
}
_CLAIM_FIELDS_V6 = _CLAIM_FIELDS | {
    "claim_revision",
    "lease_epoch",
    "last_heartbeat_at",
    "closed_at",
    "closed_by_ref",
    "close_reason",
    "reclaimed_from_claim_id",
}
_IDEA_FIELDS = {
    "idea_id",
    "parent_work_id",
    "source_ref",
    "summary",
    "status",
    "return_work_id",
    "expiry",
    "attempt_budget",
    "promotion_target",
    "evidence_ids",
}
_IDEA_FIELDS_V4 = _IDEA_FIELDS | {
    "dedupe_key",
    "scope_ref",
    "urgency",
    "review_at",
    "created_at",
    "revision",
}
_DECISION_FIELDS = {
    "decision_id",
    "work_id",
    "status",
    "statement",
    "decided_at",
    "supersedes_decision_id",
    "evidence_ids",
}
_CONSTRAINT_FIELDS = {
    "constraint_id",
    "status",
    "statement",
    "scope_work_ids",
    "expires_at",
    "supersedes_constraint_id",
    "evidence_ids",
}
_EVIDENCE_FIELDS = {
    "evidence_id",
    "kind",
    "artifact_ref",
    "content_sha256",
    "validity",
    "observed_at",
    "verified_at",
}
_BLOCKER_FIELDS = {
    "blocker_id",
    "status",
    "reason",
    "blocked_work_ids",
    "evidence_ids",
    "opened_at",
    "resolved_at",
    "supersedes_blocker_id",
}
_EFFECT_FIELDS = {
    "effect_id",
    "effect_key",
    "work_id",
    "claim_id",
    "status",
    "operation",
    "scope_ref",
    "expected_project_revision",
    "sequence_no",
    "evidence_ids",
    "result_ref",
    "requested_at",
    "completed_at",
}
_EFFECT_FIELDS_V3 = _EFFECT_FIELDS | {"attempt_id"}
_EFFECT_FIELDS_V5 = _EFFECT_FIELDS_V3 | {"request_sha256"}
_EFFECT_FIELDS_V6 = _EFFECT_FIELDS_V5 | {
    "lease_epoch",
    "dispatch_receipt_sha256",
    "dispatch_started_at",
}
_EXPERIMENT_ATTEMPT_FIELDS = {
    "attempt_id",
    "work_id",
    "claim_id",
    "actor_ref",
    "attempt_no",
    "experiment_contract_sha256",
    "started_at",
}
_EXPERIMENT_PROMOTION_FIELDS = {
    "promotion_id",
    "kind",
    "proposal_id",
    "work_id",
    "target_work_id",
    "actor_ref",
    "source_work_revision",
    "target_work_revision",
    "attempt_id",
    "experiment_contract_sha256",
    "criterion_evidence",
    "created_at",
}
_IDEA_RELATIONSHIP_FIELDS = {
    "relationship_id",
    "source_idea_id",
    "target_idea_id",
    "relationship_kind",
    "evidence_ids",
    "created_at",
}
_IDEA_OCCURRENCE_FIELDS = {
    "occurrence_id",
    "idea_id",
    "submitted_idea_id",
    "source_ref",
    "dedupe_key",
    "observed_at",
    "actor_ref",
    "request_sha256",
    "origin",
}
_IDEA_REVIEW_FIELDS = {
    "review_id",
    "idea_id",
    "reviewer_ref",
    "decision",
    "urgency",
    "impact",
    "review_at",
    "evidence_ids",
    "reviewed_at",
}
_CORRECTION_PROTECTION_FIELDS = {
    "protection_id",
    "idea_id",
    "status",
    "affected_work_ids",
    "affected_scope_refs",
    "reason",
    "evidence_ids",
    "opened_by_ref",
    "opened_at",
    "released_by_ref",
    "release_reason",
    "release_evidence_ids",
    "released_at",
}
_SCOPE_FIELDS = {"scope_kind", "scope_ref"}

_WORK_KINDS = {"campaign", "goal", "work", "experiment"}
_WORK_STATUSES = {
    "proposed",
    "blocked",
    "ready",
    "active",
    "verifying",
    "completed",
    "rejected",
    "reverted",
    "superseded",
}
_DEDUPE_STATUSES = {"clear", "candidate", "blocked", "coordinated"}
_CLAIM_STATUSES = {"active", "released", "expired", "revoked"}
_IDEA_STATUSES = {
    "candidate",
    "parked",
    "proposed",
    "approved",
    "expired",
    "rejected",
    "superseded",
}
_IDEA_URGENCIES = {"immediate", "next", "later", "review-date"}
_IDEA_REVIEW_DECISIONS = {"keep", "park", "reject", "supersede", "approve"}
_IDEA_IMPACTS = {"none", "low", "medium", "high"}
_IDEA_RELATIONSHIP_KINDS = {
    "related",
    "depends-on",
    "duplicates",
    "supersedes",
    "blocks",
}
_OCCURRENCE_ORIGINS = {"capture", "legacy-v3"}
_CORRECTION_PROTECTION_STATUSES = {"active", "released", "superseded"}
_DECISION_STATUSES = {"proposed", "accepted", "rejected", "reverted", "superseded"}
_CONSTRAINT_STATUSES = {"active", "satisfied", "expired", "rejected", "superseded"}
_EVIDENCE_KINDS = {
    "source-code",
    "standard",
    "os-official",
    "software-official",
    "test",
    "artifact",
    "user-decision",
}
_EVIDENCE_VALIDITY = {"candidate", "verified", "stale", "rejected"}
_BLOCKER_STATUSES = {"open", "resolved", "superseded"}
_EFFECT_STATUSES = {
    "planned",
    "authorized",
    "started",
    "succeeded",
    "failed",
    "compensated",
}
_PROMOTION_KINDS = {"proposed", "approved"}
_SCOPE_KINDS = {"repo", "directory", "file", "symbol", "capability", "effect"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TypedStateError(ValueError):
    """Raised when a typed state document violates the M2-01 contract."""


def _fields(value: Any, expected: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise TypedStateError(f"{field} fields do not match the contract")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypedStateError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _uint(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypedStateError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: Any, field: str, *, optional: bool = False) -> datetime | None:
    if optional and value is None:
        return None
    value = _string(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TypedStateError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise TypedStateError(f"{field} must include timezone")
    return parsed


def _strings(
    value: Any,
    field: str,
    *,
    non_empty: bool = False,
) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise TypedStateError(f"{field} must be a string list")
    if non_empty and not value:
        raise TypedStateError(f"{field} must be a non-empty string list")
    if len(value) != len(set(value)):
        raise TypedStateError(f"{field} must contain unique values")
    return value


def _objects(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TypedStateError(f"{field} must be an object list")
    return value


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if value not in allowed:
        raise TypedStateError(f"{field} is unsupported")
    return value


def _index(
    items: list[dict[str, Any]], id_field: str, field: str
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        item_id = _string(item[id_field], f"{field}.{id_field}")
        if item_id in indexed:
            raise TypedStateError(f"{field}.{id_field} must be unique")
        indexed[item_id] = item
    return indexed


def _references(values: list[str], index: dict[str, Any], field: str) -> None:
    missing = [value for value in values if value not in index]
    if missing:
        raise TypedStateError(f"{field} contains unknown references")


def _acyclic_supersedes(
    index: dict[str, dict[str, Any]], supersedes_field: str, label: str
) -> None:
    for start in index:
        seen: set[str] = set()
        current: str | None = start
        while current is not None:
            if current in seen:
                raise TypedStateError(f"{label} supersedes cycle is invalid")
            seen.add(current)
            current = index[current][supersedes_field]


def _assert_combined_work_graph_acyclic(
    work_by_id: dict[str, dict[str, Any]],
) -> None:
    outgoing: dict[str, list[str]] = {work_id: [] for work_id in work_by_id}
    indegree = {work_id: 0 for work_id in work_by_id}
    for work_id, work in work_by_id.items():
        targets = list(work["dependency_ids"])
        if work["parent_work_id"] is not None:
            targets.append(work["parent_work_id"])
        for target in set(targets):
            outgoing[work_id].append(target)
            indegree[target] += 1

    ready = [work_id for work_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(work_by_id):
        raise TypedStateError("work combined-edge cycle is invalid")


def _validate_work_graph_v2(work_by_id: dict[str, dict[str, Any]]) -> None:
    _assert_combined_work_graph_acyclic(work_by_id)
    children: dict[str, list[str]] = {work_id: [] for work_id in work_by_id}
    roots: list[str] = []
    for work_id, work in work_by_id.items():
        parent_id = work["parent_work_id"]
        if parent_id is None:
            roots.append(work_id)
        else:
            children[parent_id].append(work_id)
    entered: dict[str, int] = {}
    exited: dict[str, int] = {}
    clock = 0
    stack: list[tuple[str, bool]] = [(root, False) for root in reversed(roots)]
    while stack:
        work_id, leaving = stack.pop()
        if leaving:
            exited[work_id] = clock
            clock += 1
            continue
        entered[work_id] = clock
        clock += 1
        stack.append((work_id, True))
        stack.extend((child, False) for child in reversed(children[work_id]))

    def is_ancestor(ancestor_id: str, descendant_id: str) -> bool:
        return (
            entered[ancestor_id] < entered[descendant_id]
            and exited[descendant_id] < exited[ancestor_id]
        )

    parent_kinds = {
        "campaign": set(),
        "goal": {"campaign", "goal"},
        "work": {"goal", "work"},
        "experiment": {"goal", "work", "experiment"},
    }
    for work_id, work in work_by_id.items():
        parent_id = work["parent_work_id"]
        if work["kind"] == "campaign":
            if parent_id is not None:
                raise TypedStateError("Campaign must be a work graph root")
        elif parent_id is None:
            raise TypedStateError("work graph contains an orphan")
        elif work_by_id[parent_id]["kind"] not in parent_kinds[work["kind"]]:
            raise TypedStateError("work graph parent kind is invalid")

        return_point = _optional_string(
            work["return_point_work_id"], "experiment.return_point_work_id"
        )
        exit_criteria = _strings(work["exit_criteria"], "experiment.exit_criteria")
        attempt_budget = work["attempt_budget"]
        expires_at = work["expires_at"]
        promotion_target = _optional_string(
            work["promotion_target_work_id"], "experiment.promotion_target_work_id"
        )
        authority = work["mainline_authority"]
        if type(authority) is not bool:
            raise TypedStateError("experiment.mainline_authority must be boolean")
        if work["kind"] == "experiment":
            if return_point is None or return_point not in work_by_id:
                raise TypedStateError("experiment requires a valid return point")
            if not is_ancestor(return_point, work_id):
                raise TypedStateError("experiment return point must be an ancestor")
            if not exit_criteria:
                raise TypedStateError("experiment requires exit criteria")
            if type(attempt_budget) is not int or attempt_budget <= 0:
                raise TypedStateError("experiment attempt budget must be positive")
            _timestamp(expires_at, "experiment.expires_at")
            if promotion_target is None or promotion_target not in work_by_id:
                raise TypedStateError("experiment requires a valid promotion target")
            if not is_ancestor(promotion_target, work_id):
                raise TypedStateError("experiment promotion target must be an ancestor")
            if authority:
                raise TypedStateError("experiment cannot have mainline authority")
            if not work_by_id[promotion_target]["mainline_authority"]:
                raise TypedStateError(
                    "experiment promotion target requires mainline authority"
                )
        elif (
            return_point is not None
            or exit_criteria
            or attempt_budget is not None
            or expires_at is not None
            or promotion_target is not None
            or not authority
        ):
            raise TypedStateError("non-experiment cannot claim experiment authority")


def _scope(
    value: Any,
    field: str,
    *,
    canonical: bool,
) -> tuple[str, str]:
    scope = _fields(value, _SCOPE_FIELDS, field)
    kind = _enum(scope["scope_kind"], _SCOPE_KINDS, f"{field}.scope_kind")
    ref = _string(scope["scope_ref"], f"{field}.scope_ref")
    if canonical:
        try:
            validate_scope(scope)
        except ValueError as exc:
            raise TypedStateError(f"{field} is not canonical") from exc
    return kind, ref


def validate_typed_state(document: dict[str, Any]) -> None:
    """Validate a complete M2-01 typed state snapshot."""
    if not isinstance(document, dict):
        raise TypedStateError("document fields do not match the contract")
    if document.get("schema_version") in {
        IDEA_REVIEW_SCHEMA_VERSION,
        DURABLE_EFFECT_SCHEMA_VERSION,
        SHARED_WORK_SCHEMA_VERSION,
    }:
        document_fields = _DOCUMENT_FIELDS_V4
    elif document.get("schema_version") == EXPERIMENT_LIFECYCLE_SCHEMA_VERSION:
        document_fields = _DOCUMENT_FIELDS_V3
    else:
        document_fields = _DOCUMENT_FIELDS
    document = _fields(document, document_fields, "document")
    schema_version = document["schema_version"]
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise TypedStateError("unsupported schema_version")

    project = _fields(document["project"], _PROJECT_FIELDS, "project")
    _string(project["project_id"], "project.project_id")
    revision = _uint(project["revision"], "project.revision")
    _string(project["governance_ref"], "project.governance_ref")
    updated_at = _timestamp(project["updated_at"], "project.updated_at")
    assert updated_at is not None
    active_work_ids = _strings(project["active_work_ids"], "project.active_work_ids")
    current_decision_ids = _strings(
        project["current_decision_ids"], "project.current_decision_ids"
    )
    active_constraint_ids = _strings(
        project["active_constraint_ids"], "project.active_constraint_ids"
    )
    open_blocker_ids = _strings(project["open_blocker_ids"], "project.open_blocker_ids")
    high_watermark = _uint(
        project["effect_high_watermark"], "project.effect_high_watermark"
    )

    works = _objects(document["works"], "works")
    claims = _objects(document["claims"], "claims")
    ideas = _objects(document["ideas"], "ideas")
    decisions = _objects(document["decisions"], "decisions")
    constraints = _objects(document["constraints"], "constraints")
    evidence = _objects(document["evidence"], "evidence")
    blockers = _objects(document["blockers"], "blockers")
    effects = _objects(document["effects"], "effects")
    experiment_attempts = _objects(
        document.get("experiment_attempts", []), "experiment_attempts"
    )
    experiment_promotions = _objects(
        document.get("experiment_promotions", []), "experiment_promotions"
    )
    idea_relationships = _objects(
        document.get("idea_relationships", []), "idea_relationships"
    )
    idea_occurrences = _objects(
        document.get("idea_occurrences", []), "idea_occurrences"
    )
    idea_reviews = _objects(document.get("idea_reviews", []), "idea_reviews")
    correction_protections = _objects(
        document.get("correction_protections", []), "correction_protections"
    )

    canonical_scopes = schema_version in {
        SCHEMA_VERSION,
        EXPERIMENT_LIFECYCLE_SCHEMA_VERSION,
        IDEA_REVIEW_SCHEMA_VERSION,
        DURABLE_EFFECT_SCHEMA_VERSION,
        SHARED_WORK_SCHEMA_VERSION,
    }
    work_fields = (
        _WORK_FIELDS_V3
        if schema_version == SHARED_WORK_SCHEMA_VERSION
        else (_WORK_FIELDS_V2 if canonical_scopes else _WORK_FIELDS_V1)
    )
    for item in works:
        _fields(item, work_fields, "work")
    claim_fields = (
        _CLAIM_FIELDS_V6
        if schema_version == SHARED_WORK_SCHEMA_VERSION
        else _CLAIM_FIELDS
    )
    for item in claims:
        _fields(item, claim_fields, "claim")
    idea_fields = (
        _IDEA_FIELDS_V4
        if schema_version
        in {
            IDEA_REVIEW_SCHEMA_VERSION,
            DURABLE_EFFECT_SCHEMA_VERSION,
            SHARED_WORK_SCHEMA_VERSION,
        }
        else _IDEA_FIELDS
    )
    for item in ideas:
        _fields(item, idea_fields, "idea")
    for item in decisions:
        _fields(item, _DECISION_FIELDS, "decision")
    for item in constraints:
        _fields(item, _CONSTRAINT_FIELDS, "constraint")
    for item in evidence:
        _fields(item, _EVIDENCE_FIELDS, "evidence")
    for item in blockers:
        _fields(item, _BLOCKER_FIELDS, "blocker")
    if schema_version == SHARED_WORK_SCHEMA_VERSION:
        effect_fields = _EFFECT_FIELDS_V6
    elif schema_version == DURABLE_EFFECT_SCHEMA_VERSION:
        effect_fields = _EFFECT_FIELDS_V5
    elif schema_version in {
        EXPERIMENT_LIFECYCLE_SCHEMA_VERSION,
        IDEA_REVIEW_SCHEMA_VERSION,
        SHARED_WORK_SCHEMA_VERSION,
    }:
        effect_fields = _EFFECT_FIELDS_V3
    else:
        effect_fields = _EFFECT_FIELDS
    for item in effects:
        _fields(item, effect_fields, "effect")
    for item in experiment_attempts:
        _fields(item, _EXPERIMENT_ATTEMPT_FIELDS, "experiment_attempt")
    for item in experiment_promotions:
        _fields(item, _EXPERIMENT_PROMOTION_FIELDS, "experiment_promotion")
    for item in idea_relationships:
        _fields(item, _IDEA_RELATIONSHIP_FIELDS, "idea_relationship")
    for item in idea_occurrences:
        _fields(item, _IDEA_OCCURRENCE_FIELDS, "idea_occurrence")
    for item in idea_reviews:
        _fields(item, _IDEA_REVIEW_FIELDS, "idea_review")
    for item in correction_protections:
        _fields(item, _CORRECTION_PROTECTION_FIELDS, "correction_protection")

    work_by_id = _index(works, "work_id", "work")
    claim_by_id = _index(claims, "claim_id", "claim")
    idea_by_id = _index(ideas, "idea_id", "idea")
    decision_by_id = _index(decisions, "decision_id", "decision")
    constraint_by_id = _index(constraints, "constraint_id", "constraint")
    evidence_by_id = _index(evidence, "evidence_id", "evidence")
    blocker_by_id = _index(blockers, "blocker_id", "blocker")
    effect_by_id = _index(effects, "effect_id", "effect")
    attempt_by_id = _index(experiment_attempts, "attempt_id", "experiment_attempt")
    promotion_by_id = _index(
        experiment_promotions, "promotion_id", "experiment_promotion"
    )
    relationship_by_id = _index(
        idea_relationships, "relationship_id", "idea_relationship"
    )
    occurrence_by_id = _index(idea_occurrences, "occurrence_id", "idea_occurrence")
    review_by_id = _index(idea_reviews, "review_id", "idea_review")
    protection_by_id = _index(
        correction_protections, "protection_id", "correction_protection"
    )

    all_ids = [
        *work_by_id,
        *claim_by_id,
        *idea_by_id,
        *decision_by_id,
        *constraint_by_id,
        *evidence_by_id,
        *blocker_by_id,
        *effect_by_id,
        *attempt_by_id,
        *promotion_by_id,
        *relationship_by_id,
        *occurrence_by_id,
        *review_by_id,
        *protection_by_id,
    ]
    if len(all_ids) != len(set(all_ids)):
        raise TypedStateError("object IDs must be globally unique")

    for item in evidence:
        _enum(item["kind"], _EVIDENCE_KINDS, "evidence.kind")
        _string(item["artifact_ref"], "evidence.artifact_ref")
        if not isinstance(item["content_sha256"], str) or not _SHA256_RE.fullmatch(
            item["content_sha256"]
        ):
            raise TypedStateError("evidence.content_sha256 must be lowercase SHA-256")
        validity = _enum(item["validity"], _EVIDENCE_VALIDITY, "evidence.validity")
        _timestamp(item["observed_at"], "evidence.observed_at")
        verified_at = _timestamp(
            item["verified_at"], "evidence.verified_at", optional=True
        )
        if validity == "verified" and verified_at is None:
            raise TypedStateError("verified evidence requires verified_at")

    for item in blockers:
        status = _enum(item["status"], _BLOCKER_STATUSES, "blocker.status")
        _string(item["reason"], "blocker.reason")
        blocked = _strings(item["blocked_work_ids"], "blocker.blocked_work_ids")
        refs = _strings(item["evidence_ids"], "blocker.evidence_ids")
        _references(blocked, work_by_id, "blocker.blocked_work_ids")
        _references(refs, evidence_by_id, "blocker.evidence_ids")
        _timestamp(item["opened_at"], "blocker.opened_at")
        resolved = _timestamp(item["resolved_at"], "blocker.resolved_at", optional=True)
        supersedes = _optional_string(
            item["supersedes_blocker_id"], "blocker.supersedes_blocker_id"
        )
        if supersedes is not None and supersedes not in blocker_by_id:
            raise TypedStateError("blocker.supersedes_blocker_id is unknown")
        if status == "open" and resolved is not None:
            raise TypedStateError("open blocker cannot have resolved_at")
        if status != "open" and resolved is None:
            raise TypedStateError("closed blocker requires resolved_at")
    _acyclic_supersedes(blocker_by_id, "supersedes_blocker_id", "blocker")

    for item in works:
        work_id = item["work_id"]
        _enum(item["kind"], _WORK_KINDS, "work.kind")
        status = _enum(item["status"], _WORK_STATUSES, "work.status")
        _string(item["title"], "work.title")
        _uint(item["revision"], "work.revision")
        parent = _optional_string(item["parent_work_id"], "work.parent_work_id")
        dependencies = _strings(item["dependency_ids"], "work.dependency_ids")
        _strings(item["owner_refs"], "work.owner_refs")
        scopes = [
            _scope(scope, "work.scope_ref", canonical=canonical_scopes)
            for scope in item["scope_refs"]
        ]
        if len(scopes) != len(set(scopes)):
            raise TypedStateError("work.scope_refs must be unique")
        overlaps = _strings(item["overlap_candidate_ids"], "work.overlap_candidate_ids")
        dedupe = _enum(item["dedupe_status"], _DEDUPE_STATUSES, "work.dedupe_status")
        supersedes = _optional_string(
            item["supersedes_work_id"], "work.supersedes_work_id"
        )
        evidence_ids = _strings(item["evidence_ids"], "work.evidence_ids")
        blocker_ids = _strings(item["blocker_ids"], "work.blocker_ids")
        if parent is not None and (parent == work_id or parent not in work_by_id):
            raise TypedStateError("work.parent_work_id is invalid")
        _references(dependencies, work_by_id, "work.dependency_ids")
        _references(overlaps, work_by_id, "work.overlap_candidate_ids")
        _references(evidence_ids, evidence_by_id, "work.evidence_ids")
        _references(blocker_ids, blocker_by_id, "work.blocker_ids")
        if work_id in dependencies or work_id in overlaps:
            raise TypedStateError("work cannot reference itself")
        if supersedes is not None and (
            supersedes == work_id or supersedes not in work_by_id
        ):
            raise TypedStateError("work.supersedes_work_id is invalid")
        if overlaps and dedupe == "clear":
            raise TypedStateError("work with overlap candidates requires dedupe review")
        if not overlaps and dedupe != "clear":
            raise TypedStateError(
                "work without overlap candidates must have clear dedupe status"
            )
        if (
            overlaps
            and status in {"ready", "active", "verifying"}
            and dedupe != "coordinated"
        ):
            raise TypedStateError("work cannot activate before dedupe coordination")
        if status == "completed" and not any(
            evidence_by_id[evidence_id]["validity"] == "verified"
            for evidence_id in evidence_ids
        ):
            raise TypedStateError("completed work requires verified evidence")
        if schema_version == SHARED_WORK_SCHEMA_VERSION:
            _optional_string(item["work_source_ref"], "work.work_source_ref")
            _uint(item["source_revision"], "work.source_revision")
            for field in ("work_identity_sha256", "dedupe_receipt_sha256"):
                digest = item[field]
                if digest is not None and (
                    not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None
                ):
                    raise TypedStateError(f"work.{field} must be SHA-256")
    _acyclic_supersedes(work_by_id, "supersedes_work_id", "work")
    if canonical_scopes:
        _validate_work_graph_v2(work_by_id)

    actual_active_work_ids = {
        item["work_id"] for item in works if item["status"] == "active"
    }
    if canonical_scopes and any(
        work_by_id[work_id]["kind"] not in {"work", "experiment"}
        for work_id in actual_active_work_ids
    ):
        raise TypedStateError("active work must be an executable leaf")
    if set(active_work_ids) != actual_active_work_ids:
        raise TypedStateError("project.active_work_ids must match active Work status")
    primary_work_id = _optional_string(
        project["primary_work_id"], "project.primary_work_id"
    )
    if primary_work_id is None and active_work_ids:
        raise TypedStateError("project.primary_work_id is required when work is active")
    if primary_work_id is not None and primary_work_id not in actual_active_work_ids:
        raise TypedStateError("project.primary_work_id must reference active work")

    for item in ideas:
        _enum(item["status"], _IDEA_STATUSES, "idea.status")
        _string(item["source_ref"], "idea.source_ref")
        _string(item["summary"], "idea.summary")
        if (
            item["parent_work_id"] not in work_by_id
            or item["return_work_id"] not in work_by_id
        ):
            raise TypedStateError("idea work reference is unknown")
        _timestamp(item["expiry"], "idea.expiry", optional=True)
        if item["attempt_budget"] is not None:
            _uint(item["attempt_budget"], "idea.attempt_budget")
        _optional_string(item["promotion_target"], "idea.promotion_target")
        idea_evidence = _strings(item["evidence_ids"], "idea.evidence_ids")
        _references(idea_evidence, evidence_by_id, "idea.evidence_ids")

    if schema_version in {
        IDEA_REVIEW_SCHEMA_VERSION,
        DURABLE_EFFECT_SCHEMA_VERSION,
        SHARED_WORK_SCHEMA_VERSION,
    }:
        seen_dedupe_keys: set[str] = set()
        for item in ideas:
            dedupe_key = item["dedupe_key"]
            if not isinstance(dedupe_key, str) or not re.fullmatch(
                r"sha256:[0-9a-f]{64}", dedupe_key
            ):
                raise TypedStateError("idea.dedupe_key must be a SHA-256 key")
            if dedupe_key in seen_dedupe_keys:
                raise TypedStateError("idea.dedupe_key must be unique")
            seen_dedupe_keys.add(dedupe_key)
            _string(item["scope_ref"], "idea.scope_ref")
            urgency = _enum(item["urgency"], _IDEA_URGENCIES, "idea.urgency")
            review_at = _timestamp(item["review_at"], "idea.review_at", optional=True)
            if urgency == "review-date" and review_at is None:
                raise TypedStateError("review-date Idea requires review_at")
            _timestamp(item["created_at"], "idea.created_at", optional=True)
            if _uint(item["revision"], "idea.revision") == 0:
                raise TypedStateError("idea.revision must be positive")

        relationship_keys: set[tuple[str, str, str]] = set()
        relationship_graph: dict[str, set[str]] = {
            idea_id: set() for idea_id in idea_by_id
        }
        for item in idea_relationships:
            source_id = item["source_idea_id"]
            target_id = item["target_idea_id"]
            if source_id not in idea_by_id or target_id not in idea_by_id:
                raise TypedStateError("idea relationship references an unknown Idea")
            if source_id == target_id:
                raise TypedStateError("idea relationship cannot self-reference")
            kind = _enum(
                item["relationship_kind"],
                _IDEA_RELATIONSHIP_KINDS,
                "idea_relationship.relationship_kind",
            )
            key = (source_id, target_id, kind)
            if key in relationship_keys:
                raise TypedStateError("idea relationship must be unique")
            relationship_keys.add(key)
            relationship_graph[source_id].add(target_id)
            refs = _strings(item["evidence_ids"], "idea_relationship.evidence_ids")
            _references(refs, evidence_by_id, "idea_relationship.evidence_ids")
            _timestamp(item["created_at"], "idea_relationship.created_at")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit_idea(idea_id: str) -> None:
            if idea_id in visiting:
                raise TypedStateError("idea relationship cycle is invalid")
            if idea_id in visited:
                return
            visiting.add(idea_id)
            for target_id in relationship_graph[idea_id]:
                visit_idea(target_id)
            visiting.remove(idea_id)
            visited.add(idea_id)

        for idea_id in relationship_graph:
            visit_idea(idea_id)

        source_bindings: set[str] = set()
        for item in idea_occurrences:
            idea_id = item["idea_id"]
            if idea_id not in idea_by_id:
                raise TypedStateError("idea occurrence references an unknown Idea")
            _string(item["submitted_idea_id"], "idea_occurrence.submitted_idea_id")
            _string(item["source_ref"], "idea_occurrence.source_ref")
            if item["source_ref"] in source_bindings:
                raise TypedStateError(
                    "idea occurrence source cannot bind multiple Ideas"
                )
            source_bindings.add(item["source_ref"])
            if item["dedupe_key"] != idea_by_id[idea_id]["dedupe_key"]:
                raise TypedStateError(
                    "idea occurrence must retain the canonical dedupe key"
                )
            origin = _enum(
                item["origin"], _OCCURRENCE_ORIGINS, "idea_occurrence.origin"
            )
            observed_at = _timestamp(
                item["observed_at"], "idea_occurrence.observed_at", optional=True
            )
            _string(item["actor_ref"], "idea_occurrence.actor_ref")
            request_sha256 = item["request_sha256"]
            if request_sha256 is not None and (
                not isinstance(request_sha256, str)
                or not _SHA256_RE.fullmatch(request_sha256)
            ):
                raise TypedStateError("idea occurrence request_sha256 must be SHA-256")
            if origin == "capture" and observed_at is None:
                raise TypedStateError("captured occurrence requires observed_at")
            if origin == "capture" and request_sha256 is None:
                raise TypedStateError("captured occurrence requires request_sha256")
            if origin == "legacy-v3" and observed_at is not None:
                raise TypedStateError("legacy occurrence cannot invent observed_at")
            if origin == "legacy-v3" and request_sha256 is not None:
                raise TypedStateError("legacy occurrence cannot invent request_sha256")

        for item in idea_reviews:
            if item["idea_id"] not in idea_by_id:
                raise TypedStateError("idea review references an unknown Idea")
            _string(item["reviewer_ref"], "idea_review.reviewer_ref")
            _enum(item["decision"], _IDEA_REVIEW_DECISIONS, "idea_review.decision")
            urgency = _enum(item["urgency"], _IDEA_URGENCIES, "idea_review.urgency")
            _enum(item["impact"], _IDEA_IMPACTS, "idea_review.impact")
            review_at = _timestamp(
                item["review_at"], "idea_review.review_at", optional=True
            )
            if urgency == "review-date" and review_at is None:
                raise TypedStateError("review-date Idea review requires review_at")
            refs = _strings(item["evidence_ids"], "idea_review.evidence_ids")
            _references(refs, evidence_by_id, "idea_review.evidence_ids")
            _timestamp(item["reviewed_at"], "idea_review.reviewed_at")

        for item in correction_protections:
            if item["idea_id"] not in idea_by_id:
                raise TypedStateError(
                    "correction protection references an unknown Idea"
                )
            status = _enum(
                item["status"],
                _CORRECTION_PROTECTION_STATUSES,
                "correction_protection.status",
            )
            affected_work_ids = _strings(
                item["affected_work_ids"],
                "correction_protection.affected_work_ids",
                non_empty=True,
            )
            _references(
                affected_work_ids, work_by_id, "correction_protection.affected_work_ids"
            )
            _strings(
                item["affected_scope_refs"],
                "correction_protection.affected_scope_refs",
                non_empty=True,
            )
            _string(item["reason"], "correction_protection.reason")
            refs = _strings(item["evidence_ids"], "correction_protection.evidence_ids")
            _references(refs, evidence_by_id, "correction_protection.evidence_ids")
            _string(item["opened_by_ref"], "correction_protection.opened_by_ref")
            opened_at = _timestamp(item["opened_at"], "correction_protection.opened_at")
            released_by_ref = _optional_string(
                item["released_by_ref"], "correction_protection.released_by_ref"
            )
            release_reason = _optional_string(
                item["release_reason"], "correction_protection.release_reason"
            )
            release_refs = _strings(
                item["release_evidence_ids"],
                "correction_protection.release_evidence_ids",
            )
            _references(
                release_refs,
                evidence_by_id,
                "correction_protection.release_evidence_ids",
            )
            released_at = _timestamp(
                item["released_at"], "correction_protection.released_at", optional=True
            )
            assert opened_at is not None
            if status == "active" and (
                released_at is not None
                or released_by_ref is not None
                or release_reason is not None
                or release_refs
            ):
                raise TypedStateError(
                    "active correction protection cannot have release provenance"
                )
            if status != "active" and (
                released_at is None
                or released_by_ref is None
                or release_reason is None
                or not release_refs
            ):
                raise TypedStateError(
                    "inactive correction protection requires release provenance"
                )
            if released_at is not None and released_at < opened_at:
                raise TypedStateError("correction protection release precedes opening")

    for item in decisions:
        _enum(item["status"], _DECISION_STATUSES, "decision.status")
        if item["work_id"] not in work_by_id:
            raise TypedStateError("decision.work_id is unknown")
        _string(item["statement"], "decision.statement")
        _timestamp(item["decided_at"], "decision.decided_at")
        supersedes = _optional_string(
            item["supersedes_decision_id"], "decision.supersedes_decision_id"
        )
        if supersedes is not None and (
            supersedes == item["decision_id"] or supersedes not in decision_by_id
        ):
            raise TypedStateError("decision.supersedes_decision_id is invalid")
        refs = _strings(item["evidence_ids"], "decision.evidence_ids")
        _references(refs, evidence_by_id, "decision.evidence_ids")

    actual_current_decisions = {
        item["decision_id"] for item in decisions if item["status"] == "accepted"
    }
    if set(current_decision_ids) != actual_current_decisions:
        raise TypedStateError(
            "project.current_decision_ids must contain only accepted decisions"
        )
    _acyclic_supersedes(decision_by_id, "supersedes_decision_id", "decision")

    for item in constraints:
        _enum(item["status"], _CONSTRAINT_STATUSES, "constraint.status")
        _string(item["statement"], "constraint.statement")
        scope_work_ids = _strings(item["scope_work_ids"], "constraint.scope_work_ids")
        _references(scope_work_ids, work_by_id, "constraint.scope_work_ids")
        _timestamp(item["expires_at"], "constraint.expires_at", optional=True)
        supersedes = _optional_string(
            item["supersedes_constraint_id"], "constraint.supersedes_constraint_id"
        )
        if supersedes is not None and (
            supersedes == item["constraint_id"] or supersedes not in constraint_by_id
        ):
            raise TypedStateError("constraint.supersedes_constraint_id is invalid")
        refs = _strings(item["evidence_ids"], "constraint.evidence_ids")
        _references(refs, evidence_by_id, "constraint.evidence_ids")

    actual_active_constraints = {
        item["constraint_id"] for item in constraints if item["status"] == "active"
    }
    if set(active_constraint_ids) != actual_active_constraints:
        raise TypedStateError(
            "project.active_constraint_ids must contain only active constraints"
        )
    _acyclic_supersedes(constraint_by_id, "supersedes_constraint_id", "constraint")
    actual_open_blockers = {
        item["blocker_id"] for item in blockers if item["status"] == "open"
    }
    if set(open_blocker_ids) != actual_open_blockers:
        raise TypedStateError(
            "project.open_blocker_ids must contain only open blockers"
        )

    active_claims: dict[str, dict[str, Any]] = {}
    owned_scopes: list[tuple[dict[str, str], str]] = []
    for item in claims:
        work_id = item["work_id"]
        if work_id not in work_by_id:
            raise TypedStateError("claim.work_id is unknown")
        _string(item["actor_ref"], "claim.actor_ref")
        status = _enum(item["status"], _CLAIM_STATUSES, "claim.status")
        expected_revision = _uint(
            item["expected_project_revision"], "claim.expected_project_revision"
        )
        claimed_at = _timestamp(item["claimed_at"], "claim.claimed_at")
        lease_expires_at = _timestamp(
            item["lease_expires_at"], "claim.lease_expires_at"
        )
        released_at = _timestamp(
            item["released_at"], "claim.released_at", optional=True
        )
        if schema_version == SHARED_WORK_SCHEMA_VERSION:
            claim_revision = _uint(item["claim_revision"], "claim.claim_revision")
            lease_epoch = _uint(item["lease_epoch"], "claim.lease_epoch")
            last_heartbeat_at = _timestamp(
                item["last_heartbeat_at"],
                "claim.last_heartbeat_at",
                optional=True,
            )
            closed_at = _timestamp(item["closed_at"], "claim.closed_at", optional=True)
            closed_by_ref = _optional_string(
                item["closed_by_ref"], "claim.closed_by_ref"
            )
            close_reason = item["close_reason"]
            if close_reason not in {
                None,
                "worker_release",
                "lease_expired",
                "administrative_revoke",
            }:
                raise TypedStateError("claim.close_reason is invalid")
            _optional_string(
                item["reclaimed_from_claim_id"],
                "claim.reclaimed_from_claim_id",
            )
        assert claimed_at is not None and lease_expires_at is not None
        if lease_expires_at <= claimed_at:
            raise TypedStateError("claim lease must expire after claimed_at")
        scopes = [
            _scope(scope, "claim.scope_owner", canonical=canonical_scopes)
            for scope in item["scope_owners"]
        ]
        if not scopes or len(scopes) != len(set(scopes)):
            raise TypedStateError("claim.scope_owners must be unique and non-empty")
        work_scope_values = work_by_id[work_id]["scope_refs"]
        if canonical_scopes:
            scope_is_owned = all(
                any_scope_covers(work_scope_values, scope)
                for scope in item["scope_owners"]
            )
        else:
            scope_is_owned = set(scopes).issubset(
                {
                    _scope(value, "work.scope_ref", canonical=False)
                    for value in work_scope_values
                }
            )
        if not scope_is_owned:
            raise TypedStateError("claim scope must belong to its work")
        if status == "active":
            if schema_version == SHARED_WORK_SCHEMA_VERSION and (
                claim_revision == 0
                or lease_epoch == 0
                or last_heartbeat_at is None
                or closed_at is not None
                or closed_by_ref is not None
                or close_reason is not None
            ):
                raise TypedStateError(
                    "active claim requires a live fence and open provenance"
                )
            if work_by_id[work_id]["status"] != "active":
                raise TypedStateError("active claim requires active work")
            if item["actor_ref"] not in work_by_id[work_id]["owner_refs"]:
                raise TypedStateError("active claim actor must own its work")
            if expected_revision != revision:
                raise TypedStateError("active claim requires expected project revision")
            if released_at is not None:
                raise TypedStateError("active claim cannot have released_at")
            if lease_expires_at <= updated_at:
                raise TypedStateError("active claim lease has expired at snapshot time")
            if work_id in active_claims:
                raise TypedStateError("active work can have only one active claim")
            active_claims[work_id] = item
            for scope_value, scope in zip(item["scope_owners"], scopes, strict=True):
                overlap = (
                    any(scopes_overlap(scope_value, owned) for owned, _ in owned_scopes)
                    if canonical_scopes
                    else any(scope == owned for _, owned in owned_scopes)
                )
                if overlap:
                    raise TypedStateError("active claim scope ownership conflict")
                owned_scopes.append((scope_value, scope))
        else:
            if released_at is None:
                raise TypedStateError("inactive claim requires released_at")
            if status == "expired" and released_at < lease_expires_at:
                raise TypedStateError(
                    "expired claim cannot release before lease expiry"
                )
            if schema_version == SHARED_WORK_SCHEMA_VERSION and (
                claim_revision == 0
                or closed_at is None
                or closed_by_ref is None
                or close_reason is None
            ):
                raise TypedStateError("inactive claim requires terminal provenance")

    if set(active_claims) != actual_active_work_ids:
        raise TypedStateError("every active work requires one active claim")

    attempts_by_work: dict[str, list[dict[str, Any]]] = {}
    for item in experiment_attempts:
        work_id = item["work_id"]
        work = work_by_id.get(work_id)
        if work is None or work["kind"] != "experiment":
            raise TypedStateError("experiment attempt requires an Experiment Work")
        claim_id = item["claim_id"]
        claim = claim_by_id.get(claim_id)
        if claim is None or claim["work_id"] != work_id:
            raise TypedStateError("experiment attempt claim must belong to its Work")
        _string(item["actor_ref"], "experiment_attempt.actor_ref")
        if item["actor_ref"] != claim["actor_ref"]:
            raise TypedStateError("experiment attempt actor must match its Claim")
        attempt_no = _uint(item["attempt_no"], "experiment_attempt.attempt_no")
        if attempt_no == 0:
            raise TypedStateError("experiment attempt number must be positive")
        contract_digest = item["experiment_contract_sha256"]
        if not isinstance(contract_digest, str) or not _SHA256_RE.fullmatch(
            contract_digest
        ):
            raise TypedStateError("experiment attempt contract digest must be SHA-256")
        if contract_digest != experiment_contract_sha256(work):
            raise TypedStateError("experiment attempt contract digest drifted")
        started_at = _timestamp(item["started_at"], "experiment_attempt.started_at")
        expires_at = _timestamp(work["expires_at"], "experiment.expires_at")
        assert started_at is not None and expires_at is not None
        if started_at >= expires_at:
            raise TypedStateError("experiment attempt cannot start after expiry")
        attempts_by_work.setdefault(work_id, []).append(item)

    for work_id, attempts in attempts_by_work.items():
        if len(attempts) > work_by_id[work_id]["attempt_budget"]:
            raise TypedStateError("experiment attempt budget is exceeded")
        numbers = {item["attempt_no"] for item in attempts}
        if numbers != set(range(1, len(attempts) + 1)):
            raise TypedStateError("experiment attempt numbers must be contiguous")

    proposal_by_id: dict[str, dict[str, Any]] = {}
    approved_by_proposal: set[str] = set()
    for item in experiment_promotions:
        kind = _enum(item["kind"], _PROMOTION_KINDS, "experiment_promotion.kind")
        proposal_id = _string(item["proposal_id"], "experiment_promotion.proposal_id")
        work_id = item["work_id"]
        work = work_by_id.get(work_id)
        if work is None or work["kind"] != "experiment":
            raise TypedStateError("experiment promotion requires an Experiment Work")
        if item["target_work_id"] != work["promotion_target_work_id"]:
            raise TypedStateError("experiment promotion target must match its contract")
        target = work_by_id.get(item["target_work_id"])
        if target is None or not target["mainline_authority"]:
            raise TypedStateError(
                "experiment promotion target must retain mainline authority"
            )
        _string(item["actor_ref"], "experiment_promotion.actor_ref")
        source_revision = _uint(
            item["source_work_revision"], "experiment_promotion.source_work_revision"
        )
        target_revision = _uint(
            item["target_work_revision"], "experiment_promotion.target_work_revision"
        )
        if source_revision > work["revision"] or target_revision > target["revision"]:
            raise TypedStateError("experiment promotion work revision is invalid")
        attempt_id = _string(item["attempt_id"], "experiment_promotion.attempt_id")
        attempt = attempt_by_id.get(attempt_id)
        if (
            attempt is None
            or attempt["work_id"] != work_id
            or attempt["experiment_contract_sha256"] != experiment_contract_sha256(work)
        ):
            raise TypedStateError("experiment promotion requires a bound attempt")
        if kind == "proposed" and item["actor_ref"] != attempt["actor_ref"]:
            raise TypedStateError("promotion proposer must own the bound attempt")
        contract_digest = item["experiment_contract_sha256"]
        if not isinstance(contract_digest, str) or not _SHA256_RE.fullmatch(
            contract_digest
        ):
            raise TypedStateError(
                "experiment promotion contract digest must be SHA-256"
            )
        if contract_digest != experiment_contract_sha256(work):
            raise TypedStateError("experiment promotion contract digest drifted")
        criteria = item["criterion_evidence"]
        if not isinstance(criteria, dict) or set(criteria) != set(
            work["exit_criteria"]
        ):
            raise TypedStateError(
                "experiment promotion criteria must exactly match the contract"
            )
        for criterion, evidence_ids in criteria.items():
            _string(criterion, "experiment promotion criterion")
            refs = _strings(
                evidence_ids, "experiment_promotion.criterion_evidence", non_empty=True
            )
            _references(refs, evidence_by_id, "experiment_promotion.criterion_evidence")
            if kind == "approved" and any(
                evidence_by_id[evidence_id]["validity"] != "verified"
                for evidence_id in refs
            ):
                raise TypedStateError(
                    "approved promotion requires verified criterion evidence"
                )
        created_at = _timestamp(item["created_at"], "experiment_promotion.created_at")
        expires_at = _timestamp(work["expires_at"], "experiment.expires_at")
        assert created_at is not None and expires_at is not None
        if created_at >= expires_at:
            raise TypedStateError("experiment promotion cannot be created after expiry")
        if kind == "proposed":
            if proposal_id in proposal_by_id:
                raise TypedStateError("experiment promotion proposal_id must be unique")
            proposal_by_id[proposal_id] = item
        else:
            if proposal_id not in proposal_by_id:
                raise TypedStateError("approved promotion requires its prior proposal")
            if proposal_id in approved_by_proposal:
                raise TypedStateError("experiment promotion proposal can approve once")
            proposal = proposal_by_id[proposal_id]
            for field in (
                "proposal_id",
                "work_id",
                "target_work_id",
                "source_work_revision",
                "target_work_revision",
                "attempt_id",
                "experiment_contract_sha256",
                "criterion_evidence",
            ):
                if item[field] != proposal[field]:
                    raise TypedStateError(
                        "approved promotion must preserve its proposal lineage"
                    )
            approved_by_proposal.add(proposal_id)

    effect_keys: set[str] = set()
    sequence_numbers: set[int] = set()
    committed_sequences: list[int] = []
    pending_effect_scopes: list[dict[str, str]] = []
    for item in effects:
        effect_key = _string(item["effect_key"], "effect.effect_key")
        if effect_key in effect_keys:
            raise TypedStateError("effect.effect_key must be unique")
        effect_keys.add(effect_key)
        work_id = item["work_id"]
        if work_id not in work_by_id:
            raise TypedStateError("effect.work_id is unknown")
        if schema_version == EXPERIMENT_LIFECYCLE_SCHEMA_VERSION:
            attempt_id = _optional_string(item["attempt_id"], "effect.attempt_id")
            work = work_by_id[work_id]
            if work["kind"] == "experiment":
                attempt = (
                    attempt_by_id.get(attempt_id) if attempt_id is not None else None
                )
                if (
                    attempt is None
                    or attempt["work_id"] != work_id
                    or attempt["claim_id"] != item["claim_id"]
                    or attempt["experiment_contract_sha256"]
                    != experiment_contract_sha256(work)
                ):
                    raise TypedStateError(
                        "Experiment effect requires a bound attempt provenance"
                    )
            elif attempt_id is not None:
                raise TypedStateError("non-Experiment effect cannot bind an attempt")
        claim_id = _optional_string(item["claim_id"], "effect.claim_id")
        if claim_id is not None and claim_id not in claim_by_id:
            raise TypedStateError("effect.claim_id is unknown")
        status = _enum(item["status"], _EFFECT_STATUSES, "effect.status")
        _string(item["operation"], "effect.operation")
        request_sha256 = item.get("request_sha256")
        if (
            schema_version
            in {
                DURABLE_EFFECT_SCHEMA_VERSION,
                SHARED_WORK_SCHEMA_VERSION,
            }
            and request_sha256 is not None
            and (
                not isinstance(request_sha256, str)
                or _SHA256_RE.fullmatch(request_sha256) is None
            )
        ):
            raise TypedStateError("effect.request_sha256 must be SHA-256")
        if schema_version == SHARED_WORK_SCHEMA_VERSION:
            effect_lease_epoch = _uint(item["lease_epoch"], "effect.lease_epoch")
            dispatch_receipt = item["dispatch_receipt_sha256"]
            if dispatch_receipt is not None and (
                not isinstance(dispatch_receipt, str)
                or _SHA256_RE.fullmatch(dispatch_receipt) is None
            ):
                raise TypedStateError("effect.dispatch_receipt_sha256 must be SHA-256")
            dispatch_started_at = _timestamp(
                item["dispatch_started_at"],
                "effect.dispatch_started_at",
                optional=True,
            )
        scope = _scope(
            item["scope_ref"], "effect.scope_ref", canonical=canonical_scopes
        )
        expected_revision = _uint(
            item["expected_project_revision"], "effect.expected_project_revision"
        )
        sequence_no = _uint(item["sequence_no"], "effect.sequence_no")
        if sequence_no == 0 or sequence_no in sequence_numbers:
            raise TypedStateError("effect.sequence_no must be unique and positive")
        sequence_numbers.add(sequence_no)
        refs = _strings(item["evidence_ids"], "effect.evidence_ids")
        _references(refs, evidence_by_id, "effect.evidence_ids")
        result_ref = _optional_string(item["result_ref"], "effect.result_ref")
        _timestamp(item["requested_at"], "effect.requested_at")
        completed_at = _timestamp(
            item["completed_at"], "effect.completed_at", optional=True
        )
        if status in {"authorized", "started"}:
            if expected_revision != revision:
                raise TypedStateError(
                    "authorized effect requires expected project revision"
                )
            if claim_id is None or claim_by_id[claim_id]["status"] != "active":
                raise TypedStateError(
                    "authorized effect requires a current active claim"
                )
            if claim_by_id[claim_id]["work_id"] != work_id:
                raise TypedStateError("effect claim must belong to its work")
            if schema_version == SHARED_WORK_SCHEMA_VERSION and (
                effect_lease_epoch == 0
                or effect_lease_epoch != claim_by_id[claim_id]["lease_epoch"]
            ):
                raise TypedStateError("pending effect requires the current claim fence")
            claim_scopes = claim_by_id[claim_id]["scope_owners"]
            scope_is_owned = (
                any_scope_covers(claim_scopes, item["scope_ref"])
                if canonical_scopes
                else scope
                in {
                    _scope(value, "claim.scope_owner", canonical=False)
                    for value in claim_scopes
                }
            )
            if not scope_is_owned:
                raise TypedStateError("effect scope must be owned by its claim")
            if work_id not in actual_active_work_ids:
                raise TypedStateError("authorized effect requires active work")
            if canonical_scopes and any(
                scopes_overlap(item["scope_ref"], owned)
                for owned in pending_effect_scopes
            ):
                raise TypedStateError("pending effect scope conflict")
            if canonical_scopes:
                pending_effect_scopes.append(item["scope_ref"])
        if schema_version == SHARED_WORK_SCHEMA_VERSION:
            if status == "started" and dispatch_started_at is None:
                raise TypedStateError("started effect requires dispatch_started_at")
            if status in {"planned", "authorized"} and (
                dispatch_receipt is not None or dispatch_started_at is not None
            ):
                raise TypedStateError(
                    "undispatched effect cannot contain dispatch provenance"
                )
        if status in {"succeeded", "failed", "compensated"}:
            if claim_id is None:
                raise TypedStateError("committed effect requires claim provenance")
            if completed_at is None or result_ref is None:
                raise TypedStateError(
                    "committed effect requires result and completion time"
                )
            committed_sequences.append(sequence_no)
        elif completed_at is not None or result_ref is not None:
            raise TypedStateError("uncommitted effect cannot have completion result")

    actual_high_watermark = max(committed_sequences, default=0)
    if high_watermark != actual_high_watermark:
        raise TypedStateError(
            "project.effect_high_watermark does not match committed effects"
        )


def canonical_state_bytes(document: dict[str, Any]) -> bytes:
    """Return deterministic JSON bytes after validating the snapshot."""
    validate_typed_state(document)
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def round_trip_typed_state(document: dict[str, Any]) -> dict[str, Any]:
    """Round-trip a snapshot through canonical JSON and revalidate it."""
    restored = json.loads(canonical_state_bytes(document).decode("utf-8"))
    validate_typed_state(restored)
    return restored
