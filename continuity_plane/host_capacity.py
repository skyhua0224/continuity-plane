"""Operator-declared, host-local capacity across registered projects."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .collaboration_registry import _atomic_write, _locked, _now, default_data_root


SCHEMA = "context.host-capacity/v1alpha1"


def path(data_root=None):
    return Path(data_root or default_data_root()) / "host-capacity.json"


def load(data_root=None):
    target = path(data_root)
    try:
        value = json.loads(target.read_text(encoding="utf-8")) if target.exists() else {}
    except (OSError, ValueError) as exc:
        raise ValueError("host capacity configuration is unreadable") from exc
    if not value:
        return {
            "schema_version": SCHEMA,
            "limits": {},
            "enabled": False,
            "authority": "operator-declared host capacity; not native account quota",
        }
    if value.get("schema_version") != SCHEMA or not isinstance(
        value.get("limits"), dict
    ):
        raise ValueError("host capacity configuration schema is unsupported")
    return value


def set_limits(data_root, limits):
    if not isinstance(limits, dict):
        raise ValueError("host capacity limits must be a mapping")
    if any(
        key not in {"total", "codex", "claude", "custom"} or type(limit) is not int or limit < 1
        for key, limit in limits.items()
    ):
        raise ValueError("host capacity supports positive total/codex/claude/custom limits")
    target = path(data_root)
    with _locked(target):
        current = load(data_root)
        document = {
            **current,
            "schema_version": SCHEMA,
            "limits": {**current.get("limits", {}), **limits},
            "enabled": True,
            "updated_at": _now(),
            "authority": "operator-declared host capacity; not native account quota",
        }
        _atomic_write(target, document)
    return usage(data_root)


def _live_host_reservations(data_root):
    root = Path(data_root or default_data_root())
    from .controller_lease import _boot_fingerprint, _boot_started_at, _host_fingerprint

    current_host = _host_fingerprint()
    current_boot = _boot_fingerprint()
    boot_started_at = _boot_started_at()
    rows = []
    projects = root / "projects"
    if not projects.is_dir():
        return rows
    for context_path in sorted(projects.glob("*/project-context.json")):
        try:
            document = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        project_id = context_path.parent.name
        for task_id, reservation in document.get("dispatch_reservations", {}).items():
            if not isinstance(reservation, dict):
                continue
            if reservation.get("host_sha256") != current_host:
                continue
            if (
                reservation.get("boot_sha256")
                and current_boot
                and reservation["boot_sha256"] != current_boot
            ):
                continue
            if (
                not reservation.get("boot_sha256")
                and boot_started_at is not None
                and reservation.get("reserved_at", 0) < boot_started_at
            ):
                continue
            job_id = reservation.get("job_id")
            if isinstance(job_id, str) and re.fullmatch(r"job-[0-9a-f]{16}", job_id):
                try:
                    result = json.loads((context_path.parent / "worker-jobs" / job_id / "result.json").read_text())
                except (OSError, ValueError):
                    result = {}
                if result.get("status") in {"completed", "failed", "interrupted"}:
                    continue
            rows.append(
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "client": reservation.get("client"),
                    "origin": reservation.get("origin"),
                    "job_id": reservation.get("job_id"),
                    "state": "unknown"
                    if reservation.get("boot_sha256") != current_boot
                    else "live-or-unknown",
                }
            )
    return rows


def usage(data_root=None):
    document = load(data_root)
    rows = _live_host_reservations(data_root)
    counts = {}
    for row in rows:
        client = row.get("client")
        if client:
            counts[client] = counts.get(client, 0) + 1
    counts["total"] = len(rows)
    capacity = []
    for client, limit in sorted(document.get("limits", {}).items()):
        count = counts.get(client, 0)
        capacity.append(
            {
                "client": client,
                "limit": limit,
                "count": count,
                "remaining": max(0, limit - count),
                "full": count >= limit,
            }
        )
    return {
        "schema_version": SCHEMA,
        "enabled": bool(document.get("enabled")),
        "limits": document.get("limits", {}),
        "counts": counts,
        "capacity": capacity,
        "scope": "one local data root, current host, all registered projects, client family",
        "authority": "operator declaration plus local reservations; not native account authentication or quota",
        "reservations": rows,
    }


def enforce(data_root, client, *, exclude=(None, None)):
    document = load(data_root)
    limits = document.get("limits", {})
    if not document.get("enabled") or not limits:
        return None
    current = usage(data_root)
    for family in ("total", client):
        limit = limits.get(family)
        count = sum(
            (family == "total" or row.get("client") == family)
            and (row.get("project_id"), row.get("task_id")) != exclude
            for row in current["reservations"]
        )
        if limit is not None and count >= limit:
            raise ValueError(
                f"host-wide {family} dispatch capacity is full ({count}/{limit}); "
                "scope=current-host+all-projects. Wait for a live attempt or reconcile stale reservations."
            )
    return current
