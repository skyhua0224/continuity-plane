"""Shared bounded handoff across host compaction events, with local byte accounting."""

from __future__ import annotations

import hashlib
import json
import getpass
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

RESUME_INSTRUCTION = (
    "Resume the interrupted task. Progress notes are nonterminal; metadata status "
    "does not make authorized source/test work read-only. Use rg for a known name; "
    "use continuity_context_lookup when rg would return many matches. Do not reread planning "
    "documents or repeat answered messages."
)


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


def _one_line(value: Any, limit: int) -> str:
    return _clip(" ".join(str(value).split()), limit)


def _list_line(label: str, values: Any, limit: int) -> str | None:
    kept: list[str] = []
    for value in values or []:
        item = _one_line(value, 160)
        if item and item not in kept:
            kept.append(item)
        if len(kept) == 4:
            break
    if not kept:
        return None
    return _clip(label + ": " + "; ".join(kept), limit)


def _scene_counts(scene: Any, *, force_inbox: bool = False) -> str:
    if not isinstance(scene, dict):
        return ""
    bits: list[str] = []
    inbox = scene.get("inbox")
    count = inbox.get("count") if isinstance(inbox, dict) else None
    if force_inbox or count:
        bits.append("inbox=" + str(count or 0))
    for label, key in (
        ("workers", "workers_total"),
        ("commitments", "commitments_total"),
        ("reports", "reports_total"),
        ("drift", "drift_count"),
    ):
        value = scene.get(key)
        if type(value) is int and value > 0:
            bits.append(label + "=" + str(value))
    return ", ".join(bits)


def _executor_line(recovered: dict) -> str | None:
    candidates = [
        value
        for value in (recovered.get("executor_candidates") or [])
        if isinstance(value, str)
    ][:2]
    thinking = recovered.get("executor_thinking") or {}
    adaptation = recovered.get("executor_adaptation") or {}
    payload: dict[str, Any] = {}
    if candidates:
        payload["executor_candidates"] = candidates
        levels = {
            name: thinking[name]
            for name in candidates
            if isinstance(thinking.get(name), str)
        }
        if levels:
            payload["executor_thinking"] = levels
    for key, limit in (("when_congested", 160), ("keep_whole_when", 140)):
        value = adaptation.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = _one_line(value, limit)
    if not payload:
        return None
    return "Executor: " + _json(payload)


