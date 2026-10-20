"""Opt-in project controller fencing and serialized tool-controlled operations.

This coordinates cooperating clients; it is not an OS sandbox or account auth.
"""

import math
import os
import subprocess
import time
import uuid
import hashlib
import json
from pathlib import Path

from .project_context import _path, _load, _text, resolve_context_args
from .collaboration_registry import (
    _atomic_write,
    _locked,
    default_data_root,
    _load_ledger,
    _ledger_path,
    GUARDED_DISPATCH_LEDGER_SCHEMA,
)


def _require(doc, owner=None, token=None, *, required=False):
    lease = doc.get("controller_lease")
    if not lease:
        if required or doc.get("controller_required"):
            raise ValueError(
                "project controller lease metadata is missing; reacquire before controller operations"
            )
        return None  # Existing projects keep their explicit pre-lease workflow.
    owner = owner or os.environ.get("CONTINUITY_CONTROLLER")
    token = token or os.environ.get("CONTINUITY_CONTROLLER_TOKEN")
    if (
        lease["expires_at"] <= time.time()
        or owner != lease["owner"]
        or token != lease["token"]
    ):
        raise ValueError(
            f"controller operation is read-only: holder={lease['owner']}, expires_at={lease['expires_at']}; acquire/renew the project controller lease"
        )
    return lease


def require(data_root, project_id, owner=None, token=None):
    required = _load_ledger(Path(data_root or default_data_root()), project_id).get(
        "controller_fencing_required", False
    )
    return _require(
        _load(_path(data_root, project_id)), owner, token, required=required
    )


def acquire(
    data_root,
    project_id,
    *,
    owner,
    ttl_seconds=300,
    token=None,
    renew=False,
    client_limits=None,
):
    _text(owner, "controller owner", 256)
    if (
        type(ttl_seconds) not in (int, float)
        or not math.isfinite(ttl_seconds)
        or not 1 <= ttl_seconds <= 86400
    ):
        raise ValueError("controller lease TTL must be 1–86400 seconds")
    if client_limits is not None and (
        not isinstance(client_limits, dict)
        or any(
            k not in {"codex", "claude", "custom"} or type(v) is not int or v < 1
            for k, v in client_limits.items()
        )
    ):
        raise ValueError(
            "client limits need positive integer codex/claude/custom values"
        )
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        previous = doc.get("controller_lease")
        if renew:
            if not previous:
                raise ValueError("no controller lease exists to renew")
            _require(doc, owner, token)
        elif previous and previous["expires_at"] > time.time():
            if previous["owner"] == owner and token == previous["token"]:
                return previous
            raise ValueError(
                f"controller lease is held by {previous['owner']} until {previous['expires_at']}"
            )
        value = {
            "owner": owner,
            "token": previous["token"] if renew else uuid.uuid4().hex,
            "fence": (previous or {}).get("fence", 0) + (0 if renew else 1),
            "expires_at": time.time() + ttl_seconds,
            "client_limits": client_limits
            if client_limits is not None
            else (previous or {}).get("client_limits", {"codex": 5}),
            "scope": ["dispatch", "integration", "baseline", "fast-forward"],
            "authority": "cooperative metadata fencing; not account authentication or external approval",
        }
        doc["controller_lease"] = value
        doc["controller_required"] = True
        data = Path(data_root or default_data_root())
        with _locked(_ledger_path(data, project_id)):
            ledger = _load_ledger(data, project_id)
            ledger["schema_version"] = GUARDED_DISPATCH_LEDGER_SCHEMA
            _atomic_write(_ledger_path(data, project_id), ledger)
        _atomic_write(path, doc)
    return value


def release(data_root, project_id, *, owner, token):
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        _require(doc, owner, token)
        if not doc.get("controller_lease"):
            raise ValueError("controller lease was not acquired")
        doc["controller_lease"]["expires_at"] = 0
        _atomic_write(path, doc)
    return {"released": True, "fencing_still_enabled": True}


