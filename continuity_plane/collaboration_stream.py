"""Cursor-based, append-only collaboration streams for control-plane clients."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from .execution_lifecycle import LOST_REASONS
from .collaboration_registry import (
    CollaborationRegistryError,
    _changes_path,
    default_data_root,
    latest_change_cursor,
    list_projects,
)


STREAM_SCHEMA = "context.collaboration-event-stream/v1alpha1"

_WORKER_EVENTS = {
    "attempt.started",
    "attempt.heartbeat",
    "attempt.finished",
    "attempt.retrying",
    "attempt.timeout",
    "attempt.retry-scheduled",
    "job.completed",
    "job.failed",
    "job.interrupted",
    "job.lost",
    "run.started",
    "run.heartbeat",
    "run.finished",
    "dispatch.policy-stopped",
    "dispatch.review-required",
    "dispatch.validation-failed",
    "dispatch.fallback",
    "dispatch.fallback-deferred",
    "worker.hook-launched",
    "worker.hook-failed",
    "task.updated",
}


def parse_cursor_map(value: str | None, project_ids: Iterable[str]) -> dict[str, int]:
    """Parse one cursor for a single project or `project:cursor` pairs."""

    projects = list(dict.fromkeys(project_ids))
    if value is None or value == "":
        return {project: 0 for project in projects}
    if type(value) is not str or len(value) > 8192:
        raise CollaborationRegistryError("since-cursor is invalid")
    if value.isdigit():
        if len(projects) != 1:
            raise CollaborationRegistryError(
                "--all-projects requires since-cursor as project:cursor pairs"
            )
        return {projects[0]: int(value)}
    result: dict[str, int] = {}
    for part in value.split(","):
        project, separator, cursor = part.partition(":")
        if not separator or not project or not cursor.isdigit():
            raise CollaborationRegistryError(
                "since-cursor must be an integer or comma-separated project:cursor pairs"
            )
        if project not in projects:
            raise CollaborationRegistryError(
                f"since-cursor names an unselected project: {project}"
            )
        result[project] = int(cursor)
    return {project: result.get(project, 0) for project in projects}


def _rows_from_buffer(
    buffer: bytes, *, final: bool = False
) -> tuple[list[dict[str, Any]], bytes]:
    lines = buffer.split(b"\n")
    remainder = b""
    if not final:
        remainder = lines.pop() if lines else b""
    rows = []
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (UnicodeError, ValueError):
            continue
        if isinstance(row, dict) and type(row.get("cursor")) is int:
            rows.append(row)
    if final and remainder:
        try:
            row = json.loads(remainder)
        except (UnicodeError, ValueError):
            row = None
        if isinstance(row, dict) and type(row.get("cursor")) is int:
            rows.append(row)
        remainder = b""
    return rows, remainder


def follow_project_changes(
    data_root: Path | str | None,
    *,
    project_id: str,
    since: int = 0,
    follow: bool = True,
    poll_seconds: float = 0.5,
    max_events: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield complete appended rows once, then tail the same journal."""

    if type(since) is not int or since < 0:
        raise CollaborationRegistryError("since cursor is invalid")
    if type(poll_seconds) not in (int, float) or poll_seconds <= 0:
        raise CollaborationRegistryError("poll interval is invalid")
    root = Path(data_root or default_data_root())
    path = _changes_path(root, project_id)
    offset = 0
    buffer = b""
    emitted = 0
    while True:
        emitted_this_round = 0
        try:
            size = path.stat().st_size
            if size < offset:
                offset, buffer = 0, b""
            with path.open("rb") as stream:
                stream.seek(offset)
                while chunk := stream.read(65536):
                    offset += len(chunk)
                    buffer += chunk
                    rows, buffer = _rows_from_buffer(buffer)
                    for row in rows:
                        if row["cursor"] <= since:
                            continue
                        yield row
                        since = row["cursor"]
                        emitted += 1
                        emitted_this_round += 1
                        if max_events is not None and emitted >= max_events:
                            return
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise CollaborationRegistryError(
                f"collaboration event stream is unreadable: {exc}"
            ) from exc
        if not follow:
            rows, buffer = _rows_from_buffer(buffer, final=True)
            for row in rows:
                if row["cursor"] <= since:
                    continue
                yield row
                emitted += 1
                if max_events is not None and emitted >= max_events:
                    return
            return
        if max_events is not None and emitted >= max_events:
            return
        if emitted_this_round == 0:
            time.sleep(poll_seconds)


