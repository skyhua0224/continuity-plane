"""Local event fallback and idempotent replay; execution continues on registry I/O failure."""

import hashlib
import json
import os
from pathlib import Path
from .collaboration_registry import (
    _atomic_write,
    _locked,
    _now,
    _record_change,
    default_data_root,
)


def directory():
    return Path(
        os.environ.get("CONTINUITY_OUTBOX_ROOT")
        or Path.home() / ".local/state/continuity/outbox"
    )


def enqueue(data_root, project_id, event, task_id, **fields):
    identity = ":".join(
        (
            str(data_root or default_data_root()),
            project_id,
            event,
            task_id,
            str(fields.get("attempt_id", "")),
        )
    )
    path = directory() / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
    try:
        with _locked(path):
            _atomic_write(
                path,
                {
                    "data_root": str(data_root or default_data_root()),
                    "project_id": project_id,
                    "event": event,
                    "task_id": task_id,
                    "fields": fields,
                    "queued_at": _now(),
                    "replayed_at": None,
                },
            )
        return True
    except (OSError, ValueError):
        return False


def change(data_root, project_id, event, task_id=None, **fields):
    try:
        _record_change(data_root, project_id, event, task_id, **fields)
        return True
    except OSError:
        return enqueue(data_root, project_id, event, task_id or "", **fields)


def replay(data_root, *, project_id):
    from .execution_lifecycle import bind_execution, finish_execution
    from .collaboration_registry import _load_ledger

    target = str(data_root or default_data_root())
    paths = []
    for path in directory().glob("*.json"):
        try:
            row = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if (
            row.get("data_root") == target
            and row.get("project_id") == project_id
            and not row.get("replayed_at")
        ):
            paths.append((path, row))
    order = {
        "attempt.bind": 0,
        "attempt.started": 1,
        "attempt.heartbeat": 2,
        "attempt.finished": 3,
    }
    paths.sort(
        key=lambda pair: (
            pair[1]["fields"].get("attempt_id", ""),
            order.get(pair[1]["event"], 2),
        )
    )
    applied = 0
    pending = []
    for path, row in paths:
        fields = dict(row["fields"])
        event = row["event"]
        task = row["task_id"]
        try:
            if event == "attempt.bind":
                ledger = _load_ledger(Path(target), project_id)
                if not ledger.get("execution_bindings", {}).get(task):
                    try:
                        bind_execution(
                            target,
                            project_id=project_id,
                            task_id=task,
                            assignee=fields["assignee"],
                            attempt_id=fields["attempt_id"],
                        )
                    except ValueError:
                        pass
            elif event == "attempt.finished":
                finish_execution(target, project_id=project_id, task_id=task, **fields)
            else:
                fields.setdefault(
                    "event_id", f"outbox:{task}:{fields.get('attempt_id')}:{event}"
                )
                _record_change(Path(target), project_id, event, task, **fields)
            with _locked(path):
                row["replayed_at"] = _now()
                _atomic_write(path, row)
            applied += 1
        except (OSError, ValueError, KeyError) as exc:
            pending.append({"event": event, "task_id": task, "reason": str(exc)[:160]})
    return {"replayed": applied, "pending": pending, "retained_for_audit": True}


def command(args):
    print(json.dumps(replay(args.data_root, project_id=args.project_id)))
    return 0
