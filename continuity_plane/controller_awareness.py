"""Changed-only collaboration notices and a bounded controller recovery scene."""

import hashlib
import json
import os
from pathlib import Path
from .collaboration_registry import (
    _atomic_write,
    _locked,
    default_data_root,
    iter_changes,
    latest_change_cursor,
    resolve_project,
    list_tasks,
    _load_ledger,
)


# Terminal outcomes only. `attempt.retrying` is a retry inside a live job, and an
# `attempt.finished` that carries a job_id is reported through its `job.*` event.
_ENDED_EVENTS = {
    "attempt.finished",
    "run.finished",
    "job.completed",
    "job.failed",
    "job.interrupted",
    "job.lost",
}


def _ended(rows):
    from .collaboration_stream import superseded_by_job_event

    return [
        r
        for r in rows
        if r["event"] in _ENDED_EVENTS and not superseded_by_job_event(r)
    ]


def _notice(payload, *, root=None, data_root=None, max_bytes=512):
    if os.environ.get("CONTINUITY_DISABLED") == "1" or payload.get(
        "hook_event_name"
    ) not in {"SessionStart", "UserPromptSubmit", "PostToolUse"}:
        return None
    root = root or payload.get("cwd")
    session = payload.get("session_id")
    if not isinstance(root, (str, Path)) or not isinstance(session, str) or not session:
        return None
    data = Path(data_root or default_data_root())
    registration = resolve_project(root, data)
    if not registration or max_bytes < 160:
        return None
    key = hashlib.sha256(
        (str(Path(root).resolve()) + ":" + session).encode()
    ).hexdigest()
    marker = data / "controller-notices" / (key + ".json")
    from .rules_delivery import bundle

    rules_digest = bundle(
        data, project_id=registration["project_id"], audience="worker"
    )["rules_digest"]
    try:
        with _locked(marker):
            previous = json.loads(marker.read_text()) if marker.exists() else None
            if previous is None:
                _atomic_write(
                    marker,
                    {
                        "cursor": latest_change_cursor(
                            data, project_id=registration["project_id"]
                        ),
                        "rules_digest": rules_digest,
                    },
                )
                return None
            cursor = previous["cursor"]
            relevant = []
            scanned = 0
            for r in iter_changes(
                data,
                project_id=registration["project_id"],
                since=cursor,
            ):
                scanned += 1
                if (
                    r["event"] in _ENDED_EVENTS
                    or (r["event"] == "task.updated" and r.get("status") == "blocked")
                    or r["event"] == "rule.updated"
                    or r["event"] == "quota.changed"
                    or r["event"]
                    in {
                        "dispatch.queue-waiting",
                        "dispatch.queue-blocked",
                        "dispatch.queue-retry-scheduled",
                    }
                ):
                    relevant.append(r)
                cursor = max(cursor, r["cursor"])
                if scanned >= 5000:
                    break
            if previous.get("rules_digest") != rules_digest and not any(
                r["event"] == "rule.updated" for r in relevant
            ):
                relevant.append({"event": "rule.updated"})
            if os.environ.get("CONTINUITY_ROLE") in {"worker", "reviewer"}:
                own = os.environ.get("CONTINUITY_TASK_ID")
                relevant = [
                    r
                    for r in relevant
                    if r.get("task_id") == own or r["event"] == "rule.updated"
                ]
            extra, others = _other_projects(
                data, registration["project_id"], previous.get("projects") or {}
            )
            if not relevant and not extra:
                _atomic_write(
                    marker,
                    {"cursor": cursor, "rules_digest": rules_digest, "projects": others},
                )
                return None
            finished = _ended(relevant)
            blocked = [
                r
                for r in relevant
                if r.get("status") == "blocked" and r["event"] == "task.updated"
            ]
            rules = [r for r in relevant if r["event"] == "rule.updated"]
            quota = [r for r in relevant if r["event"] == "quota.changed"]
            queue = [
                r
                for r in relevant
                if r["event"]
                in {
                    "dispatch.queue-waiting",
                    "dispatch.queue-blocked",
                    "dispatch.queue-retry-scheduled",
                }
            ]
            pid = registration["project_id"]
            lines = _action_lines(pid, finished, blocked, rules, quota, queue)
            lines += extra
            if not lines:
                # Only non-terminal rows (a retry, or an attempt.finished whose
                # job.* event is not written yet): nothing to say.
                _atomic_write(
                    marker,
                    {"cursor": cursor, "rules_digest": rules_digest, "projects": others},
                )
                return None
            text = _fit("continuity: " + " | ".join(lines), max_bytes)
            from .handoff_journal import reserve

            if not reserve(root, session, text, data_root=data, kind="notice"):
                _atomic_write(
                    marker,
                    {
                        "cursor": cursor,
                        "rules_digest": rules_digest,
                        "projects": others,
                        "budget": "exhausted",
                    },
                )
                return None
            _atomic_write(
                marker,
                {"cursor": cursor, "rules_digest": rules_digest, "projects": others},
            )
            return text
    except (OSError, ValueError, TypeError, KeyError):
        return None


