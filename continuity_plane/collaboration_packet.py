"""Bounded collaboration packet composition."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .collaboration_registry import (
    CollaborationRegistryError,
    default_data_root,
    next_task,
    list_tasks,
    list_projects,
)
from .vocabulary_memory import VocabularyMemoryError
from .vocabulary_memory import resolve as resolve_vocabulary
from .intent_todo import load_queue
from .collaboration_inbox import list_messages
from .collaboration_context import list_rules, list_commitments
from .executor_routing import for_task


PACKET_SCHEMA = "context.collaboration-packet/v1alpha1"
MAX_PACKET_BYTES = 8192


class CollaborationPacketError(ValueError):
    """A collaboration packet cannot be composed."""


def compose_collaboration_packet(
    data_root: Path | str | None,
    *,
    project_id: str,
    query: str = "",
    assignee: str | None = None,
    max_entities: int = 3,
) -> dict[str, Any]:
    """Return one bounded task card and the vocabulary needed by its prompt."""
    root = data_root or default_data_root()
    if type(max_entities) is not int or not 1 <= max_entities <= 10:
        raise CollaborationPacketError("max_entities must be between 1 and 10; use the default when unsure")
    if assignee is not None and not str(assignee).strip():
        raise CollaborationPacketError("assignee is empty; omit it to request a controller overview")
    task = next_task(root, project_id=project_id, assignee=assignee)
    entities = resolve_vocabulary(root, query, project_id=project_id)[:max_entities]
    task_projection = None
    if task is not None:
        task_projection = {
            "task_id": task["task_id"],
            "lane_id": task["lane_id"],
            "mode": task["mode"],
            "priority": task["priority"],
            "status": task["status"],
            "title": task["title"],
            "objective": task["objective"],
            "next_action": task["next_action"],
            "assignee": task["assignee"],
            "worktree": task["worktree"],
            "allowed_files": task["allowed_files"],
            "preauthorized_effects": task["preauthorized_effects"],
            "queued_effects": task["queued_effects"],
            "exit_criteria": task["exit_criteria"],
            "report_policy": task["report_policy"],
            "branch": task.get("branch"),
            "commit": task.get("commit"),
            "verification": task.get("verification"),
            "card_updated_at": task["updated_at"],
        }
    packet = {
        "schema_version": PACKET_SCHEMA,
        "project_id": project_id,
        "task": task_projection,
        "vocabulary": entities,
        "execution_preferences": for_task(root, project_id, (task or {}).get("task_id")),
        "identity": {
            "assignee": assignee,
            "session_id": os.environ.get("CODEX_SESSION_ID") or os.environ.get("".join(("CL", "AUDE", "_SESSION_ID"))),
            "controller": os.environ.get("CONTINUITY_CONTROLLER"),
        },
        "execution_contract": {
            "authority": "collaboration-hint-only",
            "local_work_continues_when_metadata_is_missing": True,
            "local_completion_status": "ready-for-review",
            "report_policy": task["report_policy"] if task else "on_blocker",
            "activation_allowed": bool(task and task["status"] == "active"),
            "activation_note": (
                "queued task is a projection only; do not activate while another "
                "authoritative Work is active"
                if task and task["status"] == "queued"
                else None
            ),
        },
    }
    if assignee is not None and task is None:
        packet["assignment"] = {"assignee": assignee, "status": "no-card-assigned"}
    # A primary card is useful for execution; this bounded lane summary keeps
    # a controller from dumping the full dispatch ledger into context.
    try:
        all_tasks = list_tasks(root, project_id=project_id)
        packet["in_flight"] = [
            {key: item[key] for key in ("task_id", "status", "assignee", "lane_id", "title", "next_action", "updated_at")}
            for item in all_tasks
            if item["status"] in {"active", "dispatched", "blocked"}
        ]
        packet["in_flight"].sort(key=lambda item: (item["updated_at"], item["task_id"]), reverse=True)
    except CollaborationRegistryError:
        packet["in_flight"] = []
    registration = next((item for item in list_projects(root) if item["project_id"] == project_id), None)
    queue = load_queue(registration["control_root"]) if registration else None
    if queue:
        packet["todo"] = {
            "source": "intent-todo-sidecar",
            "items": [
                {"item_id": item.get("item_id"), "status": item.get("status"), "text": item.get("text") or item.get("title"), "result": item.get("result"), "blocked_reason": item.get("blocked_reason"), "task_id": item.get("task_id")}
                for item in queue.get("items", [])[:12]
            ],
        }
    rules = list_rules(root, project_id=project_id)
    commitments = list_commitments(root, project_id=project_id)
    packet["rules"] = [{"rule_id": row["rule_id"], "text": row["text"][:240], "audience": row["audience"]} for row in rules[:8]]
    packet["rule_count"] = len(rules)
    packet["commitments"] = [{"commitment_id": row["commitment_id"], "text": row["text"][:240]} for row in commitments[:8]]
    packet["commitment_count"] = len(commitments)
    messages = list_messages(root, project_id=project_id, recipient=assignee, limit=1000) if assignee else []
    packet["unread_message_count"] = len(messages)
    if messages:
        packet["inbox"] = [{**item, "body": item["body"][:512], "authority": "handoff-hint-only"} for item in messages]
        while packet["inbox"] and len(json.dumps(packet, ensure_ascii=False).encode("utf-8")) > MAX_PACKET_BYTES:
            packet["inbox"].pop()
    size = len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    original_size = size
    if size > MAX_PACKET_BYTES:
        packet["truncated"] = True
        # Preserve the primary card. Trim verbose fields before dropping the
        # oldest in-flight cards and todo entries.
        if packet.get("task"):
            packet["task"]["next_action"] = packet["task"]["next_action"][:240]
            packet["task"]["objective"] = packet["task"]["objective"][:240]
        for item in packet.get("in_flight", []):
            item["next_action"] = item["next_action"][:160]
        for item in packet.get("todo", {}).get("items", []):
            item["text"] = (item.get("text") or "")[:160]
        while len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")) > MAX_PACKET_BYTES and packet.get("in_flight"):
            packet["in_flight"].pop()
        while len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")) > MAX_PACKET_BYTES and packet.get("todo", {}).get("items"):
            packet["todo"]["items"].pop()
        while len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")) > MAX_PACKET_BYTES and packet.get("vocabulary"):
            packet["vocabulary"].pop()
        while len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8")) > MAX_PACKET_BYTES and packet.get("inbox"):
            packet["inbox"].pop()
        size = len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if size > MAX_PACKET_BYTES:
            packet["in_flight"] = []
            packet["todo"] = {"source": "intent-todo-sidecar", "items": []}
            packet["vocabulary"] = []
            packet["inbox"] = []
            size = len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        packet["truncation"] = {"original_bytes": original_size, "returned_bytes": size, "budget_bytes": MAX_PACKET_BYTES}
        while True:
            packet["truncation"]["returned_bytes"] = len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            if packet["truncation"]["returned_bytes"] <= MAX_PACKET_BYTES or not packet.get("in_flight"):
                break
            packet["in_flight"].pop()
    return packet


def compose_safe_collaboration_packet(
    data_root: Path | str | None,
    *,
    project_id: str,
    query: str = "",
    assignee: str | None = None,
    max_entities: int = 3,
) -> dict[str, Any]:
    """Compose a packet or a non-blocking degraded receipt."""
    try:
        return compose_collaboration_packet(
            data_root,
            project_id=project_id,
            query=query,
            assignee=assignee,
            max_entities=max_entities,
        )
    except (OSError, CollaborationRegistryError, VocabularyMemoryError, CollaborationPacketError) as exc:
        return {
            "schema_version": "context.collaboration-degraded/v1alpha1",
            "project_id": project_id,
            "continue": True,
            "metadata_status": "degraded",
            "reason": str(exc),
            "local_next_action": (
                "Continue the current user intent and ordinary local work; "
                "collaboration metadata is not an execution permission"
            ),
        }
