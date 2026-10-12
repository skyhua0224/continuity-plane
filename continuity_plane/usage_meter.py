"""Small local-only retrieval measurement; it never stores source text."""

from __future__ import annotations

import json
import os
import tempfile
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _path(root: Path) -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:32]
    return base / "continuity" / "usage" / (key + ".jsonl")


def record(root: str | Path, *, command: str, response_bytes: int, estimated_grep_bytes: int,
           cache_status: str | None = None) -> None:
    path = _path(Path(root))
    path.parent.mkdir(parents=True, exist_ok=True)
    item: dict[str, Any] = {
        "schema_version": "context.retrieval-usage/v1alpha1",
        "observed_at": datetime.now(UTC).isoformat(),
        "command": command,
        "response_bytes": max(0, int(response_bytes)),
        "estimated_grep_bytes": max(0, int(estimated_grep_bytes)),
        "estimated_saved_bytes": max(0, int(estimated_grep_bytes) - int(response_bytes)),
        "cache_status": cache_status,
    }
    fd, temporary = tempfile.mkstemp(prefix=".usage-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            if path.exists():
                out.write(path.read_text(encoding="utf-8"))
            out.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def report(root: str | Path) -> dict[str, Any]:
    path = _path(Path(root))
    rows = []
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()[-1000:]]
    except (OSError, ValueError):
        pass
    return {
        "schema_version": "context.retrieval-usage-report/v1alpha1",
        "calls": len(rows),
        "response_bytes": sum(int(row.get("response_bytes", 0)) for row in rows),
        "estimated_grep_bytes": sum(int(row.get("estimated_grep_bytes", 0)) for row in rows),
        "estimated_saved_bytes": sum(int(row.get("estimated_saved_bytes", 0)) for row in rows),
        "cache_hits": sum(row.get("cache_status") == "hit" for row in rows),
    }
