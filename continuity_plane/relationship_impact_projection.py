"""Deterministic read-only Relationship and Impact view over Project Graph."""

from __future__ import annotations

import copy
import hashlib
import json
from collections import deque
from pathlib import Path
from typing import Any

from .codegraph_verification import (
    CodeGraphVerificationError,
    validate_codegraph_receipt,
)
from .external_state_provider import HMACExternalStateProjectionSigner
from .project_graph_projection import PROJECT_GRAPH_PROJECTION_SCHEMA_VERSION

RELATIONSHIP_IMPACT_PROJECTION_SCHEMA_VERSION = (
    "context.relationship-impact-projection/v1alpha1"
)
RELATIONSHIP_IMPACT_VIEW_REQUEST_SCHEMA_VERSION = (
    "context.relationship-impact-view-request/v1alpha1"
)

_REQUEST_FIELDS = {
    "schema_version",
    "project_id",
    "expected_state_revision",
    "focus_node_ids",
    "direction",
    "max_depth",
    "relation_kinds",
    "node_kinds",
    "include_terminal_work",
    "max_nodes",
    "max_edges",
}
_PROJECT_GRAPH_FIELDS = {
    "schema_version",
    "project_id",
    "state_revision",
    "state_schema_version",
    "state_sha256",
    "source_projection_sha256",
    "governance_ref",
    "primary_work_id",
    "observed_at",
    "graph",
    "active_work_set",
    "work_ledger",
    "health",
    "authority",
    "projection_sha256",
    "signature",
}
_RELATION_KINDS = {
    "parent",
    "dependency",
    "supersedes",
    "return-point",
    "promotion-target",
    "references",
}
_TERMINAL_WORK_STATUSES = {"completed", "rejected", "reverted", "superseded"}
_AUTHORITY = {
    "state_write_authority": False,
    "controlled_action_authority": False,
    "approval_authority": False,
    "completion_authority": False,
    "provider_authority": 0,
    "external_effect_authority": 0,
}
_MAX_FOCUS_NODES = 64
_MAX_DEPTH = 8
_MAX_SOURCE_NODES = 10_000
_MAX_SOURCE_EDGES = 50_000
_MAX_VIEW_NODES = 2_000
_MAX_VIEW_EDGES = 5_000
_MAX_CODEGRAPH_RECEIPTS = 64
_MAX_CODEGRAPH_CLUES = 16_384


class RelationshipImpactProjectionError(ValueError):
    """Raised when the relationship view is incoherent or tampered."""


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
        raise RelationshipImpactProjectionError(
            "relationship view is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _validate_signed_project_graph(
    projection: Any,
    *,
    signer: HMACExternalStateProjectionSigner,
) -> dict[str, Any]:
    if not isinstance(projection, dict) or set(projection) != _PROJECT_GRAPH_FIELDS:
        raise RelationshipImpactProjectionError("Project Graph fields are invalid")
    if projection.get("schema_version") != PROJECT_GRAPH_PROJECTION_SCHEMA_VERSION:
        raise RelationshipImpactProjectionError("Project Graph schema is unsupported")
    unsigned = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    if projection.get("projection_sha256") != _digest(unsigned):
        raise RelationshipImpactProjectionError("Project Graph digest is invalid")
    signed = {**unsigned, "projection_sha256": projection["projection_sha256"]}
    if not signer.verify(signed, projection.get("signature")):
        raise RelationshipImpactProjectionError("Project Graph signature is invalid")
    authority = projection.get("authority")
    if (
        not isinstance(authority, dict)
        or authority.get("state_write_authority") is not False
        or authority.get("controlled_action_authority") is not False
        or authority.get("provider_authority") != 0
        or authority.get("external_effect_authority") != 0
    ):
        raise RelationshipImpactProjectionError("Project Graph authority is invalid")
    graph = projection.get("graph")
    if not isinstance(graph, dict) or set(graph) != {"root_work_ids", "nodes", "edges"}:
        raise RelationshipImpactProjectionError("Project Graph content is invalid")
    if not isinstance(graph["nodes"], list) or len(graph["nodes"]) > _MAX_SOURCE_NODES:
        raise RelationshipImpactProjectionError("Project Graph nodes exceed capacity")
    if not isinstance(graph["edges"], list) or len(graph["edges"]) > _MAX_SOURCE_EDGES:
        raise RelationshipImpactProjectionError("Project Graph edges exceed capacity")
    return copy.deepcopy(projection)


