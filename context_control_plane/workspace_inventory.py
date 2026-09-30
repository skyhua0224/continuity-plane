"""Read-only Git worktree inventory and cleanup classification."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_STALE_DAYS = 14


class WorkspaceInventoryError(ValueError):
    """The repository or worktree metadata cannot be inspected."""


@dataclass(frozen=True)
class WorktreeRecord:
    path: str
    branch: str | None
    detached: bool
    exists: bool
    age_days: float | None
    tracked_changes: int
    classification: str
    cleanup_safe: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "branch": self.branch,
            "detached": self.detached,
            "exists": self.exists,
            "age_days": None if self.age_days is None else round(self.age_days, 2),
            "tracked_changes": self.tracked_changes,
            "classification": self.classification,
            "cleanup_safe": self.cleanup_safe,
        }


def _git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WorkspaceInventoryError(f"git inventory failed: {exc}") from exc
    if completed.returncode != 0:
        raise WorkspaceInventoryError(completed.stderr.strip() or "git inventory failed")
    return completed.stdout


def _parse_porcelain(output: str) -> list[dict[str, str | None]]:
    records: list[dict[str, str | None]] = []
    current: dict[str, str | None] | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            if current is not None:
                records.append(current)
            current = {"path": line.removeprefix("worktree "), "branch": None, "detached": None}
        elif current is not None and line.startswith("branch "):
            current["branch"] = line.removeprefix("branch ")
        elif current is not None and line == "detached":
            current["detached"] = "true"
    if current is not None:
        records.append(current)
    return records


def inventory_worktrees(
    root: Path | str,
    *,
    stale_days: int = DEFAULT_STALE_DAYS,
) -> dict[str, Any]:
    """Classify worktrees without deleting, moving, or rewriting anything."""
    repository = Path(root).expanduser().resolve()
    if type(stale_days) is not int or stale_days < 1:
        raise WorkspaceInventoryError("stale_days must be positive")
    records = _parse_porcelain(_git(repository, "worktree", "list", "--porcelain"))
    result: list[WorktreeRecord] = []
    now = time.time()
    for item in records:
        path = Path(str(item["path"]))
        exists = path.is_dir()
        try:
            modified = path.stat().st_mtime
            age_days = max(0.0, (now - modified) / 86_400)
        except OSError:
            age_days = None
        tracked = 0
        if exists:
            status = _git(path, "status", "--porcelain=v1", "--untracked-files=no")
            tracked = sum(1 for line in status.splitlines() if line.strip())
        if not exists:
            classification = "missing"
            cleanup_safe = False
        elif tracked:
            classification = "dirty-hold"
            cleanup_safe = False
        elif age_days is not None and age_days >= stale_days:
            classification = "clean-stale"
            cleanup_safe = True
        else:
            classification = "clean-active-or-recent"
            cleanup_safe = False
        result.append(
            WorktreeRecord(
                path=str(path),
                branch=str(item["branch"]) if item["branch"] is not None else None,
                detached=item["detached"] == "true",
                exists=exists,
                age_days=age_days,
                tracked_changes=tracked,
                classification=classification,
                cleanup_safe=cleanup_safe,
            )
        )
    counts: dict[str, int] = {}
    for record in result:
        counts[record.classification] = counts.get(record.classification, 0) + 1
    return {
        "schema_version": "context.workspace-inventory/v1alpha1",
        "repository_root": str(repository),
        "dry_run": True,
        "stale_days": stale_days,
        "worktree_count": len(result),
        "classification_counts": counts,
        "cleanup_safe_count": sum(record.cleanup_safe for record in result),
        "worktrees": [record.as_dict() for record in result],
        "cleanup_policy": (
            "report-only; dirty and missing worktrees are never cleanup candidates"
        ),
    }
