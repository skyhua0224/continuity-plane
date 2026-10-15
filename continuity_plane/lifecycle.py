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
    payload: dict,
    *,
    host: str,
    data_root: Path | str | None = None,
    meter_injection=True,
    record_protocol=True,
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
    from .provider_usage import capture_hook

    capture_hook(
        payload, "claude" if host.startswith("claude") else "codex", data_root=data
    )
    key = hashlib.sha256((str(root) + ":" + session).encode()).hexdigest()
    path = data / "handoffs" / (key + ".json")
    if event == "PreCompact":
        registration = resolve_project(root, data)
        queue_root = Path(registration["control_root"]) if registration else root
        queue = load_queue(queue_root, data) or {}
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
        if registration:
            from .collaboration_registry import list_tasks

            owned = [
                t
                for t in list_tasks(
                    data, project_id=registration["project_id"], assignee=actor
                )
                if t["status"] in {"active", "dispatched"}
            ]
            requested = payload.get("task_id") or os.environ.get("CONTINUITY_TASK_ID")
            if requested:
                card = next((t for t in owned if t["task_id"] == requested), None)
            elif len(owned) > 1:
                card = None
            if (
                not requested
                and os.environ.get("CONTINUITY_ROLE") == "controller"
                and card
                and card.get("assignee") != session
            ):
                active = next(
                    (i for i in queue.get("items", []) if i.get("status") == "active"),
                    None,
                )
                if (
                    queue.get("owner_session_id") != session
                    or not active
                    or active.get("task_id") != card["task_id"]
                ):
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
            "scope_count": len((card or {}).get("allowed_files", [])),
            "verification_count": len((card or {}).get("exit_criteria", [])),
            "contract_preview": True,
            "contract_command": "continuity collaboration task show --task-id "
            + card["task_id"]
            if card
            else None,
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
        if registration:
            from .controller_awareness import scene

            try:
                saved["controller_scene"] = scene(data, registration["project_id"])
            except (OSError, ValueError, TypeError, KeyError):
                saved["controller_scene"] = {"status": "unavailable"}
        saved["digest"] = _digest(saved)
        with _locked(path):
            _atomic_write(path, saved)
        if record_protocol:
            from .handoff_journal import record as record_phase

            try:
                record_phase(
                    root,
                    session,
                    saved["generation"],
                    "saved",
                    data_root=data,
                    kind=payload.get("observation_kind", "native-event"),
                )
            except (OSError, ValueError):
                pass
        from .provider_usage import capture_compaction

        try:
            capture_compaction(root, session, saved["generation"], data_root=data)
        except (OSError, ValueError, TypeError, KeyError):
            pass
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
    if host.startswith("claude") and event == "PostCompact":
        # Claude's PostCompact is side-effects-only; inject at SessionStart(compact).
        # https://code.claude.com/docs/en/hooks#postcompact
        return {
            "status": "verified-await-sessionstart"
            if valid
            else "missing-handoff-await-sessionstart",
            "continue": True,
            "injected_bytes": 0,
            "handoff_digest": saved.get("digest"),
        }
    prefix = "Continue the interrupted task from the latest user intent and host summary. Progress notes are nonterminal. Metadata status alone does not make authorized source/test work read-only; respect the actual user scope. Do not repeat answered messages, announce recovery, or reread whole planning documents. For unfamiliar code, use continuity_context_lookup once, then narrow rg on a miss. "
    if valid:
        head = _git(root, "rev-parse", "HEAD")
        recovered = {
            "next_action": saved["next_action"],
            "constraints": saved["constraints"],
            "verification": saved["verification"],
            "head": head,
            "executor_candidates": saved.get("executor_candidates", []),
            "executor_thinking": saved.get("executor_thinking", {}),
            "executor_adaptation": saved.get("executor_adaptation", {}),
            "handoff_head_changed": head != saved["head"],
            "task_id": saved["task_id"],
            "scope_count": saved.get("scope_count"),
            "verification_count": saved.get("verification_count"),
            "contract_preview": saved.get("contract_preview", True),
            "contract_command": saved.get("contract_command"),
            "controller_scene": saved.get("controller_scene", {}),
        }
        context = prefix + _json(recovered)
        while len(context.encode()) > MAX_CONTEXT_BYTES:
            scene = recovered.get("controller_scene", {})
            field = next(
                (
                    k
                    for k in (
                        "candidate_work",
                        "workers",
                        "commitments",
                        "batches",
                        "approved_decisions",
                    )
                    if scene.get(k)
                ),
                None,
            )
            if field:
                scene[field].pop()
                scene["details_omitted"] = True
            elif recovered.get("executor_adaptation"):
                recovered["executor_adaptation"] = {}
                recovered["details_omitted"] = True
            elif recovered.get("constraints"):
                recovered["constraints"].pop()
                recovered["details_omitted"] = True
            elif recovered.get("verification"):
                recovered["verification"].pop()
                recovered["details_omitted"] = True
            else:
                break
            context = prefix + _json(recovered)
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
        if (valid and previous.get("handoff_digest") == saved.get("digest")) or (
            not valid and previous.get("digest") == identity
        ):
            return {
                "status": "already-delivered",
                "continue": True,
                "injected_bytes": 0,
            }
        _atomic_write(
            marker, {"digest": identity, "handoff_digest": saved.get("digest")}
        )
    if valid and record_protocol:
        from .handoff_journal import record as record_phase

        try:
            record_phase(
                root,
                session,
                saved.get("generation"),
                "restored",
                data_root=data,
                kind=payload.get("observation_kind", "native-event"),
            )
        except (OSError, ValueError):
            pass
    if meter_injection:
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
        "injected_bytes": len(context.encode()) if meter_injection else 0,
        "generated_context_bytes": len(context.encode()),
        "handoff_digest": saved.get("digest"),
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": context},
    }


