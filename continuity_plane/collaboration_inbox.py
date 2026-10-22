"""Durable local inbox with explicit acknowledgements; no provider execution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import re
from typing import Any
import uuid

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
    rows = [
        r
        for r in _messages(_path(data_root, project_id))
        if r["acknowledged_at"] is None
        and (recipient is None or r["recipient"] == recipient)
    ]
    recipient_arg = recipient or "<member>"
    base = f"--project-id {project_id} --recipient {recipient_arg}"
    return {
        "count": len(rows),
        "unit": "unacknowledged inbox messages",
        "oldest_at": min((r["created_at"] for r in rows), default=None),
        "recipient": recipient,
        "scope": "recipient" if recipient else "project",
        "list_command": f"continuity collaboration inbox list {base}",
        "acknowledge_command": (
            f"continuity collaboration inbox ack {base} --message-id <message-id>"
        ),
        "bulk_ack_command": (
            f"continuity collaboration inbox ack {base} "
            "--before <timezone-ISO-8601> --limit 1000"
        ),
        "ack_all_command": (
            f"continuity collaboration inbox ack {base} --all --limit 1000"
        ),
        "archive_command": (
            f"continuity collaboration inbox archive {base} --limit 1000"
        ),
        "acknowledgement": "Acknowledged messages leave the unread count; archive then moves acknowledged messages to recoverable storage without deleting them.",
    }


def generated_message_id(sender: str, *, now: datetime | None = None) -> str:
    """Return a lowercase, bounded, collision-resistant message identity."""
    if not isinstance(sender, str) or not sender:
        raise CollaborationRegistryError("sender is required to generate a message ID")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    slug = re.sub(r"[^a-z0-9._-]+", "-", sender.casefold()).strip("-._")
    slug = (slug or "message")[:48]
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ").lower()
    return f"{slug}-{stamp}-{uuid.uuid4().hex[:8]}"


def send_message(
    data_root: Path | str | None,
    *,
    project_id: str,
    message_id: str | None = None,
    sender: str,
    recipient: str,
    body: str,
    task_id: str | None = None,
) -> dict[str, Any]:
    message_id = message_id or generated_message_id(sender)
    if (
        not isinstance(message_id, str)
        or not _ID_RE.fullmatch(message_id)
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
        archived = _load(path.parent / "inbox-archive" / (message_id + ".json"))
        if archived is not None:
            if any(archived.get(k) != v for k, v in payload.items()):
                raise CollaborationRegistryError(
                    "archived message ID already exists with different content"
                )
            return archived  # A retry is not a new notification.
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
        _record_change(
            Path(data_root or default_data_root()),
            project_id,
            "message.sent",
            task_id,
            recipient=recipient,
            message_id=message_id,
        )
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


def archive(data_root, *, project_id, recipient, limit=100, before=None):
    """Move only acknowledged messages to recoverable per-ID immutable files."""
    if not _ASSIGNEE_RE.fullmatch(recipient) or not 1 <= limit <= 1000:
        raise CollaborationRegistryError("recipient or archive limit is invalid")
    cutoff = None
    if before is not None:
        try:
            cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            raise CollaborationRegistryError(
                "archive before must be ISO-8601"
            ) from None
        if cutoff.tzinfo is None:
            raise CollaborationRegistryError("archive before must include a timezone")
    path = _path(data_root, project_id)
    with _locked(path):
        messages = _messages(path)
        selected = [
            r
            for r in messages
            if r["recipient"] == recipient
            and r["acknowledged_at"] is not None
            and (
                cutoff is None
                or datetime.fromisoformat(r["acknowledged_at"].replace("Z", "+00:00"))
                <= cutoff
            )
        ][:limit]
        for row in selected:
            target = path.parent / "inbox-archive" / (row["message_id"] + ".json")
            old = _load(target)
            if old is not None and old != row:
                raise CollaborationRegistryError(
                    "archive conflict; original inbox preserved"
                )
            if old is None:
                _atomic_write(target, row)
        identities = {r["message_id"] for r in selected}
        _atomic_write(
            path,
            {
                "schema_version": "context.collaboration-inbox/v1alpha1",
                "messages": [r for r in messages if r["message_id"] not in identities],
            },
        )
        if selected:
            _record_change(
                Path(data_root or default_data_root()),
                project_id,
                "message.archived",
                None,
                recipient=recipient,
                count=len(selected),
            )
    return {
        "archived": len(selected),
        "unacknowledged_preserved": True,
        "deleted": False,
        "recovery": str(path.parent / "inbox-archive"),
        "duplicate_sends_suppressed": True,
    }


def acknowledge_many(
    data_root,
    *,
    project_id,
    recipient,
    before=None,
    all_messages=False,
    limit=1000,
    now=None,
):
    """Acknowledge one recipient's messages in a bounded, explicit batch."""
    if not _ASSIGNEE_RE.fullmatch(recipient) or not 1 <= limit <= 1000:
        raise CollaborationRegistryError("recipient or acknowledgment limit is invalid")
    if all_messages and before is not None:
        raise CollaborationRegistryError("choose --all or --before, not both")
    if not all_messages and before is None:
        raise CollaborationRegistryError(
            "bulk acknowledgement requires --all or --before"
        )
    cutoff = None
    if before is not None:
        try:
            cutoff = datetime.fromisoformat(before.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError):
            raise CollaborationRegistryError(
                "acknowledge before must be ISO-8601"
            ) from None
        if cutoff.tzinfo is None:
            raise CollaborationRegistryError(
                "acknowledge before must include a timezone"
            )
    acknowledged_at = (now or datetime.now(UTC)).isoformat()
    path = _path(data_root, project_id)
    with _locked(path):
        messages = _messages(path)
        selected = []
        for item in messages:
            if (
                item["recipient"] != recipient
                or item["acknowledged_at"] is not None
                or (
                    cutoff is not None
                    and datetime.fromisoformat(
                        item["created_at"].replace("Z", "+00:00")
                    )
                    > cutoff
                )
            ):
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
        for item in selected:
            item["acknowledged_at"] = acknowledged_at
        if selected:
            _atomic_write(
                path,
                {
                    "schema_version": "context.collaboration-inbox/v1alpha1",
                    "messages": messages,
                },
            )
            _record_change(
                Path(data_root or default_data_root()),
                project_id,
                "message.acknowledged-bulk",
                None,
                recipient=recipient,
                count=len(selected),
                before=before,
                all=all_messages,
                first_message_id=selected[0]["message_id"],
                last_message_id=selected[-1]["message_id"],
            )
    return {
        "status": "acknowledged",
        "recipient": recipient,
        "count": len(selected),
        "limit": limit,
        "before": before,
        "all": all_messages,
        "message_ids": [item["message_id"] for item in selected],
    }


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
            _record_change(
                Path(data_root or default_data_root()),
                project_id,
                "message.acknowledged",
                item.get("task_id"),
                recipient=recipient,
                message_id=message_id,
            )
        return item