def new_tail_state(since: int = 0) -> dict[str, Any]:
    return {"offset": 0, "buffer": b"", "cursor": since}


def read_tail_batch(
    data_root, *, project_id: str, state: dict[str, Any], final: bool = False
) -> Iterator[dict[str, Any]]:
    """Read currently available rows without sleeping or blocking."""

    path = _changes_path(Path(data_root or default_data_root()), project_id)
    try:
        if state["offset"] == 0 and state["cursor"]:
            from .journal_index import offset_after

            state["offset"] = offset_after(path, state["cursor"])
        size = path.stat().st_size
        info = path.stat()
        identity = (info.st_dev, info.st_ino)
        if state.get("identity") not in (None, identity):
            state.update(offset=0, buffer=b"")
        state["identity"] = identity
        if size < state["offset"]:
            state.update(offset=0, buffer=b"")
        with path.open("rb") as stream:
            stream.seek(state["offset"])
            while chunk := stream.read(65536):
                state["offset"] += len(chunk)
                state["buffer"] += chunk
                rows, state["buffer"] = _rows_from_buffer(state["buffer"])
                for row in rows:
                    if row["cursor"] > state["cursor"]:
                        state["cursor"] = row["cursor"]
                        yield row
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CollaborationRegistryError(
            f"collaboration event stream is unreadable: {exc}"
        ) from exc
    if final and state["buffer"]:
        rows, state["buffer"] = _rows_from_buffer(state["buffer"], final=True)
        for row in rows:
            if row["cursor"] > state["cursor"]:
                state["cursor"] = row["cursor"]
                yield row


def event_kind(row: dict[str, Any]) -> str:
    event = row.get("event")
    if event in {"attempt.started", "run.started"}:
        return "started"
    if event in {"attempt.heartbeat", "run.heartbeat"}:
        return "progress"
    if event == "attempt.retrying":
        return "retrying"
    if event == "job.lost" or (
        event in {"attempt.finished", "job.interrupted"}
        and row.get("reason") in LOST_REASONS
    ):
        return "lost"
    if event in {"attempt.finished", "run.finished", "job.completed", "job.failed", "job.interrupted"}:
        failure = row.get("failure_class")
        if failure in {"rate-limit", "quota-exhausted", "capacity"}:
            return "rate-limited"
        if row.get("reason") in {"timeout", "no-output-timeout"}:
            return "timeout"
        return {
            "completed": "completed",
            "failed": "failed",
            "interrupted": "interrupted",
        }.get(row.get("status"), "finished")
    if event == "attempt.timeout":
        return "timeout"
    if event in {"dispatch.policy-stopped", "dispatch.review-required"}:
        return "blocked"
    if event == "dispatch.validation-failed":
        return "failed"
    if event == "task.updated" and row.get("status") == "blocked":
        return "blocked"
    if event == "attempt.retry-scheduled":
        return "retry-scheduled"
    return "status"


def compact_worker_event(project_id: str, row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "cursor",
        "event",
        "task_id",
        "attempt_id",
        "job_id",
        "assignee",
        "status",
        "return_code",
        "reason",
        "failure_class",
        "model",
        "from_model",
        "to_model",
        "pid",
        "host_id",
        "elapsed_seconds",
        "idle_seconds",
        "idle_reason",
        "idle_evidence",
        "observed_at",
        "finished_at",
        "retry",
        "wait_seconds",
        "runtime_tracking_error",
    )
    value = {
        "schema_version": STREAM_SCHEMA,
        "project_id": project_id,
        **{key: row[key] for key in keys if row.get(key) is not None},
        "kind": event_kind(row),
        "stream_cursor": f"{project_id}:{row['cursor']}",
    }
    for key in ("reason", "runtime_tracking_error"):
        if isinstance(value.get(key), str) and len(value[key].encode()) > 512:
            value[key] = value[key].encode()[:512].decode("utf-8", "ignore")
            value["details_omitted"] = True
    return value


