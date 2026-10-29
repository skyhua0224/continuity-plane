"""Truthful-state reconciler: card transitions the repository and jobs already prove.

Read-only by default. `--apply` records only the one change that git proves (a
card commit already on the integration line becomes `merged`); merging a ready
card and judging a stale one stay human decisions and are only suggested.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from .collaboration_registry import (
    _ASSIGNEE_RE,
    _load_ledger,
    _record_change,
    default_data_root,
    list_projects,
    update_task,
)

SCHEMA = "context.reconcile/v1alpha1"
_OPEN = {"queued", "active", "dispatched", "blocked", "ready-for-review", "completed"}
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_ACCEPT = re.compile(r"\baccept(?:ed)?\b", re.I)
_NOT_ACCEPT = re.compile(
    r"changes? needed|needs? changes|request(?:ed)? changes|\breject(?:ed)?\b|"
    r"(?:do not|don't|not) accept",
    re.I,
)


def _git_ancestor(root, commit, ref):
    """True/False when git can tell, None when the commit or ref is unknown."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, ref],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {0: True, 1: False}.get(proc.returncode)


def _mentions(card, task_id):
    pattern = re.compile(rf"(?<![\w.-]){re.escape(task_id)}(?![\w.-])")
    return any(
        pattern.search(str(card.get(key) or ""))
        for key in ("task_id", "title", "objective", "next_action")
    ) or task_id in (card.get("dependencies") or [])


def _accepted_review(ledger, card):
    """The latest completed review card for `card` whose verdict is an accept."""
    task_id = card["task_id"]
    for review in sorted(ledger["tasks"], key=lambda t: t["updated_at"], reverse=True):
        is_review = review.get("lane_id") == "review" or review["task_id"].startswith("review-")
        if (
            review["task_id"] == task_id
            or not is_review
            or review["status"] not in {"completed", "merged", "accepted"}
            or not _mentions(review, task_id)
        ):
            continue
        report = ledger.get("task_details", {}).get(review["task_id"], {}).get("report", {})
        verdict = f"{review.get('result') or ''}\n{str(report.get('text') or '')[:4000]}"
        if _ACCEPT.search(verdict) and not _NOT_ACCEPT.search(verdict):
            return review
    return None


def _running_tasks(data, project_id, ledger, now):
    from .worker_dispatch import _job_items, find_lost_jobs

    lost = {row["job_id"] for row in find_lost_jobs(data, project_id, now=now)}
    running = {
        task_id
        for task_id, binding in ledger.get("execution_bindings", {}).items()
        if binding.get("status") == "running"
    }
    for item in _job_items(data, project_id):
        if (
            isinstance(item, dict)
            and (item.get("job_status") or item.get("status")) == "running"
            and item.get("job_id") not in lost
        ):
            running.add(item.get("task_id"))
    return running


def _age_days(value, now):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (now - moment).total_seconds() / 86400


