"""Opt-in project controller fencing and serialized tool-controlled operations.

This coordinates cooperating clients; it is not an OS sandbox or account auth.
"""

import math
import os
import socket
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
    project_data_dir,
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
    if lease["expires_at"] <= time.time():
        raise ValueError(
            f"controller operation is read-only because the project lease expired: holder={lease['owner']}, expires_at={lease['expires_at']}; acquire/renew this project's lease"
        )
    if owner != lease["owner"]:
        raise ValueError(
            f"controller operation is read-only because the controller owner does not match: holder={lease['owner']}, expires_at={lease['expires_at']}; obtain or release this project's lease rather than reusing another project's controller identity"
        )
    if not token:
        raise ValueError(
            f"controller operation is read-only because a project-scoped token is required: holder={lease['owner']}; use the token returned by controller acquire/renew for this project"
        )
    if token != lease["token"]:
        raise ValueError(
            f"controller operation is read-only because the controller token does not match the active lease: holder={lease['owner']}; controller tokens are project-scoped, so acquire/renew this project's lease instead of reusing a token from another project"
        )
    return lease


def _host_fingerprint():
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()


def _boot_fingerprint():
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_bytes().strip()
    except OSError:
        return None
    return hashlib.sha256(value).hexdigest() if value else None


def _boot_started_at():
    try:
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def require(data_root, project_id, owner=None, token=None):
    required = _load_ledger(Path(data_root or default_data_root()), project_id).get(
        "controller_fencing_required", False
    )
    return _require(
        _load(_path(data_root, project_id)), owner, token, required=required
    )


def active_owner(data_root, project_id, *, now=None):
    """Return the unexpired cooperating controller owner without granting authority."""

    try:
        lease = _load(_path(data_root, project_id)).get("controller_lease") or {}
    except (OSError, ValueError, KeyError):
        return None
    expires_at = lease.get("expires_at")
    owner = lease.get("owner")
    if (
        not isinstance(owner, str)
        or type(expires_at) not in (int, float)
        or expires_at <= (time.time() if now is None else now)
    ):
        return None
    return owner


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


def _reservation_diagnostic(
    data_root, project_id, task_id, reservation, *, ledger=None
):
    data = Path(data_root or default_data_root())
    ledger = ledger or _load_ledger(data, project_id)
    binding = ledger.get("execution_bindings", {}).get(task_id, {})
    job_id = reservation.get("job_id")
    result_path = (
        project_data_dir(data, project_id) / "worker-jobs" / str(job_id) / "result.json"
    )
    try:
        result = json.loads(result_path.read_text())
    except (OSError, ValueError):
        result = {}
    age = max(0, round(time.time() - reservation.get("reserved_at", time.time())))
    current_boot = _boot_fingerprint()
    current_host = _host_fingerprint()
    boot_started_at = _boot_started_at()
    if reservation.get("host_sha256") and reservation["host_sha256"] != current_host:
        state = "stale"
        reason = "host-identity-changed"
        next_step = "This reservation belongs to another host identity. Verify the owning runtime, then clear the exact stale job."
    elif (
        reservation.get("boot_sha256")
        and current_boot
        and reservation["boot_sha256"] != current_boot
    ):
        state = "stale"
        reason = "host-boot-changed"
        next_step = "The reservation predates the current host boot. Reconcile host reservations; no worker from that boot can still be running."
    elif (
        not reservation.get("boot_sha256")
        and boot_started_at is not None
        and reservation.get("reserved_at", 0) < boot_started_at
    ):
        state = "stale"
        reason = "host-boot-changed"
        next_step = "This legacy reservation predates the current host boot. Reconcile host reservations; no worker from that boot can still be running."
    elif binding.get("status") == "running" and binding.get("attempt_id") == job_id:
        state = "live"
        reason = "execution-binding-running"
        next_step = "Inspect the live attempt and wait for its terminal event; do not clear the reservation."
    elif result.get("status"):
        state = "stale"
        reason = "terminal-result-" + str(result["status"])
        next_step = "The recorded attempt is already terminal. Reconcile the card and clear the exact reservation, then dispatch normally."
    elif binding and binding.get("status") != "running":
        state = "stale"
        reason = "terminal-execution-binding-" + str(binding.get("status"))
        next_step = "Inspect the attempt result, then clear the exact reservation after reconciling the card."
    else:
        state = "unknown"
        reason = "no-live-binding-or-terminal-result"
        next_step = "Inspect the job log, thread and process state. If the process is gone, reconcile before clearing the exact reservation."
    return {
        "state": state,
        "reason": reason,
        "job_id": job_id,
        "owner": reservation.get("owner"),
        "reserved_age_seconds": age,
        "attempt_status": binding.get("status"),
        "attempt_id": binding.get("attempt_id"),
        "result_status": result.get("status"),
        "host_sha256": reservation.get("host_sha256"),
        "boot_sha256": reservation.get("boot_sha256"),
        "current_boot": bool(
            reservation.get("boot_sha256")
            and current_boot
            and reservation["boot_sha256"] == current_boot
        ),
        "boot_started_at": boot_started_at,
        "next_step": next_step,
    }


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
    from .host_capacity import path as host_capacity_path

    with _locked(host_capacity_path(data_root)):
        return _reserve(
            data_root, project_id, task_id=task_id, job_id=job_id,
            owner=owner, token=token, profile=profile, replace_job_id=replace_job_id,
        )