def _render_resume(recovered: dict, *, force_inbox: bool = False, lead: str = "") -> str:
    """One compact packet: every scored field once, no empty JSON objects."""
    handoff = recovered.get("controller_handoff")
    if not isinstance(handoff, dict):
        handoff = {}
    scene = recovered.get("controller_scene")
    lines: list[str] = []
    task = recovered.get("task_id")
    next_action = _one_line(recovered.get("next_action") or "", 420)
    if next_action:
        lines.append(("Task " + str(task) + ": " if task else "Next: ") + next_action)
    elif task:
        lines.append("Task: " + str(task))
    plan = _one_line(handoff.get("plan") or "", 300)
    if plan and plan != next_action:
        lines.append("Plan: " + plan)
    for label, key, limit in (
        ("Constraints", "constraints", 300),
        ("Open decisions", None, 300),
        ("Verify", "verification", 320),
    ):
        values = (
            handoff.get("pending_decisions") or []
            if key is None
            else recovered.get(key) or []
        )
        line = _list_line(label, values, limit)
        if line:
            lines.append(line)
    todo = recovered.get("todo")
    if isinstance(todo, str) and todo.strip():
        lines.append(_clip("Todo: " + _one_line(todo, 160), 200))
    batch = recovered.get("batch_id")
    if batch:
        lines.append("Batch: " + str(batch))
    branch = recovered.get("branch")
    if isinstance(branch, str) and branch and branch not in next_action:
        lines.append("Branch: " + _one_line(branch, 120))
    counts = _scene_counts(scene, force_inbox=force_inbox)
    if counts:
        lines.append("Scene: " + counts)
    if isinstance(scene, dict):
        known = {_one_line(v, 160) for v in handoff.get("pending_decisions") or []}
        extra = [
            _one_line(row.get("text"), 120)
            for row in scene.get("commitments") or []
            if isinstance(row, dict) and row.get("text")
        ]
        extra = [value for value in extra if value and value not in known][:2]
        if extra:
            lines.append("Commitments: " + "; ".join(extra))
        if scene.get("status") or scene.get("warning"):
            lines.append(
                "Scene: "
                + _one_line(
                    " ".join(
                        str(scene[key])
                        for key in ("status", "warning")
                        if scene.get(key)
                    ),
                    220,
                )
            )
    executor = _executor_line(recovered)
    if executor:
        lines.append(executor)
    integration = recovered.get("integration")
    if not isinstance(integration, dict):
        integration = {}
    head = recovered.get("head")
    if head:
        line = "Integration: " + str(integration.get("ref") or "head") + " " + str(head)
        if recovered.get("handoff_head_changed"):
            line += " (changed since save; re-check before integrating)"
        lines.append(line)
    elif recovered.get("handoff_head_changed"):
        lines.append("Integration head changed since save; re-check before integrating.")
    refresh = recovered.get("controller_handoff_refresh")
    if isinstance(refresh, dict):
        line = "controller_handoff_refresh: " + str(refresh.get("status") or "deferred")
        if refresh.get("reason"):
            line += " (" + _one_line(refresh["reason"], 180) + ")"
        lines.append(line)
    warning = recovered.get("controller_observation_warning")
    if isinstance(warning, str) and warning:
        lines.append(_clip("Observation: " + warning, 220))
    entry = recovered.get("controller_entry_command")
    if isinstance(entry, str) and entry:
        lines.append("Controller entry: " + entry)
    command = recovered.get("contract_command")
    if isinstance(command, str) and command:
        lines.append("Details: " + command)
    body = "\n".join(lines)
    return RESUME_INSTRUCTION + ("\n" + lead if lead else "") + ("\n" + body if body else "")


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
    if event == "PostToolUse" and host not in {"claude-bridge", "claude-remote"}:
        # Bridged hooks are counted by the bridge spool and recorded remotely.
        from .adoption import record_hook

        record_hook(payload, host, data)
    if host.startswith("claude") and event in {"SessionStart", "SessionEnd"}:
        registration = resolve_project(root, data)
        if registration:
            from .native_subagents import session_event

            try:
                session_event(data, registration["project_id"], payload)
            except (OSError, ValueError):
                pass  # Observation failure must not block the host session.
        if event == "SessionEnd":
            return {
                "continue": True,
                "injected_bytes": 0,
                "status": "session-end-observed",
            }
    if event in {"SessionStart", "PostToolUse"}:
        registration = resolve_project(root, data)
        if registration:
            from .native_subagents import flush

            try:
                flush(data, registration["project_id"])
            except (OSError, ValueError):
                pass
    if event in {"SubagentStart", "SubagentStop"}:
        if not host.startswith("claude"):
            return {
                "continue": True,
                "injected_bytes": 0,
                "status": "unsupported-native-agent-host",
            }
        registration = resolve_project(root, data)
        if registration:
            from .native_subagents import observe

            return {
                "continue": True,
                "injected_bytes": 0,
                "observation": observe(data, registration["project_id"], payload),
            }
        return {
            "continue": True,
            "injected_bytes": 0,
            "status": "unregistered-native-agent",
        }
    from .provider_usage import capture_hook

    capture_hook(
        payload, "claude" if host.startswith("claude") else "codex", data_root=data
    )
    key = hashlib.sha256((str(root) + ":" + session).encode()).hexdigest()
    path = data / "handoffs" / (key + ".json")
    if event == "PreCompact":
        registration = resolve_project(root, data)
        provider = "claude" if host.startswith("claude") else "codex"
        from .project_context import binding, integration

        try:
            session_binding = (
                binding(data, registration["project_id"], provider, session)
                if registration
                else None
            )
        except (OSError, ValueError, KeyError):
            session_binding = None
        if session_binding and not session_binding.get("active"):
            session_binding = None
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
            requested = (
                payload.get("task_id")
                or os.environ.get("CONTINUITY_TASK_ID")
                or (session_binding or {}).get("task_id")
            )
            if requested:
                card = next((t for t in owned if t["task_id"] == requested), None)
                if (
                    card is None
                    and session_binding
                    and requested == session_binding.get("task_id")
                ):
                    card = next(
                        (
                            t
                            for t in list_tasks(
                                data, project_id=registration["project_id"]
                            )
                            if t["task_id"] == requested
                        ),
                        None,
                    )
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
            or (
                "Continue integration batch "
                + session_binding["batch_id"]
                + "; inspect its workers, commitments and verification before continuing."
                if (session_binding or {}).get("batch_id")
                else "Continue the host's latest unfinished user instruction."
            )
        )
        if not isinstance(action, str):
            action = "Continue the host's latest unfinished user instruction."
        preferences = (
            for_task(data, registration["project_id"], (card or {}).get("task_id"))
            if registration
            else {}
        )
        authority = (
            integration(data, registration["project_id"], registration["control_root"])
            if registration
            else {"head": None, "available": False}
        )
        saved = {
            "schema_version": "context.session-handoff/v1alpha1",
            "root": str(root),
            "session": session,
            "generation": uuid.uuid4().hex,
            "head": authority["head"],
            "integration": authority,
            "checkout_head": _git(root, "rev-parse", "HEAD"),
            "branch": _git(root, "branch", "--show-current"),
            "next_action": _clip(action, 800),
            "task_id": (card or {}).get("task_id"),
            "batch_id": (session_binding or {}).get("batch_id"),
            "todo": _clip(str((item or {}).get("title") or ""), 200) or None,
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
                saved["controller_scene"] = scene(
                    data,
                    registration["project_id"],
                    root=registration["control_root"],
                    session_id=session,
                    provider=provider,
                )
            except (OSError, ValueError, TypeError, KeyError):
                saved["controller_scene"] = {"status": "unavailable"}
            from .project_context import handoff, handoff_summary

            try:
                if (
                    os.environ.get("CONTINUITY_ROLE") == "controller"
                    or (session_binding or {}).get("role") == "controller"
                ):
                    handoff(
                        registration["control_root"],
                        data,
                        registration["project_id"],
                        next_action=action,
                        batch_id=(session_binding or {}).get("batch_id"),
                    )
            except (OSError, ValueError, KeyError) as exc:
                saved["controller_handoff_refresh"] = {
                    "status": "deferred",
                    "reason": _clip(str(exc), 512),
                    "local_handoff_saved": True,
                }
            try:
                saved["controller_handoff"] = handoff_summary(
                    data, registration["project_id"]
                )
            except (OSError, ValueError, KeyError):
                saved["controller_handoff"] = {"status": "unavailable"}
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
            "status": "saved-with-warning"
            if saved.get("controller_handoff_refresh")
            else "saved",
            "continue": True,
            "handoff_digest": saved["digest"],
            "injected_bytes": 0,
            **(
                {"controller_handoff_refresh": saved["controller_handoff_refresh"]}
                if saved.get("controller_handoff_refresh")
                else {}
            ),
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
    stable_project_recovery = None
    session_binding = None
    if valid:
        registration = resolve_project(root, data)
        from .project_context import integration

        authority = (
            integration(data, registration["project_id"], registration["control_root"])
            if registration
            else {"head": None, "available": False}
        )
        head = authority["head"]
        recovered = {
            "next_action": saved["next_action"],
            "constraints": saved["constraints"],
            "verification": saved["verification"],
            "head": head,
            "integration": authority,
            "executor_candidates": saved.get("executor_candidates", []),
            "executor_thinking": saved.get("executor_thinking", {}),
            "executor_adaptation": saved.get("executor_adaptation", {}),
            "handoff_head_changed": head != saved["head"],
            "task_id": saved["task_id"],
            "batch_id": saved.get("batch_id"),
            "branch": saved.get("branch"),
            "todo": saved.get("todo"),
            "contract_command": saved.get("contract_command"),
            "controller_scene": saved.get("controller_scene", {}),
            "controller_handoff": saved.get("controller_handoff", {}),
            **(
                {"controller_handoff_refresh": saved["controller_handoff_refresh"]}
                if saved.get("controller_handoff_refresh")
                else {}
            ),
        }
        if registration:
            from .project_context import handoff_summary
            from .controller_awareness import scene

            try:
                recovered["controller_handoff"] = handoff_summary(
                    data, registration["project_id"]
                )
                recovered["controller_scene"] = scene(
                    data, registration["project_id"], root=registration["control_root"]
                )
            except (OSError, ValueError, KeyError):
                recovered["controller_observation_warning"] = (
                    "Current project observations unavailable; saved session evidence remains labeled as a snapshot."
                )
            controller_role = (
                os.environ.get("CONTINUITY_ROLE") == "controller"
                or (session_binding or {}).get("role") == "controller"
            )
            if controller_role:
                member = os.environ.get("CONTINUITY_MEMBER") or getpass.getuser()
                recovered["controller_entry_command"] = (
                    "continuity controller start --project-id "
                    + registration["project_id"]
                    + " --member "
                    + member
                    + " --format brief --max-output-bytes 4096"
                )
        context = _render_resume(recovered)
    else:
        lead = "No verified session handoff exists; do not revive an older saved Work."
        context = RESUME_INSTRUCTION + "\n" + lead
        registration = resolve_project(root, data)
        if registration:
            from .project_context import handoff_summary
            from .controller_awareness import scene

            try:
                shared = handoff_summary(data, registration["project_id"])
                if shared.get("revision"):
                    try:
                        current_scene = scene(
                            data,
                            registration["project_id"],
                            root=registration["control_root"],
                            max_bytes=1400,
                            max_workers=4,
                        )
                    except (OSError, ValueError, KeyError):
                        current_scene = {
                            "status": "unavailable",
                            "warning": "Project plan exists; current observations could not be read.",
                        }
                    stable_project_recovery = {
                        "controller_handoff": shared,
                        "controller_scene": {
                            k: v for k, v in current_scene.items() if k != "observed_at"
                        },
                    }
                    context = _render_resume(
                        {
                            "next_action": shared.get("next_action"),
                            "batch_id": shared.get("current_batch"),
                            "controller_handoff": shared,
                            "controller_scene": current_scene,
                            "contract_command": "continuity collaboration task list --project-id "
                            + registration["project_id"],
                        },
                        force_inbox=True,
                        lead="No verified handoff for this session. A project controller "
                        "handoff exists; use its declared plan and "
                        "current controller_scene observations below. This grants no lease "
                        "or operation approval.",
                    )
                else:
                    context += " Project controller handoff is not recorded."
            except (OSError, ValueError, KeyError):
                context = RESUME_INSTRUCTION + "\n" + lead
                context += " Project controller handoff lookup failed; check continuity handoff show for this project before assuming it is absent."
    context = _clip(context, MAX_CONTEXT_BYTES)
    marker = path.with_suffix(".delivered.json")
    identity = hashlib.sha256(
        (
            _json(stable_project_recovery) if stable_project_recovery else context
        ).encode()
    ).hexdigest()
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
        from .adoption import cli_nudge, merge_context

        merge_context(
            result, "PostToolUse", cli_nudge(payload), MAX_CONTEXT_BYTES
        )
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

            base = output.get("hookSpecificOutput", {}).get("additionalContext", "")
            hint = planning_context(
                payload,
                host="claude",
                max_bytes=MAX_CONTEXT_BYTES - len(base.encode()) - 1,
            )
            if hint:
                combined = (base + "\n" + hint).strip()
                if len(combined.encode()) <= MAX_CONTEXT_BYTES:
                    output["hookSpecificOutput"] = {
                        "hookEventName": event,
                        "additionalContext": combined,
                    }
        from .adoption import cli_nudge, merge_context

        merge_context(output, "PostToolUse", cli_nudge(payload), MAX_CONTEXT_BYTES)
        from .controller_awareness import notice

        base = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        note = notice(
            payload,
            root=payload.get("cwd"),
            max_bytes=min(1024, MAX_CONTEXT_BYTES - len(base.encode()) - 1),
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
                    output.get("status") in {"saved", "saved-with-warning"}
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
