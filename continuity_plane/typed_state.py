"""M2-01 typed state contract validation and canonical round trips."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any


LEGACY_SCHEMA_VERSION = "context.typed-state/v1alpha1"
SCHEMA_VERSION = "context.typed-state/v2alpha1"
SUPPORTED_SCHEMA_VERSIONS = {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}

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
_IDEA_STATUSES = {"candidate", "parked", "proposed", "approved", "rejected", "superseded"}
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
_EFFECT_STATUSES = {"planned", "authorized", "started", "succeeded", "failed", "compensated"}
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


def _strings(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise TypedStateError(f"{field} must be a string list")
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


def _index(items: list[dict[str, Any]], id_field: str, field: str) -> dict[str, dict[str, Any]]:
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
            if (
                type(attempt_budget) is not int
                or attempt_budget <= 0
            ):
                raise TypedStateError("experiment attempt budget must be positive")
            _timestamp(expires_at, "experiment.expires_at")
            if promotion_target is None or promotion_target not in work_by_id:
                raise TypedStateError("experiment requires a valid promotion target")
            if not is_ancestor(promotion_target, work_id):
                raise TypedStateError("experiment promotion target must be an ancestor")
            if authority:
                raise TypedStateError("experiment cannot have mainline authority")
            if not work_by_id[promotion_target]["mainline_authority"]:
                raise TypedStateError("experiment promotion target requires mainline authority")
        elif (
            return_point is not None
            or exit_criteria
            or attempt_budget is not None
            or expires_at is not None
            or promotion_target is not None
            or not authority
        ):
            raise TypedStateError("non-experiment cannot claim experiment authority")


def _scope(value: Any, field: str) -> tuple[str, str]:
    scope = _fields(value, _SCOPE_FIELDS, field)
    kind = _enum(scope["scope_kind"], _SCOPE_KINDS, f"{field}.scope_kind")
    ref = _string(scope["scope_ref"], f"{field}.scope_ref")
    return kind, ref


def validate_typed_state(document: dict[str, Any]) -> None:
    """Validate a complete M2-01 typed state snapshot."""
    document = _fields(document, _DOCUMENT_FIELDS, "document")
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
    high_watermark = _uint(project["effect_high_watermark"], "project.effect_high_watermark")

    works = _objects(document["works"], "works")
    claims = _objects(document["claims"], "claims")
    ideas = _objects(document["ideas"], "ideas")
    decisions = _objects(document["decisions"], "decisions")
    constraints = _objects(document["constraints"], "constraints")
    evidence = _objects(document["evidence"], "evidence")
    blockers = _objects(document["blockers"], "blockers")
    effects = _objects(document["effects"], "effects")

    work_fields = _WORK_FIELDS_V2 if schema_version == SCHEMA_VERSION else _WORK_FIELDS_V1
    for item in works:
        _fields(item, work_fields, "work")
    for item in claims:
        _fields(item, _CLAIM_FIELDS, "claim")
    for item in ideas:
        _fields(item, _IDEA_FIELDS, "idea")
    for item in decisions:
        _fields(item, _DECISION_FIELDS, "decision")
    for item in constraints:
        _fields(item, _CONSTRAINT_FIELDS, "constraint")
    for item in evidence:
        _fields(item, _EVIDENCE_FIELDS, "evidence")
    for item in blockers:
        _fields(item, _BLOCKER_FIELDS, "blocker")
    for item in effects:
        _fields(item, _EFFECT_FIELDS, "effect")

    work_by_id = _index(works, "work_id", "work")
    claim_by_id = _index(claims, "claim_id", "claim")
    idea_by_id = _index(ideas, "idea_id", "idea")
    decision_by_id = _index(decisions, "decision_id", "decision")
    constraint_by_id = _index(constraints, "constraint_id", "constraint")
    evidence_by_id = _index(evidence, "evidence_id", "evidence")
    blocker_by_id = _index(blockers, "blocker_id", "blocker")
    effect_by_id = _index(effects, "effect_id", "effect")

    all_ids = [
        *work_by_id,
        *claim_by_id,
        *idea_by_id,
        *decision_by_id,
        *constraint_by_id,
        *evidence_by_id,
        *blocker_by_id,
        *effect_by_id,
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
        verified_at = _timestamp(item["verified_at"], "evidence.verified_at", optional=True)
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
        supersedes = _optional_string(item["supersedes_blocker_id"], "blocker.supersedes_blocker_id")
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
        scopes = [_scope(scope, "work.scope_ref") for scope in item["scope_refs"]]
        if len(scopes) != len(set(scopes)):
            raise TypedStateError("work.scope_refs must be unique")
        overlaps = _strings(item["overlap_candidate_ids"], "work.overlap_candidate_ids")
        dedupe = _enum(item["dedupe_status"], _DEDUPE_STATUSES, "work.dedupe_status")
        supersedes = _optional_string(item["supersedes_work_id"], "work.supersedes_work_id")
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
        if supersedes is not None and (supersedes == work_id or supersedes not in work_by_id):
            raise TypedStateError("work.supersedes_work_id is invalid")
        if overlaps and dedupe == "clear":
            raise TypedStateError("work with overlap candidates requires dedupe review")
        if not overlaps and dedupe != "clear":
            raise TypedStateError("work without overlap candidates must have clear dedupe status")
        if overlaps and status in {"ready", "active", "verifying"} and dedupe != "coordinated":
            raise TypedStateError("work cannot activate before dedupe coordination")
        if status == "completed" and not any(
            evidence_by_id[evidence_id]["validity"] == "verified"
            for evidence_id in evidence_ids
        ):
            raise TypedStateError("completed work requires verified evidence")
    _acyclic_supersedes(work_by_id, "supersedes_work_id", "work")
    if schema_version == SCHEMA_VERSION:
        _validate_work_graph_v2(work_by_id)

    actual_active_work_ids = {item["work_id"] for item in works if item["status"] == "active"}
    if schema_version == SCHEMA_VERSION and any(
        work_by_id[work_id]["kind"] not in {"work", "experiment"}
        for work_id in actual_active_work_ids
    ):
        raise TypedStateError("active work must be an executable leaf")
    if set(active_work_ids) != actual_active_work_ids:
        raise TypedStateError("project.active_work_ids must match active Work status")
    primary_work_id = _optional_string(project["primary_work_id"], "project.primary_work_id")
    if primary_work_id is None and active_work_ids:
        raise TypedStateError("project.primary_work_id is required when work is active")
    if primary_work_id is not None and primary_work_id not in actual_active_work_ids:
        raise TypedStateError("project.primary_work_id must reference active work")

    for item in ideas:
        _enum(item["status"], _IDEA_STATUSES, "idea.status")
        _string(item["source_ref"], "idea.source_ref")
        _string(item["summary"], "idea.summary")
        if item["parent_work_id"] not in work_by_id or item["return_work_id"] not in work_by_id:
            raise TypedStateError("idea work reference is unknown")
        _timestamp(item["expiry"], "idea.expiry", optional=True)
        if item["attempt_budget"] is not None:
            _uint(item["attempt_budget"], "idea.attempt_budget")
        _optional_string(item["promotion_target"], "idea.promotion_target")
        idea_evidence = _strings(item["evidence_ids"], "idea.evidence_ids")
        _references(idea_evidence, evidence_by_id, "idea.evidence_ids")

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
        raise TypedStateError("project.current_decision_ids must contain only accepted decisions")
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
        raise TypedStateError("project.active_constraint_ids must contain only active constraints")
    _acyclic_supersedes(
        constraint_by_id, "supersedes_constraint_id", "constraint"
    )
    actual_open_blockers = {item["blocker_id"] for item in blockers if item["status"] == "open"}
    if set(open_blocker_ids) != actual_open_blockers:
        raise TypedStateError("project.open_blocker_ids must contain only open blockers")

    active_claims: dict[str, dict[str, Any]] = {}
    owned_scopes: dict[tuple[str, str], str] = {}
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
        lease_expires_at = _timestamp(item["lease_expires_at"], "claim.lease_expires_at")
        released_at = _timestamp(item["released_at"], "claim.released_at", optional=True)
        assert claimed_at is not None and lease_expires_at is not None
        if lease_expires_at <= claimed_at:
            raise TypedStateError("claim lease must expire after claimed_at")
        scopes = [_scope(scope, "claim.scope_owner") for scope in item["scope_owners"]]
        if not scopes or len(scopes) != len(set(scopes)):
            raise TypedStateError("claim.scope_owners must be unique and non-empty")
        work_scopes = {
            _scope(scope, "work.scope_ref")
            for scope in work_by_id[work_id]["scope_refs"]
        }
        if not set(scopes).issubset(work_scopes):
            raise TypedStateError("claim scope must belong to its work")
        if status == "active":
            if work_by_id[work_id]["status"] != "active":
                raise TypedStateError("active claim requires active work")
            if expected_revision != revision:
                raise TypedStateError("active claim requires expected project revision")
            if released_at is not None:
                raise TypedStateError("active claim cannot have released_at")
            if lease_expires_at <= updated_at:
                raise TypedStateError("active claim lease has expired at snapshot time")
            if work_id in active_claims:
                raise TypedStateError("active work can have only one active claim")
            active_claims[work_id] = item
            for scope in scopes:
                if scope in owned_scopes:
                    raise TypedStateError("active claim scope ownership conflict")
                owned_scopes[scope] = item["claim_id"]
        elif released_at is None:
            raise TypedStateError("inactive claim requires released_at")

    if set(active_claims) != actual_active_work_ids:
        raise TypedStateError("every active work requires one active claim")

    effect_keys: set[str] = set()
    sequence_numbers: set[int] = set()
    committed_sequences: list[int] = []
    for item in effects:
        effect_key = _string(item["effect_key"], "effect.effect_key")
        if effect_key in effect_keys:
            raise TypedStateError("effect.effect_key must be unique")
        effect_keys.add(effect_key)
        work_id = item["work_id"]
        if work_id not in work_by_id:
            raise TypedStateError("effect.work_id is unknown")
        claim_id = _optional_string(item["claim_id"], "effect.claim_id")
        if claim_id is not None and claim_id not in claim_by_id:
            raise TypedStateError("effect.claim_id is unknown")
        status = _enum(item["status"], _EFFECT_STATUSES, "effect.status")
        _string(item["operation"], "effect.operation")
        scope = _scope(item["scope_ref"], "effect.scope_ref")
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
        completed_at = _timestamp(item["completed_at"], "effect.completed_at", optional=True)
        if status in {"authorized", "started"}:
            if expected_revision != revision:
                raise TypedStateError("authorized effect requires expected project revision")
            if claim_id is None or claim_by_id[claim_id]["status"] != "active":
                raise TypedStateError("authorized effect requires a current active claim")
            if claim_by_id[claim_id]["work_id"] != work_id:
                raise TypedStateError("effect claim must belong to its work")
            claim_scopes = {
                _scope(value, "claim.scope_owner")
                for value in claim_by_id[claim_id]["scope_owners"]
            }
            if scope not in claim_scopes:
                raise TypedStateError("effect scope must be owned by its claim")
            if work_id not in actual_active_work_ids:
                raise TypedStateError("authorized effect requires active work")
        if status in {"succeeded", "failed", "compensated"}:
            if claim_id is None:
                raise TypedStateError("committed effect requires claim provenance")
            if completed_at is None or result_ref is None:
                raise TypedStateError("committed effect requires result and completion time")
            committed_sequences.append(sequence_no)
        elif completed_at is not None or result_ref is not None:
            raise TypedStateError("uncommitted effect cannot have completion result")

    actual_high_watermark = max(committed_sequences, default=0)
    if high_watermark != actual_high_watermark:
        raise TypedStateError("project.effect_high_watermark does not match committed effects")


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