def main() -> int:
    if os.environ.get("CONTINUITY_DISABLED") == "1":
        return 0
    try:
        payload = json.load(sys.stdin)
        result = handle_event(payload, host=os.environ.get("CONTINUITY_HOST", "host"))
        if "hookSpecificOutput" in result:
            print(_json(result))
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return 0


def host_main() -> int:
    if os.environ.get("CONTINUITY_DISABLED") == "1":
        return 0
    """Installed hook entry uses the core's interpreter, including its dependencies."""
    # The profile installer uses this entrypoint for both local and SSH-backed
    # hooks. Route before consuming stdin: otherwise --config is silently ignored
    # and the native client writes an unrelated host-local recovery store.
    if any(
        arg == "--config" or arg.startswith("--config=") or arg in {"--help", "-h"}
        for arg in sys.argv[1:]
    ):
        from .remote_hook import main as remote_main

        return remote_main(sys.argv[1:])
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        event = payload.get("hook_event_name")
        if event == "UserPromptSubmit":
            output = {"continue": True}
        else:
            output = handle_event(payload, host="claude", meter_injection=False)
        if (
            event in {"UserPromptSubmit", "SessionStart"}
            and payload.get("source") != "compact"
        ):
            from .planning_adoption import context as planning_context

            hint = planning_context(payload, host="claude")
            if hint:
                base = output.get("hookSpecificOutput", {}).get("additionalContext", "")
                combined = (base + "\n" + hint).strip()
                if len(combined.encode()) <= MAX_CONTEXT_BYTES:
                    output["hookSpecificOutput"] = {
                        "hookEventName": event,
                        "additionalContext": combined,
                    }
        from .controller_awareness import notice

        base = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        note = notice(
            payload,
            root=payload.get("cwd"),
            max_bytes=min(512, MAX_CONTEXT_BYTES - len(base.encode()) - 1),
        )
        if note:
            output["hookSpecificOutput"] = {
                "hookEventName": event,
                "additionalContext": (base + "\n" + note).strip(),
            }
        if "hookSpecificOutput" in output:
            context = output["hookSpecificOutput"]["additionalContext"]
            output["injected_bytes"] = len(context.encode())
            print(
                _json({"hookSpecificOutput": output["hookSpecificOutput"]}), flush=True
            )
            record(
                payload.get("cwd") or os.getcwd(),
                command="hook:" + str(event),
                response_bytes=0,
                estimated_grep_bytes=0,
                injected_bytes=len(context.encode()),
                session_id=payload.get("session_id"),
                host="claude",
            )
        create = event == "PreCompact"
        verify = event == "PostCompact" or (
            event == "SessionStart"
            and payload.get("source") == "compact"
            and output.get("status") != "already-delivered"
        )
        if create or verify:
            if os.environ.get("CONTINUITY_STATE_CHECKPOINTS") != "1":
                ok = (
                    output.get("status") == "saved"
                    if create
                    else output.get("status")
                    in {"verified-await-sessionstart", "restored", "already-delivered"}
                )
                record(
                    payload.get("cwd") or os.getcwd(),
                    command="checkpoint:session-handoff:"
                    + ("create" if create else "verify")
                    + (":ok" if ok else ":failed"),
                    response_bytes=0,
                    estimated_grep_bytes=0,
                    session_id=payload.get("session_id"),
                    host="claude",
                )
                return 0
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
