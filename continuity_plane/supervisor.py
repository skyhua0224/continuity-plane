"""Provider-neutral local executor supervisor.

The supervisor observes processes and projects current-attempt results onto collaboration
cards. It never edits State SQLite or declares a Work accepted/merged.
"""

from __future__ import annotations

import os
import math
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .collaboration_registry import (
    _load_ledger,
    default_data_root,
    iter_changes,
    latest_change_cursor,
)
from .execution_outbox import change as _record_change, enqueue
from .execution_lifecycle import (
    bind_execution,
    finish_execution,
    record_execution_process,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ValueError):
        return False


def _process_identity(pid):
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        return boot + ":" + start
    except (OSError, IndexError):
        return None


def _observed_workspace(pid, fallback=None):
    """Observe the process checkout without treating it as the card's planned worktree."""

    observed = {
        "observed_cwd": None,
        "observed_head": None,
        "observed_branch": None,
        "observation_error": None,
    }
    try:
        observed["observed_cwd"] = os.readlink(f"/proc/{pid}/cwd")
    except (OSError, ValueError):
        observed["observation_error"] = "process-cwd-unavailable"
    cwd = observed["observed_cwd"]
    if not cwd:
        return observed
    for key, argv in (
        ("observed_head", ["git", "-C", cwd, "rev-parse", "--verify", "HEAD"]),
        (
            "observed_branch",
            ["git", "-C", cwd, "symbolic-ref", "--quiet", "--short", "HEAD"],
        ),
    ):
        try:
            completed = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2,
                check=False,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                observed[key] = completed.stdout.strip()[:256]
        except (OSError, subprocess.SubprocessError):
            continue
    return observed


def _idle_diagnostic(tail, idle_seconds):
    """Classify an idle output tail without claiming it proves the wait cause."""

    text = (tail or "")[-4096:].casefold()
    patterns = (
        (r"waiting (?:for|on).{0,80}(?:build|w2-|shared|lock)", "waiting-for-build-lock"),
        (r"(?:build|shared)[-_ ]lock.{0,80}(?:wait|blocked)", "waiting-for-build-lock"),
        (r"flock|fcntl.{0,40}lock|futex", "possible-lock-wait"),
        (r"waiting for (?:another|other) (?:process|worker)", "waiting-for-peer-process"),
    )
    for pattern, reason in patterns:
        if re.search(pattern, text):
            return {
                "idle_seconds": round(float(idle_seconds), 3),
                "idle_reason": reason,
                "idle_evidence": "bounded-output-tail-heuristic; not proof",
            }
    return {
        "idle_seconds": round(float(idle_seconds), 3),
        "idle_reason": "no-output-observed" if idle_seconds >= 1 else "recent-output",
        "idle_evidence": "process-output-observation; no termination by this diagnostic",
    }


def _kill(process):
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass


