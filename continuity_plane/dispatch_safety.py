"""Incremental, derived index for dispatch policy refusals.

The change log remains authoritative. This sidecar only remembers the small
number of policy/failure events needed before every worker launch, together
with the byte offset already consumed. Corruption or rewrite causes a full
rebuild rather than weakening the refusal gate.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .collaboration_registry import (
    _atomic_write,
    _changes_path,
    _locked,
    default_data_root,
    project_data_dir,
)

SCHEMA = "context.dispatch-safety-index/v1"
POLICY_EVENTS = {
    "dispatch.policy-stopped",
    "dispatch.review-required",
    "dispatch.classification-corrected",
}


def index_path(data_root, project_id: str) -> Path:
    root = Path(data_root or default_data_root())
    return project_data_dir(root, project_id) / "dispatch-safety-index.json"


def _empty(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "project_id": project_id,
        "journal": {
            "offset": 0,
            "device": None,
            "inode": None,
            "head_sha256": None,
        },
        "entries": [],
        "legacy_worker_results_bootstrapped": False,
    }


def _valid(document: dict[str, Any], project_id: str) -> bool:
    if not isinstance(document, dict):
        return False
    journal = document.get("journal")
    entries = document.get("entries")
    return (
        document.get("schema_version") == SCHEMA
        and document.get("project_id") == project_id
        and isinstance(journal, dict)
        and type(journal.get("offset")) is int
        and journal["offset"] >= 0
        and isinstance(entries, list)
        and all(isinstance(item, dict) for item in entries)
        and type(document.get("legacy_worker_results_bootstrapped")) is bool
    )


def _head_hash(stream) -> str:
    stream.seek(0)
    return hashlib.sha256(stream.read(4096)).hexdigest()


def _entry(row: dict[str, Any]) -> dict[str, Any] | None:
    event = row.get("event")
    task_id = row.get("task_id")
    scope = row.get("scope_sha256")
    if event not in POLICY_EVENTS or not isinstance(task_id, str):
        return None
    if not isinstance(scope, str) or not scope:
        return None
    return {
        "event": event,
        "task_id": task_id,
        "scope_sha256": scope,
        "job_id": row.get("job_id") or row.get("attempt_id"),
        "cursor": row.get("cursor"),
        "observed_at": row.get("observed_at"),
    }


def _scan_journal(path: Path, start: int, entries: list[dict[str, Any]]) -> int:
    if not path.exists():
        return 0
    offset = start
    with path.open("rb") as stream:
        stream.seek(start)
        while True:
            line = stream.readline()
            if not line:
                break
            if not line.endswith(b"\n"):
                break
            offset += len(line)
            try:
                row = json.loads(line.decode("utf-8"))
            except (UnicodeError, ValueError):
                continue
            if isinstance(row, dict):
                entry = _entry(row)
                if entry is not None:
                    entries.append(entry)
    return offset


def _bootstrap_worker_results(data_root, project_id: str, entries: list[dict[str, Any]]) -> None:
    jobs = project_data_dir(Path(data_root or default_data_root()), project_id) / "worker-jobs"
    if not jobs.is_dir():
        return
    for result_path in jobs.glob("*/result.json"):
        job_path = result_path.with_name("job.json")
        try:
            job = json.loads(job_path.read_text(encoding="utf-8"))
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        failure = result.get("failure_class")
        if failure not in {"policy-review", "output-incomplete"}:
            continue
        task_id = job.get("task_id")
        scope = job.get("scope_sha256")
        if not isinstance(task_id, str) or not isinstance(scope, str) or not scope:
            continue
        entries.append(
            {
                "event": (
                    "dispatch.policy-stopped"
                    if failure == "policy-review"
                    else "dispatch.review-required"
                ),
                "task_id": task_id,
                "scope_sha256": scope,
                "job_id": job.get("job_id"),
                "cursor": None,
                "observed_at": result.get("finished_at"),
                "source": "legacy-worker-result",
            }
        )


def refresh(data_root, project_id: str) -> dict[str, Any]:
    """Return the current index, consuming only appended journal bytes.

    First use on an existing project performs one bounded, one-pass backfill.
    Later dispatches read a small derived file and, normally, zero journal bytes.
    """

    path = index_path(data_root, project_id)
    changes = _changes_path(Path(data_root or default_data_root()), project_id)
    with _locked(changes):
        needs_write = not path.exists()
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            document = _empty(project_id)
            needs_write = True
        if not _valid(document, project_id):
            document = _empty(project_id)
            needs_write = True

        if not changes.exists():
            document = _empty(project_id)
            needs_write = True
        if changes.exists():
            stat = changes.stat()
            with changes.open("rb") as stream:
                current_head = _head_hash(stream)
            prior = document["journal"]
            rewrite = (
                prior.get("offset", 0) > stat.st_size
                or (
                    prior.get("inode") is not None
                    and prior.get("inode") != stat.st_ino
                )
                or (
                    prior.get("device") is not None
                    and prior.get("device") != stat.st_dev
                )
                or (
                    prior.get("head_sha256") is not None
                    and prior.get("head_sha256") != current_head
                )
            )
            if rewrite:
                document = _empty(project_id)
                needs_write = True
        else:
            stat = None
            current_head = None

        start = document["journal"].get("offset", 0)
        if document["journal"].get("head_sha256") is None:
            document["journal"]["head_sha256"] = current_head
        before_entries = len(document["entries"])
        offset = _scan_journal(changes, start, document["entries"])
        needs_write |= offset != start or len(document["entries"]) != before_entries
        if stat is not None:
            document["journal"].update(
                offset=offset,
                device=stat.st_dev,
                inode=stat.st_ino,
                head_sha256=current_head,
            )
        if not document["legacy_worker_results_bootstrapped"]:
            _bootstrap_worker_results(data_root, project_id, document["entries"])
            document["legacy_worker_results_bootstrapped"] = True
            needs_write = True
        if needs_write:
            _atomic_write(path, document)
        return document


def blocked(data_root, project_id: str, task_id: str, scope: str) -> tuple[str, Any] | None:
    """Return the first uncorrected refusal for an exact task scope."""

    document = refresh(data_root, project_id)
    corrected = {
        (entry["task_id"], entry.get("job_id"), entry["scope_sha256"])
        for entry in document["entries"]
        if entry.get("event") == "dispatch.classification-corrected"
    }
    for entry in document["entries"]:
        if entry.get("task_id") != task_id or entry.get("scope_sha256") != scope:
            continue
        if entry.get("event") == "dispatch.classification-corrected":
            continue
        key = (task_id, entry.get("job_id"), scope)
        if key not in corrected:
            return entry["event"], entry.get("job_id")
    return None


def stats(data_root, project_id: str) -> dict[str, Any]:
    document = refresh(data_root, project_id)
    return {
        "schema_version": document["schema_version"],
        "journal_offset": document["journal"]["offset"],
        "indexed_policy_events": len(document["entries"]),
        "legacy_worker_results_bootstrapped": document[
            "legacy_worker_results_bootstrapped"
        ],
    }


__all__ = ["blocked", "index_path", "refresh", "stats"]
