"""Durable at-least-once change consumers: reads never acknowledge delivery."""

from contextlib import contextmanager
import hashlib
import json

from .collaboration_registry import (
    _ID_RE,
    _atomic_write,
    _locked,
    _now,
    default_data_root,
    project_data_dir,
)


@contextmanager
def consumer(data_root, project_id, name, *, ack=None, since=None, reset=False):
    if not isinstance(name, str) or not _ID_RE.fullmatch(name):
        raise ValueError("consumer must be a stable lowercase name, max 128 characters")
    directory = project_data_dir(data_root or default_data_root(), project_id)
    if not (directory / "project.json").is_file():
        raise ValueError("consumer project is not registered")
    if since is not None and not reset:
        raise ValueError(
            "consumer uses its persisted cursor; --since requires --reset-consumer"
        )
    if reset and (since is None or ack is not None):
        raise ValueError(
            "--reset-consumer requires --since and cannot be combined with --ack"
        )
    if any(type(v) is not int or v < 0 for v in (ack, since) if v is not None):
        raise ValueError("consumer cursors must be nonnegative integers")
    path = (
        directory
        / "change-consumers"
        / (hashlib.sha256(name.encode()).hexdigest() + ".json")
    )
    with _locked(path):
        if path.is_symlink() or (path.exists() and path.stat().st_size > 8192):
            raise ValueError("consumer state must be a bounded local metadata file")
        state = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "schema_version": "context.change-consumer/v1",
                "consumer": name,
                "project_id": project_id,
                "acknowledged_cursor": 0,
                "delivered_cursor": 0,
            }
        )
        if (
            not isinstance(state, dict)
            or state.get("schema_version") != "context.change-consumer/v1"
            or state.get("consumer") != name
            or state.get("project_id") != project_id
            or any(
                type(state.get(k)) is not int or state[k] < 0
                for k in ("acknowledged_cursor", "delivered_cursor")
            )
        ):
            raise ValueError("consumer state is invalid; no automatic reset")
        if state["acknowledged_cursor"] > state["delivered_cursor"]:
            raise ValueError("consumer acknowledged cursor exceeds delivered cursor")
        if reset:
            state.update(acknowledged_cursor=since, delivered_cursor=since)
        if ack is not None:
            if not state["acknowledged_cursor"] <= ack <= state["delivered_cursor"]:
                raise ValueError(
                    "ack must be between the acknowledged and previously delivered cursors; reread after an interrupted delivery"
                )
            state["acknowledged_cursor"] = ack
        yield state
        state["updated_at"] = _now()
        _atomic_write(path, state)


def command(args, list_changes, bounded):
    if args.consumer is None:
        if args.ack is not None or args.reset_consumer:
            raise ValueError("--ack/--reset-consumer require --consumer")
        result = list_changes(
            args.data_root,
            project_id=args.project_id,
            since=args.since or 0,
            max_results=args.max_results,
        )
        print(bounded(result, args.max_output_bytes))
        return 0
    with consumer(
        args.data_root,
        args.project_id,
        args.consumer,
        ack=args.ack,
        since=args.since,
        reset=args.reset_consumer,
    ) as state:
        result = list_changes(
            args.data_root,
            project_id=args.project_id,
            since=state["acknowledged_cursor"],
            max_results=args.max_results,
        )
        if state["acknowledged_cursor"] > result["high_water_cursor"]:
            raise ValueError(
                "consumer cursor is ahead of the retained event log; inspect recovery or explicitly reset"
            )
        result["consumer"] = {
            "name": args.consumer,
            "acknowledged_cursor": state["acknowledged_cursor"],
            "delivery": "at-least-once; acknowledge returned next_cursor with --ack",
        }
        text = bounded(result, args.max_output_bytes)
        emitted = json.loads(text)
        print(text)  # Failed output must not acknowledge or advance the consumer.
        state["delivered_cursor"] = max(
            state["delivered_cursor"], emitted["next_cursor"]
        )
    return 0
