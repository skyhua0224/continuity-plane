"""Registered storage locations, tier configuration, reporting, and safe GC."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from urllib.request import url2pathname

import yaml

from .collaboration_registry import (
    _atomic_write,
    _locked,
    _load,
    default_data_root,
    list_tasks,
    project_data_dir,
)
from .persistent_storage import describe


STORAGE_LOCATION_SCHEMA = "context.storage-location/v1alpha1"
STORAGE_REGISTRY_SCHEMA = "context.storage-registry/v1alpha1"
STORAGE_TIERS_SCHEMA = "context.storage-tiers/v1alpha1"
STORAGE_REPORT_SCHEMA = "context.storage-report/v1alpha1"
DEFAULT_MACHINE_ID = socket.gethostname()
DEFAULT_JOB_OUTPUT_TAIL_BYTES = 64 * 1024
HASH_CHUNK_BYTES = 1024 * 1024
TERMINAL_STATUSES = {"completed", "merged", "superseded"}
KINDS = {"workspace", "scratch", "evidence", "package", "job"}
RETENTION_CLASSES = {"scratch", "evidence", "package"}
CREATORS = {"dispatch", "delegate", "operator"}
EXIT_OK = 0
EXIT_INVALID_CONFIG = 10
EXIT_STALE_REGISTRY = 11
EXIT_BLOCKED = 12
EXIT_MOVER = 13
EXIT_RECOVERY = 14


class StorageLifecycleError(ValueError):
    """A storage lifecycle request is invalid or cannot be safely completed."""

    def __init__(self, message: str, *, code: int = EXIT_INVALID_CONFIG):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _operation_id(value: str | None) -> str:
    if value is None:
        return "storage-" + uuid.uuid4().hex
    if not isinstance(value, str) or not value or len(value) > 128:
        raise StorageLifecycleError("operation_id is invalid")
    if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for char in value):
        raise StorageLifecycleError("operation_id contains invalid characters")
    return value


def _storage_dir(data_root: Path | str | None, project_id: str) -> Path:
    return project_data_dir(data_root or default_data_root(), project_id) / "storage"


def _registry_path(data_root: Path | str | None, project_id: str) -> Path:
    return _storage_dir(data_root, project_id) / "registry.json"


def _journal_path(data_root: Path | str | None, project_id: str) -> Path:
    return _storage_dir(data_root, project_id) / "journal.jsonl"


def _empty_registry(project_id: str) -> dict[str, Any]:
    return {
        "schema_version": STORAGE_REGISTRY_SCHEMA,
        "project_id": project_id,
        "revision": 0,
        "locations": [],
        "journal_head": None,
    }


def _validate_location(location: Any) -> dict[str, Any]:
    if not isinstance(location, dict):
        raise StorageLifecycleError("location is invalid")
    required = {
        "location_id",
        "project_id",
        "task_id",
        "machine_id",
        "uri",
        "location_kind",
        "retention_class",
        "created_by",
        "created_at",
        "last_card_update_at",
        "last_job_activity_at",
        "last_observed_at",
        "size_bytes",
        "content_sha256",
        "tier",
        "git",
        "state",
        "registry_revision",
    }
    if set(location) != required | ({"package_name"} if "package_name" in location else set()):
        raise StorageLifecycleError("location fields are invalid")
    if not isinstance(location["location_id"], str) or not location["location_id"]:
        raise StorageLifecycleError("location_id is invalid")
    if not isinstance(location["project_id"], str) or not location["project_id"]:
        raise StorageLifecycleError("project_id is invalid")
    if not isinstance(location["task_id"], str) or not location["task_id"]:
        raise StorageLifecycleError("task_id is invalid")
    if not isinstance(location["machine_id"], str) or not location["machine_id"]:
        raise StorageLifecycleError("machine_id is invalid")
    if location["location_kind"] not in KINDS:
        raise StorageLifecycleError("location_kind is invalid")
    if location["retention_class"] not in RETENTION_CLASSES:
        raise StorageLifecycleError("retention_class is invalid")
    if location["location_kind"] == "job" and location["retention_class"] != "scratch":
        raise StorageLifecycleError("job locations must use scratch retention")
    if location["location_kind"] == "package" and not location.get("package_name"):
        raise StorageLifecycleError("package_name is required for package locations")
    if location["created_by"] not in CREATORS:
        raise StorageLifecycleError("created_by is invalid")
    if not isinstance(location["uri"], str) or not location["uri"].startswith("file://"):
        raise StorageLifecycleError("stage 1 locations must use file:// URIs")
    if location["size_bytes"] is not None and (
        type(location["size_bytes"]) is not int or location["size_bytes"] < 0
    ):
        raise StorageLifecycleError("size_bytes is invalid")
    if location["content_sha256"] is not None and (
        not isinstance(location["content_sha256"], str)
        or len(location["content_sha256"]) != 64
        or any(char not in "0123456789abcdef" for char in location["content_sha256"])
    ):
        raise StorageLifecycleError("content_sha256 is invalid")
    if type(location["registry_revision"]) is not int or location["registry_revision"] < 1:
        raise StorageLifecycleError("registry_revision is invalid")
    if not isinstance(location["git"], dict):
        raise StorageLifecycleError("git state is invalid")
    return location


def _validate_registry(registry: Any, project_id: str) -> dict[str, Any]:
    if not isinstance(registry, dict) or set(registry) != {
        "schema_version",
        "project_id",
        "revision",
        "locations",
        "journal_head",
    }:
        raise StorageLifecycleError("storage registry fields are invalid", code=EXIT_RECOVERY)
    if registry["schema_version"] != STORAGE_REGISTRY_SCHEMA or registry["project_id"] != project_id:
        raise StorageLifecycleError("storage registry schema or project is invalid", code=EXIT_RECOVERY)
    if type(registry["revision"]) is not int or registry["revision"] < 0:
        raise StorageLifecycleError("storage registry revision is invalid", code=EXIT_RECOVERY)
    if not isinstance(registry["locations"], list):
        raise StorageLifecycleError("storage registry locations are invalid", code=EXIT_RECOVERY)
    for location in registry["locations"]:
        _validate_location(location)
    return registry


def _read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict) or not isinstance(event.get("location"), (dict, type(None))):
                    raise StorageLifecycleError("storage journal event is invalid", code=EXIT_RECOVERY)
                events.append(event)
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageLifecycleError("storage journal is unreadable", code=EXIT_RECOVERY) from exc
    return events


def _replay(project_id: str, events: list[dict[str, Any]]) -> dict[str, Any]:
    registry = _empty_registry(project_id)
    expected_revision = 0
    for event in events:
        if event.get("project_id") != project_id or event.get("schema_version") != STORAGE_REGISTRY_SCHEMA:
            raise StorageLifecycleError("storage journal project or schema is invalid", code=EXIT_RECOVERY)
        revision = event.get("registry_revision")
        if type(revision) is not int or revision < expected_revision:
            raise StorageLifecycleError("storage journal revision is invalid", code=EXIT_RECOVERY)
        if revision > expected_revision + 1:
            raise StorageLifecycleError("storage journal has a revision gap", code=EXIT_RECOVERY)
        location = event.get("location")
        if location is not None:
            _validate_location(location)
            existing = next(
                (index for index, item in enumerate(registry["locations"]) if item["location_id"] == location["location_id"]),
                None,
            )
            if existing is None:
                registry["locations"].append(location)
            else:
                registry["locations"][existing] = location
        registry["revision"] = revision
        registry["journal_head"] = event.get("operation_id")
        expected_revision = revision
    return _validate_registry(registry, project_id)


def load_registry(data_root: Path | str | None, project_id: str) -> dict[str, Any]:
    registry_path = _registry_path(data_root, project_id)
    journal_path = _journal_path(data_root, project_id)
    needs_snapshot = False
    try:
        registry = _load(registry_path)
        if registry is not None:
            return _validate_registry(registry, project_id)
    except Exception:
        registry = None
        needs_snapshot = True
    events = _read_journal(journal_path)
    if needs_snapshot and not events:
        raise StorageLifecycleError("storage registry is corrupt and has no replay journal", code=EXIT_RECOVERY)
    replayed = _replay(project_id, events)
    if events and (needs_snapshot or not registry_path.exists()):
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(registry_path, replayed)
    return replayed


def _append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    from .persistent_storage import sync_directory

    sync_directory(path.parent)


def _commit_registry(
    data_root: Path | str | None,
    project_id: str,
    registry: dict[str, Any],
    *,
    operation_id: str,
    action: str,
    phase: str,
    request_sha256: str,
    location_id: str | None = None,
) -> None:
    event = {
        "schema_version": STORAGE_REGISTRY_SCHEMA,
        "project_id": project_id,
        "operation_id": operation_id,
        "action": action,
        "phase": phase,
        "registry_revision": registry["revision"],
        "location_id": location_id,
        "request_sha256": request_sha256,
        "recorded_at": _now(),
        "location": next(
            (item for item in registry["locations"] if item["location_id"] == location_id),
            None,
        ),
    }
    _append_event(_journal_path(data_root, project_id), event)
    _atomic_write(_registry_path(data_root, project_id), registry)


def _existing_operation(data_root: Path | str | None, project_id: str, operation_id: str) -> dict[str, Any] | None:
    for event in _read_journal(_journal_path(data_root, project_id)):
        if event.get("operation_id") == operation_id:
            return event
    return None


def _path_from_uri(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise StorageLifecycleError("stage 1 supports local file URIs only")
    # url2pathname turns /C:/work into C:\work on Windows and unquotes on POSIX.
    return Path(url2pathname(parsed.path)).expanduser()


def path_to_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _normal_path(path: Path) -> Path:
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise StorageLifecycleError(f"location does not exist: {path}") from exc


def _contains(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
        return True
    except ValueError:
        return False


def _git(path: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    if check and result.returncode:
        raise StorageLifecycleError(result.stderr.strip() or "git inspection failed", code=EXIT_BLOCKED)
    return result


def _worktree_records(repository: Path) -> list[dict[str, Any]]:
    result = _git(repository, "worktree", "list", "--porcelain")
    if result.returncode:
        return []
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current is not None:
                records.append(current)
            current = {"path": str(Path(line[9:]).resolve()), "locked": False}
        elif current is not None and line.startswith("branch "):
            current["branch"] = line[7:]
        elif current is not None and line == "detached":
            current["detached"] = True
        elif current is not None and line.startswith("locked"):
            current["locked"] = True
    if current is not None:
        records.append(current)
    return records


def _reachable_from_refs(repository: Path, commit: str, refs: Iterable[str]) -> bool:
    for ref in refs:
        if _git(repository, "merge-base", "--is-ancestor", commit, ref).returncode == 0:
            return True
    return False


def _git_state(path: Path) -> dict[str, Any]:
    state: dict[str, Any] = {
        "topology": "none",
        "kind": "none",
        "repository_root": None,
        "worktree_root": None,
        "branch": None,
        "head": None,
        "upstream": None,
        "dirty": False,
        "untracked_files": 0,
        "unpushed_commits": 0,
        "in_progress_operation": "none",
        "locked": False,
        "detached_head_reachable": True,
        "origin_refs_complete": True,
        "last_observed_at": _now(),
        "observation_status": "clean",
    }
    probe = _git(path, "rev-parse", "--show-toplevel")
    if probe.returncode:
        return state
    top_level = Path(probe.stdout.strip()).resolve()
    repository = top_level
    common = _git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        common = _git(path, "rev-parse", "--git-common-dir")
    if common.returncode == 0:
        common_dir = Path(common.stdout.strip()).resolve()
        if common_dir.name == ".git":
            repository = common_dir.parent
    state["repository_root"] = str(repository)
    state["worktree_root"] = str(top_level)
    if path != top_level:
        # A directory inside someone else's repository (a dotfiles home, a build
        # dir in a checkout) is safe to remove only when Git tracks nothing in it.
        tracked = _git(top_level, "ls-files", "-z", "--", str(path))
        if tracked.returncode:
            state["observation_status"] = "unknown"
            return state
        if tracked.stdout:
            state["topology"] = "inside-git-tree"
            state["kind"] = "worktree" if (top_level / ".git").is_file() else "repository"
            state["observation_status"] = "inside-git-tree"
            return state
        state["topology"] = "untracked-in-repository"
        state["repository_root"] = None
        state["worktree_root"] = None
        return state
    state["kind"] = "worktree" if (path / ".git").is_file() else "repository"
    records = _worktree_records(repository)
    linked = any(record["path"] == str(path) for record in records) and (path / ".git").is_file()
    state["topology"] = "linked-worktree" if linked else "standalone-clone"
    state["locked"] = any(record["path"] == str(path) and record.get("locked") for record in records)
    status = _git(path, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode:
        state["observation_status"] = "unknown"
        return state
    status_lines = [line for line in status.stdout.splitlines() if line]
    state["dirty"] = any(not line.startswith("?? ") for line in status_lines)
    state["untracked_files"] = sum(line.startswith("?? ") for line in status_lines)
    git_dir_result = _git(path, "rev-parse", "--git-dir")
    git_dir = Path(git_dir_result.stdout.strip()).resolve() if git_dir_result.returncode == 0 else None
    for marker, operation in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge"), ("CHERRY_PICK_HEAD", "cherry-pick"), ("REVERT_HEAD", "revert")):
        if git_dir is not None and (git_dir / marker).exists():
            state["in_progress_operation"] = operation
            break
    head_result = _git(path, "rev-parse", "HEAD")
    if head_result.returncode:
        state["observation_status"] = "unknown"
        return state
    state["head"] = head_result.stdout.strip()
    branch_result = _git(path, "symbolic-ref", "-q", "HEAD")
    if branch_result.returncode == 0:
        state["branch"] = branch_result.stdout.strip()
    upstream_result = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if upstream_result.returncode == 0:
        state["upstream"] = upstream_result.stdout.strip()
        ahead = _git(path, "rev-list", "--count", "@{upstream}..HEAD")
        if ahead.returncode == 0:
            state["unpushed_commits"] = int(ahead.stdout.strip() or 0)
    if state["branch"] is None:
        refs = [line.split()[0] for line in _git(path, "for-each-ref", "--format=%(refname)").stdout.splitlines() if line]
        state["detached_head_reachable"] = _reachable_from_refs(path, state["head"], refs)
    if state["topology"] == "standalone-clone":
        origin_refs = [
            line.split()[0]
            for line in _git(path, "for-each-ref", "--format=%(refname)", "refs/remotes/origin", "refs/tags").stdout.splitlines()
            if line
        ]
        state["origin_refs_complete"] = bool(origin_refs and not _git(path, "remote", "get-url", "origin").returncode)
        local_refs = [line.split()[1] for line in _git(path, "show-ref").stdout.splitlines() if len(line.split()) == 2]
        if state["origin_refs_complete"]:
            state["unpushed_commits"] = sum(
                1 for commit in local_refs if not _reachable_from_refs(path, commit, origin_refs)
            )
    if state["dirty"] or state["untracked_files"]:
        state["observation_status"] = "dirty"
    elif state["in_progress_operation"] != "none":
        state["observation_status"] = "in-progress"
    elif state["locked"]:
        state["observation_status"] = "locked"
    elif not state["detached_head_reachable"] or not state["origin_refs_complete"] or state["unpushed_commits"]:
        state["observation_status"] = "unpushed"
    return state


def _tree_digest(path: Path, *, hash_content: bool) -> tuple[int, str | None]:
    digest = hashlib.sha256()
    total = 0
    if path.is_file() or path.is_symlink():
        info = path.lstat()
        if not hash_content or not stat.S_ISREG(info.st_mode):
            return info.st_size, None
        with path.open("rb") as stream:
            while chunk := stream.read(HASH_CHUNK_BYTES):
                total += len(chunk)
                digest.update(chunk)
        return total, digest.hexdigest()
    try:
        for directory, directories, files in os.walk(path, followlinks=False):
            directories[:] = sorted(item for item in directories if not Path(directory, item).is_symlink())
            for name in sorted(files):
                item = Path(directory) / name
                info = item.lstat()
                total += info.st_size
                if not hash_content or not stat.S_ISREG(info.st_mode):
                    continue
                relative = item.relative_to(path).as_posix().encode()
                digest.update(relative + b"\0" + info.st_size.to_bytes(8, "big"))
                with item.open("rb") as stream:
                    while chunk := stream.read(HASH_CHUNK_BYTES):
                        digest.update(chunk)
    except OSError:
        return None, None
    return total, digest.hexdigest() if hash_content else None


def _parse_uri_root(value: str) -> Path | None:
    if not isinstance(value, str):
        return None
    if value.startswith("${"):
        return None
    if value.startswith("file://"):
        return _path_from_uri(value)
    path = Path(value).expanduser()
    return path if path.is_absolute() else None


def _default_config() -> dict[str, Any]:
    from importlib import resources

    content = resources.files("continuity_plane.templates").joinpath("storage-tiers.yaml").read_text(encoding="utf-8")
    return yaml.safe_load(content)


TIER_CONFIG_FIELDS = ("schema_version", "project_id", "archive_tier", "machines", "tiers", "mover", "retention", "capacity", "automation")
TIER_CONFIG_REQUIRED = ("schema_version", "machines", "tiers")


def _with_defaults(document: dict[str, Any]) -> dict[str, Any]:
    """A project config names machines and tiers; omitted sections take the shipped defaults."""
    merged = _default_config()
    for key, value in document.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict) and key not in ("machines", "tiers"):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def validate_tier_config(document: Any, *, shipped: bool = False) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise StorageLifecycleError("storage tier config must be a mapping", code=EXIT_INVALID_CONFIG)
    unknown = sorted(set(document) - set(TIER_CONFIG_FIELDS))
    if unknown:
        raise StorageLifecycleError(
            f"storage tier config has unknown fields: {', '.join(unknown)} (allowed: {', '.join(TIER_CONFIG_FIELDS)})",
            code=EXIT_INVALID_CONFIG,
        )
    missing = [name for name in TIER_CONFIG_REQUIRED if name not in document]
    if missing:
        raise StorageLifecycleError(
            f"storage tier config is missing: {', '.join(missing)}", code=EXIT_INVALID_CONFIG
        )
    if not shipped:
        document = _with_defaults(document)
    if document["schema_version"] != STORAGE_TIERS_SCHEMA:
        raise StorageLifecycleError("storage tier config schema is unsupported")
    for name in ("machines", "tiers"):
        if not isinstance(document[name], dict):
            raise StorageLifecycleError(f"storage tier config {name} is invalid")
    if not isinstance(document["automation"], dict) or set(document["automation"]) != {"auto_apply", "grace_period", "daily_report"}:
        raise StorageLifecycleError("storage automation config is invalid")
    if not isinstance(document["automation"]["auto_apply"], list) or any(value != "scratch" for value in document["automation"]["auto_apply"]):
        raise StorageLifecycleError("stage 1 automation may opt into scratch only")
    if shipped and document["automation"]["auto_apply"] != []:
        raise StorageLifecycleError("shipped storage config must not auto-apply")
    if not isinstance(document["retention"], dict) or type(document["retention"].get("idle_days")) is not int or document["retention"]["idle_days"] < 1:
        raise StorageLifecycleError("retention idle_days is invalid")
    if type(document["retention"].get("package_keep_last")) is not int or document["retention"]["package_keep_last"] < 1:
        raise StorageLifecycleError("retention package_keep_last is invalid")
    if not isinstance(document["capacity"], dict):
        raise StorageLifecycleError("capacity config is invalid")
    watermark = document["capacity"].get("high_watermark_fraction")
    if not isinstance(watermark, (int, float)) or not 0 < watermark <= 1:
        raise StorageLifecycleError("capacity high_watermark_fraction is invalid")
    for tier_name, tier in document["tiers"].items():
        if not isinstance(tier, dict) or not isinstance(tier.get("declared_roots"), list):
            raise StorageLifecycleError(f"tier {tier_name} needs a declared_roots list")
        if shipped and any(
            isinstance(root, str) and (root.startswith("/") or "://" in root or "${" not in root)
            for root in tier["declared_roots"]
        ):
            raise StorageLifecycleError("shipped storage config contains a concrete root")
    for machine_id, machine in document["machines"].items():
        if not isinstance(machine, dict) or not isinstance(machine.get("local_tiers"), list):
            raise StorageLifecycleError(f"machine {machine_id} needs a local_tiers list")
        undefined = [name for name in machine["local_tiers"] if name not in document["tiers"]]
        if undefined:
            raise StorageLifecycleError(
                f"machine {machine_id} uses undefined tiers: {', '.join(map(str, undefined))}",
                code=EXIT_INVALID_CONFIG,
            )
        if "ssh_host_ref" in machine and not isinstance(machine["ssh_host_ref"], str):
            raise StorageLifecycleError(f"machine {machine_id} ssh_host_ref is invalid")
    return document


def load_tier_config(path: Path | str | None = None) -> dict[str, Any]:
    if path is None:
        return validate_tier_config(_default_config(), shipped=True)
    config_path = Path(path).expanduser()
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise StorageLifecycleError(f"storage tier config cannot be read: {config_path}") from exc
    return validate_tier_config(document)


def _roots_for(config: dict[str, Any], machine_id: str) -> list[tuple[str, Path]]:
    machine = config["machines"].get(machine_id)
    if machine is None:
        if not config["machines"]:
            return []
        raise StorageLifecycleError(f"machine is not declared: {machine_id}")
    names = list(machine.get("local_tiers", [])) + list(machine.get("archive_access", []))
    roots: list[tuple[str, Path]] = []
    for name in dict.fromkeys(names):
        tier = config["tiers"].get(name)
        if tier is None:
            raise StorageLifecycleError(f"machine references unknown tier: {name}")
        for value in tier.get("declared_roots", []):
            root = _parse_uri_root(value)
            if root is not None:
                if root.is_symlink():
                    raise StorageLifecycleError(f"declared root is a symlink: {root}")
                roots.append((name, root.expanduser().resolve()))
    return roots


def _tier_capacity(config: dict[str, Any], machine_id: str, tier_name: str, root: Path) -> dict[str, Any]:
    source = "statvfs"
    try:
        if hasattr(os, "statvfs"):
            stats = os.statvfs(root)
            total = stats.f_blocks * stats.f_frsize
            free = stats.f_bavail * stats.f_frsize
            used = total - stats.f_bfree * stats.f_frsize
        else:  # Windows has no statvfs; disk_usage reports the volume.
            usage = shutil.disk_usage(root)
            total, free, used = usage.total, usage.free, usage.used
            source = "disk_usage"
    except OSError:
        total = free = used = None
    observation = describe(root)
    watermark = float(config["capacity"]["high_watermark_fraction"])
    return {
        "name": tier_name,
        "machine_id": machine_id,
        "used_bytes": used,
        "free_bytes": free,
        "quota_bytes": total,
        "quota_source": source if total is not None else "unknown",
        "memory_backed": observation["memory_backed"],
        "temporary_location": observation["temporary_location"],
        "high_watermark": bool(total and used / total >= watermark),
        "capacity_observed_at": _now(),
    }


def _task_map(data_root: Path | str | None, project_id: str) -> dict[str, dict[str, Any]]:
    try:
        return {task["task_id"]: task for task in list_tasks(data_root, project_id=project_id)}
    except Exception:
        return {}


def _location_request_hash(location: dict[str, Any]) -> str:
    return _sha256(json.dumps(location, ensure_ascii=False, sort_keys=True).encode())


def register_location(
    data_root: Path | str | None,
    *,
    project_id: str,
    task_id: str,
    machine_id: str,
    uri: str,
    location_kind: str,
    retention_class: str,
    created_by: str = "operator",
    tier: str,
    package_name: str | None = None,
    operation_id: str | None = None,
    config: dict[str, Any] | None = None,
    adopt: bool = False,
) -> dict[str, Any]:
    if config is None:
        config = load_tier_config()
    if config.get("project_id") not in {None, "", "${PROJECT_ID}", project_id}:
        raise StorageLifecycleError("tier config project_id does not match project")
    operation_id = _operation_id(operation_id)
    path = _normal_path(_path_from_uri(uri))
    roots = _roots_for(config, machine_id)
    tier_roots = [root for name, root in roots if name == tier]
    if not tier_roots:
        raise StorageLifecycleError("tier has no local declared root")
    if not any(_contains(root, path) and path != root for root in tier_roots):
        raise StorageLifecycleError("location is outside declared tier roots", code=EXIT_BLOCKED)
    capacity = _tier_capacity(config, machine_id, tier, tier_roots[0])
    if location_kind in {"evidence", "package"}:
        minimum = config["capacity"].get(
            "evidence_min_free_bytes" if location_kind == "evidence" else "package_min_free_bytes"
        )
        if capacity["memory_backed"] or (
            isinstance(minimum, int)
            and (capacity["free_bytes"] is None or capacity["free_bytes"] < minimum)
        ):
            raise StorageLifecycleError("evidence/package location does not meet tier capacity safety", code=EXIT_BLOCKED)
    if not adopt and location_kind not in KINDS:
        raise StorageLifecycleError("location kind is invalid")
    git = _git_state(path)
    size, digest = _tree_digest(path, hash_content=location_kind in {"evidence", "package"})
    tasks = _task_map(data_root, project_id)
    task = tasks.get(task_id)
    now = _now()
    location: dict[str, Any] = {
        "location_id": "loc-" + uuid.uuid4().hex,
        "project_id": project_id,
        "task_id": task_id,
        "machine_id": machine_id,
        "uri": path_to_uri(path),
        "location_kind": location_kind,
        "retention_class": retention_class,
        "created_by": created_by if not adopt else "operator",
        "created_at": now,
        "last_card_update_at": task.get("updated_at", now) if task else now,
        "last_job_activity_at": now,
        "last_observed_at": now,
        "size_bytes": size,
        "content_sha256": digest,
        "tier": tier,
        "git": git,
        "state": "registered" if not adopt else "flagged",
        "registry_revision": 1,
    }
    if package_name is not None:
        location["package_name"] = package_name
    _validate_location(location)
    request_hash = _sha256(
        json.dumps(
            {
                "project_id": project_id,
                "task_id": task_id,
                "machine_id": machine_id,
                "uri": path_to_uri(path),
                "location_kind": location_kind,
                "retention_class": retention_class,
                "created_by": created_by if not adopt else "operator",
                "tier": tier,
                "package_name": package_name,
                "adopt": adopt,
            },
            sort_keys=True,
        ).encode()
    )
    registry_path = _registry_path(data_root, project_id)
    with _locked(registry_path):
        existing = _existing_operation(data_root, project_id, operation_id)
        if existing is not None:
            if existing.get("request_sha256") != request_hash:
                raise StorageLifecycleError("operation_id was reused with different content")
            current = load_registry(data_root, project_id)
            return next(item for item in current["locations"] if item["location_id"] == existing.get("location_id"))
        registry = load_registry(data_root, project_id)
        if any(item["uri"] == location["uri"] and item["task_id"] == task_id for item in registry["locations"]):
            raise StorageLifecycleError("location is already registered")
        registry["revision"] += 1
        location["registry_revision"] = registry["revision"]
        registry["locations"].append(location)
        registry["journal_head"] = operation_id
        _validate_registry(registry, project_id)
        _commit_registry(data_root, project_id, registry, operation_id=operation_id, action="register", phase="committed", request_sha256=request_hash, location_id=location["location_id"])
    return location


def _card_matches(location: dict[str, Any], tasks: dict[str, dict[str, Any]], now: datetime, idle_days: int) -> tuple[bool, str]:
    task = tasks.get(location["task_id"])
    if task is None:
        return False, "card-not-found"
    if task.get("status") in TERMINAL_STATUSES:
        return True, f"card-{task['status']}"
    if task.get("status") in {"active", "dispatched"}:
        return False, "card-active"
    values = [location.get("last_card_update_at"), location.get("last_job_activity_at"), task.get("updated_at")]
    try:
        latest = max(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in values if value)
    except (TypeError, ValueError):
        return False, "activity-unknown"
    if now - latest >= timedelta(days=idle_days):
        return True, "idle"
    return False, "card-active"


def _running_tasks(data_root: Path | str | None, project_id: str) -> set[str]:
    """Cards with a live worker job. The index only points at job directories;
    each job's own job.json carries its status."""
    project_dir = project_data_dir(data_root or default_data_root(), project_id)
    try:
        jobs = json.loads((project_dir / "worker-jobs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    if not isinstance(jobs, dict):
        return set()
    running = set()
    for task_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        status = job.get("job_status") or job.get("status")
        job_dir = job.get("job_dir") or (
            str(project_dir / "worker-jobs" / job["job_id"]) if job.get("job_id") else None
        )
        if job_dir:
            try:
                record = json.loads((Path(job_dir) / "job.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                record = {}
            if isinstance(record, dict):
                status = record.get("job_status") or record.get("status") or status
        if status in {"queued", "dispatched", "running"}:
            running.add(task_id)
    return running


def _job_finished(path: Path) -> bool:
    if (path / "result.json").is_file():
        return True
    try:
        job = json.loads((path / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(job, dict) and job.get("status") in {"completed", "failed", "cancelled", "finished"}


def _orphan_suggestion(path: Path, git: dict[str, Any], tasks: dict[str, dict[str, Any]]) -> tuple[str | None, str | None]:
    for task_id, task in tasks.items():
        worktree = task.get("worktree")
        branch = task.get("branch")
        if worktree and _contains(Path(worktree).expanduser().resolve(), path):
            return task_id, "path-under-card-worktree"
        if branch and branch == git.get("branch"):
            return task_id, "branch-match"
    return None, None


def _find_orphans(registry: dict[str, Any], roots: list[tuple[str, Path]], tasks: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    registered = [_path_from_uri(item["uri"]) for item in registry["locations"]]
    output: list[dict[str, Any]] = []
    for tier, root in roots:
        if not root.exists():
            continue
        for candidate in sorted(root.iterdir(), key=lambda item: item.name):
            if candidate.is_symlink() or not candidate.exists() or any(_contains(item, candidate) for item in registered):
                continue
            if any(_contains(candidate, item) for item in registered):
                continue
            git = _git_state(candidate)
            suggestion, reason = _orphan_suggestion(candidate, git, tasks)
            output.append({
                "tier": tier,
                "uri": path_to_uri(candidate),
                "suggested_task_id": suggestion,
                "suggestion_reason": reason,
                "decision": "report-only",
            })
    return output


def report_storage(
    data_root: Path | str | None,
    *,
    project_id: str,
    machine_id: str | None = None,
    task_id: str | None = None,
    config: dict[str, Any] | None = None,
    include_orphans: bool = False,
) -> dict[str, Any]:
    config = config or load_tier_config()
    registry = load_registry(data_root, project_id)
    tasks = _task_map(data_root, project_id)
    roots = _roots_for(config, machine_id or next(iter(config["machines"]), DEFAULT_MACHINE_ID))
    locations = [
        item for item in registry["locations"]
        if (machine_id is None or item["machine_id"] == machine_id)
        and (task_id is None or item["task_id"] == task_id)
    ]
    capacities: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for tier, root in roots:
        key = (tier, str(root))
        if key not in seen:
            capacities.append(_tier_capacity(config, machine_id or DEFAULT_MACHINE_ID, tier, root))
            seen.add(key)
    high_water = {item["name"] for item in capacities if item["high_watermark"]}
    terminal_scratch = {
        item["location_id"]
        for item in locations
        if item["retention_class"] == "scratch"
        and tasks.get(item["task_id"], {}).get("status") in TERMINAL_STATUSES
        and item["tier"] in high_water
    }
    locations.sort(
        key=lambda item: (
            0 if item["location_id"] in terminal_scratch else 1,
            -int(item.get("size_bytes") or 0),
            item["location_id"],
        )
    )
    result: dict[str, Any] = {
        "schema_version": STORAGE_REPORT_SCHEMA,
        "project_id": project_id,
        "registry_revision": registry["revision"],
        "dry_run": True,
        "locations": locations,
        "tiers": capacities,
        "high_watermark_tiers": sorted(high_water),
        "counts": {
            "registered": len(locations),
            "blocked": sum(item["state"] == "blocked" for item in locations),
            "flagged": sum(item["state"] == "flagged" for item in locations),
        },
    }
    if include_orphans:
        orphans = _find_orphans(registry, roots, tasks)
        result["orphans"] = orphans
        result["counts"]["orphaned"] = len(orphans)
    return result


def _safe_for_removal(location: dict[str, Any]) -> tuple[bool, str]:
    git = location["git"]
    if git.get("topology") == "inside-git-tree":
        return False, "inside-git-tree"
    if git.get("observation_status") == "unknown":
        return False, "git-state-unknown"
    if git.get("dirty") or git.get("untracked_files"):
        return False, "dirty-or-untracked"
    if git.get("in_progress_operation") != "none":
        return False, "git-operation-in-progress"
    if git.get("locked"):
        return False, "worktree-locked"
    if git.get("topology") == "linked-worktree" and not git.get("detached_head_reachable", True):
        return False, "detached-head-unreachable"
    if git.get("topology") == "standalone-clone" and (
        not git.get("origin_refs_complete") or git.get("unpushed_commits")
    ):
        return False, "standalone-local-only-ref"
    return True, "safe"


def _remove_path(location: dict[str, Any]) -> None:
    path = _path_from_uri(location["uri"])
    git = location["git"]
    if git.get("topology") == "linked-worktree":
        repository = Path(git["repository_root"])
        result = _git(repository, "worktree", "remove", str(path))
        if result.returncode:
            raise StorageLifecycleError(result.stderr.strip() or "git worktree remove failed", code=EXIT_BLOCKED)
        prune = _git(repository, "worktree", "prune")
        if prune.returncode:
            raise StorageLifecycleError(prune.stderr.strip() or "git worktree prune failed", code=EXIT_BLOCKED)
    elif path.exists():
        shutil.rmtree(path)


def _trim_job_output(location: dict[str, Any], tail_bytes: int) -> bool:
    path = _path_from_uri(location["uri"]) / "output.jsonl"
    if not path.is_file():
        return False
    if path.stat().st_size <= tail_bytes:
        return False
    with path.open("rb") as stream:
        stream.seek(-tail_bytes, os.SEEK_END)
        data = stream.read()
    newline = data.find(b"\n")
    if newline >= 0:
        data = data[newline + 1 :]
    temporary = path.with_name("." + path.name + ".trim")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return True


def _recheck_location(location: dict[str, Any], config: dict[str, Any]) -> tuple[Path, bool, str]:
    raw_path = _path_from_uri(location["uri"])
    if raw_path.is_symlink():
        return raw_path, False, "location-is-symlink"
    path = _normal_path(raw_path)
    declared = [
        root
        for name, root in _roots_for(config, location["machine_id"])
        if name == location["tier"]
    ]
    if not any(_contains(root, path) and path != root for root in declared):
        return path, False, "outside-declared-tier-root"
    location["git"] = _git_state(path)
    safe, reason = _safe_for_removal(location)
    return path, safe, reason


def gc_storage(
    data_root: Path | str | None,
    *,
    project_id: str,
    config: dict[str, Any] | None = None,
    machine_id: str | None = None,
    apply: bool = False,
    include_orphans: bool = False,
    idle_days: int | None = None,
    now: datetime | None = None,
    job_tail_bytes: int = DEFAULT_JOB_OUTPUT_TAIL_BYTES,
) -> dict[str, Any]:
    config = config or load_tier_config()
    registry = load_registry(data_root, project_id)
    tasks = _task_map(data_root, project_id)
    now = now or datetime.now(UTC)
    idle_days = idle_days or config["retention"]["idle_days"]
    if type(idle_days) is not int or idle_days < 1:
        raise StorageLifecycleError("idle_days is invalid")
    if type(job_tail_bytes) is not int or job_tail_bytes < 1:
        raise StorageLifecycleError("job_tail_bytes is invalid")
    running_tasks = _running_tasks(data_root, project_id)
    candidates: list[dict[str, Any]] = []
    for location in registry["locations"]:
        if machine_id is not None and location["machine_id"] != machine_id:
            continue
        if location["task_id"] in running_tasks:
            continue
        if location["retention_class"] != "scratch":
            continue
        eligible, reason = _card_matches(location, tasks, now, idle_days)
        if not eligible:
            continue
        if location["location_kind"] not in {"scratch", "job", "workspace"}:
            continue
        try:
            path, safe, safety_reason = _recheck_location(location, config)
        except StorageLifecycleError as exc:
            path, safe, safety_reason = _path_from_uri(location["uri"]), False, str(exc)
        decision = "candidate-delete" if safe and location["location_kind"] != "job" else "trim-output" if safe else "blocked"
        if location["location_kind"] == "job" and safe:
            if not _job_finished(path):
                continue
            try:
                activity = datetime.fromisoformat(location["last_job_activity_at"].replace("Z", "+00:00"))
                trim_days = int(config["capacity"].get("job_output_trim_days", 14))
                if now - activity < timedelta(days=trim_days):
                    continue
            except (TypeError, ValueError):
                decision = "blocked"
                safety_reason = "job-activity-unknown"
        candidates.append({
            "location_id": location["location_id"],
            "task_id": location["task_id"],
            "uri": location["uri"],
            "size_bytes": location["size_bytes"],
            "reason": reason if safe else safety_reason,
            "decision": decision,
        })
    candidates.sort(key=lambda item: (-int(item.get("size_bytes") or 0), item["location_id"]))
    result = {
        "schema_version": "context.storage-gc/v1alpha1",
        "project_id": project_id,
        "dry_run": not apply,
        "applied": [],
        "candidates": candidates,
        "blocked": [item for item in candidates if item["decision"] == "blocked"],
    }
    if include_orphans:
        roots = _roots_for(config, machine_id or next(iter(config["machines"]), DEFAULT_MACHINE_ID))
        result["orphans"] = _find_orphans(registry, roots, tasks)
    if not apply:
        return result
    registry_path = _registry_path(data_root, project_id)
    for candidate in candidates:
        if candidate["decision"] == "blocked":
            continue
        operation_id = "gc-" + candidate["location_id"] + "-" + uuid.uuid4().hex[:12]
        with _locked(registry_path):
            registry = load_registry(data_root, project_id)
            try:
                location = next(item for item in registry["locations"] if item["location_id"] == candidate["location_id"])
                path, safe, safety_reason = _recheck_location(location, config)
                if location["task_id"] in _running_tasks(data_root, project_id):
                    safe, safety_reason = False, "running-job"
                if location["location_kind"] == "job" and safe and not _job_finished(path):
                    safe, safety_reason = False, "job-not-finished"
            except (StopIteration, StorageLifecycleError) as exc:
                location = None
                safe = False
                safety_reason = str(exc)
            if not safe or location is None:
                candidate["decision"] = "blocked"
                candidate["reason"] = safety_reason
                result["blocked"].append(candidate)
                continue
            request_hash = _location_request_hash(location)
            _commit_registry(data_root, project_id, registry, operation_id=operation_id, action="gc", phase="prepare", request_sha256=request_hash, location_id=location["location_id"])
            _commit_registry(data_root, project_id, registry, operation_id=operation_id, action="gc", phase="delete-intent", request_sha256=request_hash, location_id=location["location_id"])
            if location["location_kind"] == "job":
                changed = _trim_job_output(location, job_tail_bytes)
                action = "trim" if changed else "noop"
                if changed:
                    location["size_bytes"], location["content_sha256"] = _tree_digest(
                        _path_from_uri(location["uri"]), hash_content=False
                    )
                    location["last_observed_at"] = _now()
            else:
                _remove_path(location)
                action = "delete"
                location["state"] = "deleted"
            registry["revision"] += 1
            location["registry_revision"] = registry["revision"]
            registry["journal_head"] = operation_id
            _commit_registry(data_root, project_id, registry, operation_id=operation_id, action=action, phase="committed", request_sha256=request_hash, location_id=location["location_id"])
            result["applied"].append({"location_id": location["location_id"], "action": action, "operation_id": operation_id})
    return result


def _print(document: dict[str, Any]) -> None:
    print(json.dumps(document, ensure_ascii=False, sort_keys=True))


def _config_from_args(args: Any) -> dict[str, Any]:
    return load_tier_config(getattr(args, "config", None))


def command(args: Any) -> int:
    try:
        config = _config_from_args(args)
        project_id = args.project_id
        data_root = args.data_root or default_data_root()
        if args.storage_action == "register":
            uri = args.uri or path_to_uri(Path(args.path))
            result = register_location(
                data_root,
                project_id=project_id,
                task_id=args.task_id,
                machine_id=args.machine,
                uri=uri,
                location_kind=args.kind,
                retention_class=args.retention_class,
                created_by=args.created_by,
                tier=args.tier,
                package_name=args.package_name,
                operation_id=args.operation_id,
                config=config,
                adopt=args.adopt,
            )
        elif args.storage_action == "report":
            result = report_storage(
                data_root,
                project_id=project_id,
                machine_id=args.machine,
                task_id=args.task_id,
                config=config,
                include_orphans=args.orphans,
            )
        else:
            result = gc_storage(
                data_root,
                project_id=project_id,
                config=config,
                machine_id=args.machine,
                apply=args.apply,
                include_orphans=args.orphans,
                idle_days=args.idle_days,
                job_tail_bytes=args.job_tail_bytes,
            )
        _print(result)
        return EXIT_BLOCKED if result.get("blocked") else EXIT_OK
    except StorageLifecycleError as exc:
        _print({"schema_version": STORAGE_REPORT_SCHEMA, "error": str(exc), "error_code": exc.code})
        return exc.code


def register_subcommands(group: Any) -> None:
    register = group.add_parser("register", help="register one local storage location")
    register.add_argument("--project-id", required=True)
    register.add_argument("--data-root", default=None)
    register.add_argument("--config", default=None)
    register.add_argument("--task-id", required=True)
    register.add_argument("--machine", default=DEFAULT_MACHINE_ID)
    source = register.add_mutually_exclusive_group(required=True)
    source.add_argument("--uri")
    source.add_argument("--path")
    register.add_argument("--kind", choices=sorted(KINDS), required=True)
    register.add_argument("--class", dest="retention_class", choices=sorted(RETENTION_CLASSES), required=True)
    register.add_argument("--tier", required=True)
    register.add_argument("--created-by", choices=sorted(CREATORS), default="operator")
    register.add_argument("--package-name", default=None)
    register.add_argument("--operation-id", default=None)
    register.add_argument("--adopt", action="store_true")
    register.set_defaults(handler=command)

    report = group.add_parser("report", help="report registered locations and tier capacity")
    report.add_argument("--project-id", required=True)
    report.add_argument("--data-root", default=None)
    report.add_argument("--config", default=None)
    report.add_argument("--machine", default=None)
    report.add_argument("--task-id", default=None)
    report.add_argument("--orphans", action="store_true")
    report.set_defaults(handler=command)

    gc = group.add_parser("gc", help="plan or apply safe scratch/job cleanup")
    gc.add_argument("--project-id", required=True)
    gc.add_argument("--data-root", default=None)
    gc.add_argument("--config", default=None)
    gc.add_argument("--machine", default=None)
    gc.add_argument("--idle-days", type=int, default=None)
    gc.add_argument("--job-tail-bytes", type=int, default=DEFAULT_JOB_OUTPUT_TAIL_BYTES)
    gc.add_argument("--orphans", action="store_true")
    gc.add_argument("--apply", action="store_true")
    gc.set_defaults(handler=command)
