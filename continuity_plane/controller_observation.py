"""Read-time project observations, kept separate from declared controller plans.

Never reads provider transcripts or interprets a report's existence as success.
Recoverable facts come from the ledger, durable events and bounded artifact paths.
"""

from datetime import UTC, datetime
import json
import os
from pathlib import Path

from .collaboration_registry import (
    _load_ledger,
    _now,
    default_data_root,
    list_changes,
    list_projects,
    project_data_dir,
)


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def event_facts(data_root, project_id, since=0):
    cursor, facts = since, []
    while True:
        page = list_changes(
            data_root, project_id=project_id, since=cursor, max_results=500
        )
        facts.extend(page["changes"])
        if not page["truncated"] or page["next_cursor"] <= cursor:
            return page["next_cursor"], facts
        cursor = page["next_cursor"]


def report_facts(data_root, project_id, cards=None, jobs=None):
    """Stat only task-local reports below explicit persistent artifact roots."""
    data = Path(data_root or default_data_root())
    directory = project_data_dir(data, project_id)
    if cards is None:
        cards = _load_ledger(data, project_id)["tasks"]
    if jobs is None:
        try:
            jobs = json.loads((directory / "worker-jobs.json").read_text())
        except (OSError, ValueError):
            jobs = {}
    work = (
        Path(os.environ.get("CONTINUITY_WORK_ROOT") or data / "work")
        .expanduser()
        .resolve()
    )
    job_root = (directory / "worker-jobs").resolve()
    found = []
    for card in cards:
        job = jobs.get(card["task_id"], {})
        candidates = [
            (
                work / project_id / card["task_id"] / "report.md",
                work / project_id,
                "project-scoped-work",
            ),
            (
                work / card["task_id"] / "report.md",
                work,
                "unattributed-legacy-candidate",
            ),
        ]
        if job.get("report_file") and job.get("task_id") == card["task_id"]:
            candidates.append((Path(job["report_file"]), job_root, "project-job-index"))
        for file, allowed, attribution in candidates:
            try:
                resolved = file.resolve()
                if not resolved.is_relative_to(allowed) or not resolved.is_file():
                    continue
                info = resolved.stat()
                if info.st_size <= 0:
                    continue
            except OSError:
                continue
            newer = info.st_mtime > (timestamp(card.get("updated_at")) or 0)
            found.append(
                {
                    "task_id": card["task_id"],
                    "path": str(resolved),
                    "bytes": info.st_size,
                    "modified_at": datetime.fromtimestamp(
                        info.st_mtime, UTC
                    ).isoformat(),
                    "newer_than_card": newer,
                    "kind": "report-without-card-update" if newer else "report-present",
                    "authority": "file metadata only; not a verified outcome",
                    "attribution": attribution,
                    "verify_project_before_reading": attribution
                    == "unattributed-legacy-candidate",
                }
            )
    return found


