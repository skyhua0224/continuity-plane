"""M3-01 Campaign/Goal/Work/Experiment graph validation."""

from __future__ import annotations

import copy
import json
from datetime import datetime
from typing import Any


SCHEMA_VERSION = "context.task-graph/v1alpha1"

_DOCUMENT_FIELDS = {
    "schema_version",
    "project_id",
    "state_revision",
    "root_ids",
    "nodes",
}
_NODE_FIELDS = {
    "node_id",
    "work_revision",
    "kind",
    "title",
    "parent_id",
    "dependency_ids",
    "return_point_id",
    "exit_criteria",
    "attempt_budget",
    "expiry",
    "promotion_target_id",
    "mainline_authority",
}
_KINDS = {"campaign", "goal", "work", "experiment"}
_PARENT_KINDS = {
    "campaign": set(),
    "goal": {"campaign", "goal"},
    "work": {"goal", "work"},
    "experiment": {"goal", "work", "experiment"},
}


class TaskGraphError(ValueError):
    """Raised when an M3-01 graph violates its deterministic contract."""


def _object(value: Any, expected: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise TaskGraphError(f"{field} fields do not match the contract")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskGraphError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _strings(value: Any, field: str, *, non_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise TaskGraphError(f"{field} must be a string list")
    if non_empty and not value:
        raise TaskGraphError(f"{field} must not be empty")
    if len(value) != len(set(value)):
        raise TaskGraphError(f"{field} must contain unique values")
    return value


def _uint(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TaskGraphError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    text = _string(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskGraphError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise TaskGraphError(f"{field} must include timezone")
    return parsed


def _assert_acyclic(
    nodes: dict[str, dict[str, Any]], edges: dict[str, list[str]], label: str
) -> None:
    indegree = {node_id: 0 for node_id in nodes}
    outgoing = {node_id: [] for node_id in nodes}
    for node_id, targets in edges.items():
        for target in set(targets):
            if target not in nodes:
                raise TaskGraphError(f"{label} contains an unknown reference")
            outgoing[node_id].append(target)
            indegree[target] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for target in outgoing[current]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited != len(nodes):
        raise TaskGraphError(f"{label} cycle is invalid")


def validate_task_graph(document: dict[str, Any]) -> None:
    """Validate the graph without reading provider state or causing effects."""
    document = _object(document, _DOCUMENT_FIELDS, "document")
    if document["schema_version"] != SCHEMA_VERSION:
        raise TaskGraphError("unsupported schema_version")
    _string(document["project_id"], "project_id")
    _uint(document["state_revision"], "state_revision")
    root_ids = _strings(document["root_ids"], "root_ids", non_empty=True)
    raw_nodes = document["nodes"]
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise TaskGraphError("nodes must be a non-empty object list")

    nodes: dict[str, dict[str, Any]] = {}
    for raw_node in raw_nodes:
        node = _object(raw_node, _NODE_FIELDS, "node")
        node_id = _string(node["node_id"], "node.node_id")
        if node_id in nodes:
            raise TaskGraphError("node.node_id must be unique")
        nodes[node_id] = node

    parent_edges: dict[str, list[str]] = {}
    dependency_edges: dict[str, list[str]] = {}
    for node_id, node in nodes.items():
        _uint(node["work_revision"], "node.work_revision")
        kind = node["kind"]
        if kind not in _KINDS:
            raise TaskGraphError("node.kind is unsupported")
        _string(node["title"], "node.title")
        parent_id = _optional_string(node["parent_id"], "node.parent_id")
        dependencies = _strings(node["dependency_ids"], "node.dependency_ids")
        if node_id in dependencies:
            raise TaskGraphError("node cannot depend on itself")
        parent_edges[node_id] = [] if parent_id is None else [parent_id]
        dependency_edges[node_id] = dependencies

    _assert_acyclic(nodes, parent_edges, "parent")
    _assert_acyclic(nodes, dependency_edges, "dependency")
    combined_edges = {
        node_id: list(parent_edges[node_id]) + list(dependency_edges[node_id])
        for node_id in nodes
    }
    _assert_acyclic(nodes, combined_edges, "combined-edge")

    actual_roots = {
        node_id for node_id, node in nodes.items() if node["parent_id"] is None
    }
    campaign_roots = {
        node_id
        for node_id in actual_roots
        if nodes[node_id]["kind"] == "campaign"
    }
    if actual_roots != campaign_roots or set(root_ids) != campaign_roots:
        raise TaskGraphError("root_ids must exactly project campaign roots; orphan node found")

    children: dict[str, list[str]] = {node_id: [] for node_id in nodes}
    for node_id, node in nodes.items():
        if node["parent_id"] is not None:
            children[node["parent_id"]].append(node_id)

    enter_exit: list[tuple[str, bool]] = [
        (root_id, False) for root_id in reversed(root_ids)
    ]
    entered: dict[str, int] = {}
    exited: dict[str, int] = {}
    traversal_index = 0
    while enter_exit:
        node_id, exiting = enter_exit.pop()
        if exiting:
            exited[node_id] = traversal_index
            traversal_index += 1
            continue
        entered[node_id] = traversal_index
        traversal_index += 1
        enter_exit.append((node_id, True))
        enter_exit.extend((child_id, False) for child_id in reversed(children[node_id]))

    def is_ancestor(ancestor_id: str, node_id: str) -> bool:
        return (
            entered[ancestor_id] < entered[node_id]
            and exited[node_id] < exited[ancestor_id]
        )

    for node_id, node in nodes.items():
        kind = node["kind"]
        parent_id = node["parent_id"]
        if kind == "campaign":
            if parent_id is not None:
                raise TaskGraphError("campaign parent kind is invalid")
        else:
            if parent_id is None:
                raise TaskGraphError("orphan non-campaign node is invalid")
            if nodes[parent_id]["kind"] not in _PARENT_KINDS[kind]:
                raise TaskGraphError(f"{kind} parent kind is invalid")

        if any(
            is_ancestor(dependency_id, node_id)
            for dependency_id in node["dependency_ids"]
        ):
            raise TaskGraphError("node cannot depend on an ancestor")
        if any(
            is_ancestor(node_id, dependency_id)
            for dependency_id in node["dependency_ids"]
        ):
            raise TaskGraphError("node dependency cannot point to a descendant")

        return_point_id = _optional_string(
            node["return_point_id"], "node.return_point_id"
        )
        promotion_target_id = _optional_string(
            node["promotion_target_id"], "node.promotion_target_id"
        )
        exit_criteria = _strings(node["exit_criteria"], "node.exit_criteria")
        attempt_budget = node["attempt_budget"]
        expiry = node["expiry"]
        authority = node["mainline_authority"]
        if not isinstance(authority, bool):
            raise TaskGraphError("node.mainline_authority must be boolean")

        if kind == "experiment":
            if return_point_id is None or not is_ancestor(return_point_id, node_id):
                raise TaskGraphError("experiment return point must be an ancestor")
            if promotion_target_id is None or not is_ancestor(
                promotion_target_id, node_id
            ):
                raise TaskGraphError("experiment promotion target must be an ancestor")
            if not nodes[promotion_target_id]["mainline_authority"]:
                raise TaskGraphError(
                    "experiment promotion target must have mainline authority"
                )
            if not exit_criteria:
                raise TaskGraphError("experiment exit criteria must not be empty")
            if (
                not isinstance(attempt_budget, int)
                or isinstance(attempt_budget, bool)
                or attempt_budget <= 0
            ):
                raise TaskGraphError("experiment attempt budget must be positive")
            _timestamp(expiry, "experiment.expiry")
            if authority:
                raise TaskGraphError("experiment cannot have mainline authority")
        elif any(
            value is not None
            for value in (
                return_point_id,
                attempt_budget,
                expiry,
                promotion_target_id,
            )
        ) or exit_criteria or not authority:
            raise TaskGraphError("non-experiment cannot claim experiment authority")


def canonical_task_graph_bytes(document: dict[str, Any]) -> bytes:
    """Return stable bytes for graph replay and digest binding."""
    validate_task_graph(document)
    canonical = copy.deepcopy(document)
    canonical["root_ids"] = sorted(canonical["root_ids"])
    for node in canonical["nodes"]:
        node["dependency_ids"] = sorted(node["dependency_ids"])
        node["exit_criteria"] = sorted(node["exit_criteria"])
    canonical["nodes"] = sorted(canonical["nodes"], key=lambda item: item["node_id"])
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_task_graph_against_state(
    graph: dict[str, Any], state: dict[str, Any]
) -> None:
    """Bind one M3 graph projection to its authoritative typed-state revision."""
    validate_task_graph(graph)
    try:
        from .typed_state import validate_typed_state

        validate_typed_state(state)
    except (TypeError, ValueError) as exc:
        raise TaskGraphError("typed state is invalid") from exc
    project = state["project"]
    if (
        graph["project_id"] != project.get("project_id")
        or graph["state_revision"] != project.get("revision")
    ):
        raise TaskGraphError("task graph does not match typed state authority")
    works = state.get("works")
    if not isinstance(works, list):
        raise TaskGraphError("typed state works are invalid")
    work_by_id = {
        item.get("work_id"): item for item in works if isinstance(item, dict)
    }
    graph_by_id = {item["node_id"]: item for item in graph["nodes"]}
    if set(work_by_id) != set(graph_by_id):
        raise TaskGraphError("task graph IDs do not match typed state")
    for work_id, node in graph_by_id.items():
        work = work_by_id[work_id]
        if (
            node["kind"] != work.get("kind")
            or node["title"] != work.get("title")
            or node["parent_id"] != work.get("parent_work_id")
            or sorted(node["dependency_ids"])
            != sorted(work.get("dependency_ids", []))
            or node["work_revision"] != work.get("revision")
        ):
            raise TaskGraphError("task graph node does not match typed state")
        if node["kind"] == "experiment" and (
            node["return_point_id"] != work.get("return_point_work_id")
            or sorted(node["exit_criteria"])
            != sorted(work.get("exit_criteria", []))
            or node["attempt_budget"] != work.get("attempt_budget")
            or node["expiry"] != work.get("expires_at")
            or node["promotion_target_id"]
            != work.get("promotion_target_work_id")
            or node["mainline_authority"] != work.get("mainline_authority")
        ):
            raise TaskGraphError("task graph node does not match typed state")
