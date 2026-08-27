"""Resolve all Git worktrees to one local Continuity control root."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import yaml


BINDING_SCHEMA_VERSION = "context.git-workspace-binding/v1alpha1"


class WorkspaceBindingError(ValueError):
    """A Git repository has an invalid or ambiguous Continuity binding."""


def _git(root: Path, *arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _common_dir(root: Path) -> Path | None:
    value = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if not value:
        return None
    try:
        return Path(value).resolve()
    except OSError:
        return None


def _profile(root: Path) -> tuple[str, str] | None:
    path = root / ".continuity/project.yaml"
    try:
        content = path.read_bytes()
        document = yaml.safe_load(content)
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("project_id"), str):
        return None
    return document["project_id"], hashlib.sha256(content).hexdigest()


def _binding_path(common_dir: Path) -> Path:
    return common_dir / "continuity-plane/project-root.json"


def _binding_digest(document: dict[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "binding_sha256"}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def register_control_root(root: str | Path) -> Path:
    control_root = Path(root).resolve()
    common_dir = _common_dir(control_root)
    profile = _profile(control_root)
    if common_dir is None or profile is None:
        return control_root
    project_id, profile_sha256 = profile
    document = {
        "schema_version": BINDING_SCHEMA_VERSION,
        "project_id": project_id,
        "control_root": str(control_root),
        "profile_sha256": profile_sha256,
        "binding_sha256": "0" * 64,
    }
    document["binding_sha256"] = _binding_digest(document)
    target = _binding_path(common_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(document, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return control_root


def _bound_root(common_dir: Path) -> Path | None:
    path = _binding_path(common_dir)
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fields = {
        "schema_version",
        "project_id",
        "control_root",
        "profile_sha256",
        "binding_sha256",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document["schema_version"] != BINDING_SCHEMA_VERSION
        or document["binding_sha256"] != _binding_digest(document)
        or not isinstance(document["control_root"], str)
    ):
        return None
    control_root = Path(document["control_root"]).resolve()
    profile = _profile(control_root)
    if (
        profile is None
        or profile != (document["project_id"], document["profile_sha256"])
        or _common_dir(control_root) != common_dir
    ):
        return None
    return control_root


def _worktree_candidates(root: Path, common_dir: Path) -> list[Path]:
    output = _git(root, "worktree", "list", "--porcelain")
    if output is None:
        return []
    candidates: list[Path] = []
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        candidate = Path(line.removeprefix("worktree ")).resolve()
        if _profile(candidate) is not None and _common_dir(candidate) == common_dir:
            candidates.append(candidate)
    return sorted(set(candidates), key=str)


def resolve_control_root(root: str | Path) -> Path:
    requested = Path(root).resolve()
    common_dir = _common_dir(requested)
    if common_dir is None:
        return requested
    bound = _bound_root(common_dir)
    if bound is not None:
        return bound
    candidates = _worktree_candidates(requested, common_dir)
    if len(candidates) > 1:
        raise WorkspaceBindingError(
            "multiple Continuity roots share this Git repository; establish one binding"
        )
    if len(candidates) == 1:
        return register_control_root(candidates[0])
    return requested


__all__ = [
    "BINDING_SCHEMA_VERSION",
    "WorkspaceBindingError",
    "register_control_root",
    "resolve_control_root",
]
