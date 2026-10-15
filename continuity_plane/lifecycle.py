"""Shared bounded handoff across host compaction events, with local byte accounting."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from .collaboration_registry import (
    default_data_root,
    resolve_project,
    next_task,
    _atomic_write,
    _locked,
)
from .intent_todo import load_queue
from .usage_meter import record
from .executor_routing import for_task

MAX_CONTEXT_BYTES = 4096


def _clip(value: str, budget: int) -> str:
    return value.encode("utf-8")[:budget].decode("utf-8", "ignore")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: dict) -> str:
    return hashlib.sha256(
        _json({k: v for k, v in value.items() if k != "digest"}).encode()
    ).hexdigest()


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=2
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def handle_event(
    payload: dict, *, host: str, data_root: Path | str | None = None
) -> dict:
    if not isinstance(payload, dict):
        return {"status": "invalid-input", "continue": True, "injected_bytes": 0}
    root = Path(payload.get("cwd") or os.getcwd()).resolve()
    event = payload.get("hook_event_name", "SessionStart")
    session = str(
        payload.get("session_id") or os.environ.get("CONTINUITY_SESSION_ID") or ""
    )
    if not session:
        return {"status": "unbound", "continue": True, "injected_bytes": 0}
    data = Path(data_root) if data_root else default_data_root()
    key = hashlib.sha256((str(root) + ":" + session).encode()).hexdigest()
    path = data / "handoffs" / (key + ".json")
    if event == "PreCompact":
        registration = resolve_project(root, data)
        queue = load_queue(root, data) or {}
        item = next(
            (i for i in queue.get("items", []) if i.get("status") == "active"), None
        )
        actor = os.environ.get("CONTINUITY_ASSIGNEE") or session
        card = (
            next_task(data, project_id=registration["project_id"], assignee=actor)
            if registration
            else None
        )
        if card and card.get("assignee") != actor:
            card = None
        if queue.get("owner_session_id") != session:
            item = None
        action = (
            payload.get("next_action")
            or (card or {}).get("next_action")
            or (item or {}).get("title")
            or "Continue the host's latest unfinished user instruction."
        )
        if not isinstance(action, str):
            action = "Continue the host's latest unfinished user instruction."
        preferences = (
            for_task(data, registration["project_id"], (card or {}).get("task_id"))
            if registration
            else {}
        )
        saved = {
            "schema_version": "context.session-handoff/v1alpha1",
            "root": str(root),
            "session": session,
            "generation": uuid.uuid4().hex,
            "head": _git(root, "rev-parse", "HEAD"),
            "branch": _git(root, "branch", "--show-current"),
            "next_action": _clip(action, 800),
            "task_id": (card or {}).get("task_id"),
            "constraints": [
                _clip(str(i), 160) for i in (card or {}).get("allowed_files", [])[:6]
            ],
            "verification": [
                _clip(str(i), 180) for i in (card or {}).get("exit_criteria", [])[:3]
            ],
            "executor_candidates": preferences.get("candidates", []),
            "executor_thinking": preferences.get("thinking_levels", {}),
            "executor_adaptation": {
                key: preferences.get("adaptation", {}).get(key)
                for key in ("when_congested", "keep_whole_when")
            }
            if preferences
            else {},
        }
        saved["digest"] = _digest(saved)
        with _locked(path):
            _atomic_write(path, saved)
        return {
            "status": "saved",
            "continue": True,
            "handoff_digest": saved["digest"],
            "injected_bytes": 0,
        }
    if event not in {"SessionStart", "PostCompact"}:
        return {"status": "ignored", "continue": True, "injected_bytes": 0}
    try:
        if path.stat().st_size > 16384:
            raise ValueError("oversized session handoff")
        saved = json.loads(path.read_text())
        valid = (
            saved["session"] == session
            and saved["root"] == str(root)
            and saved["digest"] == _digest(saved)
        )
    except (OSError, ValueError, KeyError, TypeError):
        saved, valid = {}, False
    prefix = "Continue the interrupted task silently from the latest user intent and host summary. Do not repeat answered messages, announce recovery, or reread whole planning documents. For unfamiliar code, use continuity_context_lookup once, then narrow rg on a miss. "
    if valid:
        head = _git(root, "rev-parse", "HEAD")
        context = prefix + _json(
            {
                "next_action": saved["next_action"],
                "constraints": saved["constraints"],
                "verification": saved["verification"],
                "head": head,
                "executor_candidates": saved.get("executor_candidates", []),
                "executor_thinking": saved.get("executor_thinking", {}),
                "executor_adaptation": saved.get("executor_adaptation", {}),
                "handoff_head_changed": head != saved["head"],
                "task_id": saved["task_id"],
            }
        )
    else:
        context = (
            prefix
            + "No verified session handoff exists; do not revive an older saved Work."
        )
    context = _clip(context, MAX_CONTEXT_BYTES)
    marker = path.with_suffix(".delivered.json")
    identity = hashlib.sha256(context.encode()).hexdigest()
    with _locked(marker):
        try:
            previous = json.loads(marker.read_text())
        except (OSError, ValueError):
            previous = {}
        if previous.get("digest") == identity and previous.get(
            "handoff_digest"
        ) == saved.get("digest"):
            return {
                "status": "already-delivered",
                "continue": True,
                "injected_bytes": 0,
            }
        _atomic_write(
            marker, {"digest": identity, "handoff_digest": saved.get("digest")}
        )
    record(
        root,
        command="hook:" + event,
        response_bytes=0,
        estimated_grep_bytes=0,
        injected_bytes=len(context.encode()),
        session_id=session,
        data_root=data,
        host=host,
    )
    return {
        "status": "restored" if valid else "fallback",
        "continue": True,
        "injected_bytes": len(context.encode()),
        "handoff_digest": saved.get("digest"),
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": context},
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        result = handle_event(payload, host=os.environ.get("CONTINUITY_HOST", "host"))
        if "hookSpecificOutput" in result:
            print(_json(result))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return 0


def host_main() -> int:
    """Installed hook entry uses the core's interpreter, including its dependencies."""
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        output = handle_event(payload, host="claude")
        if "hookSpecificOutput" in output:
            print(_json(output))
        event = payload.get("hook_event_name")
        create = event == "PreCompact"
        verify = event == "PostCompact" or (
            event == "SessionStart"
            and payload.get("source") == "compact"
            and output.get("status") != "already-delivered"
        )
        if create or verify:
            # Name is resolved from the current installed package, not the host shell's Python.
            command = "create" if create else "verify"
            module = __package__ + ".cli"
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    module,
                    "checkpoint",
                    command,
                    "--root",
                    payload.get("cwd") or os.getcwd(),
                ],
                capture_output=True,
                timeout=5,
                check=False,
            )
            record(
                payload.get("cwd") or os.getcwd(),
                command="checkpoint:"
                + command
                + (":ok" if completed.returncode == 0 else ":failed"),
                response_bytes=0,
                estimated_grep_bytes=0,
                session_id=payload.get("session_id"),
                host="claude",
            )
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired):
        pass
    return 0
