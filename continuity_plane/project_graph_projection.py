"""Read-only Project Graph and active Work projection over signed State."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .effect_scope_gate import scopes_overlap
from .external_state_provider import (
    ExternalStateProjectionError,
    HMACExternalStateProjectionSigner,
    validate_external_state_projection,
)
from .typed_state import LEGACY_SCHEMA_VERSION, SHARED_WORK_SCHEMA_VERSION

PROJECT_GRAPH_PROJECTION_SCHEMA_VERSION = (
    "context.project-graph-projection/v1alpha1"
)

_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_TERMINAL_WORK_STATUSES = {"completed", "rejected", "reverted", "superseded"}
_WORK_STATUSES = (
    "proposed",
    "blocked",
    "ready",
    "active",
    "verifying",
    "completed",
    "rejected",
    "reverted",
    "superseded",
)
_AUTHORITY = {
    "state_write_authority": False,
    "controlled_action_authority": False,
    "provider_authority": 0,
    "external_effect_authority": 0,
}
_MAX_IDENTIFIER_LENGTH = 1024
_MAX_TEXT_LENGTH = 4096
_MAX_LIST_ITEMS = 10_000
_MAX_GRAPH_EDGES = 50_000
_MAX_HEALTH_FINDINGS = 50_000
PROJECT_GRAPH_MAX_SCOPE_COMPARISONS = 50_000
PROJECT_GRAPH_MAX_NESTED_ITEMS = 50_000


class ProjectGraphProjectionError(ValueError):
    """Raised when a Project Graph projection is incoherent or tampered."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectGraphProjectionError("projection is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise ProjectGraphProjectionError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectGraphProjectionError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProjectGraphProjectionError(f"{field} must include timezone")
    return parsed


