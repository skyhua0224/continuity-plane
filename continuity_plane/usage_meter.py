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

    def totals(items):
        response = sum(int(row.get("response_bytes", 0)) for row in items)
        baseline = sum(int(row.get("estimated_grep_bytes", 0)) for row in items)
        injected = sum(int(row.get("injected_bytes", 0)) for row in items)
        return {
            "calls": len(items),
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
    }