def reserve(
    data_root,
    project_id,
    *,
    task_id,
    job_id,
    owner,
    token,
    profile,
    replace_job_id=None,
):
    required = _load_ledger(Path(data_root or default_data_root()), project_id).get(
        "controller_fencing_required", False
    )
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        lease = _require(doc, owner, token, required=required)
        reservations = doc.setdefault("dispatch_reservations", {})
        previous = reservations.get(task_id)
        if previous and previous["job_id"] != replace_job_id:
            binding = (
                _load_ledger(Path(data_root or default_data_root()), project_id)
                .get("execution_bindings", {})
                .get(task_id, {})
            )
            kind = (
                "duplicate-running-attempt"
                if binding.get("status") == "running"
                and binding.get("attempt_id") == previous["job_id"]
                else "stale-or-unobserved-reservation"
            )
            raise ValueError(
                f"task already has a dispatch reservation: {kind}; job={previous['job_id']} owner={previous.get('owner')} reserved_at={previous.get('reserved_at')}; inspect changes, then use controller clear-reservation with the exact job ID if reconciled; no automatic takeover"
            )
        if replace_job_id is not None and (
            not previous or previous["job_id"] != replace_job_id
        ):
            raise ValueError(
                "fallback reservation changed; inspect the current attempt before replacement"
            )
        client = profile.get("profile_provider", profile["provider"])
        origin = profile.get("execution_origin")
        active = [
            r
            for r in reservations.values()
            if r["client"] == client and r["origin"] == origin and r is not previous
        ]
        limit = (lease or {}).get("client_limits", {}).get(client)
        if limit is not None and len(active) >= limit:
            raise ValueError(
                f"{client} dispatch capacity is full ({limit}); wait for an attempt to finish"
            )
        reservations[task_id] = {
            "job_id": job_id,
            "client": client,
            "origin": origin,
            "owner": owner,
            "fence": (lease or {}).get("fence"),
            "reserved_at": time.time(),
        }
        _atomic_write(path, doc)


def view(data_root, project_id, owner=None, token=None):
    try:
        doc = _load(_path(data_root, project_id))
        lease = doc.get("controller_lease")
        required = _load_ledger(Path(data_root or default_data_root()), project_id).get(
            "controller_fencing_required", False
        )
    except (OSError, ValueError, KeyError):
        return {
            "enabled": True,
            "read_only": True,
            "status": "controller-metadata-unavailable",
            "scope": "controller operations only; ordinary work remains allowed",
        }
    value = {
        "operation": doc.get("controller_operation"),
        "lease": {k: v for k, v in lease.items() if k != "token"} if lease else None,
        "enabled": lease is not None or required,
        "read_only": required if lease is None else True,
        "scope": "protected controller operations only; ordinary work remains allowed",
    }
    if lease:
        try:
            _require({"controller_lease": lease}, owner, token)
            value["read_only"] = False
        except ValueError:
            pass
    return value


def begin_operation(data_root, project_id, *, owner, token, operation, argv):
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        lease = _require(doc, owner, token, required=True)
        previous = doc.get("controller_operation", {})
        if previous.get("status") == "running":
            raise ValueError(
                f"previous controller operation {previous['operation_id']} owned by {previous['owner']} has no confirmed finish; inspect it and use clear-operation with its exact id and a reason"
            )
        value = {
            "operation_id": uuid.uuid4().hex,
            "owner": owner,
            "fence": lease["fence"],
            "operation": operation,
            "command_sha256": hashlib.sha256(json.dumps(argv).encode()).hexdigest(),
            "status": "running",
            "started_at": time.time(),
            "authority": "started operation; not a liveness assertion",
        }
        _atomic_write(
            path.parent / "controller-operations" / (value["operation_id"] + ".json"),
            value,
        )
        doc["controller_operation"] = value
        _atomic_write(path, doc)
    return value


def end_operation(
    data_root, project_id, operation_id, *, return_code=None, reason=None
):
    if (
        not isinstance(operation_id, str)
        or len(operation_id) != 32
        or uuid.UUID(hex=operation_id).hex != operation_id
    ):
        raise ValueError("invalid controller operation id")
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        current = doc.get("controller_operation", {})
        if current.get("operation_id") != operation_id:
            raise ValueError(
                "controller operation id changed; no newer record overwritten"
            )
        if current.get("status") != "running":
            if (
                current.get("return_code") == return_code
                and current.get("reason") == reason
            ):
                return current
            raise ValueError(
                "controller operation already acknowledged; no newer record overwritten"
            )
        value = {
            **current,
            "status": "finished" if return_code is not None else "manually-cleared",
            "return_code": return_code,
            "finished_at": time.time(),
        }
        if reason is not None:
            value["reason"] = _text(reason, "operation acknowledgement", 2048)
        _atomic_write(
            path.parent / "controller-operations" / (operation_id + ".json"), value
        )
        doc["controller_operation"] = value
        _atomic_write(path, doc)
    return value


def finish_reservation(data_root, project_id, task_id, job_id, *, force=False):
    from .collaboration_registry import _load_ledger

    active = (
        _load_ledger(Path(data_root or default_data_root()), project_id)
        .get("execution_bindings", {})
        .get(task_id, {})
    )
    if (
        not force
        and active.get("attempt_id") == job_id
        and active.get("status") == "running"
    ):
        return False
    path = _path(data_root, project_id)
    with _locked(path):
        doc = _load(path)
        if (
            doc.get("dispatch_reservations", {}).get(task_id, {}).get("job_id")
            == job_id
        ):
            del doc["dispatch_reservations"][task_id]
            _atomic_write(path, doc)
            return True
        if force and task_id in doc.get("dispatch_reservations", {}):
            raise ValueError(
                "reservation belongs to a newer/different job; no reservation cleared"
            )
    return False