def _reserve(data_root, project_id, *, task_id, job_id, owner, token, profile, replace_job_id=None):
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
            detail = _reservation_diagnostic(data_root, project_id, task_id, previous)
            raise ValueError(
                "task already has a dispatch reservation: "
                f"state={detail['state']} reason={detail['reason']} "
                f"job={detail['job_id']} owner={detail['owner']} "
                f"reserved_age_seconds={detail['reserved_age_seconds']} "
                f"attempt_status={detail['attempt_status']} "
                f"result_status={detail['result_status']}. "
                + detail["next_step"]
                + " Automatic takeover is disabled; use controller clear-reservation with the exact job ID only after reconciliation."
            )
        if replace_job_id is not None and (
            not previous or previous["job_id"] != replace_job_id
        ):
            raise ValueError(
                "fallback reservation changed; inspect the current attempt before replacement"
            )
        client = profile.get("profile_provider", profile["provider"])
        origin = profile.get("execution_origin")
        from .host_capacity import enforce as enforce_host_capacity

        enforce_host_capacity(data_root, client, exclude=(project_id, task_id))
        ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
        active = []
        stale = []
        for active_task_id, reservation in reservations.items():
            if (
                reservation["client"] != client
                or reservation["origin"] != origin
                or reservation is previous
            ):
                continue
            detail = _reservation_diagnostic(
                data_root,
                project_id,
                active_task_id,
                reservation,
                ledger=ledger,
            )
            if detail["state"] == "stale":
                stale.append({"task_id": active_task_id, **detail})
            else:
                active.append({"task_id": active_task_id, **detail})
        limit = (lease or {}).get("client_limits", {}).get(client)
        if limit is not None and len(active) >= limit:
            raise ValueError(
                f"{client} project dispatch capacity is full ({len(active)}/{limit}); "
                "scope=current-project+host+profile+client-family; "
                f"stale_reservations_ignored={len(stale)}. Wait for a live attempt or use controller reservations/reconcile-host after a host restart."
            )
        reservations[task_id] = {
            "job_id": job_id,
            "client": client,
            "origin": origin,
            "owner": owner,
            "fence": (lease or {}).get("fence"),
            "reserved_at": time.time(),
            "host_sha256": _host_fingerprint(),
            "boot_sha256": _boot_fingerprint(),
        }
        _atomic_write(path, doc)


