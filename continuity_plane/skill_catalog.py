"""Provider-neutral Skill catalog admission and compatibility binding."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from continuity_plane import skill_compatibility, skill_manifest_set

SCHEMA_VERSION = "context.skill-catalog/v1alpha1"

_DOCUMENT_FIELDS = {"schema_version", "catalog_id", "catalog_revision", "entries"}
_ENTRY_FIELDS = {
    "catalog_entry_id",
    "source_kind",
    "manifest",
    "source_url",
    "source_revision",
    "source_path",
    "publisher",
    "license_ref",
    "provenance_refs",
    "capabilities",
    "approval_refs",
    "verification_refs",
    "permissions",
    "status",
}
_PERMISSION_FIELDS = {
    "state_write",
    "task_switch",
    "claim",
    "effect",
    "promotion",
    "evidence_gate",
}
_SOURCE_KINDS = {"builtin", "external", "project", "user", "workflow"}
_CATALOG_STATUSES = {
    "candidate",
    "approved",
    "active",
    "quarantined",
    "deprecated",
    "rejected",
}
_MANIFEST_STATUS_FOR_CATALOG = {
    "candidate": "proposed",
    "approved": "approved",
    "active": "active",
    "quarantined": "quarantined",
    "deprecated": "deprecated",
    "rejected": "rejected",
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_ARTIFACT_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_PINNED_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_AUTHORITY_PERMISSION_FIELDS = tuple(sorted(_PERMISSION_FIELDS))


class SkillCatalogError(ValueError):
    """Raised when catalog metadata cannot be admitted or replayed."""


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SkillCatalogError(f"{field} fields are invalid")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillCatalogError(f"{field} must be a non-empty string")
    return value


def _identifier(value: Any, field: str) -> str:
    value = _string(value, field)
    if not _ID_RE.fullmatch(value):
        raise SkillCatalogError(f"{field} is invalid")
    return value


def _artifact_refs(value: Any, field: str, *, required: bool) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise SkillCatalogError(f"{field} must be a {'non-empty ' if required else ''}list")
    if any(not isinstance(item, str) or not _ARTIFACT_RE.fullmatch(item) for item in value):
        raise SkillCatalogError(f"{field} contains an invalid artifact reference")
    if len(value) != len(set(value)):
        raise SkillCatalogError(f"{field} must be unique")
    return value


def _source_url(value: Any, source_kind: str) -> str:
    value = _string(value, "entry.source_url")
    if "\r" in value or "\n" in value:
        raise SkillCatalogError("entry.source_url cannot contain newlines")
    if source_kind == "builtin" and not value.startswith("builtin://"):
        raise SkillCatalogError("builtin source_url must use builtin://")
    if source_kind == "workflow" and not value.startswith("workflow://"):
        raise SkillCatalogError("workflow source_url must use workflow://")
    if source_kind in {"external", "project", "user"}:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SkillCatalogError("external, project and user source_url must use https:// with a host")
    return value


def _source_path(value: Any) -> str:
    value = _string(value, "entry.source_path")
    if value.startswith(("/", "\\")) or "\\" in value or ".." in value.split("/"):
        raise SkillCatalogError("entry.source_path must be a relative path")
    return value


def _validate_entry(entry: Any, *, observed_at: str | None) -> dict[str, Any]:
    entry = _object(entry, _ENTRY_FIELDS, "entry")
    _identifier(entry["catalog_entry_id"], "entry.catalog_entry_id")
    manifest = entry["manifest"]
    if not isinstance(manifest, dict):
        raise SkillCatalogError("entry.manifest must be an object")
    source_kind = entry["source_kind"]
    if source_kind not in _SOURCE_KINDS:
        raise SkillCatalogError("entry.source_kind is invalid")
    status = entry["status"]
    if status not in _CATALOG_STATUSES:
        raise SkillCatalogError("entry.status is invalid")
    expected_manifest_status = _MANIFEST_STATUS_FOR_CATALOG[status]
    if manifest.get("status") != expected_manifest_status:
        raise SkillCatalogError("entry.status and manifest.status are inconsistent")
    if entry["license_ref"] != manifest.get("license_ref"):
        raise SkillCatalogError("entry.license_ref must match manifest.license_ref")
    isolated_manifest = copy.deepcopy(manifest)
    isolated_manifest["dependencies"] = []
    isolated_manifest["conflicts"] = []
    try:
        skill_manifest_set.validate_skill_manifest_set(
            {
                "schema_version": skill_manifest_set.SCHEMA_VERSION,
                "manifests": [isolated_manifest],
            },
            observed_at=observed_at,
        )
    except skill_manifest_set.SkillManifestSetError as exc:
        raise SkillCatalogError(str(exc)) from exc
    if manifest["source_kind"] != source_kind:
        raise SkillCatalogError("catalog source kind must match manifest source kind")

    _source_url(entry["source_url"], source_kind)
    source_revision = _string(entry["source_revision"], "entry.source_revision")
    if "\n" in source_revision or "\r" in source_revision:
        raise SkillCatalogError("entry.source_revision cannot contain newlines")
    if source_revision == "dynamic-index" and status != "candidate":
        raise SkillCatalogError("dynamic source revisions are candidate-only")
    _source_path(entry["source_path"])
    publisher = _string(entry["publisher"], "entry.publisher")
    if "\r" in publisher or "\n" in publisher:
        raise SkillCatalogError("entry.publisher cannot contain newlines")
    provenance_refs = _artifact_refs(entry["provenance_refs"], "entry.provenance_refs", required=True)
    if set(provenance_refs) != set(manifest["provenance_refs"]):
        raise SkillCatalogError("entry provenance must match manifest provenance")
    capabilities = entry["capabilities"]
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) or not _CAPABILITY_RE.fullmatch(item) for item in capabilities
    ) or len(capabilities) != len(set(capabilities)):
        raise SkillCatalogError("entry.capabilities must be a unique capability list")
    _artifact_refs(entry["approval_refs"], "entry.approval_refs", required=False)
    verification_refs = _artifact_refs(
        entry["verification_refs"], "entry.verification_refs", required=False
    )
    permissions = _object(entry["permissions"], _PERMISSION_FIELDS, "entry.permissions")
    if any(type(permissions[field]) is not bool for field in _AUTHORITY_PERMISSION_FIELDS):
        raise SkillCatalogError("entry.permissions must contain booleans")
    if any(permissions.values()):
        raise SkillCatalogError("Skill catalog entries cannot grant authority")

    if status in {"approved", "active"}:
        if not entry["approval_refs"]:
            raise SkillCatalogError("approved or active entries require approval evidence")
        if not verification_refs:
            raise SkillCatalogError("approved or active entries require verification evidence")
        if source_kind in {"external", "project", "user"} and source_revision == "dynamic-index":
            raise SkillCatalogError("approved or active entries require a pinned source revision")
        if source_kind in {"external", "project", "user"} and not _PINNED_REVISION_RE.fullmatch(source_revision):
            raise SkillCatalogError("approved or active entries require a 40-character commit revision")
    if source_kind == "builtin" and status == "candidate":
        raise SkillCatalogError("builtin core entries cannot be candidate")
    return entry


def validate_skill_catalog(
    document: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> None:
    """Validate catalog metadata and fail closed on unapproved authority."""
    root = _object(document, _DOCUMENT_FIELDS, "document")
    if root["schema_version"] != SCHEMA_VERSION:
        raise SkillCatalogError("unsupported schema_version")
    _identifier(root["catalog_id"], "catalog_id")
    revision = _string(root["catalog_revision"], "catalog_revision")
    if not _RFC3339_RE.fullmatch(revision):
        raise SkillCatalogError("catalog_revision must be RFC3339")
    try:
        datetime.fromisoformat(revision.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SkillCatalogError("catalog_revision must be RFC3339") from exc
    if observed_at is not None and not _RFC3339_RE.fullmatch(observed_at):
        raise SkillCatalogError("observed_at must be RFC3339")
    entries = root["entries"]
    if not isinstance(entries, list) or not entries:
        raise SkillCatalogError("entries must be a non-empty list")
    seen: set[str] = set()
    for entry in entries:
        validated = _validate_entry(entry, observed_at=observed_at)
        entry_id = validated["catalog_entry_id"]
        if entry_id in seen:
            raise SkillCatalogError(f"duplicate catalog_entry_id: {entry_id}")
        seen.add(entry_id)

    dependency_set = {
        "schema_version": skill_manifest_set.SCHEMA_VERSION,
        "manifests": [copy.deepcopy(entry["manifest"]) for entry in entries],
    }
    for manifest in dependency_set["manifests"]:
        manifest["conflicts"] = []
    try:
        skill_manifest_set.validate_skill_manifest_set(
            dependency_set,
            observed_at=observed_at,
        )
    except skill_manifest_set.SkillManifestSetError as exc:
        raise SkillCatalogError(str(exc)) from exc


def canonical_skill_catalog_bytes(
    document: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> bytes:
    """Return stable bytes for a validated catalog without mutating input."""
    validate_skill_catalog(document, observed_at=observed_at)
    canonical = copy.deepcopy(document)
    canonical["entries"] = sorted(
        canonical["entries"], key=lambda item: item["catalog_entry_id"]
    )
    manifests = [copy.deepcopy(entry["manifest"]) for entry in canonical["entries"]]
    conflicts_by_skill = {
        manifest["skill_id"]: copy.deepcopy(manifest["conflicts"])
        for manifest in manifests
    }
    for manifest in manifests:
        manifest["conflicts"] = []
    canonical_manifest_set = json.loads(
        skill_manifest_set.canonical_skill_manifest_set_bytes(
            {
                "schema_version": skill_manifest_set.SCHEMA_VERSION,
                "manifests": manifests,
            },
            observed_at=observed_at,
        )
    )
    canonical_manifests = {
        manifest["skill_id"]: manifest
        for manifest in canonical_manifest_set["manifests"]
    }
    for skill_id, manifest in canonical_manifests.items():
        manifest["conflicts"] = sorted(
            conflicts_by_skill[skill_id],
            key=lambda item: item["skill_id"],
        )
    for entry in canonical["entries"]:
        entry["manifest"] = canonical_manifests[entry["manifest"]["skill_id"]]
        for field in ("provenance_refs", "capabilities", "approval_refs", "verification_refs"):
            entry[field] = sorted(entry[field])
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def skill_catalog_digest(document: dict[str, Any], *, observed_at: str | None = None) -> str:
    return hashlib.sha256(
        canonical_skill_catalog_bytes(document, observed_at=observed_at)
    ).hexdigest()


def active_skill_manifest_set(
    document: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Project approved/active catalog entries into the M4-01 manifest contract."""
    validate_skill_catalog(document, observed_at=observed_at)
    manifests = [
        copy.deepcopy(entry["manifest"])
        for entry in document["entries"]
        if entry["status"] in {"approved", "active"}
    ]
    if not manifests:
        raise SkillCatalogError("catalog has no approved or active manifests")
    manifest_set = {
        "schema_version": skill_manifest_set.SCHEMA_VERSION,
        "manifests": manifests,
    }
    try:
        skill_manifest_set.validate_skill_manifest_set(
            manifest_set, observed_at=observed_at
        )
    except skill_manifest_set.SkillManifestSetError as exc:
        raise SkillCatalogError(str(exc)) from exc
    return json.loads(skill_manifest_set.canonical_skill_manifest_set_bytes(manifest_set))