def _normalize_request(
    request: Any,
    *,
    project_graph: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise RelationshipImpactProjectionError("view request fields are invalid")
    if request.get("schema_version") != RELATIONSHIP_IMPACT_VIEW_REQUEST_SCHEMA_VERSION:
        raise RelationshipImpactProjectionError("view request schema is unsupported")
    if request.get("project_id") != project_graph["project_id"]:
        raise RelationshipImpactProjectionError("view request project does not match")
    revision = request.get("expected_state_revision")
    if type(revision) is not int or revision < 0:
        raise RelationshipImpactProjectionError("expected State revision is invalid")
    if revision != project_graph["state_revision"]:
        raise RelationshipImpactProjectionError("expected State revision is stale")
    focus = request.get("focus_node_ids")
    if (
        not isinstance(focus, list)
        or len(focus) > _MAX_FOCUS_NODES
        or len(set(focus)) != len(focus)
        or any(not isinstance(item, str) or not item for item in focus)
    ):
        raise RelationshipImpactProjectionError("focus nodes are invalid")
    direction = request.get("direction")
    if direction not in {"incoming", "outgoing", "both"}:
        raise RelationshipImpactProjectionError("focus direction is invalid")
    depth = request.get("max_depth")
    if type(depth) is not int or not 0 <= depth <= _MAX_DEPTH:
        raise RelationshipImpactProjectionError("focus depth is invalid")
    relations = request.get("relation_kinds")
    if (
        not isinstance(relations, list)
        or not relations
        or len(set(relations)) != len(relations)
        or not set(relations) <= _RELATION_KINDS
    ):
        raise RelationshipImpactProjectionError("relation filter is invalid")
    node_kinds = request.get("node_kinds")
    if (
        not isinstance(node_kinds, list)
        or not node_kinds
        or len(set(node_kinds)) != len(node_kinds)
        or not set(node_kinds) <= {"symbol", "work"}
    ):
        raise RelationshipImpactProjectionError("node kind filter is invalid")
    if type(request.get("include_terminal_work")) is not bool:
        raise RelationshipImpactProjectionError("terminal Work filter is invalid")
    max_nodes = request.get("max_nodes")
    max_edges = request.get("max_edges")
    if type(max_nodes) is not int or not 1 <= max_nodes <= _MAX_VIEW_NODES:
        raise RelationshipImpactProjectionError("node limit is invalid")
    if type(max_edges) is not int or not 1 <= max_edges <= _MAX_VIEW_EDGES:
        raise RelationshipImpactProjectionError("edge limit is invalid")
    normalized = copy.deepcopy(request)
    normalized["focus_node_ids"] = sorted(focus)
    normalized["relation_kinds"] = sorted(relations)
    normalized["node_kinds"] = sorted(node_kinds)
    return normalized


def _work_nodes(project_graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "node_id": f"work:{work['work_id']}",
            "node_kind": "work",
            "work_id": work["work_id"],
            "work_kind": work["kind"],
            "title": work["title"],
            "status": work["status"],
            "scope_refs": copy.deepcopy(work["scope_refs"]),
            "owner_refs": copy.deepcopy(work["owner_refs"]),
            "blocker_ids": copy.deepcopy(work["blocker_ids"]),
            "is_primary": work["work_id"] == project_graph["primary_work_id"],
            "source_kind": "authoritative-state",
        }
        for work in sorted(
            project_graph["graph"]["nodes"], key=lambda item: item["work_id"]
        )
    ]


def _work_edges(project_graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "edge_id": (
                f"state:{edge['relation']}:{edge['source_work_id']}:"
                f"{edge['target_work_id']}"
            ),
            "source_node_id": f"work:{edge['source_work_id']}",
            "target_node_id": f"work:{edge['target_work_id']}",
            "relation": edge["relation"],
            "evidence_kind": "typed-state",
            "authority_class": "authoritative-state-relationship",
            "source_ref": project_graph["projection_sha256"],
        }
        for edge in sorted(
            project_graph["graph"]["edges"],
            key=lambda item: (
                item["relation"],
                item["source_work_id"],
                item["target_work_id"],
            ),
        )
    ]


