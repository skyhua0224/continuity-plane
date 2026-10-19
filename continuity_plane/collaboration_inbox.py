"""Durable local inbox with explicit acknowledgements; no provider execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .collaboration_registry import (
    CollaborationRegistryError,
    _ASSIGNEE_RE,
    _ID_RE,
    _atomic_write,
    _load,
    _locked,
    _now,
    _record_change,
    default_data_root,
    project_data_dir,
)
from .vocabulary_memory import _SECRET_RE


def _path(data_root: Path | str | None, project_id: str) -> Path:
    directory = project_data_dir(data_root or default_data_root(), project_id)
    if not (directory / "project.json").is_file():
        raise CollaborationRegistryError("project is not registered")
    return directory / "inbox.json"


def _messages(path: Path) -> list[dict[str, Any]]:
    if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
        raise CollaborationRegistryError("inbox exceeds its bounded storage limit")
    document = _load(path)
    if document is None:
        return []
    if document.get(
        "schema_version"
    ) != "context.collaboration-inbox/v1alpha1" or not isinstance(
        document.get("messages"), list
    ):
        raise CollaborationRegistryError("inbox is invalid")
    messages = document["messages"]
    fields = {
        "message_id",
        "sender",
        "recipient",
        "body",
        "task_id",
        "created_at",
        "acknowledged_at",
    }
    if len(messages) > 1000 or any(
        not isinstance(item, dict)
        or set(item) != fields
        or not all(
            isinstance(item[key], str)
            for key in ("message_id", "sender", "recipient", "body", "created_at")
        )
        for item in messages
    ):
        raise CollaborationRegistryError("inbox message fields are invalid")
    if len({item["message_id"] for item in messages}) != len(messages):
        raise CollaborationRegistryError("inbox message IDs must be unique")
    return messages


def unread_summary(data_root, *, project_id, recipient=None):
    rows = [r for r in _messages(_path(data_root, project_id))
            if r["acknowledged_at"] is None and (recipient is None or r["recipient"] == recipient)]
    return {"count": len(rows), "oldest_at": min((r["created_at"] for r in rows), default=None),
            "recipient": recipient, "scope": "recipient" if recipient else "project"}


def send_message(
    data_root: Path | str | None,
    *,
    project_id: str,
    message_id: str,
    sender: str,
    recipient: str,
    body: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    if (
        not _ID_RE.fullmatch(message_id)
        or not _ASSIGNEE_RE.fullmatch(sender)
        or not _ASSIGNEE_RE.fullmatch(recipient)
    ):
        raise CollaborationRegistryError("message ID, sender or recipient is invalid")
    if (
        not isinstance(body, str)
        or not body.strip()
        or len(body.encode("utf-8")) > 2048
    ):
        raise CollaborationRegistryError(
            "message body must contain 1..2048 UTF-8 bytes; use a file reference for longer handoffs"
        )
    path = _path(data_root, project_id)
    if _SECRET_RE.search(body):
        raise CollaborationRegistryError(
            "credential assignments must not be stored in inbox messages; use a protected reference"
        )
    with _locked(path):
        messages = _messages(path)
        existing = next((m for m in messages if m["message_id"] == message_id), None)
        payload = {
            "message_id": message_id,
            "sender": sender,
            "recipient": recipient,
            "body": body,
            "task_id": task_id,
        }
        if existing:
            if any(existing[k] != v for k, v in payload.items()):
                raise CollaborationRegistryError(
                    "message ID already exists with different content"
                )
            return existing
        if len(messages) >= 1000:
            raise CollaborationRegistryError(
                "inbox retention limit reached; archive acknowledged messages before sending"
            )
        item = {**payload, "created_at": _now(), "acknowledged_at": None}
        messages.append(item)
        _atomic_write(
            path,
            {
                "schema_version": "context.collaboration-inbox/v1alpha1",
                "messages": messages,
            },
        )
        _record_change(Path(data_root or default_data_root()), project_id, "message.sent", task_id, recipient=recipient, message_id=message_id)
        return item


def list_messages(
    data_root: Path | str | None, *, project_id: str, recipient: str, limit: int = 3
) -> list[dict[str, Any]]:
    if not _ASSIGNEE_RE.fullmatch(recipient) or not 1 <= limit <= 1000:
        raise CollaborationRegistryError("recipient or inbox limit is invalid")
    return [
        m
        for m in _messages(_path(data_root, project_id))
        if m["recipient"] == recipient and m["acknowledged_at"] is None
    ][:limit]


def acknowledge(
    data_root: Path | str | None, *, project_id: str, recipient: str, message_id: str
) -> dict[str, Any]:
    path = _path(data_root, project_id)
    with _locked(path):
        messages = _messages(path)
        item = next(
            (
                m
                for m in messages
                if m["message_id"] == message_id and m["recipient"] == recipient
            ),
            None,
        )
        if item is None:
            raise CollaborationRegistryError("message is not in this recipient's inbox")
        if item["acknowledged_at"] is None:
            item["acknowledged_at"] = _now()
            _atomic_write(
                path,
                {
                    "schema_version": "context.collaboration-inbox/v1alpha1",
                    "messages": messages,
                },
            )
            _record_change(Path(data_root or default_data_root()), project_id, "message.acknowledged", item.get("task_id"), recipient=recipient, message_id=message_id)
        return item