def reservation_report(data_root, project_id, *, state=None):
    from .host_capacity import usage as host_capacity_usage

    path = _path(data_root, project_id)
    doc = _load(path)
    ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
    lease = doc.get("controller_lease") or {}
    rows = [
        {
            "task_id": task_id,
            **_reservation_diagnostic(
                data_root,
                project_id,
                task_id,
                reservation,
                ledger=ledger,
            ),
        }
        for task_id, reservation in sorted(doc.get("dispatch_reservations", {}).items())
    ]
    selected = [row for row in rows if state is None or row["state"] == state]
    capacity = {}
    for row in rows:
        reservation = doc["dispatch_reservations"][row["task_id"]]
        identity = (
            reservation["client"]
            + ":"
            + json.dumps(reservation["origin"], sort_keys=True)
        )
        bucket = capacity.setdefault(
            identity,
            {
                "client": reservation["client"],
                "origin": reservation["origin"],
                "limit": lease.get("client_limits", {}).get(reservation["client"]),
                "live": 0,
                "unknown": 0,
                "stale": 0,
            },
        )
        bucket[row["state"]] += 1
    for item in capacity.values():
        item["counted_against_capacity"] = item["live"] + item["unknown"]
        item["scope"] = "project+host+profile+client-family"
    return {
        "status": "ok",
        "project_id": project_id,
        "scope": "project-data-root",
        "capacity_semantics": "Limits are enforced per project registration, host, profile and client family. They do not prove a cross-project native account cap.",
        "current_host_boot_only": True,
        "items": selected,
        "total_items": len(selected),
        "counts": {
            name: sum(row["state"] == name for row in rows)
            for name in ("live", "stale", "unknown")
        },
        "capacity": list(capacity.values()),
        "host_capacity": host_capacity_usage(data_root),
    }