class _OutputTail:
    """Drain partial lines on a daemon reader; keep only a bounded byte tail."""

    def __init__(self, stream, on_output=None):
        self.stream = stream
        self.tail = b""
        self.last_output_at = time.monotonic()
        self.lock = threading.Lock()
        self.on_output = on_output
        self.policy_seen = False
        self.policy_suffix = b""
        self.drained = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while chunk := self.stream.read1(4096):
                from .retry_policy import policy_refusal

                with self.lock:
                    scan = self.policy_suffix + chunk
                    self.policy_seen |= policy_refusal(
                        scan.decode("utf-8", errors="replace")
                    )
                    self.policy_suffix = scan[-1024:]
                    self.tail = (self.tail + chunk)[-4096:]
                    self.last_output_at = time.monotonic()
                if self.on_output:
                    try:
                        self.on_output(chunk)
                    except Exception:
                        pass
            self.drained = True
        finally:
            self.stream.close()

    def snapshot(self):
        with self.lock:
            text = self.tail.decode("utf-8", errors="replace")
            text = text.encode("utf-8")[-4096:].decode("utf-8", errors="ignore")
            return self.last_output_at, text


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
    retry_delay_seconds: float = 1,
    max_retry_wait_seconds: float = 300,
    resume_command: list[str] | None = None,
    thread_file: str | None = None,
    restart_on_retry: bool = False,
    model: str | None = None,
    report_file: str | None = None,
    workdir: str | None = None,
    on_output=None,
    rules_digest: str | None = None,
    rules_audience: str = "worker",
    controller: str | None = None,
    event_prefix: str = "attempt",
    command_receipt: list[str] | None = None,
    expected_scope: str | None = None,
    expected_updated_at: str | None = None,
) -> dict[str, Any]:
    if not command:
        raise ValueError("command is required")
    if any(
        not math.isfinite(value) or value <= 0
        for value in (timeout_seconds, idle_timeout_seconds, heartbeat_seconds)
    ):
        raise ValueError("timeouts must be positive")
    if type(retry) is not int or not 0 <= retry <= 10:
        raise ValueError("retry must be an integer between 0 and 10")
    if any(
        not math.isfinite(v) or v < 0
        for v in (retry_delay_seconds, max_retry_wait_seconds)
    ):
        raise ValueError("retry waits must be finite and non-negative")
    if retry and not resume_command and not restart_on_retry:
        raise ValueError(
            "retry requires --resume-argv-json with --thread-file, or explicit --restart-on-retry"
        )
    if resume_command and (
        not thread_file
        or not isinstance(resume_command, list)
        or not all(isinstance(v, str) and v for v in resume_command)
        or not any("{thread_id}" in v for v in resume_command)
    ):
        raise ValueError(
            "resume requires string argv with {thread_id} and --thread-file"
        )
    from .retry_policy import classify, resume_argv

    root = Path(data_root or default_data_root())
    from .git_guard import prepare as prepare_git_guard

    git_guard_dir = prepare_git_guard(root, project_id)
    base_attempt = attempt_id or f"attempt-{uuid.uuid4().hex[:16]}"
    last_result: dict[str, Any] = {}
    retry_wait = 0.0
    current_command = list(command)
    for retry_number in range(retry + 1):
        current_attempt = (
            base_attempt
            if retry_number == 0
            else f"{base_attempt}-retry-{retry_number}"
        )
        try:
            bind_execution(
                root,
                project_id=project_id,
                task_id=task_id,
                assignee=assignee,
                attempt_id=current_attempt,
                expected_previous=last_result.get("attempt_id")
                if retry_number
                else None,
                require_idle=True,
                expected_scope=expected_scope,
                expected_updated_at=expected_updated_at if retry_number == 0 else None,
                model=model,
            )
        except ValueError as exc:
            if retry_number:
                last_result["retry_stopped"] = str(exc)
                break
            raise
        except OSError:
            if expected_scope is not None:
                raise  # A prepared worker must not run an unvalidated task snapshot.
            enqueue(
                root,
                project_id,
                "attempt.bind",
                task_id,
                assignee=assignee,
                attempt_id=current_attempt,
            )
        started = time.monotonic()
        started_wall = time.time()
        host_id = socket.gethostname()
        try:
            from .persistent_storage import workspace

            work_root, _storage = workspace(root)
            process = subprocess.Popen(
                current_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                env={
                    **os.environ,
                    "PATH": os.pathsep.join(
                        [
                            *([str(git_guard_dir)] if git_guard_dir else []),
                            str(Path(sys.executable).parent),
                            os.environ.get("PATH", ""),
                        ]
                    ),
                    "CONTINUITY_ASSIGNEE": assignee,
                    "CONTINUITY_TASK_ID": task_id,
                    "CONTINUITY_PROJECT_ID": project_id,
                    "CONTINUITY_DATA_ROOT": str(root.resolve()),
                    "CONTINUITY_ROLE": rules_audience,
                    "CONTINUITY_RULES_AUDIENCE": rules_audience,
                    **(
                        {
                            "CONTINUITY_WORK_ROOT": str(work_root),
                            "TMPDIR": str(work_root),
                            "TMP": str(work_root),
                            "TEMP": str(work_root),
                        }
                        if work_root
                        else {}
                    ),
                    **({"CONTINUITY_CONTROLLER": controller} if controller else {}),
                },
            )
        except OSError as exc:
            reason = f"spawn-error:{type(exc).__name__}"
            projection = _safe_finish(
                root,
                project_id=project_id,
                task_id=task_id,
                assignee=assignee,
                attempt_id=current_attempt,
                status="failed",
                return_code=1,
                reason=reason,
                host_id=host_id,
                event_prefix=event_prefix,
            )
            return {
                "attempt_id": current_attempt,
                "status": "failed",
                "return_code": 1,
                "reason": reason,
                **projection,
            }
        process_identity = _process_identity(process.pid)
        workspace_observation = _observed_workspace(process.pid, workdir)
        runtime_tracking_error = None
        try:
            record_execution_process(
                root,
                project_id=project_id,
                task_id=task_id,
                attempt_id=current_attempt,
                pid=process.pid,
                host_id=host_id,
                process_identity=process_identity,
                **workspace_observation,
            )
        except (OSError, ValueError) as exc:
            # Process observation is diagnostic metadata. It must not turn a
            # successfully spawned worker into a failed launch.
            runtime_tracking_error = (
                "execution process metadata unavailable: " + str(exc)[:300]
            )
        reader = _OutputTail(process.stdout, on_output)
        status = "failed"
        reason = None
        last_heartbeat = started
        return_code = 1
        try:
            _record_change(
                root,
                project_id,
                event_prefix + ".started",
                task_id,
                attempt_id=current_attempt,
                assignee=assignee,
                command=(
                    command_receipt
                    if retry_number == 0 and command_receipt
                    else current_command
                ),
                model=model,
                rules_digest=rules_digest,
                rules_audience=rules_audience,
                pid=process.pid,
                retry=retry_number,
                process_identity=process_identity,
                started_at=_now(),
                host_id=host_id,
                storage=_storage,
                **workspace_observation,
                **(
                    {"runtime_tracking_error": runtime_tracking_error}
                    if runtime_tracking_error
                    else {}
                ),
            )
            while process.poll() is None:
                now = time.monotonic()
                if now - last_heartbeat >= heartbeat_seconds:
                    sampled = _observed_workspace(process.pid)
                    if sampled.get("observed_cwd"):
                        workspace_observation = sampled
                    output_at, output_tail = reader.snapshot()
                    idle_metrics = _idle_diagnostic(
                        output_tail, max(0.0, now - output_at)
                    )
                    _record_change(
                        root,
                        project_id,
                        event_prefix + ".heartbeat",
                        task_id,
                        attempt_id=current_attempt,
                        assignee=assignee,
                        pid=process.pid,
                        elapsed_seconds=round(now - started, 3),
                        **workspace_observation,
                        **idle_metrics,
                    )
                    last_heartbeat = now
                if now - started >= timeout_seconds:
                    reason = "timeout"
                    status = "interrupted"
                    _kill(process)
                    break
                if now - reader.snapshot()[0] >= idle_timeout_seconds:
                    reason = "no-output-timeout"
                    status = "interrupted"
                    _kill(process)
                    break
                time.sleep(min(0.1, heartbeat_seconds))
            return_code = process.wait()
            if workspace_observation.get("observed_cwd"):
                # The process has exited: refresh HEAD in its last observed
                # checkout without inventing a new process cwd.
                checkout = workspace_observation["observed_cwd"]
                try:
                    final_head = subprocess.run(
                        ["git", "-C", checkout, "rev-parse", "--verify", "HEAD"],
                        capture_output=True, text=True, timeout=2, check=False,
                    )
                    if final_head.returncode == 0:
                        workspace_observation["observed_head"] = final_head.stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    pass
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
            _kill(process)
            return_code = process.wait()
        except OSError:
            status = "interrupted"
            reason = "supervisor-io-error"
            _kill(process)
            return_code = process.wait()
        finally:
            reader.thread.join(timeout=1)
            if status == "completed" and (
                getattr(on_output, "terminal_failure", None)
                or getattr(on_output, "policy_seen", False)
            ):
                status = "failed"
                reason = "provider-reported-failure"
            policy_seen = reader.policy_seen or getattr(on_output, "policy_seen", False)
            diagnostic_text = (
                reader.snapshot()[1]
                + "\n"
                + (getattr(on_output, "terminal_failure", None) or "")
            )
            if policy_seen:
                # Keep the original provider text and add a strong marker. The
                # previous hard-coded replacement discarded a simultaneous
                # usage-limit signal and misclassified resumable quota errors.
                diagnostic_text += "\ncyber_policy"
            failure = classify(diagnostic_text) if status != "completed" else {}
            if failure.get("failure_class") == "policy-review":
                reason = "provider-policy-review"
            elif not reader.drained:
                failure.update(
                    failure_class="output-incomplete",
                    retryable=False,
                    safety_review_required=True,
                    guidance="Output was not completely drained; inspect the original attempt before any replay",
                )
            attached_report = None
            if report_file:
                try:
                    from .delivery_records import read_report

                    file = Path(report_file)
                    if file.stat().st_mtime < started_wall:
                        raise ValueError("report predates this attempt")
                    attached_report = read_report(file)
                except (OSError, ValueError, UnicodeError) as exc:
                    failure["report_error"] = str(exc)[:256]
            projection = _safe_finish(
                root,
                project_id=project_id,
                task_id=task_id,
                attempt_id=current_attempt,
                assignee=assignee,
                pid=process.pid,
                host_id=host_id,
                status=status,
                return_code=return_code,
                reason=reason,
                output_tail=reader.snapshot()[1],
                elapsed_seconds=round(time.monotonic() - started, 3),
                model=model,
                report=attached_report,
                event_prefix=event_prefix,
                **workspace_observation,
                **(
                    {"runtime_tracking_error": runtime_tracking_error}
                    if runtime_tracking_error
                    else {}
                ),
                **failure,
            )
        last_result = {
            "attempt_id": current_attempt,
            "status": status,
            "return_code": return_code,
            "reason": reason,
            "storage": _storage,
            **workspace_observation,
            **(
                {"runtime_tracking_error": runtime_tracking_error}
                if runtime_tracking_error
                else {}
            ),
            **projection,
            **failure,
        }
        if status == "failed" and failure.get("failure_class") in {
            "rate-limit",
            "quota-exhausted",
            "capacity",
            "gateway",
            "upstream-reset",
        }:
            if retry_number >= retry or not failure.get("retryable"):
                last_result["retry_exhausted"] = retry_number >= retry
                last_result["next_actions"] = [
                    {
                        "kind": "explicit-new-session",
                        "command": [
                            "continuity",
                            "dispatch",
                            "--project-id",
                            project_id,
                            "--task-id",
                            task_id,
                            "--assignee",
                            assignee,
                            *(["--model", model] if model else []),
                        ],
                        "note": "Review current rules, card and existing work first; do not repeat completed steps. A different model needs an explicit authorized reassignment/handoff.",
                    }
                ]
                if failure.get("failure_class") in {"rate-limit", "quota-exhausted"}:
                    last_result["next_actions"] = [
                        {
                            "kind": "wait-for-availability",
                            "available_at": failure.get("available_at"),
                            "command": [
                                "continuity",
                                "quota",
                                "report",
                                "--project-id",
                                project_id,
                            ],
                            "note": "Honor provider retry-after/cooldown; a new thread does not reset quota. Consider another model only through an explicitly authorized handoff.",
                        }
                    ]
        if status != "failed" or retry_number >= retry or not failure.get("retryable"):
            break
        delay = failure.get("retry_after_seconds")
        if delay is None:
            delay = min(30, retry_delay_seconds * (2**retry_number))
        if retry_wait + delay > max_retry_wait_seconds:
            last_result["retry_stopped"] = "retry wait budget exceeded"
            break
        try:
            current_command = (
                resume_argv(resume_command, thread_file)
                if resume_command
                else list(command)
            )
        except (OSError, ValueError, UnicodeError) as exc:
            last_result["retry_stopped"] = f"resume unavailable: {exc}"
            break
        _record_change(
            root,
            project_id,
            "attempt.retry-scheduled",
            task_id,
            attempt_id=current_attempt,
            wait_seconds=delay,
            mode="resume" if resume_command else "restart",
            model=model,
        )
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            last_result["retry_stopped"] = "retry cancelled"
            break
        retry_wait += delay
    return last_result