def validate_catalog_lock_binding(entry: dict[str, Any], lock: dict[str, Any]) -> None:
    """Ensure an M4-06 lock still names the exact catalog manifest identity."""
    _validate_entry(entry, observed_at=None)
    if entry["status"] not in {"approved", "active"}:
        raise SkillCatalogError("compatibility lock can bind only an approved or active catalog entry")
    try:
        skill_compatibility.validate_skill_compatibility_lock(lock)
    except skill_compatibility.SkillCompatibilityError as exc:
        raise SkillCatalogError(str(exc)) from exc
    manifest = entry["manifest"]
    selected = [item for item in lock["skills"] if item["skill_id"] == manifest["skill_id"]]
    if len(selected) != 1:
        raise SkillCatalogError("compatibility lock does not select catalog manifest")
    locked = selected[0]
    manifest_set = {
        "schema_version": skill_manifest_set.SCHEMA_VERSION,
        "manifests": [manifest],
    }
    canonical_manifest = json.loads(
        skill_manifest_set.canonical_skill_manifest_set_bytes(manifest_set)
    )["manifests"][0]
    manifest_digest = hashlib.sha256(
        json.dumps(
            canonical_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if locked["manifest_sha256"] != manifest_digest:
        raise SkillCatalogError("compatibility lock manifest digest does not match catalog manifest")
    for field in ("version", "content_sha256", "rule_ids"):
        expected = sorted(manifest[field]) if field == "rule_ids" else manifest[field]
        actual = sorted(locked[field]) if field == "rule_ids" else locked[field]
        if actual != expected:
            raise SkillCatalogError(f"compatibility lock {field} does not match catalog manifest")