def worker_events(
    data_root,
    *,
    project_ids: list[str],
    since_cursors: dict[str, int],
    follow: bool,
    poll_seconds: float,
    max_events: int | None,
    deadline: float | None = None,
):
    """Interleave project tails while preserving each project's stable cursor.

    `deadline` is a time.monotonic() value; a following tail stops at it.
    """

    states = {
        project_id: new_tail_state(since_cursors.get(project_id, 0))
        for project_id in project_ids
    }
    emitted = 0
    processed = dict(since_cursors)
    while True:
        made_progress = False
        for project_id, state in states.items():
            for row in read_tail_batch(
                data_root,
                project_id=project_id,
                state=state,
                final=not follow,
            ):
                made_progress = True
                processed[project_id] = row["cursor"]
                if row.get("event") not in _WORKER_EVENTS:
                    continue
                if row.get("event") == "task.updated" and row.get("status") != "blocked":
                    continue
                value = compact_worker_event(project_id, row)
                if len(project_ids) > 1:
                    value["stream_cursor"] = ",".join(
                        f"{project}:{processed.get(project, 0)}" for project in project_ids
                    )
                yield value
                emitted += 1
                if max_events is not None and emitted >= max_events:
                    return
        if not follow:
            return
        if not made_progress:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                poll = min(poll_seconds, remaining)
            else:
                poll = poll_seconds
            time.sleep(poll)


def selected_project_ids(data_root, project_id: str | None, all_projects: bool):
    root = Path(data_root or default_data_root())
    available = {item["project_id"]: item for item in list_projects(root)}
    if all_projects:
        if project_id:
            raise CollaborationRegistryError("choose --project-id or --all-projects, not both")
        if not available:
            raise CollaborationRegistryError("no collaboration projects are registered")
        return sorted(available)
    if not project_id:
        raise CollaborationRegistryError("project-id is required unless --all-projects is used")
    if project_id not in available:
        raise CollaborationRegistryError("project is not registered")
    return [project_id]