def observe(data_root, project_id, *, recipient=None, since=0, max_items=12):
    from .project_context import _load, _path, integration
    from .controller_lease import view
    from .collaboration_inbox import unread_summary

    data = Path(data_root or default_data_root())
    ledger = _load_ledger(data, project_id)
    cards = ledger["tasks"]
    directory = project_data_dir(data, project_id)
    try:
        jobs = json.loads((directory / "worker-jobs.json").read_text())
    except (OSError, ValueError):
        jobs = {}
    # Handoff views only need terminal events after the saved scene cursor.
    # Replaying the entire append-only history made large projects block for
    # minutes even though older events were already represented in the snapshot.
    cursor, events = event_facts(data, project_id, since=max(0, int(since or 0)))
    terminal = {}
    for row in events:
        if row.get("event") in {
            "attempt.finished",
            "attempt.timeout",
            "dispatch.policy-stopped",
        }:
            value = {**row, **row.get("fields", {})}
            terminal[(row.get("task_id"), value.get("attempt_id"))] = value
    workers = []
    for task_id, binding in ledger.get("execution_bindings", {}).items():
        end = terminal.get((task_id, binding.get("attempt_id")))
        status = (
            end.get("status", "interrupted")
            if end
            else binding.get("status", "unknown")
        )
        workers.append(
            {
                "task_id": task_id,
                "attempt_id": binding.get("attempt_id"),
                "assignee": binding.get("assignee"),
                "status": status,
                "source": "terminal-event" if end else "execution-binding",
                "cursor": end.get("cursor") if end else None,
                "failure_class": end.get("failure_class") if end else None,
                "reason": end.get("reason") if end else None,
            }
        )
    context = _load(_path(data, project_id))
    registration = next(
        (p for p in list_projects(data) if p["project_id"] == project_id), {}
    )
    integration_state = integration(data, project_id, registration.get("control_root"))
    records = ledger.get("workflow_records", {})
    batches = list(records.get("batch", {}).values())
    decisions = [
        r for r in records.get("decision", {}).values() if r["value"].get("approved")
    ]
    now = datetime.now(UTC).timestamp()
    native = []
    for value in context.get("native_attempts", {}).values():
        age = max(0, now - (timestamp(value.get("observed_at")) or now))
        status = value["status"]
        if status == "running":
            status = "unknown-stale" if age >= 3600 else "observed-running"
        native.append(
            {
                "attempt_id": value["attempt_id"],
                "status": status,
                "agent_type": value.get("agent_type", "unknown"),
                "age_seconds": round(age),
                "heartbeat_available": False,
                "reason": value.get("reason"),
                "source": "native-hook-observation",
            }
        )
    reports = report_facts(data, project_id, cards, jobs)
    stale = [
        {"task_id": t["task_id"], "kind": "stale-dispatch"}
        for t in cards
        if t["status"] in {"active", "dispatched"}
        and now - (timestamp(t.get("updated_at")) or now) >= 6 * 3600
    ]
    attention = []
    for card in cards:
        if card["status"] == "blocked":
            attention.append(
                {
                    "kind": "blocked-card",
                    "task_id": card["task_id"],
                    "status": card["status"],
                    "title": card["title"],
                    "reason": card.get("blocked_reason") or card.get("next_action"),
                    "details_command": f"continuity collaboration task show --project-id {project_id} --task-id {card['task_id']}",
                }
            )
        elif card["status"] == "ready-for-review":
            attention.append(
                {
                    "kind": "review-waiting",
                    "task_id": card["task_id"],
                    "status": card["status"],
                    "title": card["title"],
                    "reason": card.get("next_action"),
                    "details_command": f"continuity collaboration task show --project-id {project_id} --task-id {card['task_id']}",
                }
            )
    for worker in workers:
        if worker["status"] in {"failed", "interrupted", "blocked"}:
            attention.append(
                {
                    "kind": "failed-or-interrupted-attempt",
                    "task_id": worker["task_id"],
                    "attempt_id": worker["attempt_id"],
                    "status": worker["status"],
                    "failure_class": worker.get("failure_class"),
                    "reason": worker.get("reason"),
                    "details_command": f"continuity collaboration task show --project-id {project_id} --task-id {worker['task_id']}",
                }
            )
    for report in reports:
        if report.get("newer_than_card"):
            attention.append(
                {
                    "kind": "report-without-card-update",
                    "task_id": report["task_id"],
                    "status": "attention",
                    "reason": "worker report is newer than the card; review it before redispatch",
                    "details_command": f"continuity worker report --project-id {project_id} --task-id {report['task_id']}",
                }
            )
    unique_attention = []
    seen_attention = set()
    for row in attention:
        identity = (
            row.get("kind"),
            row.get("task_id"),
            row.get("attempt_id"),
        )
        if identity not in seen_attention:
            unique_attention.append(row)
            seen_attention.add(identity)
    priority = {
        "blocked-card": 0,
        "failed-or-interrupted-attempt": 1,
        "report-without-card-update": 2,
        "review-waiting": 3,
    }
    unique_attention.sort(
        key=lambda row: (
            priority.get(row.get("kind"), 99),
            row.get("task_id") or "",
        )
    )
    changes = [
        {k: r.get(k) for k in ("cursor", "event", "task_id", "observed_at")}
        for r in events
        if r["cursor"] > since
    ]
    changed_ids = {r["task_id"] for r in changes} | {
        r["task_id"] for r in reports if r["newer_than_card"]
    }
    changed_cards = [
        {
            "task_id": card["task_id"],
            "status": card["status"],
            "assignee": card["assignee"],
            "title": card["title"],
            "blocked_reason": card.get("blocked_reason"),
            "next_action": card["next_action"][:240],
            "updated_at": card["updated_at"],
            "details_command": f"continuity task show --project-id {project_id} --task-id {card['task_id']}",
        }
        for card in sorted(cards, key=lambda r: r["updated_at"], reverse=True)
        if card["task_id"] in changed_ids
    ]
    limit = max(0, max_items)
    return {
        "as_of": _now(),
        "integration": integration_state,
        "latest_integrated_batch": batches[-1]["id"] if batches else None,
        "recent_batches": [
            {
                "id": r["id"],
                "baseline": r["value"].get("baseline"),
                "result_commit": r["value"].get("result_commit"),
            }
            for r in batches[-2:]
        ],
        "approved_decisions": [
            {
                "id": r["id"],
                "source": str(r["value"].get("source") or "")[:160],
                "authority": "recorded declaration; verify decision provenance",
            }
            for r in decisions[-3:]
        ],
        "change_cursor": cursor,
        "last_ended_cursor": max([r["cursor"] for r in terminal.values()] or [0]),
        "workers": workers[-limit:] if limit else [],
        "workers_total": len(workers),
        "running_workers_total": sum(r["status"] == "running" for r in workers),
        "native_workers": native[-limit:] if limit else [],
        "native_workers_total": len(native),
        "native_unresolved_total": sum(
            r["status"] in {"observed-running", "unknown-stale"} for r in native
        ),
        "reports": reports[:limit],
        "reports_total": len(reports),
        "attention": unique_attention[:limit],
        "attention_total": len(unique_attention),
        "drift": (stale + [r for r in reports if r["newer_than_card"]])[:limit],
        "inbox": unread_summary(data, project_id=project_id, recipient=recipient),
        "controller": view(data, project_id, recipient),
        "changes_since_handoff": changes[-limit:] if limit else [],
        "changes_since_handoff_total": len(changes),
        "changed_cards": changed_cards[:limit],
        "changed_cards_total": len(changed_cards),
        "source": "current ledger, terminal events and report metadata; no provider query",
    }


