"""Strict Skill manifest set validation and canonicalization."""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from continuity_plane.schema_governance import (
    SchemaGovernanceError,
    SemanticVersion,
)

SCHEMA_VERSION = "context.skill-manifest-set/v1alpha1"
SPDX_LICENSE_LIST_VERSION = "3.28.0"

_DOCUMENT_FIELDS = {"schema_version", "manifests"}
_MANIFEST_FIELDS = {
    "skill_id",
    "version",
    "content_sha256",
    "source_kind",
    "license_ref",
    "applicability",
    "rule_ids",
    "dependencies",
    "conflicts",
    "expires_at",
    "compatibility",
    "provenance_refs",
    "status",
}
_APPLICABILITY_FIELDS = {"kind", "ref"}
_VERSION_RANGE_FIELDS = {
    "minimum",
    "minimum_inclusive",
    "maximum",
    "maximum_inclusive",
}
_DEPENDENCY_FIELDS = {"skill_id", "version_range"}
_COMPATIBILITY_FIELDS = {"schema_refs", "provider_contract_refs"}

_SOURCE_KINDS = {"builtin", "external", "project", "user", "workflow"}
_STATUSES = {
    "proposed",
    "approved",
    "active",
    "quarantined",
    "deprecated",
    "rejected",
}
_SELECTED_STATUSES = {"approved", "active"}
_TASK_KINDS = {
    "code-change",
    "incident-response",
    "migration",
    "read-only-review",
    "release",
    "research",
    "test",
}
_ROLE_IDS = {"executor", "reviewer", "thinker", "verifier"}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_REF_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_SCHEMA_REF_RE = re.compile(r"^context\.[a-z0-9._/-]+/v[a-z0-9._-]+$")
_PROVIDER_CONTRACT_REF_RE = re.compile(
    r"^provider://[a-z0-9][a-z0-9._-]*/v[a-z0-9._-]+$"
)
_APPLICABILITY_REF_RE = {
    "project": re.compile(
        r"^project://[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$"
    ),
    "repo": re.compile(r"^repo://[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$"),
    "path": re.compile(
        r"^path://[A-Za-z0-9][A-Za-z0-9._-]*"
        r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*$"
    ),
    "operation": re.compile(r"^operation://[a-z0-9][a-z0-9._-]*$"),
    "provider": re.compile(r"^provider://[a-z0-9][a-z0-9._-]*$"),
}
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

_SPDX_RESOURCE = "schemas/m4-01/spdx-license-ids-3.28.0.json"
try:
    _SPDX_TEXT = (
        resources.files("continuity_plane")
        .joinpath(_SPDX_RESOURCE)
        .read_text(encoding="utf-8")
    )
except (FileNotFoundError, ModuleNotFoundError):
    _SPDX_SNAPSHOT_PATH = (
        Path(__file__).resolve().parents[1]
        / "schemas"
        / "m4-01"
        / "spdx-license-ids-3.28.0.json"
    )
    _SPDX_TEXT = _SPDX_SNAPSHOT_PATH.read_text(encoding="utf-8")
_SPDX_SNAPSHOT = json.loads(_SPDX_TEXT)
if _SPDX_SNAPSHOT.get("license_list_version") != SPDX_LICENSE_LIST_VERSION:
    raise RuntimeError("SPDX license snapshot version mismatch")
_SPDX_LICENSE_IDS = frozenset(_SPDX_SNAPSHOT.get("license_ids", ()))


class SkillManifestSetError(ValueError):
    """Raised when a Skill manifest set violates its contract."""


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SkillManifestSetError(f"{field} must be an object")
    actual = set(value)
    if actual != fields:
        unexpected = sorted(actual - fields)
        missing = sorted(fields - actual)
        detail = ", ".join(unexpected or missing)
        raise SkillManifestSetError(f"{field} fields are invalid: {detail}")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillManifestSetError(f"{field} must be a non-empty string")
    return value


def _identifier(value: Any, field: str) -> str:
    value = _string(value, field)
    if not _ID_RE.fullmatch(value):
        raise SkillManifestSetError(f"{field} must be a stable lowercase identifier")
    return value


def _semver(value: Any, field: str) -> str:
    try:
        SemanticVersion.parse(value)
    except SchemaGovernanceError as exc:
        raise SkillManifestSetError(f"{field} must be strict SemVer") from exc
    return value