def _combined_edges(works: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    edges: dict[str, list[str]] = {}
    for work_id, work in works.items():
        targets = list(work["dependency_ids"])
        if work["parent_work_id"] is not None:
            targets.append(work["parent_work_id"])
        edges[work_id] = sorted(set(targets))
    return edges


def _cycle_work_ids(works: dict[str, dict[str, Any]]) -> list[str]:
    adjacency = _combined_edges(works)
    seen: set[str] = set()
    finish_order: list[str] = []
    for start in sorted(adjacency):
        if start in seen:
            continue
        seen.add(start)
        stack: list[tuple[str, int]] = [(start, 0)]
        while stack:
            node_id, index = stack[-1]
            targets = adjacency[node_id]
            if index < len(targets):
                target = targets[index]
                stack[-1] = (node_id, index + 1)
                if target not in seen:
                    seen.add(target)
                    stack.append((target, 0))
                continue
            finish_order.append(node_id)
            stack.pop()

    reverse: dict[str, list[str]] = {work_id: [] for work_id in adjacency}
    for source, targets in adjacency.items():
        for target in targets:
            reverse[target].append(source)
    cycle_ids: set[str] = set()
    assigned: set[str] = set()
    for start in reversed(finish_order):
        if start in assigned:
            continue
        component: list[str] = []
        assigned.add(start)
        stack = [start]
        while stack:
            current = stack.pop()
            component.append(current)
            for target in reverse[current]:
                if target not in assigned:
                    assigned.add(target)
                    stack.append(target)
        if len(component) > 1 or start in adjacency[start]:
            cycle_ids.update(component)
    return sorted(cycle_ids)


def _orphan_work_ids(works: dict[str, dict[str, Any]]) -> list[str]:
    children: dict[str, list[str]] = {work_id: [] for work_id in works}
    campaign_roots: list[str] = []
    for work_id, work in works.items():
        parent_id = work["parent_work_id"]
        if parent_id is None:
            if work["kind"] == "campaign":
                campaign_roots.append(work_id)
        else:
            children[parent_id].append(work_id)
    reachable: set[str] = set()
    stack = sorted(campaign_roots, reverse=True)
    while stack:
        work_id = stack.pop()
        if work_id in reachable:
            continue
        reachable.add(work_id)
        stack.extend(sorted(children[work_id], reverse=True))
    return sorted(set(works) - reachable)


def _work_scope_overlap_candidates(
    open_works: list[dict[str, Any]], *, legacy: bool
) -> list[dict[str, Any]]:
    by_id = {work["work_id"]: work for work in open_works}
    declared_pairs = {
        tuple(sorted((work["work_id"], candidate_id)))
        for work in open_works
        for candidate_id in work["overlap_candidate_ids"]
        if candidate_id in by_id
    }
    findings: list[dict[str, Any]] = []
    for left_id, right_id in sorted(declared_pairs):
        left = by_id[left_id]
        right = by_id[right_id]
        for left_scope in left["scope_refs"]:
            for right_scope in right["scope_refs"]:
                overlaps = (
                    left_scope == right_scope
                    if legacy
                    else scopes_overlap(left_scope, right_scope)
                )
                if overlaps:
                    findings.append(
                        {
                            "left_work_id": left_id,
                            "right_work_id": right_id,
                            "left_scope": copy.deepcopy(left_scope),
                            "right_scope": copy.deepcopy(right_scope),
                        }
                    )
                    if len(findings) > _MAX_HEALTH_FINDINGS:
                        raise ProjectGraphProjectionError(
                            "work overlap findings exceed the projection contract"
                        )
    return findings


def _bounded_string(value: Any, field: str, *, maximum: int) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ProjectGraphProjectionError(f"{field} exceeds the projection contract")


def _bounded_identifiers(values: list[Any], field: str) -> None:
    if len(values) > _MAX_LIST_ITEMS:
        raise ProjectGraphProjectionError(f"{field} exceeds the projection contract")
    for value in values:
        _bounded_string(value, field, maximum=_MAX_IDENTIFIER_LENGTH)


def _add_nested_items(current: int, added: int) -> int:
    total = current + added
    if total > PROJECT_GRAPH_MAX_NESTED_ITEMS:
        raise ProjectGraphProjectionError(
            "nested items exceed the projection contract"
        )
    return total


def _validate_projection_capacity(snapshot: dict[str, Any]) -> None:
    works = snapshot["works"]
    if len(works) > _MAX_LIST_ITEMS:
        raise ProjectGraphProjectionError("works exceed the projection contract")
    project = snapshot["project"]
    _bounded_string(
        project["project_id"], "project.project_id", maximum=_MAX_IDENTIFIER_LENGTH
    )
    _bounded_string(
        project["governance_ref"],
        "project.governance_ref",
        maximum=_MAX_TEXT_LENGTH,
    )
    _bounded_identifiers(project["active_work_ids"], "project.active_work_ids")
    if project["primary_work_id"] is not None:
        _bounded_string(
            project["primary_work_id"],
            "project.primary_work_id",
            maximum=_MAX_IDENTIFIER_LENGTH,
        )

    edge_count = 0
    overlap_reference_count = 0
    nested_item_count = len(project["active_work_ids"])
    for work in works:
        _bounded_string(work["work_id"], "work.work_id", maximum=_MAX_IDENTIFIER_LENGTH)
        _bounded_string(work["title"], "work.title", maximum=_MAX_TEXT_LENGTH)
        for field in (
            "dependency_ids",
            "owner_refs",
            "blocker_ids",
            "overlap_candidate_ids",
        ):
            nested_item_count = _add_nested_items(
                nested_item_count, len(work[field])
            )
            _bounded_identifiers(work[field], f"work.{field}")
        if "exit_criteria" in work:
            nested_item_count = _add_nested_items(
                nested_item_count, len(work["exit_criteria"])
            )
            _bounded_identifiers(work["exit_criteria"], "work.exit_criteria")
        if len(work["scope_refs"]) > _MAX_LIST_ITEMS:
            raise ProjectGraphProjectionError(
                "work.scope_refs exceed the projection contract"
            )
        nested_item_count = _add_nested_items(
            nested_item_count, len(work["scope_refs"])
        )
        for scope in work["scope_refs"]:
            _bounded_string(
                scope["scope_ref"], "work.scope_ref", maximum=_MAX_TEXT_LENGTH
            )
        for field in (
            "parent_work_id",
            "supersedes_work_id",
            "return_point_work_id",
            "promotion_target_work_id",
            "work_source_ref",
        ):
            value = work.get(field)
            if value is not None:
                _bounded_string(value, f"work.{field}", maximum=_MAX_IDENTIFIER_LENGTH)
        edge_count += len(work["dependency_ids"])
        overlap_reference_count += len(work["overlap_candidate_ids"])
        edge_count += sum(
            work.get(field) is not None
            for field in (
                "parent_work_id",
                "supersedes_work_id",
                "return_point_work_id",
                "promotion_target_work_id",
            )
        )
    if edge_count > _MAX_GRAPH_EDGES:
        raise ProjectGraphProjectionError("graph edges exceed the projection contract")
    if overlap_reference_count > _MAX_HEALTH_FINDINGS:
        raise ProjectGraphProjectionError(
            "overlap references exceed the projection contract"
        )
    open_works = {
        work["work_id"]: work
        for work in works
        if work["status"] not in _TERMINAL_WORK_STATUSES
    }
    declared_pairs = {
        tuple(sorted((work_id, candidate_id)))
        for work_id, work in open_works.items()
        for candidate_id in work["overlap_candidate_ids"]
        if candidate_id in open_works
    }
    scope_comparisons = sum(
        len(open_works[left_id]["scope_refs"])
        * len(open_works[right_id]["scope_refs"])
        for left_id, right_id in declared_pairs
    )
    if scope_comparisons > PROJECT_GRAPH_MAX_SCOPE_COMPARISONS:
        raise ProjectGraphProjectionError(
            "scope comparisons exceed the projection contract"
        )

    active_claims = [
        claim for claim in snapshot["claims"] if claim["status"] == "active"
    ]
    if len(active_claims) > _MAX_LIST_ITEMS:
        raise ProjectGraphProjectionError("active claims exceed the projection contract")
    active_claim_scope_count = 0
    for claim in active_claims:
        for field in ("claim_id", "work_id", "actor_ref"):
            _bounded_string(
                claim[field], f"claim.{field}", maximum=_MAX_IDENTIFIER_LENGTH
            )
        if len(claim["scope_owners"]) > _MAX_LIST_ITEMS:
            raise ProjectGraphProjectionError(
                "claim.scope_owners exceed the projection contract"
            )
        active_claim_scope_count += len(claim["scope_owners"])
        nested_item_count = _add_nested_items(
            nested_item_count, len(claim["scope_owners"])
        )
        for scope in claim["scope_owners"]:
            _bounded_string(
                scope["scope_ref"], "claim.scope_owner", maximum=_MAX_TEXT_LENGTH
            )
    if active_claim_scope_count > _MAX_HEALTH_FINDINGS:
        raise ProjectGraphProjectionError(
            "active claim scopes exceed the projection contract"
        )

    open_blockers = [
        blocker for blocker in snapshot["blockers"] if blocker["status"] == "open"
    ]
    if len(open_blockers) > _MAX_LIST_ITEMS:
        raise ProjectGraphProjectionError("open blockers exceed the projection contract")
    for blocker in open_blockers:
        _bounded_string(
            blocker["blocker_id"],
            "blocker.blocker_id",
            maximum=_MAX_IDENTIFIER_LENGTH,
        )
        _bounded_string(
            blocker["reason"], "blocker.reason", maximum=_MAX_TEXT_LENGTH
        )
        _bounded_identifiers(blocker["blocked_work_ids"], "blocker.blocked_work_ids")
        _bounded_identifiers(blocker["evidence_ids"], "blocker.evidence_ids")
        nested_item_count = _add_nested_items(
            nested_item_count,
            len(blocker["blocked_work_ids"]) + len(blocker["evidence_ids"]),
        )


def _graph(works: dict[str, dict[str, Any]], *, legacy: bool) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, str]] = []
    for work_id in sorted(works):
        work = works[work_id]
        nodes.append(
            {
                "work_id": work_id,
                "kind": work["kind"],
                "title": work["title"],
                "status": work["status"],
                "parent_work_id": work["parent_work_id"],
                "dependency_ids": sorted(work["dependency_ids"]),
                "supersedes_work_id": work["supersedes_work_id"],
                "owner_refs": sorted(work["owner_refs"]),
                "scope_refs": sorted(
                    copy.deepcopy(work["scope_refs"]),
                    key=lambda item: (item["scope_kind"], item["scope_ref"]),
                ),
                "blocker_ids": sorted(work["blocker_ids"]),
                "overlap_candidate_ids": sorted(work["overlap_candidate_ids"]),
                "dedupe_status": work["dedupe_status"],
                "return_point_work_id": (
                    None if legacy else work["return_point_work_id"]
                ),
                "exit_criteria": [] if legacy else sorted(work["exit_criteria"]),
                "attempt_budget": None if legacy else work["attempt_budget"],
                "expires_at": None if legacy else work["expires_at"],
                "promotion_target_work_id": (
                    None if legacy else work["promotion_target_work_id"]
                ),
                "mainline_authority": (
                    None if legacy else work["mainline_authority"]
                ),
                "work_revision": work["revision"],
                "work_source_ref": work.get("work_source_ref"),
                "source_revision": work.get("source_revision"),
                "work_identity_sha256": work.get("work_identity_sha256"),
                "dedupe_receipt_sha256": work.get("dedupe_receipt_sha256"),
            }
        )
        relationships = [
            ("parent", work["parent_work_id"]),
            ("supersedes", work["supersedes_work_id"]),
            ("return-point", None if legacy else work["return_point_work_id"]),
            (
                "promotion-target",
                None if legacy else work["promotion_target_work_id"],
            ),
        ]
        relationships.extend(("dependency", item) for item in work["dependency_ids"])
        edges.extend(
            {
                "source_work_id": work_id,
                "target_work_id": target,
                "relation": relation,
            }
            for relation, target in relationships
            if target is not None
        )
    return {
        "root_work_ids": sorted(
            work_id
            for work_id, work in works.items()
            if work["parent_work_id"] is None
        ),
        "nodes": nodes,
        "edges": sorted(
            edges,
            key=lambda item: (
                item["source_work_id"],
                item["relation"],
                item["target_work_id"],
            ),
        ),
    }


