"""Changed-only collaboration notices and a bounded controller recovery scene."""

import hashlib
import json
import os
from pathlib import Path
from .collaboration_registry import (
    _atomic_write,
    _locked,
    default_data_root,
    resolve_project,
    list_changes,
    list_tasks,
    _load_ledger,
)


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
            first = list_changes(
                data,
                project_id=registration["project_id"],
                since=(previous or {}).get("cursor", 0),
                max_results=500,
            )
            if previous is None:
                _atomic_write(
                    marker,
                    {
                        "cursor": first["high_water_cursor"],
                        "rules_digest": rules_digest,
                    },
                )
                return None
            cursor = previous["cursor"]
            relevant = []
            page = first
            for _ in range(10):
                relevant.extend(
                    r
                    for r in page["changes"]
                    if r["event"] in {"attempt.finished", "run.finished"}
                    or (r["event"] == "task.updated" and r.get("status") == "blocked")
                    or r["event"] == "rule.updated"
                    or r["event"] == "quota.changed"
                )
                cursor = page["next_cursor"]
                if not page["truncated"]:
                    break
                page = list_changes(
                    data,
                    project_id=registration["project_id"],
                    since=cursor,
                    max_results=500,
                )
            if previous.get("rules_digest") != rules_digest and not any(
                r["event"] == "rule.updated" for r in relevant
            ):
                relevant.append({"event": "rule.updated"})
            if not relevant:
                _atomic_write(marker, {"cursor": cursor, "rules_digest": rules_digest})
                return None
            if os.environ.get("CONTINUITY_ROLE") in {"worker", "reviewer"}:
                own = os.environ.get("CONTINUITY_TASK_ID")
                relevant = [
                    r
                    for r in relevant
                    if r.get("task_id") == own or r["event"] == "rule.updated"
                ]
                if not relevant:
                    _atomic_write(
                        marker, {"cursor": cursor, "rules_digest": rules_digest}
                    )
                    return None
            finished = [
                r
                for r in relevant
                if r["event"] in {"attempt.finished", "run.finished"}
            ]
            blocked = [
                r
                for r in relevant
                if r.get("status") == "blocked" and r["event"] == "task.updated"
            ]
            rules = [r for r in relevant if r["event"] == "rule.updated"]
            quota = [r for r in relevant if r["event"] == "quota.changed"]
            parts = []
            if finished:
                parts.append(
                    f"{len(finished)} worker ended ("
                    + "; ".join(
                        "%s %s" % (r.get("task_id"), r.get("reason") or r.get("status"))
                        for r in finished[:3]
                    )
                    + ")"
                )
            if blocked:
                parts.append(f"{len(blocked)} cards blocked")
            if rules:
                parts.append(f"{len(rules)} rule revisions; inspect rules affected")
            if quota:
                parts.append(
                    "quota changed: "
                    + "; ".join(
                        f"{r.get('model')} {r.get('status')}" for r in quota[:3]
                    )
                    + "; inspect quota report/handoff"
                )
            text = (
                ("continuity: " + ", ".join(parts) + ".")
                .encode()[:max_bytes]
                .decode("utf-8", "ignore")
            )
            _atomic_write(marker, {"cursor": cursor, "rules_digest": rules_digest})
            return text
    except (OSError, ValueError, TypeError, KeyError):
        return None


def notice(*args, **kwargs):
    try:
        return _notice(*args, **kwargs)
    except Exception:
        return None


def scene(data_root, project_id):
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
    running = [
        t
        for t in cards
        if ledger.get("execution_bindings", {}).get(t["task_id"], {}).get("status")
        == "running"
    ]
    for task in running[:8]:
        job = jobs.get(task["task_id"], {})
        thread = None
        if job.get("thread_file"):
            try:
                with Path(job["thread_file"]).open("rb") as stream:
                    thread = stream.read(256).decode("utf-8").strip()
            except (OSError, UnicodeError):
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
    result = {
        "workers": workers,
        "workers_total": len(running),
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
    while len(json.dumps(result, ensure_ascii=False).encode()) > 1400:
        key = next(
            (
                k
                for k in (
                    "workers",
                    "commitments",
                    "batches",
                    "approved_decisions",
                    "candidate_work",
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
