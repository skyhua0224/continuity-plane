"""M0-07 schema/version/release governance primitives.

The registry and transition checks are deterministic and side-effect free. They
are intentionally independent of PostgreSQL and State MCP so they can gate
schema migrations before a runtime state service exists.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SchemaGovernanceError(ValueError):
    """Raised when a schema registry or version transition is unsafe."""


_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9][0-9]*)\."
    r"(?P<minor>0|[1-9][0-9]*)\."
    r"(?P<patch>0|[1-9][0-9]*)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+[0-9A-Za-z.-]+)?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RELEASE_STATES = {"current", "deprecated"}
_CHANGE_KINDS = {"metadata", "backward-compatible", "breaking"}


@dataclass(frozen=True)
class SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: str | None = None

    @classmethod
    def parse(cls, value: str) -> "SemanticVersion":
        if not isinstance(value, str):
            raise SchemaGovernanceError("version must be a string")
        match = _SEMVER_RE.fullmatch(value)
        if not match:
            raise SchemaGovernanceError(f"invalid semantic version: {value}")
        return cls(
            major=int(match.group("major")),
            minor=int(match.group("minor")),
            patch=int(match.group("patch")),
            prerelease=match.group("pre"),
        )

    @property
    def core(self) -> tuple[int, int, int]:
        return self.major, self.minor, self.patch


def _is_newer(previous: SemanticVersion, current: SemanticVersion) -> bool:
    return current.core > previous.core or (
        current.core == previous.core
        and previous.prerelease is not None
        and current.prerelease is None
    )


def registry_digest(registry: dict[str, Any]) -> str:
    """Return a stable digest independent of registry entry ordering."""
    canonical = copy.deepcopy(registry)
    schemas = canonical.get("schemas")
    if isinstance(schemas, list):
        canonical["schemas"] = sorted(
            schemas,
            key=lambda entry: (entry.get("schema_id", ""), entry.get("current_wire_version", "")),
        )
        for entry in canonical["schemas"]:
            migrations = entry.get("migrations")
            if isinstance(migrations, list):
                entry["migrations"] = sorted(
                    migrations,
                    key=lambda item: (item.get("from", ""), item.get("to", "")),
                )
    payload = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _artifact_path(root: Path, relative_path: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise SchemaGovernanceError("artifact_path must be a non-empty relative path")
    path = (root / relative_path).resolve()
    root = root.resolve()
    if path == root or root not in path.parents:
        raise SchemaGovernanceError("artifact_path must remain inside repository root")
    return path


def validate_registry(registry: dict[str, Any], *, root: Path) -> None:
    """Validate registry structure and hashes for the current repository."""
    if not isinstance(registry, dict):
        raise SchemaGovernanceError("registry must be an object")
    if registry.get("registry_version") != "context.schema-registry/v1alpha1":
        raise SchemaGovernanceError("unsupported registry_version")
    entries = registry.get("schemas")
    if not isinstance(entries, list) or not entries:
        raise SchemaGovernanceError("schemas must be a non-empty list")

    seen_ids: set[str] = set()
    for entry in entries:
        required = {
            "schema_id",
            "current_semver",
            "current_wire_version",
            "supported_wire_versions",
            "artifact_path",
            "content_sha256",
            "status",
            "compatibility_mode",
            "migrations",
        }
        if not isinstance(entry, dict) or not required.issubset(entry):
            raise SchemaGovernanceError("schema entry is missing required fields")
        schema_id = entry["schema_id"]
        if not isinstance(schema_id, str) or not schema_id or schema_id in seen_ids:
            raise SchemaGovernanceError("schema_id must be unique and non-empty")
        seen_ids.add(schema_id)
        SemanticVersion.parse(entry["current_semver"])
        current_wire = entry["current_wire_version"]
        supported = entry["supported_wire_versions"]
        if current_wire not in supported:
            raise SchemaGovernanceError("current wire version must be supported")
        if not isinstance(supported, list) or not all(isinstance(item, str) and item for item in supported):
            raise SchemaGovernanceError("supported_wire_versions must contain strings")
        digest = entry["content_sha256"]
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise SchemaGovernanceError("content_sha256 must be lowercase SHA-256")
        artifact = _artifact_path(root, entry["artifact_path"])
        if not artifact.is_file():
            raise SchemaGovernanceError(f"schema artifact is missing: {entry['artifact_path']}")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != digest:
            raise SchemaGovernanceError(f"schema artifact hash mismatch: {schema_id}")
        if entry["status"] not in _RELEASE_STATES:
            raise SchemaGovernanceError("unsupported schema release status")
        if entry["compatibility_mode"] not in {"strict-versioned", "profile-contract"}:
            raise SchemaGovernanceError("unsupported compatibility_mode")
        migrations = entry["migrations"]
        if not isinstance(migrations, list):
            raise SchemaGovernanceError("migrations must be a list")
        for migration in migrations:
            if not isinstance(migration, dict) or not {
                "from",
                "to",
                "replay_passed",
                "idempotent",
                "rollback_ref",
            }.issubset(migration):
                raise SchemaGovernanceError("migration requires replay, idempotency, and rollback evidence")
            if not migration["replay_passed"] or not migration["idempotent"]:
                raise SchemaGovernanceError("migration without replay/idempotency evidence is quarantined")
            if not migration["rollback_ref"]:
                raise SchemaGovernanceError("migration requires rollback_ref")


def resolve_wire_version(
    registry: dict[str, Any], schema_id: str, wire_version: str
) -> dict[str, str]:
    """Resolve a wire version or reject it with an explicit quarantine error."""
    entries = [entry for entry in registry.get("schemas", []) if entry.get("schema_id") == schema_id]
    if len(entries) != 1:
        raise SchemaGovernanceError(f"unknown schema_id is quarantined: {schema_id}")
    entry = entries[0]
    if wire_version == entry["current_wire_version"]:
        return {"schema_id": schema_id, "wire_version": wire_version, "status": "current"}
    legacy = set(entry.get("supported_wire_versions", [])) - {entry["current_wire_version"]}
    if wire_version in legacy and any(
        migration.get("from") == wire_version
        and migration.get("to") == entry["current_wire_version"]
        and migration.get("replay_passed")
        for migration in entry.get("migrations", [])
    ):
        return {"schema_id": schema_id, "wire_version": wire_version, "status": "migratable"}
    raise SchemaGovernanceError(
        f"wire version is unknown or lacks a verified migration; quarantine: {schema_id}@{wire_version}"
    )


def validate_version_transition(
    previous: str,
    current: str,
    *,
    change_kind: str,
    replay_passed: bool,
    migration_present: bool = False,
    rollback_present: bool = False,
) -> None:
    """Enforce semver and evidence gates for a schema release transition."""
    if change_kind not in _CHANGE_KINDS:
        raise SchemaGovernanceError("unsupported change_kind")
    previous_version = SemanticVersion.parse(previous)
    current_version = SemanticVersion.parse(current)
    if not _is_newer(previous_version, current_version):
        raise SchemaGovernanceError("schema release must advance monotonically")
    if not replay_passed:
        raise SchemaGovernanceError("schema transition requires replay evidence")

    if change_kind == "metadata":
        if current_version.core[:2] != previous_version.core[:2]:
            raise SchemaGovernanceError("metadata-only change must use a patch release")
        return
    if change_kind == "backward-compatible":
        if current_version.major == previous_version.major and current_version.minor <= previous_version.minor:
            raise SchemaGovernanceError("backward-compatible change requires a minor or major bump")
        return
    if current_version.major <= previous_version.major:
        raise SchemaGovernanceError("breaking change requires a major version bump")
    if not migration_present or not rollback_present:
        raise SchemaGovernanceError("breaking change requires migration and rollback evidence")
