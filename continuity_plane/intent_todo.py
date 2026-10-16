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

from .plan_route import master_route, route_context
from .executor_routing import recommend

SCHEMA_VERSION = "context.intent-todo/v1alpha1"
MAX_ITEMS = 32
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


def load_queue(
    root: Path | str, data_root: Path | str | None = None
) -> dict[str, Any] | None:
    path = queue_path(root, data_root)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
    ):
        return None
    items = document.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        return None
    if document.get("card_projection"):
        from .card_todo import project

        return project(document, root, data_root)
    return document


def _split_items(prompt: str) -> list[str]:
    lines = [line.strip() for line in prompt.splitlines() if line.strip()]
    marked = []
    for line in lines:
        match = _ITEM_RE.match(line)
        if match:
            marked.append(match.group(1).strip())
    if marked:
        return marked
    clauses = [part.strip() for part in re.split(r"[。；;]+", prompt) if part.strip()]
    if len(clauses) >= 2:
        return clauses
    return [prompt.strip()]


def compile_queue(
    root: Path | str,
    prompt: str,
    *,
    data_root: Path | str | None = None,
    master_path: str | None = None,
    master_section: str | None = None,
    session_id: str | None = None,
    assignee: str | None = None,
) -> dict[str, Any] | None:
    """Create an idempotent queue only for explicit multi-item execution intent."""
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    for field, value in (("session_id", session_id), ("assignee", assignee)):
        if value is not None and (
            not isinstance(value, str) or not value or len(value) > 256
        ):
            raise IntentTodoError(f"{field} must be a bounded identifier")
    values = _split_items(prompt)
    if len(values) < 2 and not _ITEM_RE.match(prompt.strip()):
        return None
    if len(values) > MAX_ITEMS:
        raise IntentTodoError(
            f"todo capacity exceeded ({MAX_ITEMS}); no items were saved or dropped"
        )
    if any(len(value.encode("utf-8")) > MAX_ITEM_BYTES for value in values):
        raise IntentTodoError(
            f"todo item exceeds {MAX_ITEM_BYTES} bytes; shorten it; no items were dropped"
        )
    path = queue_path(root, data_root)
    existing = load_queue(root, data_root)
    if existing and existing.get("card_projection"):
        from .card_todo import append_actions

        return append_actions(root, existing, values, data_root)
    prompt_hash = _prompt_key(prompt)
    queue_id = f"todo-{prompt_hash[:24]}"
    items = [
        {
            "item_id": f"{queue_id}-{index + 1}",
            "order": index + 1,
            "title": value,
            "status": "active" if index == 0 else "queued",
            "result": None,
            "task_id": None,
        }
        for index, value in enumerate(values)
    ]
    if existing is not None and prompt_hash in existing.get(
        "prompt_hashes", [existing.get("prompt_sha256")]
    ):
        # Older builds saved the full prompt hash after silently clipping its
        # last items. Repair only missing IDs, preserving progress and evidence.
        known = {item.get("item_id") for item in existing["items"]}
        missing = [item for item in items if item["item_id"] not in known]
        if not missing:
            return existing
        if len(existing["items"]) + len(missing) > MAX_ITEMS:
            raise IntentTodoError(
                "todo capacity exceeded; existing progress was preserved"
            )
        has_active = any(item.get("status") == "active" for item in existing["items"])
        for item in missing:
            item["order"] = len(existing["items"]) + 1
            item["status"] = "queued"
            if not has_active and existing.get("status") != "blocked":
                item["status"] = "active"
                has_active = True
            existing["items"].append(item)
        if has_active:
            existing["status"] = "active"
        return _save(root, existing, data_root)
    if existing is not None and existing.get("status") in {"active", "blocked"}:
        # A follow-up adds work; it must not erase the active return point.
        offset = len(existing["items"])
        if offset + len(items) > MAX_ITEMS:
            raise IntentTodoError(
                "todo capacity exceeded; reconcile the existing queue explicitly"
            )
        for item in items:
            item["order"] += offset
            item["status"] = "queued"
        existing["items"].extend(items)
        existing.setdefault("prompt_hashes", [existing["prompt_sha256"]]).append(
            prompt_hash
        )
        return _save(root, existing, data_root)
    route = master_route(root, master_path=master_path, section=master_section)
    document = {
        "schema_version": SCHEMA_VERSION,
        "queue_id": queue_id,
        "root": str(Path(root).resolve()),
        "prompt_sha256": prompt_hash,
        "prompt_hashes": [prompt_hash],
        "plan_binding": {
            key: route[key] for key in ("master_path", "section", "master_sha256")
        },
        "status": "active",
        "owner_session_id": session_id,
        "owner_assignee": assignee,
        "execution_preferences": recommend(),
        "items": items,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _write(path, document)
    return document


def _save(
    root: Path | str, document: dict[str, Any], data_root: Path | str | None
) -> dict[str, Any]:
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
    target = next(
        (item for item in document["items"] if item.get("item_id") == item_id), None
    )
    if target is None:
        raise IntentTodoError("todo item does not exist")
    if document.get("card_projection") and target.get("status") == "completed":
        return document
    if target.get("status") not in {"queued", "active", "blocked"}:
        raise IntentTodoError("todo item is already completed")
    if document.get("card_projection"):
        from .card_todo import transition

        return transition(
            document,
            root,
            item_id,
            data_root=data_root,
            status="ready-for-review",
            result=result,
        )
    target["status"] = "completed"
    target["result"] = result
    next_item = next(
        (item for item in document["items"] if item.get("status") == "queued"), None
    )
    if next_item is None and not any(
        item.get("status") == "active" for item in document["items"]
    ):
        document["status"] = (
            "blocked"
            if any(item.get("status") == "blocked" for item in document["items"])
            else "completed"
        )
    elif next_item is not None and not any(
        item.get("status") == "active" for item in document["items"]
    ):
        next_item["status"] = "active"
    return _save(root, document, data_root)


def link_item(
    root: Path | str, item_id: str, task_id: str, *, data_root: Path | str | None = None
) -> dict[str, Any]:
    document = load_queue(root, data_root)
    if document is None:
        raise IntentTodoError("no active intent todo queue")
    target = next(
        (item for item in document["items"] if item.get("item_id") == item_id), None
    )
    if target is None:
        raise IntentTodoError("todo item does not exist")
    if document.get("card_projection"):
        if target.get("task_id") != task_id:
            raise IntentTodoError(
                "card-projected items cannot be rebound to another card"
            )
        return document
    from .collaboration_registry import resolve_project, list_tasks

    project = resolve_project(root, data_root)
    if project is None or not any(
        row["task_id"] == task_id
        for row in list_tasks(data_root, project_id=project["project_id"])
    ):
        raise IntentTodoError("task_id does not exist in this registered project")
    target["task_id"] = task_id
    return _save(root, document, data_root)


def activate_item(root, item_id: str, *, reason: str, data_root=None, session_id=None, assignee=None):
    """Reorder current intent explicitly without erasing earlier progress."""
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 2048:
        raise IntentTodoError("activation requires a bounded reason")
    document = load_queue(root, data_root)
    if document is None:
        raise IntentTodoError("no active intent todo queue")
    target = next(
        (item for item in document["items"] if item.get("item_id") == item_id), None
    )
    if target is None or target.get("status") == "completed":
        raise IntentTodoError("choose an existing unfinished todo item")
    if document.get("card_projection"):
        from .card_todo import transition

        return transition(
            document,
            root,
            item_id,
            data_root=data_root,
            status="active",
            # Session IDs identify recovery, not executor ownership.
            assignee=assignee or os.environ.get("CONTINUITY_ASSIGNEE"),
        )
    if session_id is not None and (
        not isinstance(session_id, str) or not session_id or len(session_id) > 256
    ):
        raise IntentTodoError("session_id must be a bounded identifier")
    if target.get("status") == "active":
        if session_id and document.get("owner_session_id") != session_id:
            document["owner_session_id"] = session_id
            return _save(root, document, data_root)
        return document
    previous = []
    for item in document["items"]:
        if item.get("status") == "active":
            previous.append(item["item_id"])
            item["status"] = "queued"
    document["switch_history"] = (
        document.get("switch_history", [])
        + [{"from": previous, "to": item_id, "reason": reason.strip(), "at": _now()}]
    )[-8:]
    target["status"] = "active"
    if session_id:
        document["owner_session_id"] = session_id
    document["status"] = "active"
    return _save(root, document, data_root)


def block_item(
    root: Path | str,
    item_id: str,
    reason: str,
    *,
    data_root: Path | str | None = None,
) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 2048:
        raise IntentTodoError("blocker requires a reason of at most 2048 bytes")
    document = load_queue(root, data_root)
    if document is None:
        raise IntentTodoError("no active intent todo queue")
    target = next(
        (item for item in document["items"] if item.get("item_id") == item_id), None
    )
    if target is None or target.get("status") == "completed":
        raise IntentTodoError("choose an existing unfinished todo item")
    if document.get("card_projection"):
        from .card_todo import transition

        return transition(
            document,
            root,
            item_id,
            data_root=data_root,
            status="blocked",
            reason=reason,
        )
    target["status"] = "blocked"
    target["result"] = reason
    document["status"] = (
        "active"
        if any(item.get("status") == "active" for item in document["items"])
        else "blocked"
    )
    return _save(root, document, data_root)


def active_context(root: Path | str, data_root: Path | str | None = None) -> str | None:
    document = load_queue(root, data_root)
    if document is None or document.get("status") != "active":
        return None
    active = next(
        (item for item in document["items"] if item.get("status") == "active"), None
    )
    if active is None:
        return None
    binding = document.get("plan_binding") or {}
    route = (
        {}
        if document.get("card_projection")
        else master_route(
            root, master_path=binding.get("master_path"), section=binding.get("section")
        )
    )
    digest = binding.get("master_sha256")
    if (
        not document.get("card_projection")
        and digest
        and (route.get("status") != "current" or route.get("master_sha256") != digest)
    ):
        return (
            "TodoQueue plan changed; do not execute old todo titles as current instructions. "
            "Reconcile remaining items with current user intent and the relevant MASTER section, "
            "preserving completed evidence and real blockers. Ordinary work remains allowed. "
            + route_context(route)
        )
    remaining = sum(
        item.get("status") in {"active", "queued", "blocked"}
        for item in document["items"]
    )
    return (
        f"Intent TodoQueue {document['queue_id']}: item {active['order']}/{len(document['items'])}. "
        f"Current item: {active['title']}\n"
        f"Remaining items: {remaining}. Execute this item, verify it, then run: "
        f"continuity todo complete --root {document['root']} --item-id {active['item_id']}. "
        "Continue to the next item automatically; report only completion, a real blocker, "
        "or a required user decision."
    )
