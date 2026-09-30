"""Bounded collaboration packet composition."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .collaboration_registry import (
    CollaborationRegistryError,
    default_data_root,
    next_task,
)
from .vocabulary_memory import VocabularyMemoryError
from .vocabulary_memory import resolve as resolve_vocabulary


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
        raise CollaborationPacketError("max_entities must be between 1 and 10")
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
        }
    packet = {
        "schema_version": PACKET_SCHEMA,
        "project_id": project_id,
        "task": task_projection,
        "vocabulary": entities,
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
    size = len(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    if size > MAX_PACKET_BYTES:
        raise CollaborationPacketError(
            f"collaboration packet exceeds {MAX_PACKET_BYTES} bytes"
        )
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
    except (CollaborationRegistryError, VocabularyMemoryError, CollaborationPacketError):
        return {
            "schema_version": "context.collaboration-degraded/v1alpha1",
            "project_id": project_id,
            "continue": True,
            "metadata_status": "degraded",
            "local_next_action": (
                "Continue the current user intent and ordinary local work; "
                "collaboration metadata is not an execution permission"
            ),
        }
