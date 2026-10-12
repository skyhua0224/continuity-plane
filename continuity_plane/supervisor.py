"""Provider-neutral local executor supervisor.

The supervisor owns process observation only. Continuity collaboration events remain
the durable record; this module never edits State SQLite or declares a Work complete.
"""

from __future__ import annotations

import os
import select
import subprocess
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .collaboration_registry import _record_change, list_changes, default_data_root


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def run_attempt(
    data_root: Path | str | None,
    *,
    project_id: str,
    task_id: str,
    assignee: str,
    command: list[str],
    attempt_id: str | None = None,
    timeout_seconds: float = 3600,
    idle_timeout_seconds: float = 600,
    heartbeat_seconds: float = 10,
    retry: int = 0,
) -> dict[str, Any]:
    if not command:
        raise ValueError("command is required")
    if timeout_seconds <= 0 or idle_timeout_seconds <= 0 or heartbeat_seconds <= 0:
        raise ValueError("timeouts must be positive")
    root = Path(data_root or default_data_root())
    base_attempt = attempt_id or f"attempt-{uuid.uuid4().hex[:16]}"
    last_result: dict[str, Any] = {}
    for retry_number in range(retry + 1):
        current_attempt = base_attempt if retry_number == 0 else f"{base_attempt}-retry-{retry_number}"
        started = time.monotonic()
        last_output = ""
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=(os.name != "nt"),
        )
        _record_change(root, project_id, "attempt.started", task_id, attempt_id=current_attempt,
                       assignee=assignee, command=command, pid=process.pid, retry=retry_number,
                       started_at=_now())
        status = "failed"
        reason = None
        last_heartbeat = started
        last_output_at = started
        try:
            while process.poll() is None:
                now = time.monotonic()
                if now - last_heartbeat >= heartbeat_seconds:
                    _record_change(root, project_id, "attempt.heartbeat", task_id,
                                   attempt_id=current_attempt, assignee=assignee, pid=process.pid,
                                   elapsed_seconds=round(now - started, 3))
                    last_heartbeat = now
                if now - started >= timeout_seconds:
                    reason = "timeout"
                    status = "interrupted"
                    process.kill()
                    break
                if now - last_output_at >= idle_timeout_seconds:
                    reason = "no-output-timeout"
                    status = "interrupted"
                    process.kill()
                    break
                if process.stdout is not None:
                    readable, _, _ = select.select([process.stdout], [], [], 0)
                    if readable:
                        line = process.stdout.readline()
                        if line:
                            last_output += line[-4096:]
                            last_output_at = now
                time.sleep(min(0.1, heartbeat_seconds))
            return_code = process.wait()
            if status == "failed" and return_code == 0:
                status = "completed"
            elif status == "failed" and return_code < 0:
                status = "interrupted"
                reason = reason or f"signal:{-return_code}"
            elif status == "failed":
                reason = reason or f"exit:{return_code}"
        except KeyboardInterrupt:
            status = "interrupted"
            reason = "supervisor-interrupted"
            process.kill()
            return_code = process.wait()
        finally:
            if process.stdout is not None:
                process.stdout.close()
            _record_change(root, project_id, "attempt.finished", task_id,
                           attempt_id=current_attempt, assignee=assignee, pid=process.pid,
                           status=status, return_code=return_code, reason=reason,
                           output_tail=last_output[-4096:], finished_at=_now())
        last_result = {"attempt_id": current_attempt, "status": status, "return_code": return_code, "reason": reason}
        if status == "completed" or retry_number >= retry:
            break
    return last_result


def daemon_once(data_root: Path | str | None, *, project_id: str) -> dict[str, Any]:
    """Reconcile started attempts whose process disappeared without a finish event."""
    root = Path(data_root or default_data_root())
    events = list_changes(root, project_id=project_id, since=0, max_results=500)
    starts = {row.get("attempt_id"): row for row in events["changes"] if row.get("event") == "attempt.started"}
    finished = {row.get("attempt_id") for row in events["changes"] if row.get("event") == "attempt.finished"}
    reconciled = []
    for attempt_id, row in starts.items():
        if attempt_id in finished or not isinstance(row.get("pid"), int) or _alive(row["pid"]):
            continue
        _record_change(root, project_id, "attempt.finished", row.get("task_id"), attempt_id=attempt_id,
                       assignee=row.get("assignee"), pid=row["pid"], status="interrupted",
                       return_code=None, reason="process-not-alive", finished_at=_now())
        reconciled.append(attempt_id)
    return {"project_id": project_id, "reconciled": reconciled, "next_cursor": events["next_cursor"]}
