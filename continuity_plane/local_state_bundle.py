"""Portable local-embedded State export, import, and rollback bundles."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .sqlite_state_store import SQLiteStateStore


BUNDLE_SCHEMA_VERSION = "context.local-state-bundle/v1alpha1"
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_FILES = 4096

_MANIFEST_FIELDS = {
    "schema_version",
    "project_id",
    "runtime_profile",
    "source_revision",
    "source_event_head",
    "generated_at",
    "files",
    "total_size_bytes",
    "manifest_sha256",
}
_FILE_FIELDS = {"path", "role", "size_bytes", "content_sha256"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROOT_FILES = {
    "project.yaml": "profile",
    "MASTER.md": "governance",
    "MASTER.en.md": "governance",
    "STATUS.md": "routing",
    "STATUS.en.md": "routing",
    "attach-proposal.json": "source-binding",
    "checkpoint-ref.json": "checkpoint",
}


class LocalStateBundleError(ValueError):
    """A local State bundle is invalid or cannot be applied safely."""


def _canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _strict_json(payload: bytes, field: str) -> dict[str, Any]:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(
            payload.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise LocalStateBundleError(f"{field} is not strict JSON") from exc
    if not isinstance(document, dict):
        raise LocalStateBundleError(f"{field} must be an object")
    return document


def _load_profile(control: Path) -> dict[str, Any]:
    path = control / "project.yaml"
    try:
        profile = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LocalStateBundleError("project profile is unavailable or invalid") from exc
    if (
        not isinstance(profile, dict)
        or profile.get("schema_version") != "context.project/v1alpha1"
        or profile.get("runtime_profile") != "local-embedded"
        or profile.get("state_store")
        != {"adapter": "sqlite", "path": ".continuity/state.sqlite3"}
        or not isinstance(profile.get("project_id"), str)
        or not profile["project_id"]
    ):
        raise LocalStateBundleError("project profile is not local-embedded")
    return profile


def _event_head(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    return {
        "sequence_no": events[-1]["sequence_no"],
        "event_sha256": events[-1]["event_sha256"],
    }


def _backup_database(source: Path, target: Path) -> None:
    if not source.is_file() or source.is_symlink():
        raise LocalStateBundleError("State database is unavailable or invalid")
    source_uri = f"file:{source.as_posix()}?mode=ro"
    try:
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            with closing(sqlite3.connect(target)) as target_connection:
                source_connection.backup(target_connection)
                target_connection.commit()
    except sqlite3.Error as exc:
        raise LocalStateBundleError("SQLite backup failed") from exc


def _collect_export_payloads(control: Path, database_backup: Path) -> dict[str, bytes]:
    payloads = {"state.sqlite3": database_backup.read_bytes()}
    for name in sorted(_ROOT_FILES):
        path = control / name
        if not path.exists():
            continue
        if not path.is_file() or path.is_symlink():
            raise LocalStateBundleError(f"bundle source is not a regular file: {name}")
        payloads[name] = path.read_bytes()
    artifact_root = control / "artifacts" / "objects" / "sha256"
    if artifact_root.exists():
        if not artifact_root.is_dir() or artifact_root.is_symlink():
            raise LocalStateBundleError("artifact object root is invalid")
        for path in sorted(artifact_root.rglob("*")):
            if path.is_dir():
                continue
            if not path.is_file() or path.is_symlink():
                raise LocalStateBundleError("artifact object is not a regular file")
            relative = path.relative_to(control).as_posix()
            payloads[relative] = path.read_bytes()
    if len(payloads) > MAX_FILES:
        raise LocalStateBundleError("bundle file count exceeds the configured bound")
    if any(len(payload) > MAX_FILE_BYTES for payload in payloads.values()):
        raise LocalStateBundleError("bundle member exceeds the configured bound")
    if sum(map(len, payloads.values())) > MAX_BUNDLE_BYTES:
        raise LocalStateBundleError("bundle payload exceeds the configured bound")
    return payloads


def _role(path: str) -> str:
    if path == "state.sqlite3":
        return "state"
    if path.startswith("artifacts/objects/sha256/"):
        return "artifact"
    return _ROOT_FILES[path]


def _build_manifest(
    profile: dict[str, Any],
    snapshot: dict[str, Any],
    events: list[dict[str, Any]],
    payloads: dict[str, bytes],
) -> dict[str, Any]:
    files = [
        {
            "path": path,
            "role": _role(path),
            "size_bytes": len(payload),
            "content_sha256": _sha256(payload),
        }
        for path, payload in sorted(payloads.items())
    ]
    manifest = {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "project_id": profile["project_id"],
        "runtime_profile": "local-embedded",
        "source_revision": snapshot["project"]["revision"],
        "source_event_head": _event_head(events),
        "generated_at": snapshot["project"]["updated_at"],
        "files": files,
        "total_size_bytes": sum(item["size_bytes"] for item in files),
        "manifest_sha256": "",
    }
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    manifest["manifest_sha256"] = _sha256(_canonical(unsigned))
    return manifest


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _write_bundle(output: Path, manifest: dict[str, Any], payloads: dict[str, bytes]) -> None:
    if output.exists():
        raise LocalStateBundleError(f"export would overwrite an existing file: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            archive.writestr(_zip_info("manifest.json"), _canonical(manifest))
            for path, payload in sorted(payloads.items()):
                archive.writestr(_zip_info(path), payload)
        if temporary.stat().st_size > MAX_BUNDLE_BYTES:
            raise LocalStateBundleError("compressed bundle exceeds the configured bound")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def export_local_state(root: Path, output: Path) -> dict[str, Any]:
    """Export one consistent local-embedded control directory."""
    root = root.resolve()
    output = output.resolve()
    control = root / ".continuity"
    profile = _load_profile(control)
    state_store = SQLiteStateStore(control / "state.sqlite3")
    state_store.initialize()
    snapshot = state_store.read_project(profile["project_id"])
    events = state_store.read_events(profile["project_id"])
    with tempfile.TemporaryDirectory(prefix="continuity-export-") as directory:
        backup = Path(directory) / "state.sqlite3"
        _backup_database(control / "state.sqlite3", backup)
        verified_store = SQLiteStateStore(backup)
        verified_store.initialize()
        verified_snapshot = verified_store.read_project(profile["project_id"])
        verified_events = verified_store.read_events(profile["project_id"])
        if verified_snapshot != snapshot or verified_events != events:
            raise LocalStateBundleError("State changed during export")
        payloads = _collect_export_payloads(control, backup)
        manifest = _build_manifest(profile, snapshot, events, payloads)
        _write_bundle(output, manifest, payloads)
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "project_id": profile["project_id"],
        "revision": snapshot["project"]["revision"],
        "event_head": _event_head(events),
        "bundle": str(output),
        "bundle_sha256": _sha256(output.read_bytes()),
        "file_count": len(manifest["files"]),
    }


def _safe_member_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and "\\" not in name
        and not path.is_absolute()
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if set(manifest) != _MANIFEST_FIELDS:
        raise LocalStateBundleError("bundle manifest fields are invalid")
    if manifest["schema_version"] != BUNDLE_SCHEMA_VERSION:
        raise LocalStateBundleError("bundle schema_version is unsupported")
    if (
        not isinstance(manifest["project_id"], str)
        or not manifest["project_id"]
        or manifest["runtime_profile"] != "local-embedded"
        or type(manifest["source_revision"]) is not int
        or manifest["source_revision"] < 0
        or not isinstance(manifest["generated_at"], str)
        or not manifest["generated_at"]
    ):
        raise LocalStateBundleError("bundle authority fields are invalid")
    head = manifest["source_event_head"]
    if head is not None and (
        not isinstance(head, dict)
        or set(head) != {"sequence_no", "event_sha256"}
        or type(head["sequence_no"]) is not int
        or head["sequence_no"] <= 0
        or not isinstance(head["event_sha256"], str)
        or _SHA256_RE.fullmatch(head["event_sha256"]) is None
    ):
        raise LocalStateBundleError("bundle event head is invalid")
    files = manifest["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_FILES:
        raise LocalStateBundleError("bundle files are invalid")
    paths = []
    total = 0
    for item in files:
        if not isinstance(item, dict) or set(item) != _FILE_FIELDS:
            raise LocalStateBundleError("bundle file entry fields are invalid")
        path = item["path"]
        if not isinstance(path, str) or not _safe_member_name(path):
            raise LocalStateBundleError("bundle member path is invalid")
        if item["role"] not in {
            "profile",
            "state",
            "governance",
            "routing",
            "source-binding",
            "checkpoint",
            "artifact",
        }:
            raise LocalStateBundleError("bundle member role is invalid")
        if (
            type(item["size_bytes"]) is not int
            or item["size_bytes"] < 0
            or item["size_bytes"] > MAX_FILE_BYTES
            or not isinstance(item["content_sha256"], str)
            or _SHA256_RE.fullmatch(item["content_sha256"]) is None
        ):
            raise LocalStateBundleError("bundle member metadata is invalid")
        paths.append(path)
        total += item["size_bytes"]
    if len(paths) != len(set(paths)) or {"project.yaml", "state.sqlite3"} - set(paths):
        raise LocalStateBundleError("bundle member set is invalid")
    if total != manifest["total_size_bytes"] or total > MAX_BUNDLE_BYTES:
        raise LocalStateBundleError("bundle total size is invalid")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if (
        not isinstance(manifest["manifest_sha256"], str)
        or _SHA256_RE.fullmatch(manifest["manifest_sha256"]) is None
        or manifest["manifest_sha256"] != _sha256(_canonical(unsigned))
    ):
        raise LocalStateBundleError("bundle manifest digest mismatch")
    return files


def _read_bundle(bundle: Path) -> tuple[dict[str, Any], dict[str, bytes], str]:
    if not bundle.is_file() or bundle.is_symlink():
        raise LocalStateBundleError("bundle is unavailable or invalid")
    if bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise LocalStateBundleError("bundle exceeds the configured bound")
    bundle_digest = _sha256(bundle.read_bytes())
    try:
        with zipfile.ZipFile(bundle, "r") as archive:
            members = archive.infolist()
            names = [item.filename for item in members]
            if len(names) != len(set(names)) or len(names) > MAX_FILES + 1:
                raise LocalStateBundleError("bundle member set is invalid")
            if any(not _safe_member_name(name) for name in names):
                raise LocalStateBundleError("bundle member path is invalid")
            if any(
                item.file_size > MAX_FILE_BYTES
                or ((item.external_attr >> 16) & 0o170000) == 0o120000
                for item in members
            ) or sum(item.file_size for item in members) > MAX_BUNDLE_BYTES:
                raise LocalStateBundleError("bundle member metadata exceeds safety bounds")
            if "manifest.json" not in names:
                raise LocalStateBundleError("bundle manifest is missing")
            manifest = _strict_json(archive.read("manifest.json"), "bundle manifest")
            entries = _validate_manifest(manifest)
            expected_names = {"manifest.json", *(item["path"] for item in entries)}
            if set(names) != expected_names:
                raise LocalStateBundleError("bundle contains unregistered members")
            payloads = {}
            for entry in entries:
                payload = archive.read(entry["path"])
                if len(payload) != entry["size_bytes"]:
                    raise LocalStateBundleError("bundle member size mismatch")
                if _sha256(payload) != entry["content_sha256"]:
                    raise LocalStateBundleError("bundle member digest mismatch")
                payloads[entry["path"]] = payload
    except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise LocalStateBundleError("bundle archive is invalid") from exc
    return manifest, payloads, bundle_digest


def _write_stage(stage: Path, payloads: dict[str, bytes]) -> None:
    stage.mkdir(mode=0o700)
    for name, payload in sorted(payloads.items()):
        target = stage / PurePosixPath(name)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_bytes(payload)


def _validate_stage(stage: Path, manifest: dict[str, Any]) -> None:
    profile = _load_profile(stage)
    if profile["project_id"] != manifest["project_id"]:
        raise LocalStateBundleError("bundle profile project_id mismatch")
    store = SQLiteStateStore(stage / "state.sqlite3")
    store.initialize()
    snapshot = store.read_project(profile["project_id"])
    events = store.read_events(profile["project_id"])
    if (
        snapshot["project"]["revision"] != manifest["source_revision"]
        or _event_head(events) != manifest["source_event_head"]
    ):
        raise LocalStateBundleError("bundle State authority mismatch")


def _swap_control(root: Path, stage: Path, current: Path) -> None:
    old = root / f".continuity.old-{uuid.uuid4().hex}"
    moved_old = False
    try:
        if current.exists():
            os.replace(current, old)
            moved_old = True
        os.replace(stage, current)
    except OSError as exc:
        if moved_old and old.exists() and not current.exists():
            os.replace(old, current)
        raise LocalStateBundleError("atomic control-plane replacement failed") from exc
    if old.exists():
        shutil.rmtree(old)


def import_local_state(
    root: Path,
    bundle: Path,
    *,
    replace: bool,
) -> dict[str, Any]:
    """Validate one bundle in staging, then atomically install it."""
    root = root.resolve()
    bundle = bundle.resolve()
    manifest, payloads, bundle_digest = _read_bundle(bundle)
    root.mkdir(parents=True, exist_ok=True)
    current = root / ".continuity"
    if current.is_symlink():
        raise LocalStateBundleError("import target .continuity must not be a symlink")
    if current.exists() and not replace:
        raise LocalStateBundleError("import target already has .continuity; use --replace")
    stage = root / f".continuity.import-{uuid.uuid4().hex}"
    try:
        _write_stage(stage, payloads)
        _validate_stage(stage, manifest)
        if current.exists():
            rollback = stage / "rollback" / "previous.zip"
            rollback.parent.mkdir(parents=True, mode=0o700)
            export_local_state(root, rollback)
        _swap_control(root, stage, current)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return {
        "schema_version": BUNDLE_SCHEMA_VERSION,
        "project_id": manifest["project_id"],
        "revision": manifest["source_revision"],
        "event_head": manifest["source_event_head"],
        "bundle": str(bundle),
        "bundle_sha256": bundle_digest,
        "rollback_available": (current / "rollback/previous.zip").is_file(),
    }


def rollback_local_state(root: Path) -> dict[str, Any]:
    """Swap the current control directory with its verified previous bundle."""
    root = root.resolve()
    current = root / ".continuity"
    if current.is_symlink():
        raise LocalStateBundleError("rollback target .continuity must not be a symlink")
    rollback = current / "rollback" / "previous.zip"
    if not rollback.is_file() or rollback.is_symlink():
        raise LocalStateBundleError("rollback bundle is unavailable")
    with tempfile.TemporaryDirectory(prefix="continuity-rollback-") as directory:
        copied = Path(directory) / "previous.zip"
        shutil.copyfile(rollback, copied)
        receipt = import_local_state(root, copied, replace=True)
    receipt["rollback_available"] = (current / "rollback/previous.zip").is_file()
    return receipt


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "LocalStateBundleError",
    "export_local_state",
    "import_local_state",
    "rollback_local_state",
]