def _codegraph_universe(
    receipts: list[dict[str, Any]] | None,
    *,
    roots: dict[str, str | Path] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    if receipts is None:
        if roots not in (None, {}):
            raise RelationshipImpactProjectionError(
                "CodeGraph roots require CodeGraph receipts"
            )
        return [], [], []
    if (
        not isinstance(receipts, list)
        or not receipts
        or len(receipts) > _MAX_CODEGRAPH_RECEIPTS
        or not isinstance(roots, dict)
    ):
        raise RelationshipImpactProjectionError("CodeGraph sources are invalid")
    receipt_digests = [
        receipt.get("receipt_sha256") if isinstance(receipt, dict) else None
        for receipt in receipts
    ]
    if (
        any(not isinstance(digest, str) for digest in receipt_digests)
        or len(set(receipt_digests)) != len(receipt_digests)
        or set(roots) != set(receipt_digests)
    ):
        raise RelationshipImpactProjectionError(
            "CodeGraph receipt roots are incomplete or duplicated"
        )
    clue_count = sum(
        len(receipt.get("clues", [])) if isinstance(receipt, dict) else 0
        for receipt in receipts
    )
    if clue_count > _MAX_CODEGRAPH_CLUES:
        raise RelationshipImpactProjectionError("CodeGraph clues exceed capacity")

    sources: list[dict[str, Any]] = []
    nodes_by_id: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    for receipt in sorted(receipts, key=lambda item: item["receipt_sha256"]):
        receipt_sha256 = receipt["receipt_sha256"]
        try:
            validate_codegraph_receipt(
                receipt,
                root=Path(roots[receipt_sha256]).resolve(),
            )
        except (CodeGraphVerificationError, OSError, TypeError) as exc:
            raise RelationshipImpactProjectionError(
                "CodeGraph receipt does not match its trusted root"
            ) from exc
        repositories = sorted(
            {
                repository
                for clue in receipt["clues"]
                for repository in (
                    clue["source_repository"],
                    clue["target_repository"],
                )
            }
        )
        sources.append(
            {
                "receipt_id": receipt["receipt_id"],
                "receipt_sha256": receipt_sha256,
                "verified_at": receipt["verified_at"],
                "repositories": repositories,
                "index_revisions": sorted(
                    {clue["index_revision"] for clue in receipt["clues"]}
                ),
                "clue_count": len(receipt["clues"]),
                "state_revision_binding": None,
                "authority_class": "verified-non-authoritative-clue",
            }
        )
        for clue in sorted(receipt["clues"], key=lambda item: item["clue_id"]):
            source_id = f"symbol:{clue['source_repository']}:{clue['source_symbol']}"
            target_id = f"symbol:{clue['target_repository']}:{clue['target_symbol']}"
            for node_id, repository, symbol in (
                (source_id, clue["source_repository"], clue["source_symbol"]),
                (target_id, clue["target_repository"], clue["target_symbol"]),
            ):
                candidate = {
                    "node_id": node_id,
                    "node_kind": "symbol",
                    "repository": repository,
                    "qualified_symbol": symbol,
                    "source_kind": "verified-code-clue",
                }
                existing = nodes_by_id.get(node_id)
                if existing is not None and existing != candidate:
                    raise RelationshipImpactProjectionError(
                        "CodeGraph symbol identity collides"
                    )
                nodes_by_id[node_id] = candidate
            edges.append(
                {
                    "edge_id": f"code:{receipt_sha256}:{clue['clue_id']}",
                    "source_node_id": source_id,
                    "target_node_id": target_id,
                    "relation": "references",
                    "evidence_kind": "rg+lsp",
                    "authority_class": "verified-non-authoritative-clue",
                    "source_ref": receipt_sha256,
                }
            )
    edge_ids = [edge["edge_id"] for edge in edges]
    if len(set(edge_ids)) != len(edge_ids):
        raise RelationshipImpactProjectionError("CodeGraph edge identity collides")
    return (
        sources,
        sorted(nodes_by_id.values(), key=lambda item: item["node_id"]),
        sorted(edges, key=lambda item: item["edge_id"]),
    )


def _focused_node_ids(
    *,
    all_node_ids: set[str],
    edges: list[dict[str, Any]],
    request: dict[str, Any],
) -> set[str]:
    focus = request["focus_node_ids"]
    if any(node_id not in all_node_ids for node_id in focus):
        raise RelationshipImpactProjectionError("focus node does not exist")
    if not focus:
        return set(all_node_ids)
    outgoing: dict[str, set[str]] = {node_id: set() for node_id in all_node_ids}
    incoming: dict[str, set[str]] = {node_id: set() for node_id in all_node_ids}
    for edge in edges:
        outgoing[edge["source_node_id"]].add(edge["target_node_id"])
        incoming[edge["target_node_id"]].add(edge["source_node_id"])
    selected = set(focus)
    queue: deque[tuple[str, int]] = deque((node_id, 0) for node_id in focus)
    while queue:
        node_id, depth = queue.popleft()
        if depth >= request["max_depth"]:
            continue
        neighbours: set[str] = set()
        if request["direction"] in {"outgoing", "both"}:
            neighbours.update(outgoing[node_id])
        if request["direction"] in {"incoming", "both"}:
            neighbours.update(incoming[node_id])
        for neighbour in sorted(neighbours):
            if neighbour not in selected:
                selected.add(neighbour)
                queue.append((neighbour, depth + 1))
    return selected


def _reachable_work_ids(
    focus_work_ids: set[str],
    dependency_edges: list[dict[str, Any]],
    *,
    reverse: bool,
) -> tuple[list[str], list[str]]:
    adjacency: dict[str, set[str]] = {}
    for edge in dependency_edges:
        source = edge["source_node_id"].removeprefix("work:")
        target = edge["target_node_id"].removeprefix("work:")
        if reverse:
            source, target = target, source
        adjacency.setdefault(source, set()).add(target)
    direct = sorted(
        {
            target
            for work_id in focus_work_ids
            for target in adjacency.get(work_id, set())
        }
        - focus_work_ids
    )
    reached = set(direct)
    queue = deque(direct)
    while queue:
        node_id = queue.popleft()
        for target in sorted(adjacency.get(node_id, set())):
            if target not in focus_work_ids and target not in reached:
                reached.add(target)
                queue.append(target)
    return direct, sorted(reached)


def _clusters(
    nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    node_ids = {node["node_id"] for node in nodes}
    adjacency = {node_id: set() for node_id in node_ids}
    for edge in edges:
        source = edge["source_node_id"]
        target = edge["target_node_id"]
        adjacency[source].add(target)
        adjacency[target].add(source)
    unseen = set(node_ids)
    result: list[dict[str, Any]] = []
    while unseen:
        start = min(unseen)
        members: set[str] = set()
        queue = deque([start])
        unseen.remove(start)
        while queue:
            current = queue.popleft()
            members.add(current)
            for neighbour in sorted(adjacency[current]):
                if neighbour in unseen:
                    unseen.remove(neighbour)
                    queue.append(neighbour)
        member_ids = sorted(members)
        result.append(
            {
                "cluster_id": f"cluster:{_digest(member_ids)[:16]}",
                "member_node_ids": member_ids,
                "node_count": len(member_ids),
            }
        )
    return sorted(result, key=lambda item: item["cluster_id"])


def build_relationship_impact_projection(
    project_graph_projection: dict[str, Any],
    *,
    view_request: dict[str, Any],
    signer: HMACExternalStateProjectionSigner,
    codegraph_receipts: list[dict[str, Any]] | None = None,
    codegraph_roots: dict[str, str | Path] | None = None,
) -> dict[str, Any]:
    """Build one bounded semantic view without producing graph coordinates."""
    project_graph = _validate_signed_project_graph(
        project_graph_projection,
        signer=signer,
    )
    request = _normalize_request(view_request, project_graph=project_graph)
    code_sources, code_nodes, code_edges = _codegraph_universe(
        codegraph_receipts,
        roots=codegraph_roots,
    )
    all_nodes = _work_nodes(project_graph) + code_nodes
    all_edges = _work_edges(project_graph) + code_edges
    allowed_nodes = [
        node
        for node in all_nodes
        if node["node_kind"] in request["node_kinds"]
        and (
            node["node_kind"] != "work"
            or request["include_terminal_work"]
            or node["status"] not in _TERMINAL_WORK_STATUSES
        )
    ]
    allowed_ids = {node["node_id"] for node in allowed_nodes}
    allowed_edges = [
        edge
        for edge in all_edges
        if edge["relation"] in request["relation_kinds"]
        and edge["source_node_id"] in allowed_ids
        and edge["target_node_id"] in allowed_ids
    ]
    selected_ids = _focused_node_ids(
        all_node_ids=allowed_ids,
        edges=allowed_edges,
        request=request,
    )
    nodes = [node for node in allowed_nodes if node["node_id"] in selected_ids]
    edges = [
        edge
        for edge in allowed_edges
        if edge["source_node_id"] in selected_ids
        and edge["target_node_id"] in selected_ids
    ]
    if len(nodes) > request["max_nodes"] or len(edges) > request["max_edges"]:
        raise RelationshipImpactProjectionError(
            "relationship view exceeds request limits"
        )
    focus_work_ids = {
        node_id.removeprefix("work:") for node_id in request["focus_node_ids"]
    }
    dependencies, all_dependencies = _reachable_work_ids(
        focus_work_ids,
        [edge for edge in allowed_edges if edge["relation"] == "dependency"],
        reverse=False,
    )
    dependents, all_dependents = _reachable_work_ids(
        focus_work_ids,
        [edge for edge in allowed_edges if edge["relation"] == "dependency"],
        reverse=True,
    )
    request_sha256 = _digest(request)
    projection: dict[str, Any] = {
        "schema_version": RELATIONSHIP_IMPACT_PROJECTION_SCHEMA_VERSION,
        "project_id": project_graph["project_id"],
        "state_revision": project_graph["state_revision"],
        "state_sha256": project_graph["state_sha256"],
        "project_graph_projection_sha256": project_graph["projection_sha256"],
        "observed_at": project_graph["observed_at"],
        "code_sources": code_sources,
        "view_request": request,
        "view_request_sha256": request_sha256,
        "nodes": nodes,
        "edges": edges,
        "clusters": _clusters(nodes, edges),
        "impact": {
            "focus_work_ids": sorted(focus_work_ids),
            "direct_dependency_work_ids": dependencies,
            "dependency_work_ids": all_dependencies,
            "direct_dependent_work_ids": dependents,
            "dependent_work_ids": all_dependents,
        },
        "completeness": {
            "source_node_count": len(all_nodes),
            "eligible_node_count": len(allowed_nodes),
            "returned_node_count": len(nodes),
            "excluded_node_count": len(all_nodes) - len(allowed_nodes),
            "outside_focus_node_count": len(allowed_nodes) - len(nodes),
            "source_edge_count": len(all_edges),
            "eligible_edge_count": len(allowed_edges),
            "returned_edge_count": len(edges),
            "excluded_edge_count": len(all_edges) - len(allowed_edges),
            "outside_focus_edge_count": len(allowed_edges) - len(edges),
            "truncated": False,
            "impact_complete": True,
        },
        "layout": {
            "algorithm": "force-directed",
            "seed_sha256": _digest(
                {
                    "project_graph_projection_sha256": project_graph[
                        "projection_sha256"
                    ],
                    "view_request_sha256": request_sha256,
                }
            ),
            "coordinates_authority": False,
            "interaction_state_persisted": False,
        },
        "navigation": {
            "allowed_route_kinds": ["work-detail", "project-graph"],
            "governance_submission_route": "context.human-governance/v1alpha1",
            "direct_state_tool_access": False,
        },
        "authority": copy.deepcopy(_AUTHORITY),
    }
    projection["projection_sha256"] = _digest(projection)
    projection["signature"] = signer.sign(projection)
    return projection


def validate_relationship_impact_projection(
    projection: dict[str, Any],
    *,
    project_graph_projection: dict[str, Any],
    view_request: dict[str, Any],
    signer: HMACExternalStateProjectionSigner,
    codegraph_receipts: list[dict[str, Any]] | None = None,
    codegraph_roots: dict[str, str | Path] | None = None,
) -> dict[str, Any]:
    """Rebuild the complete view from its signed Project Graph source."""
    if not isinstance(projection, dict):
        raise RelationshipImpactProjectionError("relationship projection is invalid")
    authority = projection.get("authority")
    if authority != _AUTHORITY:
        raise RelationshipImpactProjectionError(
            "relationship authority must remain zero"
        )
    expected = build_relationship_impact_projection(
        project_graph_projection,
        view_request=view_request,
        signer=signer,
        codegraph_receipts=codegraph_receipts,
        codegraph_roots=codegraph_roots,
    )
    if _canonical_bytes(projection) != _canonical_bytes(expected):
        raise RelationshipImpactProjectionError(
            "relationship projection does not match signed Project Graph"
        )
    return copy.deepcopy(projection)


__all__ = [
    "RELATIONSHIP_IMPACT_PROJECTION_SCHEMA_VERSION",
    "RELATIONSHIP_IMPACT_VIEW_REQUEST_SCHEMA_VERSION",
    "RelationshipImpactProjectionError",
    "build_relationship_impact_projection",
    "validate_relationship_impact_projection",
]
