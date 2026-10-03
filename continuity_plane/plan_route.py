"""Read-only MASTER routing hints, separate from State mutation authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_EXCERPT_BYTES = 2048


def _json(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > 128 * 1024:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def master_route(
    root: Path | str, *, master_path: str | None = None, section: str | None = None
) -> dict[str, Any]:
    """Resolve only explicit or registered sources; never guess from old Work IDs.

    External governance repositories are supported through the existing attach
    proposal or an explicit caller path. Markdown links are hints, not followed.
    """
    root = Path(root).resolve()
    proposal = _json(root / ".continuity/attach-proposal.json")
    source = next((entry for entry in proposal.get("sources", [])
                   if isinstance(entry, dict) and entry.get("kind") == "master"), {})
    configured = master_path or source.get("path")
    if not configured:
        try:
            profile_path = root / ".continuity/project.yaml"
            if profile_path.stat().st_size > 128 * 1024:
                raise ValueError("oversized profile")
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
            configured = profile.get("governance", {}).get("master_path")
        except (OSError, ValueError, AttributeError, yaml.YAMLError):
            configured = None
    result: dict[str, Any] = {
        "schema_version": "context.master-route/v1alpha1",
        "status": "unavailable",
        "root": str(root),
        "master_path": None,
        "section": section,
        "master_sha256": None,
        "source_changed_since_attach": None,
        "state_write_authority": False,
        "ordinary_project_work_allowed": True,
        "precedence": ["current-user-intent", "current-master", "todo-projection", "state-cursor"],
    }
    if not isinstance(configured, str) or not configured:
        return result
    path = Path(configured)
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    result["master_path"] = str(path)
    try:
        with path.open("rb") as handle:
            encoded = handle.read(MAX_SOURCE_BYTES + 1)
        if len(encoded) > MAX_SOURCE_BYTES:
            result["status"] = "oversized"
            return result
        content = encoded.decode("utf-8")
    except (OSError, UnicodeError):
        return result
    digest = hashlib.sha256(encoded).hexdigest()
    result["master_sha256"] = digest
    if not master_path or path == (root / str(source.get("path", ""))).resolve():
        attached = source.get("content_sha256")
        if isinstance(attached, str):
            result["source_changed_since_attach"] = digest != attached
    lines = content.splitlines()
    start = 0
    if section:
        matches = [index for index, line in enumerate(lines)
                   if line.lstrip().startswith("#") and line.lstrip("# ").strip() == section]
        if len(matches) != 1:
            result["status"] = "section-unresolved"
            return result
        start = matches[0]
    end = min(len(lines), start + 40)
    if section:
        level = len(lines[start]) - len(lines[start].lstrip("#"))
        for index in range(start + 1, end):
            line = lines[index]
            if line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
                end = index
                break
    excerpt = "\n".join(lines[start:end]).encode("utf-8")[:MAX_EXCERPT_BYTES].decode("utf-8", errors="ignore")
    result.update(status="current", start_line=start + 1, excerpt=excerpt,
                  excerpt_truncated=len("\n".join(lines[start:end]).encode("utf-8")) > MAX_EXCERPT_BYTES)
    return result


def route_context(route: dict[str, Any]) -> str:
    """A bounded source reference, not a replacement plan or permission grant."""
    prefix = (
        "MASTER defines goals, order and acceptance gates under current user intent. "
        "State Work/claim records ownership and effect authorization, not a permanent task order. "
        "A stale State cursor must not replay an old task or require idle binding for ordinary work. "
        "Do not mark old Work complete or close blockers to reconcile a plan. "
    )
    if route.get("status") != "current":
        return prefix + "MASTER route unavailable; preserve native continuation and explicit user intent."
    return prefix + (
        f"MASTER reference: {route['master_path']}:{route['start_line']} "
        f"sha256={route['master_sha256']}. "
        f"Changed since attach: {route['source_changed_since_attach']}. "
        "Read only the relevant current section when reconciling; reuse unchanged hash-bound references. "
        "Document HEAD is historical evidence; use Git HEAD in the actual worktree as the code baseline."
    )
