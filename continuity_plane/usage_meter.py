"""Small local-only retrieval measurement; it never stores source text."""

from __future__ import annotations

import json
import os
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from .collaboration_registry import _locked, default_data_root


def _path(root: Path, data_root: Path | str | None = None) -> Path:
    base = Path(data_root) if data_root else default_data_root()
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:32]
    return base / "usage" / (key + ".jsonl")


def record(
    root: str | Path,
    *,
    command: str,
    response_bytes: int,
    estimated_grep_bytes: int,
    cache_status: str | None = None,
    injected_bytes: int = 0,
    session_id: str | None = None,
    data_root: Path | str | None = None,
    host: str | None = None,
) -> None:
    if os.environ.get("CONTINUITY_METER", "1") == "0":
        return
    path = _path(Path(root), data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    item: dict[str, Any] = {
        "schema_version": "context.retrieval-usage/v1alpha1",
        "observed_at": datetime.now(UTC).isoformat(),
        "command": command,
        "response_bytes": max(0, int(response_bytes)),
        "estimated_grep_bytes": max(0, int(estimated_grep_bytes)),
        "estimated_saved_bytes": int(estimated_grep_bytes)
        - int(response_bytes)
        - int(injected_bytes),
        "injected_bytes": max(0, int(injected_bytes)),
        "host": host,
        "session_sha256": hashlib.sha256(
            (session_id or os.environ.get("CODEX_SESSION_ID") or "cli").encode()
        ).hexdigest(),
        "cache_status": cache_status,
    }
    with _locked(path):
        if path.exists() and path.stat().st_size > 4 * 1024 * 1024:
            previous = path.read_bytes()[-2 * 1024 * 1024 :].partition(b"\n")[2]
            path.write_bytes(previous)
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")


def report(root: str | Path, *, data_root: Path | str | None = None) -> dict[str, Any]:
    path = _path(Path(root), data_root)
    rows = []
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            offset = max(0, stream.tell() - 4 * 1024 * 1024)
            stream.seek(offset)
            if offset:
                stream.readline()
            for line in stream:
                try:
                    item = json.loads(line)
                    if isinstance(item, dict) and all(
                        type(item.get(k, 0)) is int
                        for k in (
                            "response_bytes",
                            "estimated_grep_bytes",
                            "injected_bytes",
                        )
                    ):
                        if not isinstance(item.get("session_sha256"), str):
                            item["session_sha256"] = "legacy"
                        rows.append(item)
                except (ValueError, UnicodeError):
                    continue
    except (OSError, ValueError):
        pass
    snapshot_path = path.parent / (path.stem + ".bridge.json")
    try:
        snapshots = (
            json.loads(snapshot_path.read_text()) if snapshot_path.exists() else {}
        )
        rows.extend(
            {
                "command": "hook:ssh-bridge-output",
                "session_sha256": s["session_sha256"],
                "response_bytes": 0,
                "estimated_grep_bytes": 0,
                "injected_bytes": s["hook_injected_bytes"],
                "call_count": s["calls"],
            }
            for s in snapshots.values()
        )
    except (OSError, ValueError, TypeError, KeyError):
        pass

    def totals(items):
        response = sum(int(row.get("response_bytes", 0)) for row in items)
        baseline = sum(int(row.get("estimated_grep_bytes", 0)) for row in items)
        injected = sum(int(row.get("injected_bytes", 0)) for row in items)
        return {
            "calls": sum(r.get("call_count", 1) for r in items),
            "response_bytes": response,
            "estimated_grep_bytes": baseline,
            "hook_injected_bytes": injected,
            "estimated_saved_bytes": baseline - response - injected,
            "cache_hits": sum(row.get("cache_status") == "hit" for row in items),
        }

    return {
        "schema_version": "context.retrieval-usage-report/v1alpha1",
        **totals(rows),
        "by_command": {
            key: totals([r for r in rows if r.get("command", "unknown") == key])
            for key in sorted({r.get("command", "unknown") for r in rows})
        },
        "by_session": {
            key: totals([r for r in rows if r.get("session_sha256", "legacy") == key])
            for key in sorted({r.get("session_sha256", "legacy") for r in rows})
        },
        "measurement": "bytes; replacement baseline is an estimate, not measured token savings",
        "cache_hits_kind": "code-index cache; not provider prompt-cache tokens",
        "scope": "requested checkout root; existing negative estimates are retained",
    }


def import_hook_snapshot(root, snapshot, data_root=None):
    import re

    if (
        not isinstance(snapshot, dict)
        or not re.fullmatch(r"[0-9a-f]{64}", snapshot.get("session_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{32}", snapshot.get("instance_id", ""))
    ):
        raise ValueError("invalid bridge usage identity")
    if any(
        type(snapshot.get(k)) is not int or snapshot[k] < 0
        for k in ("calls", "hook_injected_bytes")
    ):
        raise ValueError("invalid bridge usage counters")
    path = _path(Path(root), data_root)
    path = path.parent / (path.stem + ".bridge.json")
    key = snapshot["session_sha256"] + ":" + snapshot["instance_id"]
    with _locked(path):
        rows = json.loads(path.read_text()) if path.exists() else {}
        previous = rows.get(key, {})
        rows[key] = {
            **snapshot,
            "calls": max(snapshot["calls"], previous.get("calls", 0)),
            "hook_injected_bytes": max(
                snapshot["hook_injected_bytes"], previous.get("hook_injected_bytes", 0)
            ),
        }
        from .collaboration_registry import _atomic_write

        _atomic_write(path, rows)


def adaptive_defaults(root, *, data_root=None, format="brief"):
    if format == "json" or os.environ.get("CONTINUITY_ADAPTIVE_BUDGET") == "0":
        return 20, 8192
    try:
        usage = report(root, data_root=data_root)
        rows = [
            v
            for k, v in usage["by_command"].items()
            if k.startswith(("context", "lookup", "search"))
        ]
        if (
            sum(r["calls"] for r in rows) >= 5
            and sum(r["estimated_saved_bytes"] for r in rows) < 0
        ):
            return 8, 2048
    except (OSError, ValueError, KeyError):
        pass
    return 20, 4096