def notice(*args, **kwargs):
    try:
        return _notice(*args, **kwargs)
    except Exception:
        return None


def scene(
    data_root,
    project_id,
    *,
    root=None,
    session_id=None,
    provider=None,
    max_bytes=2200,
    max_workers=8,
):
    from .collaboration_context import list_commitments
    from .collaboration_registry import project_data_dir

    data = Path(data_root or default_data_root())
    cards = [
        t
        for t in list_tasks(data, project_id=project_id)
        if t["status"] in {"active", "dispatched", "blocked"}
    ]
    ledger = _load_ledger(data, project_id)
    index = project_data_dir(data, project_id) / "worker-jobs.json"
    try:
        jobs = json.loads(index.read_text()) if index.exists() else {}
    except (OSError, ValueError):
        jobs = {}
    workers = []
    from .controller_observation import observe

    current_cursor = latest_change_cursor(data, project_id=project_id)
    observed = observe(
        data,
        project_id,
        recipient=os.environ.get("CONTINUITY_ASSIGNEE"),
        since=current_cursor,
        max_items=max(64, len(ledger.get("execution_bindings", {}))),
    )
    running_ids = {
        r["task_id"] for r in observed["workers"] if r["status"] == "running"
    }
    running = [t for t in cards if t["task_id"] in running_ids]
    native = [r for r in observed["native_workers"] if r["status"] != "stopped"]
    for task in running[:max_workers]:
        job = jobs.get(task["task_id"], {})
        thread = None
        if job.get("thread_file"):
            try:
                from .runtime_origin import require_local

                require_local(
                    job.get("execution_origin"),
                    job.get("profile", {}).get("profile_provider", "codex"),
                )
                with Path(job["thread_file"]).open("rb") as stream:
                    thread = stream.read(256).decode("utf-8").strip()
            except (OSError, UnicodeError, ValueError):
                pass
        workers.append(
            {
                "task": task["task_id"],
                "status": task["status"],
                "assignee": task["assignee"],
                "thread": thread,
            }
        )
    commitments = list_commitments(data, project_id=project_id)
    records = ledger.get("workflow_records", {})
    batches = list(records.get("batch", {}).values())[-2:]
    decisions = [
        r for r in records.get("decision", {}).values() if r["value"].get("approved")
    ][-3:]
    from .project_context import integration, binding, lessons

    integration_state = (
        integration(data, project_id, root)
        if root
        else {"head": None, "available": False}
    )
    session_binding = (
        binding(data, project_id, provider, session_id)
        if session_id and provider
        else None
    )
    learned = lessons(data, project_id, max_results=2)
    cursor = observed["change_cursor"]
    ended = observed["last_ended_cursor"] or None
    result = {
        "integration": integration_state,
        "session_binding": {
            k: session_binding.get(k) for k in ("task_id", "batch_id", "active")
        }
        if session_binding
        else None,
        "change_cursor": cursor,
        "last_ended_cursor": ended,
        "lessons": [
            {"id": r["lesson_id"], "text": r["text"][:120], "source": r["source"][:100]}
            for r in learned["items"]
        ],
        "lessons_total": learned["total_items"],
        "workers": workers,
        "workers_total": len(running),
        "native_workers": [
            {
                "attempt_id": r["attempt_id"],
                "agent_type": r["agent_type"],
                "status": "observed-running; no heartbeat"
                if r["status"] == "observed-running"
                else r["status"],
            }
            for r in native[:4]
        ],
        "native_workers_total": len(native),
        "observed_at": observed["as_of"],
        "reports": observed["reports"][:3],
        "reports_total": observed["reports_total"],
        "inbox": observed["inbox"],
        "controller_access": observed["controller"],
        "drift_count": len(observed["drift"]),
        "work_cards_total": len(cards),
        "candidate_work": [
            {
                "task": t["task_id"],
                "next_action": t["next_action"][:120],
                "updated_at": t["updated_at"],
            }
            for t in sorted(cards, key=lambda t: t["updated_at"], reverse=True)
            if t.get("assignee") == os.environ.get("CONTINUITY_ASSIGNEE")
        ][:3],
        "commitments": [
            {"id": c["commitment_id"], "text": c["text"][:100]} for c in commitments[:4]
        ],
        "commitments_total": len(commitments),
        "batches": [
            {
                "id": b["id"],
                "baseline": b["value"].get("baseline"),
                "result": b["value"].get("result_commit"),
            }
            for b in batches
        ],
        "approved_decisions": [
            {
                "id": d["id"],
                "commit": d["value"].get("commit"),
                "source": d["value"].get("source"),
            }
            for d in decisions
        ],
    }
    while len(json.dumps(result, ensure_ascii=False).encode()) > max_bytes:
        key = next(
            (
                k
                for k in (
                    "workers",
                    "commitments",
                    "batches",
                    "approved_decisions",
                    "candidate_work",
                    "lessons",
                    "native_workers",
                    "reports",
                )
                if result[k]
            ),
            None,
        )
        if not key:
            break
        result[key].pop()
        result["details_omitted"] = True
    return result