def worker_events_command(args) -> int:
    if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        raise CollaborationRegistryError("poll-seconds must be finite and positive")
    projects = selected_project_ids(args.data_root, args.project_id, args.all_projects)
    cursors = parse_cursor_map(args.since_cursor, projects)
    max_events = args.max_events
    if max_events is None and not args.follow:
        max_events = 100
    count = 0
    try:
        for row in worker_events(
            args.data_root,
            project_ids=projects,
            since_cursors=cursors,
            follow=args.follow,
            poll_seconds=args.poll_seconds,
            max_events=max_events,
        ):
            text = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if len(text.encode()) + 1 > args.max_output_bytes:
                row.pop("runtime_tracking_error", None)
                row["details_omitted"] = True
                text = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if len(text.encode()) + 1 > args.max_output_bytes:
                raise CollaborationRegistryError(
                    "worker event identity exceeds --max-output-bytes"
                )
            print(text, flush=True)
            count += 1
    except KeyboardInterrupt:
        return 0
    if count == 0:
        print(
            json.dumps(
                {
                    "schema_version": STREAM_SCHEMA,
                    "status": "caught-up",
                    "project_id": projects[0] if len(projects) == 1 else None,
                    "project_ids": projects if len(projects) > 1 else None,
                    "provider_execution": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 0


# Kinds that end a worker's turn; a controller waiting outside the model wakes on these.
_WAIT_KINDS = {
    "completed",
    "failed",
    "interrupted",
    "timeout",
    "rate-limited",
    "blocked",
    "finished",
    "lost",
}
WAIT_TIMEOUT_EXIT = 3


def superseded_by_job_event(row: dict[str, Any]) -> bool:
    """`attempt.finished` that is followed by its own `job.*` outcome event.

    Waiters and notices act on the job event so one outcome is reported once;
    older rows without `job_id` still count as terminal themselves.
    """
    return row.get("event") == "attempt.finished" and bool(row.get("job_id"))


def wait_next_hint(event: dict[str, Any]) -> str:
    project, task = event.get("project_id"), event.get("task_id")
    if event["kind"] in {"blocked", "rate-limited"}:
        return f"continuity_worker_jobs(project_id={project!r})"
    return f"continuity_worker_report(project_id={project!r}, task_id={task!r})"


def worker_wait_command(args) -> int:
    """Block outside the model until a terminal worker event; one JSON line per event."""

    for name in ("poll_seconds", "timeout_seconds"):
        value = getattr(args, name)
        if not math.isfinite(value) or value < 0 or (name == "poll_seconds" and value == 0):
            raise CollaborationRegistryError(f"{name.replace('_', '-')} is invalid")
    projects = selected_project_ids(args.data_root, args.project_id, args.all_projects)
    cursors = parse_cursor_map(args.since_cursor, projects)
    if args.since_cursor in (None, ""):
        # Never replay history: start at the current tail.
        cursors = {
            project: latest_change_cursor(args.data_root, project_id=project)
            for project in projects
        }
    tasks = set(args.task_id or [])
    started = time.monotonic()
    deadline = started + args.timeout_seconds if args.timeout_seconds else None
    count = 0
    last_cursor = ",".join(f"{project}:{cursors[project]}" for project in projects)
    try:
        for event in worker_events(
            args.data_root,
            project_ids=projects,
            since_cursors=cursors,
            follow=True,
            poll_seconds=args.poll_seconds,
            max_events=None,
            deadline=deadline,
        ):
            last_cursor = event.get("stream_cursor", last_cursor)
            if event["kind"] not in _WAIT_KINDS or superseded_by_job_event(event):
                continue
            if tasks and event.get("task_id") not in tasks:
                continue
            line = {
                key: event[key]
                for key in (
                    "project_id", "task_id", "kind", "reason", "failure_class",
                    "status", "attempt_id", "stream_cursor",
                )
                if event.get(key) is not None
            }
            line["next"] = wait_next_hint(event)
            print(json.dumps(line, ensure_ascii=False, sort_keys=True), flush=True)
            count += 1
            if count >= args.max_events:
                return 0
    except KeyboardInterrupt:
        return 130
    print(
        json.dumps(
            {
                "status": "timeout",
                "waited_seconds": round(time.monotonic() - started, 1),
                "emitted": count,
                "stream_cursor": last_cursor,
                "projects": projects,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    return WAIT_TIMEOUT_EXIT


def inbox_messages(
    data_root,
    *,
    project_id: str,
    recipient: str,
    since_cursor: int | None = None,
    follow: bool = False,
    timeout_seconds: float | None = 300,
    poll_seconds: float = 0.5,
):
    from .collaboration_inbox import list_messages

    if since_cursor is None:
        since_cursor = latest_change_cursor(data_root, project_id=project_id)
    if type(since_cursor) is not int or since_cursor < 0:
        raise CollaborationRegistryError("since-cursor is invalid")
    if timeout_seconds is not None and (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds) or timeout_seconds < 0
    ):
        raise CollaborationRegistryError("timeout-seconds is invalid")
    if (type(poll_seconds) not in (int, float)
        or not math.isfinite(poll_seconds) or poll_seconds <= 0):
        raise CollaborationRegistryError("poll-seconds is invalid")
    deadline = (
        None if timeout_seconds is None else time.monotonic() + timeout_seconds
    )
    state = new_tail_state(since_cursor)
    while True:
        for row in read_tail_batch(
            data_root,
            project_id=project_id,
            state=state,
            final=not follow,
        ):
            if row.get("event") != "message.sent" or row.get("recipient") != recipient:
                continue
            message_id = row.get("message_id")
            match = next(
                (
                    item
                    for item in list_messages(
                        data_root, project_id=project_id, recipient=recipient, limit=1000
                    )
                    if item.get("message_id") == message_id
                ),
                None,
            )
            if match:
                yield {
                    "schema_version": STREAM_SCHEMA,
                    "status": "message",
                    "project_id": project_id,
                    "recipient": recipient,
                    "cursor": row["cursor"],
                    "stream_cursor": f"{project_id}:{row['cursor']}",
                    "message": match,
                    "provider_execution": False,
                }
                if not follow:
                    return
        if deadline is not None and time.monotonic() >= deadline:
            yield {
                "schema_version": STREAM_SCHEMA,
                "status": "timed-out",
                "project_id": project_id,
                "recipient": recipient,
                "cursor": state["cursor"],
                "stream_cursor": f"{project_id}:{state['cursor']}",
                "timed_out": True,
            }
            return
        time.sleep(poll_seconds)


def wait_for_inbox_message(data_root, **options):
    """Return the first message or timeout; CLI follow consumes inbox_messages."""
    return next(inbox_messages(data_root, **options))