def _active_work_set(
    snapshot: dict[str, Any],
    works: dict[str, dict[str, Any]],
    *,
    legacy: bool,
    observed_at: datetime,
) -> list[dict[str, Any]]:
    active_claims = {
        claim["work_id"]: claim
        for claim in snapshot["claims"]
        if claim["status"] == "active"
    }
    result: list[dict[str, Any]] = []
    for work_id in sorted(snapshot["project"]["active_work_ids"]):
        work = works[work_id]
        claim = active_claims.get(work_id)
        result.append(
            {
                "work_id": work_id,
                "kind": work["kind"],
                "title": work["title"],
                "status": work["status"],
                "work_revision": work["revision"],
                "owner_refs": sorted(work["owner_refs"]),
                "scope_refs": sorted(
                    copy.deepcopy(work["scope_refs"]),
                    key=lambda item: (item["scope_kind"], item["scope_ref"]),
                ),
                "dependency_ids": sorted(work["dependency_ids"]),
                "blocker_ids": sorted(work["blocker_ids"]),
                "overlap_candidate_ids": sorted(work["overlap_candidate_ids"]),
                "dedupe_status": work["dedupe_status"],
                "return_point_work_id": (
                    None if legacy else work["return_point_work_id"]
                ),
                "expires_at": None if legacy else work["expires_at"],
                "promotion_target_work_id": (
                    None if legacy else work["promotion_target_work_id"]
                ),
                "mainline_authority": (
                    None if legacy else work["mainline_authority"]
                ),
                "work_source_ref": work.get("work_source_ref"),
                "source_revision": work.get("source_revision"),
                "work_identity_sha256": work.get("work_identity_sha256"),
                "dedupe_receipt_sha256": work.get("dedupe_receipt_sha256"),
                "claim": (
                    None
                    if claim is None
                    else {
                        "claim_id": claim["claim_id"],
                        "actor_ref": claim["actor_ref"],
                        "status": claim["status"],
                        "claimed_at": claim["claimed_at"],
                        "lease_expires_at": claim["lease_expires_at"],
                        "lease_expired_at_observation": _timestamp(
                            claim["lease_expires_at"],
                            "claim.lease_expires_at",
                        )
                        <= observed_at,
                        "scope_owners": sorted(
                            copy.deepcopy(claim["scope_owners"]),
                            key=lambda item: (
                                item["scope_kind"],
                                item["scope_ref"],
                            ),
                        ),
                        "claim_revision": claim.get("claim_revision"),
                        "lease_epoch": claim.get("lease_epoch"),
                        "last_heartbeat_at": claim.get("last_heartbeat_at"),
                        "closed_at": claim.get("closed_at"),
                        "closed_by_ref": claim.get("closed_by_ref"),
                        "close_reason": claim.get("close_reason"),
                        "reclaimed_from_claim_id": claim.get(
                            "reclaimed_from_claim_id"
                        ),
                    }
                ),
            }
        )
    return result


