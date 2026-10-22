"""Durable, single-instance dispatch queue with observed availability gates.

The queue is local metadata. It launches the existing worker supervisor only
after the normal controller reservation, capacity, card and quota checks pass.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import math
import os
from pathlib import Path
import re
import socket
import time
import uuid

from .collaboration_registry import (
    _atomic_write,
    _locked,
    _record_change,
    default_data_root,
    list_tasks,
    project_data_dir,
    resolve_project,
)

SCHEMA = "context.dispatch-queue/v1"
MAX_REQUESTS = 256
MAX_BYTES = 4 * 1024 * 1024
ACTIVE = {"launching", "running"}
WAITING_ERRORS = (
    "dispatch capacity is full",
    "origin/model dispatch paused",
    "controller operation is read-only",
    "no configured and available adapter",
    "this task already has a running worker",
    "dispatch reservation",
    "task dependencies are not complete",
)
TRANSIENT_FAILURES = {
    "quota-exhausted",
    "rate-limit",
    "capacity",
    "gateway",
    "upstream-reset",
}


def _now():
    return datetime.now(UTC).isoformat()


def _data_root(value):
    return Path(value or default_data_root()).expanduser().resolve()


def _registration(root, data_root, project_id):
    registration = resolve_project(root, data_root)
    selected = project_id or (registration or {}).get("project_id")
    if not selected:
        raise ValueError("registered project root or project-id is required")
    if registration is None:
        from .collaboration_registry import list_projects

        registration = next(
            (row for row in list_projects(data_root) if row["project_id"] == selected),
            None,
        )
    if registration is None:
        raise ValueError("project is not registered")
    return registration, selected


def _queue_path(data_root, project_id):
    return project_data_dir(_data_root(data_root), project_id) / "dispatch-queue.json"


def _control_path(data_root):
    return _data_root(data_root) / "dispatch-host-control.json"


def _read(path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, ValueError):
        return default


def _load(data_root, project_id):
    path = _queue_path(data_root, project_id)
    if path.exists():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError(
                "dispatch queue metadata is invalid; original preserved"
            ) from None
    else:
        value = {"schema_version": SCHEMA, "items": [], "updated_at": _now()}
    if value.get("schema_version") != SCHEMA or not isinstance(
        value.get("items"), list
    ):
        raise ValueError("dispatch queue metadata is invalid; original preserved")
    return value


def _save(data_root, project_id, value):
    value["updated_at"] = _now()
    if len(value["items"]) > MAX_REQUESTS:
        raise ValueError(
            f"dispatch queue capacity exceeded ({MAX_REQUESTS}); no request was dropped"
        )
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_BYTES:
        raise ValueError(
            f"dispatch queue exceeds {MAX_BYTES} bytes; reconcile or cancel old requests first"
        )
    _atomic_write(_queue_path(data_root, project_id), value)


def _event(data_root, project_id, event, task_id, **fields):
    try:
        _record_change(
            data_root,
            project_id,
            event,
            task_id,
            **fields,
        )
        return True
    except (OSError, ValueError, KeyError):
        return False


def host_control(data_root):
    value = _read(
        _control_path(data_root),
        {
            "paused": False,
            "reason": None,
            "host_id": socket.gethostname(),
            "updated_at": None,
        },
    )
    if value.get("host_id") not in {None, socket.gethostname()}:
        raise ValueError("host pause metadata belongs to another host; not reused")
    return value


def pause_host(data_root, reason):
    if not isinstance(reason, str) or not reason.strip() or len(reason.encode()) > 2048:
        raise ValueError("host pause requires a bounded reason")
    path = _control_path(data_root)
    value = {
        "paused": True,
        "reason": reason.strip(),
        "host_id": socket.gethostname(),
        "updated_at": _now(),
    }
    with _locked(path):
        _atomic_write(path, value)
    return value


def resume_host(data_root):
    path = _control_path(data_root)
    previous = host_control(data_root)
    value = {
        "paused": False,
        "reason": previous.get("reason"),
        "host_id": socket.gethostname(),
        "updated_at": _now(),
        "resumed_at": _now(),
    }
    with _locked(path):
        _atomic_write(path, value)
    return value


def _validated_request(
    *,
    task_id,
    model,
    effort,
    assignee,
    continuation,
    retry,
    timeout_seconds,
    idle_timeout_seconds,
    fallback_models,
    resume,
    resume_job,
    audience,
    priority,
    request_id,
    controller,
    reassign,
    reassign_actor,
):
    for field, value in (
        ("task_id", task_id),
        ("model", model),
        ("audience", audience),
    ):
        if field == "model" and resume and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}", value
        ):
            raise ValueError(f"{field} is invalid")
    for field, value in (
        ("effort", effort),
        ("assignee", assignee),
        ("request_id", request_id),
        ("controller", controller),
        ("reassign_actor", reassign_actor),
    ):
        if value is not None and (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/ -]{0,255}", value)
        ):
            raise ValueError(f"{field} is invalid")
    if not isinstance(continuation, str) or len(continuation.encode()) > 32768:
        raise ValueError("continuation is invalid")
    if type(retry) is not int or not 0 <= retry <= 10:
        raise ValueError("retry must be 0..10")
    if type(priority) is not int or not -1000 <= priority <= 1000:
        raise ValueError("priority must be an integer between -1000 and 1000")
    for field, value in (
        ("timeout_seconds", timeout_seconds),
        ("idle_timeout_seconds", idle_timeout_seconds),
    ):
        if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
            raise ValueError(f"{field} must be finite and positive")
    if not isinstance(fallback_models, (list, tuple)) or len(fallback_models) > 5:
        raise ValueError("fallback_models must be a short list")
    if any(not isinstance(item, str) for item in fallback_models):
        raise ValueError("fallback_models must contain adapter names")
    if resume_job and (not resume or not re.fullmatch(r"job-[0-9a-f]{16}", resume_job)):
        raise ValueError("resume_job requires resume and a recorded job ID")
    return {
        "request_id": request_id or "request-" + uuid.uuid4().hex[:16],
        "task_id": task_id,
        "model": model,
        "effort": effort,
        "assignee": assignee,
        "continuation": continuation,
        "retry": retry,
        "timeout_seconds": timeout_seconds,
        "idle_timeout_seconds": idle_timeout_seconds,
        "fallback_models": list(fallback_models),
        "resume": bool(resume),
        "resume_job": resume_job,
        "audience": audience,
        "priority": priority,
        "controller": controller,
        "reassign": bool(reassign),
        "reassign_actor": reassign_actor,
    }


def enqueue(
    root,
    *,
    data_root=None,
    project_id=None,
    task_id,
    model,
    effort=None,
    assignee=None,
    continuation="Continue the assigned task; preserve completed progress.",
    retry=0,
    timeout_seconds=None,
    idle_timeout_seconds=None,
    fallback_models=(),
    resume=False,
    resume_job=None,
    audience="worker",
    priority=100,
    request_id=None,
    controller=None,
    reassign=False,
    reassign_actor=None,
):
    data = _data_root(data_root)
    registration, project_id = _registration(root, data, project_id)
    if not any(
        task["task_id"] == task_id for task in list_tasks(data, project_id=project_id)
    ):
        raise ValueError("task_id does not exist")
    from .worker_dispatch import (
        DEFAULT_IDLE_TIMEOUT_SECONDS,
        DEFAULT_TIMEOUT_SECONDS,
    )

    request = _validated_request(
        task_id=task_id,
        model=model,
        effort=effort,
        assignee=assignee,
        continuation=continuation,
        retry=retry,
        timeout_seconds=(
            DEFAULT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        ),
        idle_timeout_seconds=(
            DEFAULT_IDLE_TIMEOUT_SECONDS
            if idle_timeout_seconds is None
            else idle_timeout_seconds
        ),
        fallback_models=fallback_models,
        resume=resume,
        resume_job=resume_job,
        audience=audience,
        priority=priority,
        request_id=request_id,
        controller=controller,
        reassign=reassign,
        reassign_actor=reassign_actor,
    )
    path = _queue_path(data, project_id)
    with _locked(path):
        document = _load(data, project_id)
        existing = next(
            (
                row
                for row in document["items"]
                if row["request_id"] == request["request_id"]
            ),
            None,
        )
        if existing:
            comparable = {
                key: existing.get(key) for key in request if key != "request_id"
            }
            expected = {
                key: value for key, value in request.items() if key != "request_id"
            }
            if comparable != expected:
                raise ValueError(
                    "request_id already has different queue content; original preserved"
                )
            return {**existing, "duplicate_suppressed": True}
        duplicate_task = next(
            (
                row
                for row in document["items"]
                if row["task_id"] == task_id
                and row["status"] in {"queued", "launching", "running"}
            ),
            None,
        )
        if duplicate_task:
            return {
                **duplicate_task,
                "duplicate_suppressed": True,
                "reason": "task already has a queued or running dispatch request",
            }
        if len(document["items"]) >= MAX_REQUESTS:
            raise ValueError(
                f"dispatch queue capacity reached ({MAX_REQUESTS}); archive or cancel old requests first"
            )
        item = {
            **request,
            "status": "queued",
            "order": max((row.get("order", 0) for row in document["items"]), default=0)
            + 1,
            "attempts": 0,
            "job_id": None,
            "queued_at": _now(),
            "updated_at": _now(),
            "last_gate": None,
            "last_error": None,
            "retry_not_before": None,
            "control_root": registration["control_root"],
        }
        document["items"].append(item)
        _save(data, project_id, document)
        _event(
            data,
            project_id,
            "dispatch.queue-enqueued",
            task_id,
            request_id=item["request_id"],
            model=model,
            priority=priority,
            resume=bool(resume),
        )
    return item


@contextmanager
def _runner_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                locked = True
            except OSError:
                pass
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                pass
        if locked:
            handle.seek(0)
            handle.truncate()
            json.dump(
                {
                    "state": "running",
                    "pid": os.getpid(),
                    "host_id": socket.gethostname(),
                    "started_at": _now(),
                },
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())
        yield locked
        if locked:
            handle.seek(0)
            handle.truncate()
            json.dump(
                {
                    "state": "released",
                    "pid": os.getpid(),
                    "host_id": socket.gethostname(),
                    "released_at": _now(),
                },
                handle,
            )
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if locked and os.name != "nt":
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif locked:
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _job_result(data, project_id, job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"job-[0-9a-f]{16}", job_id):
        return {}
    return _read(
        project_data_dir(data, project_id) / "worker-jobs" / job_id / "result.json",
        {},
    )


def _reconcile(data, project_id, document):
    changed = False
    for item in document["items"]:
        if item["status"] != "running" or not item.get("job_id"):
            continue
        result = _job_result(data, project_id, item["job_id"])
        if result.get("status") == "completed":
            item.update(
                status="completed", finished_at=_now(), result_status="completed"
            )
            changed = True
        elif result.get("status") in {"failed", "interrupted"}:
            failure_class = result.get("failure_class")
            if failure_class in TRANSIENT_FAILURES:
                attempts = max(1, item.get("attempts", 1))
                retry_not_before = (
                    result.get("available_at")
                    or (
                        datetime.now(UTC)
                        + timedelta(seconds=min(300, 15 * (2 ** min(attempts, 4))))
                    ).isoformat()
                )
                item.update(
                    status="queued",
                    job_id=None,
                    result_status=result["status"],
                    failure_class=failure_class,
                    last_gate=failure_class,
                    last_error=result.get("reason") or failure_class,
                    retry_not_before=retry_not_before,
                    updated_at=_now(),
                )
                _event(
                    data,
                    project_id,
                    "dispatch.queue-retry-scheduled",
                    item["task_id"],
                    request_id=item["request_id"],
                    failure_class=failure_class,
                    retry_not_before=retry_not_before,
                )
            else:
                item.update(
                    status="failed",
                    finished_at=_now(),
                    result_status=result["status"],
                    failure_class=failure_class,
                    last_error=result.get("reason") or failure_class,
                )
            changed = True
    if changed:
        _save(data, project_id, document)


def _dispatch(**kwargs):
    from .worker_dispatch import dispatch

    return dispatch(**kwargs)


def _run_pass(
    root, data, project_id, *, controller, controller_token, max_launches=None
):
    control = host_control(data)
    path = _queue_path(data, project_id)
    with _locked(path):
        document = _load(data, project_id)
        _reconcile(data, project_id, document)
    if control.get("paused"):
        return {
            "status": "paused",
            "reason": control.get("reason"),
            "launched": [],
            "waiting": [],
            "blocked": [],
        }
    launched, waiting, blocked = [], [], []
    now = datetime.now(UTC)
    queued = sorted(
        (
            row
            for row in document["items"]
            if row["status"] == "queued"
            and (
                not row.get("retry_not_before")
                or datetime.fromisoformat(
                    row["retry_not_before"].replace("Z", "+00:00")
                )
                <= now
            )
        ),
        key=lambda row: (row.get("priority", 100), row.get("order", 0)),
    )
    for item in queued:
        if max_launches is not None and len(launched) >= max_launches:
            break
        if host_control(data).get("paused"):
            break
        with _locked(path):
            current = _load(data, project_id)
            target = next(
                (
                    row
                    for row in current["items"]
                    if row["request_id"] == item["request_id"]
                ),
                None,
            )
            if target is None or target["status"] != "queued":
                continue
            target.update(status="launching", updated_at=_now())
            _save(data, project_id, current)
        try:
            result = _dispatch(
                item["control_root"],
                data_root=data,
                project_id=project_id,
                task_id=item["task_id"],
                model=item["model"],
                effort=item.get("effort"),
                assignee=item.get("assignee"),
                continuation=item["continuation"],
                retry=item["retry"],
                controller=item.get("controller") or controller,
                controller_token=controller_token,
                audience=item["audience"],
                fallback_models=tuple(item.get("fallback_models") or ()),
                resume=bool(item.get("resume")),
                resume_job=item.get("resume_job"),
                timeout_seconds=item["timeout_seconds"],
                idle_timeout_seconds=item["idle_timeout_seconds"],
                background=True,
                reassign=bool(item.get("reassign")),
                reassign_actor=item.get("reassign_actor"),
            )
        except ValueError as exc:
            message = str(exc)
            waiting_gate = next(
                (gate for gate in WAITING_ERRORS if gate in message),
                None,
            )
            with _locked(path):
                current = _load(data, project_id)
                target = next(
                    (
                        row
                        for row in current["items"]
                        if row["request_id"] == item["request_id"]
                    ),
                    None,
                )
                if target:
                    changed_gate = (
                        target.get("status")
                        != ("queued" if waiting_gate else "blocked")
                        or target.get("last_gate") != waiting_gate
                    )
                    target.update(
                        status="queued" if waiting_gate else "blocked",
                        updated_at=_now(),
                        attempts=target.get("attempts", 0) + 1,
                        last_gate=waiting_gate,
                        last_error=message[:2048],
                        retry_not_before=(
                            datetime.now(UTC) + timedelta(seconds=15)
                        ).isoformat()
                        if waiting_gate
                        else None,
                    )
                    _save(data, project_id, current)
                    if changed_gate:
                        _event(
                            data,
                            project_id,
                            (
                                "dispatch.queue-waiting"
                                if waiting_gate
                                else "dispatch.queue-blocked"
                            ),
                            item["task_id"],
                            request_id=item["request_id"],
                            gate=waiting_gate,
                            reason=message[:500],
                        )
                    (waiting if waiting_gate else blocked).append(
                        {
                            "request_id": item["request_id"],
                            "task_id": item["task_id"],
                            "gate": waiting_gate,
                            "reason": message[:500],
                        }
                    )
            continue
        except Exception as exc:
            with _locked(path):
                current = _load(data, project_id)
                target = next(
                    (
                        row
                        for row in current["items"]
                        if row["request_id"] == item["request_id"]
                    ),
                    None,
                )
                if target:
                    changed = target.get("status") != "blocked"
                    target.update(
                        status="blocked",
                        updated_at=_now(),
                        attempts=target.get("attempts", 0) + 1,
                        last_error=(type(exc).__name__ + ": " + str(exc))[:2048],
                    )
                    _save(data, project_id, current)
                    if changed:
                        _event(
                            data,
                            project_id,
                            "dispatch.queue-blocked",
                            item["task_id"],
                            request_id=item["request_id"],
                            reason=type(exc).__name__ + ": " + str(exc)[:500],
                        )
            blocked.append(
                {
                    "request_id": item["request_id"],
                    "task_id": item["task_id"],
                    "reason": type(exc).__name__ + ": " + str(exc)[:500],
                }
            )
            continue
        with _locked(path):
            current = _load(data, project_id)
            target = next(
                (
                    row
                    for row in current["items"]
                    if row["request_id"] == item["request_id"]
                ),
                None,
            )
            if target:
                target.update(
                    status="running",
                    job_id=result["job_id"],
                    attempts=target.get("attempts", 0) + 1,
                    launched_at=_now(),
                    updated_at=_now(),
                    last_gate=None,
                    last_error=None,
                    retry_not_before=None,
                )
                _save(data, project_id, current)
                _event(
                    data,
                    project_id,
                    "dispatch.queue-launched",
                    item["task_id"],
                    request_id=item["request_id"],
                    job_id=result["job_id"],
                    model=item.get("model"),
                )
        launched.append(
            {
                "request_id": item["request_id"],
                "task_id": item["task_id"],
                "job_id": result["job_id"],
            }
        )
    return {
        "status": "completed",
        "launched": launched,
        "waiting": waiting,
        "blocked": blocked,
    }


def run(
    root,
    *,
    data_root=None,
    project_id=None,
    controller=None,
    controller_token=None,
    watch=False,
    interval_seconds=5,
    max_launches=None,
):
    data = _data_root(data_root)
    _, project_id = _registration(root, data, project_id)
    if (
        type(interval_seconds) not in {int, float}
        or not math.isfinite(interval_seconds)
        or interval_seconds < 1
    ):
        raise ValueError("interval-seconds must be at least one second")
    lock_path = project_data_dir(data, project_id) / "dispatch-queue-runner.lock"
    with _runner_lock(lock_path) as locked:
        if not locked:
            return {
                "status": "already-running",
                "message": "another dispatch queue runner holds the single-instance host/project lock",
            }
        passes = 0
        results = []
        while True:
            result = _run_pass(
                root,
                data,
                project_id,
                controller=controller,
                controller_token=controller_token,
                max_launches=max_launches,
            )
            results.append(result)
            passes += 1
            if not watch or result["status"] == "paused":
                return {**result, "passes": passes}
            time.sleep(interval_seconds)


def status(root, *, data_root=None, project_id=None):
    data = _data_root(data_root)
    _, project_id = _registration(root, data, project_id)
    path = _queue_path(data, project_id)
    with _locked(path):
        document = _load(data, project_id)
        _reconcile(data, project_id, document)
    counts = {}
    for item in document["items"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    runner = _read(
        project_data_dir(data, project_id) / "dispatch-queue-runner.lock", {}
    )
    if runner.get("state") == "running":
        try:
            os.kill(int(runner["pid"]), 0)
            runner["pid_alive"] = True
        except (OSError, ValueError, TypeError):
            runner["pid_alive"] = False
    return {
        "status": "ok",
        "project_id": project_id,
        "host": host_control(data),
        "counts": counts,
        "items": sorted(
            document["items"],
            key=lambda row: (row.get("priority", 100), row.get("order", 0)),
        ),
        "runner": runner,
        "single_instance_scope": "project on this host/data root",
    }


def cancel(root, request_id, *, data_root=None, project_id=None, reason=None):
    data = _data_root(data_root)
    _, project_id = _registration(root, data, project_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("cancel requires a reason")
    path = _queue_path(data, project_id)
    with _locked(path):
        document = _load(data, project_id)
        item = next(
            (row for row in document["items"] if row["request_id"] == request_id),
            None,
        )
        if item is None:
            raise ValueError("queue request does not exist")
        if item["status"] in {"running", "completed"}:
            raise ValueError(
                "running/completed attempts cannot be cancelled in the queue; inspect the supervisor and card"
            )
        item.update(status="cancelled", reason=reason[:2048], updated_at=_now())
        _save(data, project_id, document)
        _event(
            data,
            project_id,
            "dispatch.queue-cancelled",
            item["task_id"],
            request_id=request_id,
            reason=reason[:500],
        )
    return item


def requeue(root, request_id, *, data_root=None, project_id=None, reason):
    data = _data_root(data_root)
    _, project_id = _registration(root, data, project_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("requeue requires a reason")
    path = _queue_path(data, project_id)
    with _locked(path):
        document = _load(data, project_id)
        item = next(
            (row for row in document["items"] if row["request_id"] == request_id),
            None,
        )
        if item is None:
            raise ValueError("queue request does not exist")
        if item["status"] not in {"blocked", "failed", "cancelled"}:
            raise ValueError(
                "only blocked, failed or cancelled requests can be requeued"
            )
        item.update(
            status="queued",
            requeue_reason=reason[:2048],
            requeued_at=_now(),
            updated_at=_now(),
            last_gate=None,
            last_error=None,
            retry_not_before=None,
            job_id=None,
        )
        _save(data, project_id, document)
        _event(
            data,
            project_id,
            "dispatch.queue-requeued",
            item["task_id"],
            request_id=request_id,
            reason=reason[:500],
        )
    return item


def command(args):
    from .planning_api import render

    action = args.queue_action
    if action == "pause":
        value = pause_host(args.data_root, args.reason)
    elif action == "resume":
        value = resume_host(args.data_root)
    elif action == "run":
        value = run(
            args.root,
            data_root=args.data_root,
            project_id=args.project_id,
            controller=args.controller,
            controller_token=args.controller_token,
            watch=args.watch,
            interval_seconds=args.interval_seconds,
            max_launches=args.max_launches,
        )
    elif action == "cancel":
        value = cancel(
            args.root,
            args.request_id,
            data_root=args.data_root,
            project_id=args.project_id,
            reason=args.reason,
        )
    elif action == "requeue":
        value = requeue(
            args.root,
            args.request_id,
            data_root=args.data_root,
            project_id=args.project_id,
            reason=args.reason,
        )
    else:
        value = status(args.root, data_root=args.data_root, project_id=args.project_id)
    print(
        render(
            value,
            format=args.format,
            max_output_bytes=args.max_output_bytes,
        )
    )
    return 1 if value.get("status") == "already-running" and action == "run" else 0


def register_cli(worker_subparsers):
    parser = worker_subparsers.add_parser(
        "queue",
        help="Durable single-instance dispatch queue; enqueue with worker dispatch --enqueue",
    )
    leaves = parser.add_subparsers(dest="queue_action", required=True)
    for action in ("status", "run", "pause", "resume", "cancel", "requeue"):
        leaf = leaves.add_parser(action)
        leaf.set_defaults(handler=command, queue_action=action)
        leaf.add_argument("--root", default=".")
        leaf.add_argument("--data-root")
        leaf.add_argument("--project-id")
        leaf.add_argument("--format", choices=("brief", "json"), default="brief")
        leaf.add_argument("--max-output-bytes", type=int, default=8192)
        if action == "run":
            leaf.add_argument("--controller")
            leaf.add_argument("--controller-token")
            leaf.add_argument(
                "--watch",
                action="store_true",
                help="Hold the single-instance lock and continue until interrupted",
            )
            leaf.add_argument("--interval-seconds", type=float, default=5)
            leaf.add_argument(
                "--max-launches",
                type=int,
                help="Bound launches in this pass for controlled rollout",
            )
        if action in {"cancel", "requeue"}:
            leaf.add_argument("--request-id", required=True)
            leaf.add_argument("--reason", required=True)
        if action == "pause":
            leaf.add_argument("--reason", required=True)
    enqueue = leaves.add_parser(
        "enqueue",
        help="Persist a dispatch request without starting a provider",
    )
    from .worker_dispatch import _run_parser

    _run_parser(enqueue, "enqueue")