def _safe_finish(data_root, **fields):
    try:
        return finish_execution(data_root, **fields)
    except OSError:
        project = fields.pop("project_id")
        task = fields.pop("task_id")
        fields.setdefault("finished_at", _now())
        queued = enqueue(data_root, project, "attempt.finished", task, **fields)
        return {
            "card_projection": "pending-outbox" if queued else "metadata-unavailable",
            "card_status": None,
        }


def daemon_once(data_root: Path | str | None, *, project_id: str) -> dict[str, Any]:
    """Reconcile started attempts whose process disappeared without a finish event."""
    root = Path(data_root or default_data_root())
    ledger = _load_ledger(root, project_id)
    bindings = {
        task_id: binding
        for task_id, binding in ledger.get("execution_bindings", {}).items()
        if binding.get("status") == "running"
    }
    # Ordinary dispatches with no in-flight binding must not replay the entire
    # append-only journal. This was the dominant cost on large projects.
    if not bindings:
        return {
            "project_id": project_id,
            "reconciled": [],
            "next_cursor": latest_change_cursor(root, project_id=project_id),
        }
    reconciled = []
    local_host = socket.gethostname()
    now = datetime.now(UTC)
    high_water = latest_change_cursor(root, project_id=project_id)
    for task_id, binding in bindings.items():
        attempt_id = binding.get("attempt_id")
        start_cursor = binding.get("event_start_cursor")
        since = (
            start_cursor
            if type(start_cursor) is int
            else max(0, high_water - 5000)
        )
        started = None
        finished = None
        for row in iter_changes(root, project_id=project_id, since=since):
            if (
                row.get("task_id") != task_id
                or row.get("attempt_id") != attempt_id
                or row.get("observed_native")
            ):
                continue
            if row.get("event") in {"attempt.started", "run.started"}:
                started = row
            elif row.get("event") in {"attempt.finished", "run.finished"}:
                finished = row
        if finished is not None:
            # Finish facts precede the card write, so replay repairs that crash
            # gap with the exact original outcome and report.
            finish_execution(
                root,
                project_id=project_id,
                task_id=task_id,
                assignee=finished.get("assignee"),
                attempt_id=attempt_id,
                status=finished["status"],
                return_code=finished.get("return_code"),
                reason=finished.get("reason"),
                finished_at=finished.get("finished_at"),
                host_id=finished.get("host_id"),
                report=finished.get("report"),
                replay=True,
                event_prefix=finished["event"].split(".")[0],
            )
            reconciled.append(attempt_id)
            continue
        evidence = {**binding, **(started or {})}
        host_id = evidence.get("host_id")
        pid = evidence.get("pid")
        if host_id and host_id != local_host:
            continue
        alive = _alive(pid) if type(pid) is int else False
        current_identity = _process_identity(pid) if alive else None
        reused = bool(
            alive
            and evidence.get("process_identity")
            and current_identity
            and evidence["process_identity"] != current_identity
        )
        if alive and not reused:
            continue
        if started is None and type(pid) is not int:
            bound_at = evidence.get("bound_at")
            try:
                age = (
                    now - datetime.fromisoformat(str(bound_at).replace("Z", "+00:00"))
                ).total_seconds()
            except (TypeError, ValueError):
                age = 0
            # A concurrent launcher may be between bind and start-event write.
            if age < 15:
                continue
            # Missing process evidence cannot prove death. Host restart
            # reconciliation uses boot-bound reservations separately.
            continue
        finish_execution(
            root,
            project_id=project_id,
            task_id=task_id,
            attempt_id=attempt_id,
            assignee=binding.get("assignee"),
            pid=pid,
            host_id=host_id or local_host,
            status="interrupted",
            return_code=None,
            reason="process-identity-changed" if reused else "process-not-alive",
            event_prefix=(started or {}).get("event", "attempt.started").split(".")[0],
        )
        reconciled.append(attempt_id)
    return {
        "project_id": project_id,
        "reconciled": reconciled,
        "next_cursor": latest_change_cursor(root, project_id=project_id),
    }