def _compare_semver(left: SemanticVersion, right: SemanticVersion) -> int:
    if left.core != right.core:
        return -1 if left.core < right.core else 1
    if left.prerelease is None:
        return 0 if right.prerelease is None else 1
    if right.prerelease is None:
        return -1

    left_parts = left.prerelease.split(".")
    right_parts = right.prerelease.split(".")
    for left_part, right_part in zip(left_parts, right_parts, strict=False):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            if len(left_part) != len(right_part):
                return -1 if len(left_part) < len(right_part) else 1
            return -1 if left_part < right_part else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_part < right_part else 1
    if len(left_parts) == len(right_parts):
        return 0
    return -1 if len(left_parts) < len(right_parts) else 1


def _validate_version_range(value: Any, field: str) -> dict[str, Any]:
    version_range = _object(value, _VERSION_RANGE_FIELDS, field)
    minimum_value = version_range["minimum"]
    maximum_value = version_range["maximum"]
    minimum = (
        None
        if minimum_value is None
        else SemanticVersion.parse(_semver(minimum_value, f"{field}.minimum"))
    )
    maximum = (
        None
        if maximum_value is None
        else SemanticVersion.parse(_semver(maximum_value, f"{field}.maximum"))
    )
    for bound in ("minimum_inclusive", "maximum_inclusive"):
        if not isinstance(version_range[bound], bool):
            raise SkillManifestSetError(f"{field}.{bound} must be boolean")
    if minimum is None and maximum is None:
        raise SkillManifestSetError(f"{field} cannot be unbounded on both sides")
    if minimum is None and version_range["minimum_inclusive"]:
        raise SkillManifestSetError(f"{field} has no inclusive minimum")
    if maximum is None and version_range["maximum_inclusive"]:
        raise SkillManifestSetError(f"{field} has no inclusive maximum")
    if minimum is not None and maximum is not None:
        order = _compare_semver(minimum, maximum)
        if order > 0:
            raise SkillManifestSetError(f"{field} is reversed")
        if order == 0 and not (
            version_range["minimum_inclusive"] and version_range["maximum_inclusive"]
        ):
            raise SkillManifestSetError(f"{field} is empty")
    return version_range


def _version_in_range(version: str, version_range: dict[str, Any]) -> bool:
    candidate = SemanticVersion.parse(version)
    minimum_value = version_range["minimum"]
    if minimum_value is not None:
        minimum = SemanticVersion.parse(minimum_value)
        order = _compare_semver(candidate, minimum)
        if order < 0 or (order == 0 and not version_range["minimum_inclusive"]):
            return False
    maximum_value = version_range["maximum"]
    if maximum_value is not None:
        maximum = SemanticVersion.parse(maximum_value)
        order = _compare_semver(candidate, maximum)
        if order > 0 or (order == 0 and not version_range["maximum_inclusive"]):
            return False
    return True


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_RE.fullmatch(value):
        raise SkillManifestSetError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SkillManifestSetError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise SkillManifestSetError(f"{field} must include a timezone")
    return parsed


def _license_ref(value: Any) -> str:
    value = _string(value, "manifest.license_ref")
    if value not in _SPDX_LICENSE_IDS and not _ARTIFACT_REF_RE.fullmatch(value):
        raise SkillManifestSetError("manifest.license_ref is invalid")
    return value


def _provenance_ref(value: Any, field: str) -> str:
    value = _string(value, field)
    if not _ARTIFACT_REF_RE.fullmatch(value):
        raise SkillManifestSetError(f"{field} contains an invalid artifact reference")
    return value