def command(args):
    from .planning_api import render

    render({}, max_output_bytes=args.max_output_bytes)
    resolve_context_args(args)
    project_id = args.project_id
    if args.controller_action in {"acquire", "renew"}:
        limits = None
        if args.client_limit:
            limits = {}
            for entry in args.client_limit:
                name, separator, value = entry.partition("=")
                if not separator or not value.isdigit():
                    raise ValueError("client-limit must be CLIENT=POSITIVE_INTEGER")
                limits[name] = int(value)
        value = acquire(
            args.data_root,
            project_id,
            owner=args.owner,
            token=args.token,
            ttl_seconds=args.ttl_seconds,
            renew=args.controller_action == "renew",
            client_limits=limits,
        )
    elif args.controller_action == "release":
        value = release(args.data_root, project_id, owner=args.owner, token=args.token)
    elif args.controller_action == "clear-operation":
        require(args.data_root, project_id, args.owner, args.token)
        value = end_operation(
            args.data_root, project_id, args.operation_id, reason=args.reason
        )
    elif args.controller_action == "clear-reservation":
        from .collaboration_registry import _record_change, default_data_root

        require(args.data_root, project_id, args.owner, args.token)
        _text(args.reason, "override reason", 2048)
        cleared = finish_reservation(
            args.data_root, project_id, args.task_id, args.job_id, force=True
        )
        if cleared:
            _record_change(
                Path(args.data_root or default_data_root()),
                project_id,
                "dispatch.reservation-cleared",
                args.task_id,
                attempt_id=args.job_id,
                reason=args.reason,
                owner=args.owner,
            )
        value = {
            "cleared": cleared,
            "warning": "Operator declared the old attempt safe to replace; no process was terminated",
        }
    elif args.controller_action == "run":
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        if not argv:
            raise ValueError("controller run requires an explicit command")
        with _locked(
            _path(args.data_root, project_id).with_name("controller-operation.lock")
        ):
            lease = require(args.data_root, project_id, args.owner, args.token)
            if not lease:
                raise ValueError(
                    "acquire a controller lease before running a protected operation"
                )
            started = begin_operation(
                args.data_root,
                project_id,
                owner=args.owner,
                token=args.token,
                operation=args.operation,
                argv=argv,
            )
            try:
                result = subprocess.run(argv, cwd=args.root, check=False)
            except (FileNotFoundError, PermissionError):
                end_operation(
                    args.data_root,
                    project_id,
                    started["operation_id"],
                    return_code=127,
                    reason="launch failed before a child command started",
                )
                raise
            end_operation(
                args.data_root,
                project_id,
                started["operation_id"],
                return_code=result.returncode,
            )
            # A command may outlive its lease; never automatically replay it.
            try:
                require(args.data_root, project_id, args.owner, args.token)
                current = True
            except ValueError:
                current = False
            print(
                render(
                    {
                        "return_code": result.returncode,
                        "lease_still_current": current,
                        "fence": lease["fence"],
                        "operation": args.operation,
                    },
                    max_output_bytes=args.max_output_bytes,
                )
            )
            return (
                result.returncode if result.returncode >= 0 else 128 - result.returncode
            )
    else:
        value = view(args.data_root, project_id, args.owner, args.token)
    print(render(value, format="json", max_output_bytes=args.max_output_bytes))
    return 0


def register_cli(commands):
    parser = commands.add_parser(
        "controller",
        help="Project controller lease/fence and serialized cooperating operations",
    )
    leaves = parser.add_subparsers(dest="controller_action", required=True)
    for action in (
        "acquire",
        "renew",
        "release",
        "status",
        "run",
        "clear-reservation",
        "clear-operation",
    ):
        leaf = leaves.add_parser(action)
        leaf.set_defaults(handler=command)
        leaf.add_argument("--root")
        leaf.add_argument("--project-id")
        leaf.add_argument("--data-root")
        leaf.add_argument("--max-output-bytes", type=int, default=8192)
        leaf.add_argument("--owner", required=action != "status")
        leaf.add_argument(
            "--token",
            required=action
            in {"renew", "release", "run", "clear-reservation", "clear-operation"},
        )
        if action == "clear-operation":
            leaf.add_argument("--operation-id", required=True)
            leaf.add_argument("--reason", required=True)
        if action == "clear-reservation":
            leaf.add_argument("--task-id", required=True)
            leaf.add_argument("--job-id", required=True)
            leaf.add_argument("--reason", required=True)
        if action in {"acquire", "renew"}:
            leaf.add_argument("--ttl-seconds", type=float, default=300)
            leaf.add_argument(
                "--client-limit",
                action="append",
                help="Limit a native client family on the same host/profile, e.g. codex=5",
            )
        if action == "run":
            import argparse

            leaf.add_argument(
                "--operation",
                choices=("integration", "baseline", "fast-forward", "dispatch"),
                required=True,
            )
            leaf.add_argument("argv", nargs=argparse.REMAINDER)