def handoff_view(data_root, project_id, *, recipient=None):
    from .project_context import _load, _path

    saved = _load(_path(data_root, project_id)).get("handoff")
    since = (saved or {}).get("scene", {}).get("change_cursor", 0)
    if not since and saved and saved.get("updated_at"):
        from .collaboration_registry import change_cursor_at_or_before

        since = change_cursor_at_or_before(
            data_root,
            project_id=project_id,
            timestamp=saved["updated_at"],
        )
    observed = observe(data_root, project_id, recipient=recipient, since=since)
    # Preserve the original scene's in-flight-only counters for older clients.
    scene = {
        **observed,
        "workers": [r for r in observed["workers"] if r["status"] == "running"],
        "workers_total": observed["running_workers_total"],
        "native_workers": [
            r
            for r in observed["native_workers"]
            if r["status"] in {"observed-running", "unknown-stale"}
        ],
        "native_workers_total": observed["native_unresolved_total"],
    }
    return {
        **(saved or {"status": "not-recorded"}),
        "declared": {
            k: saved.get(k)
            for k in (
                "revision",
                "updated_at",
                "plan",
                "next_action",
                "current_batch",
                "pending_decisions",
            )
        }
        if saved
        else None,
        "snapshot_scene": (saved or {}).get("scene"),
        "scene": scene,
        "observed": observed,
        "read_model": "declared plan is versioned; observed state recomputed on every read",
    }


def draft(data_root, project_id):
    """Produce evidence-linked candidates; never invent or replace the plan."""
    view = handoff_view(data_root, project_id)
    observation = view["observed"]
    return {
        "authority": "candidate-not-approved-plan",
        "base_revision": view.get("revision", 0),
        "since": (view.get("snapshot_scene") or {}).get("change_cursor", 0),
        "through_cursor": observation["change_cursor"],
        "candidates": observation["changes_since_handoff"],
        "candidates_total": observation["changes_since_handoff_total"],
        "reports": observation["reports"],
        "changed_cards": observation["changed_cards"],
        "requires_controller_plan_review": True,
    }