def _unique_strings(
    value: Any,
    field: str,
    *,
    non_empty: bool = False,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or (non_empty and not value):
        raise SkillManifestSetError(f"{field} must be a unique string list")
    result: list[str] = []
    for item in value:
        item = _string(item, field)
        if pattern is not None and not pattern.fullmatch(item):
            raise SkillManifestSetError(f"{field} contains an invalid reference")
        result.append(item)
    if len(result) != len(set(result)):
        raise SkillManifestSetError(f"{field} must be unique")
    return result


def _validate_applicability(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise SkillManifestSetError(f"{field} must be a non-empty list")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        item = _object(item, _APPLICABILITY_FIELDS, field)
        kind = _string(item["kind"], f"{field}.kind")
        ref = _string(item["ref"], f"{field}.ref")
        if kind == "task":
            valid = (
                ref.startswith("task://") and ref.removeprefix("task://") in _TASK_KINDS
            )
        elif kind == "role":
            valid = (
                ref.startswith("role://") and ref.removeprefix("role://") in _ROLE_IDS
            )
        else:
            pattern = _APPLICABILITY_REF_RE.get(kind)
            valid = pattern is not None and pattern.fullmatch(ref) is not None
        if not valid:
            raise SkillManifestSetError(f"{field} contains an invalid typed reference")
        key = kind, ref
        if key in seen:
            raise SkillManifestSetError(f"{field} must be unique")
        seen.add(key)
        result.append(item)
    return result


def _validate_manifest_shape(manifest: Any) -> dict[str, Any]:
    manifest = _object(manifest, _MANIFEST_FIELDS, "manifest")
    _identifier(manifest["skill_id"], "manifest.skill_id")
    _semver(manifest["version"], "manifest.version")
    if not isinstance(manifest["content_sha256"], str) or not _SHA256_RE.fullmatch(
        manifest["content_sha256"]
    ):
        raise SkillManifestSetError("manifest.content_sha256 must be lowercase SHA-256")
    if manifest["source_kind"] not in _SOURCE_KINDS:
        raise SkillManifestSetError("manifest.source_kind is invalid")
    _license_ref(manifest["license_ref"])
    _validate_applicability(manifest["applicability"], "manifest.applicability")
    _unique_strings(
        manifest["rule_ids"],
        "manifest.rule_ids",
        non_empty=True,
        pattern=_ID_RE,
    )

    dependencies = manifest["dependencies"]
    if not isinstance(dependencies, list):
        raise SkillManifestSetError("manifest.dependencies must be a list")
    dependency_ids: list[str] = []
    for dependency in dependencies:
        dependency = _object(
            dependency,
            _DEPENDENCY_FIELDS,
            "manifest.dependency",
        )
        dependency_ids.append(
            _identifier(dependency["skill_id"], "manifest.dependency.skill_id")
        )
        _validate_version_range(
            dependency["version_range"],
            "manifest.dependency.version_range",
        )
    if len(dependency_ids) != len(set(dependency_ids)):
        raise SkillManifestSetError("manifest dependencies must be unique")

    conflicts = manifest["conflicts"]
    if not isinstance(conflicts, list):
        raise SkillManifestSetError("manifest.conflicts must be a list")
    conflict_ids: list[str] = []
    for conflict in conflicts:
        conflict = _object(
            conflict,
            _DEPENDENCY_FIELDS,
            "manifest.conflict",
        )
        conflict_ids.append(
            _identifier(conflict["skill_id"], "manifest.conflict.skill_id")
        )
        _validate_version_range(
            conflict["version_range"],
            "manifest.conflict.version_range",
        )
    if len(conflict_ids) != len(set(conflict_ids)):
        raise SkillManifestSetError("manifest conflicts must be unique")
    expires_at = manifest["expires_at"]
    if expires_at is not None:
        _timestamp(expires_at, "manifest.expires_at")

    compatibility = _object(
        manifest["compatibility"],
        _COMPATIBILITY_FIELDS,
        "manifest.compatibility",
    )
    _unique_strings(
        compatibility["schema_refs"],
        "manifest.compatibility.schema_refs",
        non_empty=True,
        pattern=_SCHEMA_REF_RE,
    )
    _unique_strings(
        compatibility["provider_contract_refs"],
        "manifest.compatibility.provider_contract_refs",
        pattern=_PROVIDER_CONTRACT_REF_RE,
    )
    provenance_refs = manifest["provenance_refs"]
    if not isinstance(provenance_refs, list) or not provenance_refs:
        raise SkillManifestSetError(
            "manifest.provenance_refs must be a non-empty unique string list"
        )
    validated_provenance = [
        _provenance_ref(item, "manifest.provenance_refs") for item in provenance_refs
    ]
    if len(validated_provenance) != len(set(validated_provenance)):
        raise SkillManifestSetError("manifest.provenance_refs must be unique")
    if manifest["status"] not in _STATUSES:
        raise SkillManifestSetError("manifest.status is invalid")
    return manifest


def validate_skill_manifest_set(
    document: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> None:
    """Validate a provider-neutral selected Skill manifest set."""
    root = _object(document, _DOCUMENT_FIELDS, "document")
    if root["schema_version"] != SCHEMA_VERSION:
        raise SkillManifestSetError("unsupported schema_version")
    manifests = root["manifests"]
    if not isinstance(manifests, list) or not manifests:
        raise SkillManifestSetError("manifests must be a non-empty list")

    by_id: dict[str, dict[str, Any]] = {}
    rule_owner: dict[str, str] = {}
    for item in manifests:
        manifest = _validate_manifest_shape(item)
        skill_id = manifest["skill_id"]
        if skill_id in by_id:
            raise SkillManifestSetError(f"duplicate skill_id: {skill_id}")
        by_id[skill_id] = manifest
        for rule_id in manifest["rule_ids"]:
            if rule_id in rule_owner:
                raise SkillManifestSetError(f"duplicate rule_id: {rule_id}")
            rule_owner[rule_id] = skill_id

    observed: datetime | None = None
    if observed_at is not None:
        observed = _timestamp(observed_at, "observed_at")

    dependency_graph: dict[str, set[str]] = {skill_id: set() for skill_id in by_id}
    for skill_id, manifest in by_id.items():
        if (
            manifest["status"] in _SELECTED_STATUSES
            and manifest["expires_at"] is not None
        ):
            if observed is None:
                raise SkillManifestSetError(
                    f"observed_at is required for expiring selected manifest: {skill_id}"
                )
            if _timestamp(manifest["expires_at"], "manifest.expires_at") <= observed:
                raise SkillManifestSetError(f"selected manifest is expired: {skill_id}")

        for dependency in manifest["dependencies"]:
            dependency_id = dependency["skill_id"]
            if dependency_id == skill_id:
                raise SkillManifestSetError(f"self dependency: {skill_id}")
            target = by_id.get(dependency_id)
            if target is None:
                raise SkillManifestSetError(f"missing dependency: {dependency_id}")
            if not _version_in_range(target["version"], dependency["version_range"]):
                raise SkillManifestSetError(
                    f"dependency version is outside range: {skill_id} -> {dependency_id}"
                )
            if (
                manifest["status"] in _SELECTED_STATUSES
                and target["status"] not in _SELECTED_STATUSES
            ):
                raise SkillManifestSetError(
                    f"selected dependency status is invalid: {skill_id} -> {dependency_id}"
                )
            dependency_graph[skill_id].add(dependency_id)

        for conflict in manifest["conflicts"]:
            conflict_id = conflict["skill_id"]
            if conflict_id == skill_id:
                raise SkillManifestSetError(f"self conflict: {skill_id}")
            target = by_id.get(conflict_id)
            if target is None:
                continue
            if (
                manifest["status"] in _SELECTED_STATUSES
                and target["status"] in _SELECTED_STATUSES
                and _version_in_range(
                    target["version"],
                    conflict["version_range"],
                )
            ):
                raise SkillManifestSetError(
                    f"selected Skill conflict: {skill_id} -> {conflict_id}"
                )

        provider_applicability = {
            item["ref"].removeprefix("provider://")
            for item in manifest["applicability"]
            if item["kind"] == "provider"
        }
        provider_contracts = {
            ref.removeprefix("provider://").split("/", 1)[0]
            for ref in manifest["compatibility"]["provider_contract_refs"]
        }
        if not provider_applicability.issubset(provider_contracts):
            raise SkillManifestSetError(
                f"provider applicability lacks a matching provider contract: {skill_id}"
            )

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(skill_id: str) -> None:
        if skill_id in visiting:
            raise SkillManifestSetError(f"dependency cycle: {skill_id}")
        if skill_id in visited:
            return
        visiting.add(skill_id)
        for dependency_id in sorted(dependency_graph[skill_id]):
            visit(dependency_id)
        visiting.remove(skill_id)
        visited.add(skill_id)

    for skill_id in sorted(by_id):
        visit(skill_id)


def canonical_skill_manifest_set_bytes(
    document: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> bytes:
    """Return deterministic canonical JSON without mutating the input."""
    validate_skill_manifest_set(document, observed_at=observed_at)
    canonical = copy.deepcopy(document)
    for manifest in canonical["manifests"]:
        manifest["applicability"] = sorted(
            manifest["applicability"],
            key=lambda item: (item["kind"], item["ref"]),
        )
        manifest["rule_ids"] = sorted(manifest["rule_ids"])
        manifest["dependencies"] = sorted(
            manifest["dependencies"],
            key=lambda item: item["skill_id"],
        )
        manifest["conflicts"] = sorted(
            manifest["conflicts"],
            key=lambda item: item["skill_id"],
        )
        manifest["compatibility"]["schema_refs"] = sorted(
            manifest["compatibility"]["schema_refs"]
        )
        manifest["compatibility"]["provider_contract_refs"] = sorted(
            manifest["compatibility"]["provider_contract_refs"]
        )
        manifest["provenance_refs"] = sorted(manifest["provenance_refs"])
    canonical["manifests"] = sorted(
        canonical["manifests"],
        key=lambda item: (item["skill_id"], item["version"]),
    )
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