_ENDED_NEXT = "continuity_worker_report(project_id={p!r}, task_id={t!r})"


def _outcome(row):
    from .collaboration_stream import event_kind

    kind = event_kind(row)
    reason = row.get("failure_class") or row.get("reason")
    return kind + (f" ({reason})" if reason and reason not in {kind, "completed"} else "")


def _action_lines(pid, finished, blocked, rules, quota, queue, limit=4):
    """One line per event, each naming the MCP call that handles it."""
    lines = []
    for row in finished[:limit]:
        task = row.get("task_id") or row.get("agent_type") or row.get("attempt_id")
        lines.append(
            f"[{pid}] worker {task} {_outcome(row)} → "
            + _ENDED_NEXT.format(p=pid, t=task)
        )
    if len(finished) > limit:
        lines.append(
            f"[{pid}] {len(finished) - limit} more ended → continuity_events_wait()"
        )
    for row in blocked[:2]:
        lines.append(
            f"[{pid}] card {row.get('task_id')} blocked → "
            f"continuity_task_show(project_id={pid!r}, task_id={row.get('task_id')!r})"
        )
    if rules:
        lines.append(
            f"[{pid}] rules changed → continuity_plan(action='rules.affected', project_id={pid!r})"
        )
    if quota:
        lines.append(
            f"[{pid}] quota changed ("
            + "; ".join(f"{r.get('model')} {r.get('status')}" for r in quota[:3])
            + f") → continuity_plan(action='quota.report', project_id={pid!r})"
        )
    if queue:
        lines.append(
            f"[{pid}] {len(queue)} queued dispatch waiting → "
            f"continuity_plan(action='worker.queue.status', project_id={pid!r})"
        )
    return lines


def _watched_projects(data, current):
    """Projects named in CONTINUITY_WATCH_PROJECTS or led by this controller."""
    from .collaboration_registry import list_projects
    from .controller_lease import active_owner

    me = {
        v
        for v in (
            os.environ.get("CONTINUITY_CONTROLLER"),
            os.environ.get("CONTINUITY_ASSIGNEE"),
        )
        if v
    }
    named = {
        p.strip()
        for p in os.environ.get("CONTINUITY_WATCH_PROJECTS", "").split(",")
        if p.strip()
    }
    if os.environ.get("CONTINUITY_ROLE") in {"worker", "reviewer"}:
        return []
    watched = []
    for item in list_projects(data):
        pid = item["project_id"]
        if pid == current:
            continue
        if pid in named or (me and active_owner(data, pid) in me):
            watched.append(pid)
    return watched


def _other_projects(data, current, cursors, scan_limit=2000):
    lines = []
    updated = dict(cursors)
    for pid in _watched_projects(data, current):
        if pid not in updated:
            updated[pid] = latest_change_cursor(data, project_id=pid)
            continue
        finished, blocked = [], []
        for scanned, row in enumerate(
            iter_changes(data, project_id=pid, since=updated[pid])
        ):
            if row["event"] in _ENDED_EVENTS:
                finished.append(row)
            elif row["event"] == "task.updated" and row.get("status") == "blocked":
                blocked.append(row)
            updated[pid] = max(updated[pid], row["cursor"])
            if scanned >= scan_limit:
                break
        lines += _action_lines(pid, _ended(finished), blocked, [], [], [], limit=3)
    return lines, updated


def _fit(text, max_bytes):
    data = text.encode()
    if len(data) <= max_bytes:
        return text
    return data[: max_bytes - 3].decode("utf-8", "ignore") + "…"