def build_project_graph_projection(
    source_projection: dict[str, Any],
    *,
    signer: HMACExternalStateProjectionSigner,
    observed_at: str,
) -> dict[str, Any]:
    """Build one immutable view from an authenticated State projection."""
    observed = _timestamp(observed_at, "observed_at")
    try:
        source = validate_external_state_projection(
            source_projection,
            signer=signer,
        )
    except (ExternalStateProjectionError, ValueError) as exc:
        raise ProjectGraphProjectionError("external State projection is invalid") from exc
    snapshot = source["snapshot"]
    _validate_projection_capacity(snapshot)
    works = {work["work_id"]: work for work in snapshot["works"]}
    legacy = snapshot["schema_version"] == LEGACY_SCHEMA_VERSION
    shared_work = snapshot["schema_version"] == SHARED_WORK_SCHEMA_VERSION
    snapshot_updated_at = _timestamp(
        snapshot["project"]["updated_at"],
        "project.updated_at",
    )
    if observed < snapshot_updated_at:
        raise ProjectGraphProjectionError("observed_at precedes the State snapshot")
    open_works = sorted(
        (
            work
            for work in snapshot["works"]
            if work["status"] not in _TERMINAL_WORK_STATUSES
        ),
        key=lambda item: item["work_id"],
    )
    expired_branches = sorted(
        work["work_id"]
        for work in snapshot["works"]
        if not legacy
        and work["kind"] == "experiment"
        and work["status"] not in _TERMINAL_WORK_STATUSES
        and _timestamp(work["expires_at"], "experiment.expires_at") <= observed
    )
    status_counts = {
        status: sum(work["status"] == status for work in snapshot["works"])
        for status in _WORK_STATUSES
    }
    open_blocker_count = sum(
        blocker["status"] == "open" for blocker in snapshot["blockers"]
    )
    active_claims = sorted(
        (claim for claim in snapshot["claims"] if claim["status"] == "active"),
        key=lambda item: item["claim_id"],
    )
    expired_active_claim_ids = sorted(
        claim["claim_id"]
        for claim in active_claims
        if _timestamp(claim["lease_expires_at"], "claim.lease_expires_at")
        <= observed
    )
    open_blockers = sorted(
        (
            {
                "blocker_id": blocker["blocker_id"],
                "reason": blocker["reason"],
                "blocked_work_ids": sorted(blocker["blocked_work_ids"]),
                "evidence_ids": sorted(blocker["evidence_ids"]),
                "opened_at": blocker["opened_at"],
            }
            for blocker in snapshot["blockers"]
            if blocker["status"] == "open"
        ),
        key=lambda item: item["blocker_id"],
    )
    duplicate_candidates = sorted(
        (
            {
                "work_id": work["work_id"],
                "candidate_work_ids": sorted(work["overlap_candidate_ids"]),
                "dedupe_status": work["dedupe_status"],
            }
            for work in snapshot["works"]
            if work["overlap_candidate_ids"]
        ),
        key=lambda item: item["work_id"],
    )
    overlap_candidate_pairs = {
        tuple(sorted((work["work_id"], candidate_id)))
        for work in snapshot["works"]
        for candidate_id in work["overlap_candidate_ids"]
    }
    experiment_branches = sorted(
        (
            {
                "work_id": work["work_id"],
                "status": work["status"],
                "return_point_work_id": work["return_point_work_id"],
                "exit_criteria": sorted(work["exit_criteria"]),
                "attempt_budget": work["attempt_budget"],
                "expires_at": work["expires_at"],
                "promotion_target_work_id": work["promotion_target_work_id"],
                "mainline_authority": work["mainline_authority"],
            }
            for work in snapshot["works"]
            if not legacy and work["kind"] == "experiment"
        ),
        key=lambda item: item["work_id"],
    )
    projection: dict[str, Any] = {
        "schema_version": PROJECT_GRAPH_PROJECTION_SCHEMA_VERSION,
        "project_id": source["project_id"],
        "state_revision": source["state_revision"],
        "state_schema_version": snapshot["schema_version"],
        "state_sha256": source["state_sha256"],
        "source_projection_sha256": source["projection_sha256"],
        "governance_ref": snapshot["project"]["governance_ref"],
        "primary_work_id": snapshot["project"]["primary_work_id"],
        "observed_at": observed_at,
        "graph": _graph(works, legacy=legacy),
        "active_work_set": _active_work_set(
            snapshot,
            works,
            legacy=legacy,
            observed_at=observed,
        ),
        "work_ledger": {
            "work_count": len(snapshot["works"]),
            "status_counts": status_counts,
            "active_work_count": len(snapshot["project"]["active_work_ids"]),
            "active_claim_count": sum(
                claim["status"] == "active" for claim in snapshot["claims"]
            ),
            "open_blocker_count": open_blocker_count,
            "overlap_candidate_pair_count": len(overlap_candidate_pairs),
            "capabilities": {
                "claim_fencing": shared_work,
                "work_provenance": shared_work,
            },
            "open_blockers": open_blockers,
            "duplicate_candidates": duplicate_candidates,
            "experiment_branches": experiment_branches,
        },
        "health": {
            "snapshot_updated_at": snapshot["project"]["updated_at"],
            "snapshot_age_ms": int(
                (observed - snapshot_updated_at).total_seconds() * 1000
            ),
            "cycle_work_ids": _cycle_work_ids(works),
            "orphan_work_ids": _orphan_work_ids(works),
            "expired_branch_work_ids": expired_branches,
            "expired_active_claim_ids": expired_active_claim_ids,
            "work_scope_overlap_candidates": _work_scope_overlap_candidates(
                open_works,
                legacy=legacy,
            ),
            "claim_ownership_overlaps": [],
        },
        "authority": copy.deepcopy(_AUTHORITY),
    }
    projection["projection_sha256"] = _digest(projection)
    projection["signature"] = signer.sign(projection)
    return projection


def validate_project_graph_projection(
    projection: dict[str, Any],
    *,
    source_projection: dict[str, Any],
    signer: HMACExternalStateProjectionSigner,
) -> dict[str, Any]:
    """Rebuild and verify a Project Graph projection against signed State."""
    if not isinstance(projection, dict):
        raise ProjectGraphProjectionError("projection must be an object")
    authority = projection.get("authority")
    if (
        not isinstance(authority, dict)
        or authority.get("state_write_authority") is not False
        or authority.get("controlled_action_authority") is not False
        or type(authority.get("provider_authority")) is not int
        or authority["provider_authority"] != 0
        or type(authority.get("external_effect_authority")) is not int
        or authority["external_effect_authority"] != 0
    ):
        raise ProjectGraphProjectionError("projection authority must remain zero")
    try:
        expected = build_project_graph_projection(
            source_projection,
            signer=signer,
            observed_at=projection.get("observed_at"),
        )
    except (ProjectGraphProjectionError, TypeError) as exc:
        raise ProjectGraphProjectionError("projection cannot be rebuilt") from exc
    if _canonical_bytes(projection) != _canonical_bytes(expected):
        raise ProjectGraphProjectionError("projection does not match signed State")
    return copy.deepcopy(projection)
