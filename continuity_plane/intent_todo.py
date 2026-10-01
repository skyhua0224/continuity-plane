"""Bounded, project-local todo queues compiled from explicit user intent.

This sidecar is deliberately separate from authoritative Continuity State. It
helps a Session finish ordinary source/test work without granting claim,
completion, or external-effect authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "context.intent-todo/v1alpha1"
MAX_ITEMS = 12
MAX_ITEM_BYTES = 512
_ITEM_RE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$")
_STATUS = {"queued", "active", "completed", "blocked"}


class IntentTodoError(ValueError):
    """Invalid or inconsistent todo sidecar."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _root_key(root: Path | str) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:32]


def _prompt_key(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def _data_root(data_root: Path | str | None) -> Path:
    if data_root is not None:
        return Path(data_root).expanduser()
    configured = os.environ.get("CONTINUITY_DATA_ROOT")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "continuity"
    return Path.home() / ".local" / "share" / "continuity"


def queue_path(root: Path | str, data_root: Path | str | None = None) -> Path:
    return _data_root(data_root) / "intent-todos" / f"{_root_key(root)}.json"


def _write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_queue(root: Path | str, data_root: Path | str | None = None) -> dict[str, Any] | None:
    path = queue_path(root, data_root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        return None
    items = document.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None
    return document


def _split_items(prompt: str) -> list[str]:
    lines = [line.strip() for line in prompt.splitlines() if line.strip()]
    marked = []
    for line in lines:
        match = _ITEM_RE.match(line)
        if match:
            marked.append(match.group(1).strip())
    if len(marked) >= 2:
        return marked[:MAX_ITEMS]
    clauses = [part.strip() for part in re.split(r"[。；;]+", prompt) if part.strip()]
    if len(clauses) >= 2:
        return clauses[:MAX_ITEMS]
    return [prompt.strip()]


def compile_queue(
    root: Path | str,
    prompt: str,
    *,
    data_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Create an idempotent queue only for explicit multi-item execution intent."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    values = _split_items(prompt)
    if len(values) < 2:
        return None
    values = [value for value in values if len(value.encode("utf-8")) <= MAX_ITEM_BYTES]
    if len(values) < 2:
        return None
    path = queue_path(root, data_root)
    existing = load_queue(root, data_root)
    prompt_hash = _prompt_key(prompt)
    if existing is not None and existing.get("prompt_sha256") == prompt_hash:
        return existing
    queue_id = f"todo-{prompt_hash[:24]}"
    items = [
        {
            "item_id": f"{queue_id}-{index + 1}",
            "order": index + 1,
            "title": value,
            "status": "active" if index == 0 else "queued",
            "result": None,
        }
        for index, value in enumerate(values)
    ]
    document = {
        "schema_version": SCHEMA_VERSION,
        "queue_id": queue_id,
        "root": str(Path(root).resolve()),
        "prompt_sha256": prompt_hash,
        "status": "active",
        "items": items,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _write(path, document)
    return document


def _save(root: Path | str, document: dict[str, Any], data_root: Path | str | None) -> dict[str, Any]:
    document["updated_at"] = _now()
    _write(queue_path(root, data_root), document)
    return document


def complete_item(
    root: Path | str,
    item_id: str,
    *,
    result: str | None = None,
    data_root: Path | str | None = None,
) -> dict[str, Any]:
    document = load_queue(root, data_root)
    if document is None:
        raise IntentTodoError("no active intent todo queue")
    target = next((item for item in document["items"] if item.get("item_id") == item_id), None)
    if target is None:
        raise IntentTodoError("todo item does not exist")
    if target.get("status") != "active":
        raise IntentTodoError("only the active todo item can be completed")
    target["status"] = "completed"
    target["result"] = result
    next_item = next((item for item in document["items"] if item.get("status") == "queued"), None)
    if next_item is None:
        document["status"] = "completed"
    else:
        next_item["status"] = "active"
    return _save(root, document, data_root)


def block_item(
    root: Path | str,
    item_id: str,
    reason: str,
    *,
    data_root: Path | str | None = None,
) -> dict[str, Any]:
    document = load_queue(root, data_root)
    if document is None:
        raise IntentTodoError("no active intent todo queue")
    target = next((item for item in document["items"] if item.get("item_id") == item_id), None)
    if target is None:
        raise IntentTodoError("todo item does not exist")
    target["status"] = "blocked"
    target["result"] = reason[:MAX_ITEM_BYTES]
    document["status"] = "blocked"
    return _save(root, document, data_root)


def active_context(root: Path | str, data_root: Path | str | None = None) -> str | None:
    document = load_queue(root, data_root)
    if document is None or document.get("status") != "active":
        return None
    active = next((item for item in document["items"] if item.get("status") == "active"), None)
    if active is None:
        return None
    remaining = sum(item.get("status") in {"active", "queued"} for item in document["items"])
    return (
        f"Intent TodoQueue {document['queue_id']}: item {active['order']}/{len(document['items'])}. "
        f"Current item: {active['title']}\n"
        f"Remaining items: {remaining}. Execute this item, verify it, then run: "
        f"continuity todo complete --root {document['root']} --item-id {active['item_id']}. "
        "Continue to the next item automatically; report only completion, a real blocker, "
        "or a required user decision."
    )