def reconcile(
    data_root,
    *,
    project_id,
    root=None,
    integration_ref=None,
    stale_days=3.0,
    apply=False,
    actor=None,
    reason=None,
    task_ids=None,
    now=None,
):
    data = Path(data_root or default_data_root())
    if apply and (not isinstance(actor, str) or not _ASSIGNEE_RE.fullmatch(actor)):
        raise ValueError("reconcile --apply requires --actor naming who records the change")
    if not stale_days >= 0:
        raise ValueError("stale-days must be zero or greater")
    now = now or datetime.now(UTC)
    ledger = _load_ledger(data, project_id)
    registration = next((p for p in list_projects(data) if p["project_id"] == project_id), {})
    from .project_context import integration

    selected = integration(data, project_id, root or registration.get("control_root") or ".")
    git_root = selected.get("root") or root or "."
    ref = integration_ref or selected.get("ref") or "refs/heads/main"
    available = bool(
        subprocess.run(
            ["git", "-C", str(git_root), "rev-parse", "--verify", ref + "^{commit}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    suggestions = []
    merged_ids = set()
    if available:
        for card in ledger["tasks"]:
            commit = card.get("commit")
            if card["status"] not in _OPEN or not isinstance(commit, str):
                continue
            if _COMMIT.fullmatch(commit) and _git_ancestor(git_root, commit, ref):
                merged_ids.add(card["task_id"])
                suggestions.append(
                    {
                        "kind": "merged",
                        "task_id": card["task_id"],
                        "status": card["status"],
                        "commit": commit,
                        "summary": f"commit {commit[:12]} is an ancestor of {ref}",
                        "command": shlex.join(
                            [
                                "continuity", "collaboration", "reconcile",
                                "--project-id", project_id, "--apply",
                                "--actor", actor or "<actor>",
                                "--task-id", card["task_id"],
                            ]
                        ),
                        "applies": True,
                    }
                )
    for card in ledger["tasks"]:
        if card["status"] != "ready-for-review" or card["task_id"] in merged_ids:
            continue
        review = _accepted_review(ledger, card)
        if review:
            suggestions.append(
                {
                    "kind": "ready-to-merge",
                    "task_id": card["task_id"],
                    "status": card["status"],
                    "commit": card.get("commit"),
                    "review_task_id": review["task_id"],
                    "summary": f"review {review['task_id']} accepted it; merge, then record it",
                    "command": shlex.join(
                        [
                            "continuity", "collaboration", "task", "update",
                            "--project-id", project_id, "--task-id", card["task_id"],
                            "--actor", actor or "<actor>", "--status", "merged",
                        ]
                    ),
                    "applies": False,
                }
            )
    running = _running_tasks(data, project_id, ledger, now)
    for card in ledger["tasks"]:
        age = _age_days(card["updated_at"], now)
        if card["status"] == "active" and card["task_id"] not in running and age >= stale_days:
            suggestions.append(
                {
                    "kind": "stale",
                    "task_id": card["task_id"],
                    "status": card["status"],
                    "summary": f"active with no running job and no update for {age:.0f} days",
                    "command": shlex.join(
                        [
                            "continuity", "collaboration", "task", "update",
                            "--project-id", project_id, "--task-id", card["task_id"],
                            "--actor", actor or "<actor>", "--status", "blocked",
                            "--blocked-reason",
                            f"stale: no running job and no update for {age:.0f} days",
                        ]
                    ),
                    "applies": False,
                }
            )
    applied = []
    if apply:
        wanted = set(task_ids or [])
        for row in suggestions:
            if not row["applies"] or (wanted and row["task_id"] not in wanted):
                continue
            update_task(
                data,
                project_id=project_id,
                task_id=row["task_id"],
                status="merged",
                landed_commit=row["commit"],
                actor=actor,
            )
            _record_change(
                data,
                project_id,
                "task.reconciled",
                row["task_id"],
                kind=row["kind"],
                actor=actor,
                reason=reason or row["summary"],
                evidence=row["summary"],
                commit=row["commit"],
                integration_ref=ref,
                previous_status=row["status"],
            )
            applied.append(row["task_id"])
    return {
        "schema_version": SCHEMA,
        "project_id": project_id,
        "integration": {"root": str(git_root), "ref": ref, "available": available},
        "stale_days": stale_days,
        "applied": applied,
        "suggestions": suggestions,
        "read_only": not apply,
    }


def brief(value):
    integration = value["integration"]
    lines = [
        f"reconcile project={value['project_id']} integration={integration['ref']}"
        f"{'' if integration['available'] else ' (unavailable)'} "
        f"suggestions={len(value['suggestions'])} applied={len(value['applied'])} "
        f"{'read-only' if value['read_only'] else 'applied'}"
    ]
    for row in value["suggestions"]:
        done = " [applied]" if row["task_id"] in value["applied"] else ""
        lines.append(
            f"{row['kind']} {row['task_id']}{done} — {row['summary']} → {row['command']}"
        )
    return "\n".join(lines)