def reconcile_host(
    data_root,
    project_id,
    *,
    apply=False,
    include_terminal=False,
    reason=None,
    owner=None,
    token=None,
):
    from .collaboration_registry import _record_change

    if apply and (not isinstance(reason, str) or not reason.strip()):
        raise ValueError("reconcile-host --apply requires --reason")
    path = _path(data_root, project_id)
    cleared = []
    with _locked(path):
        doc = _load(path)
        if apply:
            _require(doc, owner, token, required=True)
        ledger = _load_ledger(Path(data_root or default_data_root()), project_id)
        rows = []
        for task_id, reservation in sorted(
            doc.get("dispatch_reservations", {}).items()
        ):
            rows.append(
                {
                    "task_id": task_id,
                    **_reservation_diagnostic(
                        data_root,
                        project_id,
                        task_id,
                        reservation,
                        ledger=ledger,
                    ),
                }
            )
        candidates = [
            row
            for row in rows
            if row["state"] == "stale"
            and (
                include_terminal
                or row["reason"] in {"host-boot-changed", "host-identity-changed"}
            )
        ]
        if apply:
            for row in candidates:
                reservation = doc.get("dispatch_reservations", {}).get(row["task_id"])
                if reservation and reservation.get("job_id") == row["job_id"]:
                    del doc["dispatch_reservations"][row["task_id"]]
                    cleared.append(row)
            if cleared:
                _atomic_write(path, doc)
    if apply:
        for row in cleared:
            _record_change(
                Path(data_root or default_data_root()),
                project_id,
                "dispatch.reservation-reconciled",
                row["task_id"],
                attempt_id=row["job_id"],
                reason=reason,
                state_reason=row["reason"],
                owner=owner,
                scope="host-restart",
            )
    return {
        "status": "applied" if apply else "dry-run",
        "project_id": project_id,
        "scope": "project-data-root",
        "include_terminal": include_terminal,
        "candidates": candidates,
        "cleared_count": len(cleared) if apply else 0,
        "cleared": cleared,
        "unknown_preserved": sum(row["state"] == "unknown" for row in rows),
        "live_preserved": sum(row["state"] == "live" for row in rows),
    }


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
        before = (
            _load(_path(args.data_root, project_id))
            .get("dispatch_reservations", {})
            .get(args.task_id)
        )
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
            "status": "cleared" if cleared else "not-found",
            "task_id": args.task_id,
            "job_id": args.job_id,
            "reservation_before_clear": before,
            "message": (
                "The exact reservation was removed. No process was terminated."
                if cleared
                else "No reservation with this exact job ID exists; nothing was changed."
            ),
        }
    elif args.controller_action == "reservations":
        value = reservation_report(
            args.data_root,
            project_id,
            state=args.state,
        )
    elif args.controller_action == "host-capacity":
        from .host_capacity import load as host_capacity_load, set_limits, usage

        if args.host_client_limit:
            if not project_id:
                raise ValueError("changing host capacity requires a registered project lease")
            require(args.data_root, project_id, args.owner, args.token)
            limits = {}
            for entry in args.host_client_limit:
                name, separator, limit = entry.partition("=")
                if not separator or not limit.isdigit() or int(limit) < 1:
                    raise ValueError(
                        "host-client-limit must be CLIENT=POSITIVE_INTEGER"
                    )
                limits[name] = int(limit)
            value = set_limits(args.data_root, limits)
        else:
            value = {**host_capacity_load(args.data_root), **usage(args.data_root)}
    elif args.controller_action == "reconcile-host":
        value = reconcile_host(
            args.data_root,
            project_id,
            apply=args.apply,
            include_terminal=args.include_terminal,
            reason=args.reason,
            owner=args.owner,
            token=args.token,
        )
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
    elif args.controller_action == "status":
        value = view(args.data_root, project_id, args.owner, args.token)
        reservations = reservation_report(args.data_root, project_id)
        value.update(
            reservation_counts=reservations["counts"],
            capacity=reservations["capacity"],
            capacity_semantics=reservations["capacity_semantics"],
            host_capacity=reservations["host_capacity"],
        )
    else:
        raise ValueError("unsupported controller action")
    print(render(value, format=args.format, max_output_bytes=args.max_output_bytes))
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
        "reservations",
        "reconcile-host",
        "run",
        "clear-reservation",
        "clear-operation",
        "host-capacity",
    ):
        leaf = leaves.add_parser(action)
        leaf.set_defaults(handler=command)
        leaf.add_argument("--root")
        leaf.add_argument("--project-id")
        leaf.add_argument("--data-root")
        leaf.add_argument("--max-output-bytes", type=int, default=8192)
        leaf.add_argument(
            "--owner",
            required=action
            in {
                "acquire",
                "renew",
                "release",
                "run",
                "clear-reservation",
                "clear-operation",
            },
        )
        leaf.add_argument(
            "--token",
            required=action
            in {"renew", "release", "run", "clear-reservation", "clear-operation"},
        )
        leaf.add_argument("--format", choices=("brief", "json"), default="json")
        if action == "clear-operation":
            leaf.add_argument("--operation-id", required=True)
            leaf.add_argument("--reason", required=True)
        if action == "clear-reservation":
            leaf.add_argument("--task-id", required=True)
            leaf.add_argument("--job-id", required=True)
            leaf.add_argument("--reason", required=True)
        if action == "reservations":
            leaf.add_argument("--state", choices=("live", "stale", "unknown"))
        if action == "host-capacity":
            leaf.add_argument(
                "--host-client-limit",
                action="append",
                default=[],
                help="Set host-wide client capacity, e.g. codex=8; requires a controller lease",
            )
        if action == "reconcile-host":
            leaf.add_argument("--apply", action="store_true")
            leaf.add_argument(
                "--include-terminal",
                action="store_true",
                help="Also clear stale reservations whose attempt result is already terminal; default only host reboot/identity changes",
            )
            leaf.add_argument("--reason")
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
