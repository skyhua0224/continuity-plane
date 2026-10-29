"""Small, deduplicated planning hints; no claims, tool gates or provider calls."""

import hashlib
import json
import os
import re
import shlex
from pathlib import Path

from .collaboration_registry import (
    _atomic_write,
    _locked,
    default_data_root,
    next_task,
    list_tasks,
    resolve_project,
)
from .intent_todo import IntentTodoError, compile_queue, load_queue
from .plan_route import master_route

MAX_HINT_BYTES = 1024


def context(payload, *, root=None, host="host", data_root=None, max_bytes=None):
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in {
        "SessionStart",
        "UserPromptSubmit",
    }:
        return None
    cwd = root or payload.get("cwd")
    session = payload.get("session_id")
    if (
        not isinstance(session, str)
        or not session
        or len(session) > 256
        or not isinstance(cwd, (str, Path))
        or not Path(cwd).is_absolute()
    ):
        return None
    root = Path(cwd).resolve()
    data = Path(data_root or default_data_root())
    try:
        project = resolve_project(root, data)
        if project is None and not (root / ".continuity").is_dir():
            return None
        queue_root = Path(project["control_root"]) if project else root
        actor = os.environ.get("CONTINUITY_ASSIGNEE") or session
        prompt = payload.get("prompt") or ""
        if not isinstance(prompt, str):
            return None
        action = r"修复|实现|完成|新增|添加|补充|处理|修改|测试|继续|推进|\b(?:fix|implement|complete|add|test|execute|continue|resume)\b"
        explicit = bool(
            re.search(
                r"^\s*(?:(?:请|帮我|please)\s*)?(?:" + action + r")", prompt, re.I
            )
        )
        if payload["hook_event_name"] == "UserPromptSubmit" and not explicit:
            return None
        failure = None
        marked = re.findall(r"(?m)^\s*(?:[-*•]|\d+[.)])\s+(.+)$", prompt)
        item_action = r"^(?:请|帮我|please\s+)?(?:修复|实现|完成|新增|添加|补充|处理|修改|测试|fix\b|implement\b|complete\b|add\b|test\b)"
        requested_list = bool(
            re.search(
                r"^(?:请|帮我).*(?:处理|完成|实现|修复)|^(?:please\s+)?(?:implement|fix|complete|execute)\b",
                prompt,
                re.I,
            )
        )
        clear_checklist = len(marked) >= 2 and (
            requested_list
            or all(
                re.search(item_action, item, re.I)
                and not item.rstrip().endswith(("?", "？"))
                for item in marked
            )
        )
        # Deterministic capture only for clear checklists, not arbitrary chat,
        # questions, incidental ideas or model-generated guesses.
        existing = load_queue(queue_root, data) or {}
        card = next_task(data, project_id=project["project_id"], assignee=actor) if project else None
        if card and card.get("assignee") != actor:
            card = None
        cards_only = bool(existing.get("card_projection") or card)
        if project and not cards_only and not (existing.get("owner_session_id") == session and existing.get("items")):
            cards_only = any(t["status"] in {"queued", "active", "dispatched", "blocked", "ready-for-review"} for t in list_tasks(data, project_id=project["project_id"]))
        foreign = existing.get("owner_session_id") not in {None, session}
        if clear_checklist and foreign and not cards_only:
            failure = "Another session owns the shared Todo; use separately assigned cards rather than silently appending to its queue"
        if clear_checklist and not foreign and not cards_only:
            try:
                compile_queue(
                    queue_root,
                    prompt,
                    data_root=data,
                    session_id=session,
                    assignee=actor,
                )
            except IntentTodoError as exc:
                failure = str(exc)
        queue = load_queue(queue_root, data) or {}
        active = next(
            (i for i in queue.get("items", []) if i.get("status") == "active"), None
        )
        card = (
            next_task(data, project_id=project["project_id"], assignee=actor)
            if project
            else None
        )
        if card and card.get("assignee") != actor:
            card = None
        if queue.get("owner_session_id") != session and not (
            card and active and active.get("task_id") == card["task_id"]
        ):
            active = None
        plan_changed = False
        binding = queue.get("plan_binding") or {}
        if active and binding.get("master_sha256") and not queue.get("card_projection"):
            route = master_route(
                queue_root,
                master_path=binding.get("master_path"),
                section=binding.get("section"),
            )
            plan_changed = route.get("master_sha256") != binding["master_sha256"]
        remaining = (
            sum(i.get("status") != "completed" for i in queue.get("items", []))
            if active
            else None
        )
        semantic = {
            "assignee": actor,
            "controller": os.environ.get("CONTINUITY_CONTROLLER"),
            "card": {k: card.get(k) for k in ("task_id", "status", "next_action")}
            if card
            else None,
            "active": {k: active.get(k) for k in ("item_id", "title", "task_id")}
            if active
            else None,
            "remaining": remaining,
            "plan_changed": plan_changed,
            "failure": failure,
        }
        from .reference_library import hint as reference_hint

        try:
            references = reference_hint(queue_root, data_root=data)
        except (OSError, ValueError, KeyError, TypeError):
            references = None
        semantic["references"] = references
        marker = (
            data
            / "planning-hints"
            / (
                hashlib.sha256(
                    (str(root) + ":" + host + ":" + session).encode()
                ).hexdigest()
                + ".json"
            )
        )
        digest = hashlib.sha256(
            json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        command = "continuity collaboration task list" if project else "continuity todo list"
        command += " --root " + shlex.quote(str(queue_root)) + " --data-root " + shlex.quote(str(data))
        if card:
            command = "continuity collaboration task show --project-id " + shlex.quote(project["project_id"]) + " --task-id " + shlex.quote(card["task_id"]) + " --data-root " + shlex.quote(str(data))
        pid = project["project_id"] if project else None
        scope = f"project_id={pid!r}" if pid else ""
        if card:
            look = f"continuity_task_show({scope}, task_id={card['task_id']!r})"
        elif pid:
            look = f"continuity_task_list({scope})"
        else:
            look = "continuity_plan(action='todo.list')"
        role = os.environ.get("CONTINUITY_ROLE") or ("worker" if card else "controller")
        lines = [
            "Continuity is connected"
            + (f" for {pid}" if pid else "")
            + ": use its MCP tools (CLI only as fallback); ordinary work remains allowed.",
            "Inspect current work: " + look,
            (
                "As worker: continuity_progress at milestones; continuity_report_final(summary=…, checks=…) when done; continuity_review_request for a review."
                if role in {"worker", "reviewer"}
                else "As controller: continuity_task_add per piece of work; continuity_worker_dispatch then continuity_events_wait; continuity_handoff_write before you stop."
            ),
            "CLI fallback: " + command,
        ]
        if references:
            lines.insert(0, references)
        if failure:
            lines.append(
                "Checklist not fully captured: "
                + failure
                + ". Reconcile with cards; do not drop work or stop ordinary execution."
            )
        if plan_changed:
            lines.append(
                "Saved plan changed: reconcile remaining items with current intent; do not replay old titles."
            )
        elif active:
            title = active["title"].encode()[:200].decode("utf-8", "ignore")
            lines.append(
                f"Todo {active['item_id']}: {title}; task={active.get('task_id') or 'unlinked'}, remaining={remaining}."
            )
        if card:
            action_text = card["next_action"].encode()[:160].decode("utf-8", "ignore")
            lines.append(
                f"Your card {card['task_id']} ({card['status']}): {action_text}"
            )
        ending = "\nPut multi-step work on cards once, verify, report and continue; progress notes are nonterminal."
        # Include complete lines only: never turn a bounded hint into a broken
        # command. Unknown write arguments are deliberately not invented.
        kept = []
        for line in lines:
            if len(("\n".join(kept + [line]) + ending).encode()) <= MAX_HINT_BYTES:
                kept.append(line)
        text = "\n".join(kept) + ending
        size = len(text.encode())
        if max_bytes is not None and size > max_bytes:
            # The event had no room; leave the digest unmarked so a later event
            # with room can still deliver this hint.
            return None
        from .handoff_journal import reserve

        with _locked(marker):
            try:
                previous = json.loads(marker.read_text())
            except (OSError, ValueError):
                previous = {}
            if previous.get("digest") == digest:
                return None
            if not reserve(root, session, text, data_root=data, kind="planning-hint"):
                _atomic_write(marker, {"digest": digest, "budget": "exhausted"})
                return None
            _atomic_write(marker, {"digest": digest})
        return text
    except (OSError, ValueError, KeyError, TypeError):
        return None


def omitted_checklist_hint(payload, *, data_root=None, max_bytes=MAX_HINT_BYTES):
    """One advisory per omitted instruction/session; never requires a Todo."""
    root, session = payload.get("cwd"), payload.get("session_id")
    if not isinstance(root, str) or not Path(root).is_absolute() or not isinstance(session, str) or not session:
        return None
    key = hashlib.sha256((root + ":" + session + ":" + str(payload.get("prompt_omitted_sha256", "once"))).encode()).hexdigest()
    path = Path(data_root or default_data_root()) / "omitted-checklists" / (key + ".json")
    with _locked(path):
        if path.exists():
            return None
        command = "continuity collaboration task list --root " + shlex.quote(root)
        if data_root:
            command += " --data-root " + shlex.quote(str(data_root))
        text = "Large instruction was not auto-captured. Existing cards/handoff remain the plan; no separate Todo is required. Look with continuity_task_list(); add missing pieces with continuity_task_add. CLI fallback: " + command
        if len(text.encode()) > MAX_HINT_BYTES:
            text = "Large instruction was not auto-captured; keep using existing cards/handoff. Look with continuity_task_list(); parameters: continuity help task.list"
        if len(text.encode()) > max_bytes:
            return None
        from .handoff_journal import reserve

        if not reserve(root, session, text, data_root=data_root, kind="omitted-hint"):
            return None
        _atomic_write(path, {"noticed": True})
    return text
