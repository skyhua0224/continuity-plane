"""Bounded external Skill source snapshots with offline artifact replay."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from context_control_plane import skill_catalog, skill_manifest_set
from context_control_plane.artifact_store import (
    ArtifactRef,
    ArtifactStoreError,
    LocalArtifactStore,
)

SCHEMA_VERSION = "context.external-skill-source-snapshot/v1alpha1"
RESOURCE_MANIFEST_VERSION = "context.external-skill-resource-manifest/v1alpha1"
MAX_SOURCES = 256
MAX_RESOURCES_PER_SOURCE = 256
MAX_RETRIEVAL_BYTES = 4 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 1024 * 1024
MAX_LIST_ITEMS = 4096

_REQUEST_FIELDS = {
    "source_id",
    "adapter_id",
    "adapter_version",
    "resource_kind",
    "source_revision",
    "source_path",
    "retrieval_url",
}
_OBSERVATION_FIELDS = {
    "final_url",
    "retrieved_at",
    "acquisition",
    "files",
    "expected_paths",
    "license_ref",
    "license_evidence",
    "publisher_evidence",
    "declared_capabilities",
}
_TREE_OBSERVATION_FIELDS = {
    "final_url",
    "source_revision",
    "truncated",
    "entries",
}
_TREE_ENTRY_FIELDS = {"path", "kind"}
_FILE_FIELDS = {"path", "kind", "content"}
_DOCUMENT_FIELDS = {
    "schema_version",
    "snapshot_id",
    "generator_version",
    "generated_at",
    "input_sha256",
    "adapter_policy_sha256",
    "sources",
}
_SOURCE_FIELDS = {
    "source_id",
    "source_class",
    "resource_kind",
    "adapter_id",
    "adapter_version",
    "publisher_id",
    "publisher_verification_refs",
    "canonical_url",
    "retrieval_url",
    "final_url",
    "revision_kind",
    "source_revision",
    "retrieved_at",
    "acquisition",
    "source_path",
    "content_size_bytes",
    "content_sha256",
    "content_artifact_ref",
    "resource_count",
    "resource_tree_sha256",
    "resource_manifest_size_bytes",
    "resource_manifest_ref",
    "tree_evidence_ref",
    "license_ref",
    "license_evidence_refs",
    "declared_capabilities",
    "provenance_refs",
    "trust_tier",
    "status",
    "quarantine_reasons",
}
_RESOURCE_FIELDS = {"path", "kind", "size_bytes", "sha256", "artifact_ref"}
_MANIFEST_FIELDS = {
    "schema_version",
    "source_id",
    "source_revision",
    "final_url",
    "retrieved_at",
    "acquisition",
    "expected_paths",
    "tree_evidence_ref",
    "license_ref",
    "license_evidence_refs",
    "publisher_verification_refs",
    "declared_capabilities",
    "resources",
}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_ARTIFACT_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_CAPABILITY_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_RESOURCE_KINDS = {"skill", "standard", "catalog-index", "reference"}
_QUARANTINE_REASONS = {
    "license-unverified",
    "mutable-source-revision",
    "publisher-unverified",
    "retrieval-url-not-revision-bound",
    "redirect-outside-policy",
    "source-path-outside-policy",
    "resource-missing",
    "resource-unlisted",
    "unsafe-resource-kind",
    "resource-outside-scope",
    "resource-tree-unverified",
}
_POLICY = {
    "adapter.openai-plugins-git": {
        "adapter_version": "1.0.0",
        "source_class": "official",
        "trust_tier": "official",
        "publisher_id": "openai",
        "canonical_url": "https://github.com/openai/plugins",
        "raw_root": "https://raw.githubusercontent.com/openai/plugins",
        "tree_url_template": "https://api.github.com/repos/openai/plugins/git/trees/{revision}?recursive=1",
        "path_exact": "plugins/build-web-data-visualization/skills/node-link-and-diagram-layout/SKILL.md",
        "revision_kind": "git-commit",
        "resource_kinds": ["skill", "reference"],
        "license_ref": "MIT",
    },
    "adapter.agent-skills-standard-git": {
        "adapter_version": "1.0.0",
        "source_class": "standard",
        "trust_tier": "standard",
        "publisher_id": "agent-skills",
        "canonical_url": "https://github.com/agentskills/agentskills",
        "raw_root": "https://raw.githubusercontent.com/agentskills/agentskills",
        "tree_url_template": "https://api.github.com/repos/agentskills/agentskills/git/trees/{revision}?recursive=1",
        "path_exact": "docs/specification.mdx",
        "revision_kind": "git-commit",
        "resource_kinds": ["standard"],
        "license_ref": "CC-BY-4.0",
    },
    "adapter.github-awesome-copilot-git": {
        "adapter_version": "1.0.0",
        "source_class": "verified-organization",
        "trust_tier": "verified-organization",
        "publisher_id": "github-awesome-copilot-community",
        "canonical_url": "https://github.com/github/awesome-copilot",
        "raw_root": "https://raw.githubusercontent.com/github/awesome-copilot",
        "tree_url_template": "https://api.github.com/repos/github/awesome-copilot/git/trees/{revision}?recursive=1",
        "path_prefix": "skills/",
        "path_suffix": "/SKILL.md",
        "revision_kind": "git-commit",
        "resource_kinds": ["skill", "reference"],
        "license_ref": "MIT",
    },
    "adapter.skills-sh-index": {
        "adapter_version": "1.0.0",
        "source_class": "marketplace-community",
        "trust_tier": "marketplace-community",
        "publisher_id": "skills-sh",
        "canonical_url": "https://skills.sh/",
        "retrieval_host": "skills.sh",
        "retrieval_url_exact": "https://skills.sh/api/search?q=skills&limit=20",
        "path_exact": "index.json",
        "revision_kind": "dynamic-index",
        "resource_kinds": ["catalog-index"],
        "license_ref": None,
    },
}


class ExternalSkillSourceError(ValueError):
    """Raised when a source observation cannot satisfy the snapshot contract."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


_CURRENT_POLICY = copy.deepcopy(_POLICY)
_CURRENT_POLICY_BYTES = _canonical(_CURRENT_POLICY)
_CURRENT_POLICY_SHA256 = hashlib.sha256(_CURRENT_POLICY_BYTES).hexdigest()
_POLICY_REGISTRY = {_CURRENT_POLICY_SHA256: _CURRENT_POLICY_BYTES}


def _policy_snapshot(policy_sha256: str | None = None) -> tuple[str, dict[str, Any]]:
    digest = policy_sha256 or _CURRENT_POLICY_SHA256
    payload = _POLICY_REGISTRY.get(digest)
    if payload is None:
        raise ExternalSkillSourceError("unknown adapter policy identity")
    return digest, json.loads(payload)


def adapter_policy_sha256() -> str:
    """Return the immutable identity of the built-in adapter policy."""
    return _CURRENT_POLICY_SHA256


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ExternalSkillSourceError(f"{field} fields are invalid")
    return value


def _string(value: Any, field: str, *, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ExternalSkillSourceError(f"{field} must be a bounded non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ExternalSkillSourceError(f"{field} contains control characters")
    return value


def _identifier(value: Any, field: str) -> str:
    value = _string(value, field)
    if not _ID_RE.fullmatch(value):
        raise ExternalSkillSourceError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    value = _string(value, field, maximum=64)
    if not _TIMESTAMP_RE.fullmatch(value):
        raise ExternalSkillSourceError(f"{field} must be RFC3339")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExternalSkillSourceError(f"{field} must be RFC3339") from exc
    return value


def _timestamp_value(value: Any, field: str) -> datetime:
    return datetime.fromisoformat(_timestamp(value, field).replace("Z", "+00:00"))


def _bounded_strings(
    value: Any,
    field: str,
    *,
    required: bool = False,
    pattern: re.Pattern[str] | None = None,
) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIST_ITEMS:
        raise ExternalSkillSourceError(f"{field} must be a bounded list")
    if required and not value:
        raise ExternalSkillSourceError(f"{field} must not be empty")
    result = []
    for index, item in enumerate(value):
        item = _string(item, f"{field}[{index}]", maximum=2048)
        if pattern is not None and not pattern.fullmatch(item):
            raise ExternalSkillSourceError(f"{field}[{index}] is invalid")
        result.append(item)
    if len(result) != len(set(result)):
        raise ExternalSkillSourceError(f"{field} must contain unique values")
    return result


def _artifact_refs(value: Any, field: str, *, required: bool) -> list[str]:
    return _bounded_strings(value, field, required=required, pattern=_ARTIFACT_RE)


def _safe_path(value: Any, field: str) -> bool:
    if not isinstance(value, str) or not value or len(value) > 2048:
        return False
    decoded = unquote(value)
    if decoded != value or "\\" in value or "\x00" in value or value.startswith("/"):
        return False
    path = PurePosixPath(value)
    return (
        path.as_posix() == value
        and "//" not in value
        and not value.endswith("/")
        and not any(part in {"", ".", ".."} for part in path.parts)
    )


def _valid_https_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
        and not any(ord(character) < 32 for character in value)
    )


def _path_allowed(path: str, policy: dict[str, Any]) -> bool:
    if not _safe_path(path, "source_path"):
        return False
    exact = policy.get("path_exact")
    if exact is not None:
        return path == exact
    return path.startswith(policy["path_prefix"]) and path.endswith(
        policy["path_suffix"]
    )


def _expected_retrieval_url(
    request: dict[str, Any], policy: dict[str, Any]
) -> str | None:
    if policy["revision_kind"] != "git-commit":
        return policy.get("retrieval_url_exact")
    return f"{policy['raw_root']}/{request['source_revision']}/{request['source_path']}"


def _request_preflight(
    requests: Any, *, policy_snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    if not isinstance(requests, list) or not requests or len(requests) > MAX_SOURCES:
        raise ExternalSkillSourceError("sources must be a bounded non-empty list")
    result = []
    seen = set()
    seen_source_identity = set()
    for index, request in enumerate(requests):
        request = _object(request, _REQUEST_FIELDS, f"sources[{index}]")
        source_id = _identifier(request["source_id"], f"sources[{index}].source_id")
        if source_id in seen:
            raise ExternalSkillSourceError("source_id must be unique")
        seen.add(source_id)
        adapter_id = _identifier(request["adapter_id"], f"sources[{index}].adapter_id")
        if adapter_id not in policy_snapshot:
            raise ExternalSkillSourceError("unknown adapter_id")
        if not _SEMVER_RE.fullmatch(
            _string(request["adapter_version"], f"sources[{index}].adapter_version")
        ):
            raise ExternalSkillSourceError("adapter_version must be SemVer")
        if request["resource_kind"] not in _RESOURCE_KINDS:
            raise ExternalSkillSourceError("resource_kind is invalid")
        revision = _string(
            request["source_revision"], f"sources[{index}].source_revision"
        )
        policy = policy_snapshot[adapter_id]
        if request["adapter_version"] != policy["adapter_version"]:
            raise ExternalSkillSourceError(
                "adapter_version does not match adapter policy"
            )
        if policy["revision_kind"] == "git-commit" and not _GIT_REVISION_RE.fullmatch(
            revision
        ):
            raise ExternalSkillSourceError("Git adapter requires an immutable commit")
        if policy["revision_kind"] == "dynamic-index" and revision != "dynamic-index":
            raise ExternalSkillSourceError(
                "index adapter requires dynamic-index revision"
            )
        _string(request["source_path"], f"sources[{index}].source_path", maximum=2048)
        _string(
            request["retrieval_url"], f"sources[{index}].retrieval_url", maximum=4096
        )
        if not _path_allowed(request["source_path"], policy):
            raise ExternalSkillSourceError("source_path is outside adapter policy")
        if not _valid_https_url(request["retrieval_url"]):
            raise ExternalSkillSourceError("retrieval_url is invalid")
        expected_url = _expected_retrieval_url(request, policy)
        if expected_url is not None and request["retrieval_url"] != expected_url:
            raise ExternalSkillSourceError("retrieval_url is outside adapter policy")
        if (
            expected_url is None
            and urlsplit(request["retrieval_url"]).hostname != policy["retrieval_host"]
        ):
            raise ExternalSkillSourceError(
                "retrieval_url host is outside adapter policy"
            )
        if request["resource_kind"] not in policy["resource_kinds"]:
            raise ExternalSkillSourceError("resource_kind is outside adapter policy")
        identity = (adapter_id, revision, request["source_path"])
        if identity in seen_source_identity:
            raise ExternalSkillSourceError("source revision and path must be unique")
        seen_source_identity.add(identity)
        result.append(copy.deepcopy(request))
    canonical = _canonical(sorted(result, key=lambda item: item["source_id"]))
    if len(canonical) > MAX_REQUEST_BYTES:
        raise ExternalSkillSourceError("canonical request exceeds byte bound")
    return result


def external_skill_tree_url(request: dict[str, Any]) -> str:
    """Return the pinned Git tree URL fixed by the registered adapter policy."""
    _, policies = _policy_snapshot()
    adapter_id = request.get("adapter_id") if isinstance(request, dict) else None
    if adapter_id not in policies:
        raise ExternalSkillSourceError("unknown adapter_id")
    policy = policies[adapter_id]
    template = policy.get("tree_url_template")
    if template is None or request.get("resource_kind") != "skill":
        raise ExternalSkillSourceError("source does not use a Skill tree")
    revision = request.get("source_revision")
    if not isinstance(revision, str) or not _GIT_REVISION_RE.fullmatch(revision):
        raise ExternalSkillSourceError("Git revision is invalid")
    return template.format(revision=revision)


def _input_sha256(requests: list[dict[str, Any]]) -> str:
    return hashlib.sha256(
        _canonical(sorted(requests, key=lambda item: item["source_id"]))
    ).hexdigest()


def _put_optional_evidence(
    store: LocalArtifactStore, payload: Any, field: str
) -> list[str]:
    if payload is None:
        return []
    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_RETRIEVAL_BYTES
    ):
        raise ExternalSkillSourceError(f"{field} must be bounded non-empty bytes")
    return [store.put_bytes(payload).uri]


class _StagingArtifactStore:
    """Compute CAS identities in memory and publish only after full preflight."""

    def __init__(self) -> None:
        self.payloads: dict[str, bytes] = {}

    def put_bytes(self, payload: bytes) -> ArtifactRef:
        if not isinstance(payload, bytes):
            raise ExternalSkillSourceError("staged artifact must be bytes")
        ref = ArtifactRef(hashlib.sha256(payload).hexdigest(), len(payload))
        self.payloads[ref.uri] = payload
        return ref

    def publish(self, store: LocalArtifactStore) -> None:
        for uri, payload in sorted(self.payloads.items()):
            if store.put_bytes(payload).uri != uri:
                raise ExternalSkillSourceError("artifact publication identity mismatch")


def _skill_tree_observation(
    request: dict[str, Any], observation: Any, *, policy: dict[str, Any]
) -> tuple[list[str] | None, bytes | None, set[str]]:
    reasons: set[str] = set()
    if observation is None:
        return None, None, {"resource-tree-unverified"}
    tree = _object(observation, _TREE_OBSERVATION_FIELDS, "tree observation")
    expected_url = policy["tree_url_template"].format(
        revision=request["source_revision"]
    )
    if tree["final_url"] != expected_url:
        raise ExternalSkillSourceError("tree final_url is outside adapter policy")
    if tree["source_revision"] != request["source_revision"]:
        raise ExternalSkillSourceError("tree revision does not match source revision")
    if type(tree["truncated"]) is not bool:
        raise ExternalSkillSourceError("tree truncated must be boolean")
    if tree["truncated"]:
        reasons.add("resource-tree-unverified")
    entries = tree["entries"]
    if not isinstance(entries, list) or len(entries) > MAX_LIST_ITEMS:
        raise ExternalSkillSourceError("tree entries must be a bounded list")
    skill_root = PurePosixPath(request["source_path"]).parent
    expected_paths = []
    seen = set()
    for index, entry in enumerate(entries):
        entry = _object(entry, _TREE_ENTRY_FIELDS, f"tree.entries[{index}]")
        path = _string(entry["path"], f"tree.entries[{index}].path", maximum=2048)
        kind = _string(entry["kind"], f"tree.entries[{index}].kind", maximum=32)
        if not _safe_path(path, f"tree.entries[{index}].path"):
            raise ExternalSkillSourceError("tree entry path is not canonical")
        if kind not in {"file", "symlink", "submodule"}:
            raise ExternalSkillSourceError("tree entry kind is invalid")
        if path in seen:
            raise ExternalSkillSourceError("tree entry paths must be unique")
        seen.add(path)
        candidate = PurePosixPath(path)
        if candidate == skill_root or skill_root in candidate.parents:
            if kind != "file":
                reasons.add("unsafe-resource-kind")
            expected_paths.append(path)
    if request["source_path"] not in expected_paths:
        reasons.add("resource-missing")
    if len(expected_paths) > MAX_RESOURCES_PER_SOURCE:
        reasons.add("resource-tree-unverified")
    tree_bytes = _canonical(tree)
    if len(tree_bytes) > MAX_RETRIEVAL_BYTES:
        raise ExternalSkillSourceError("tree observation exceeds byte bound")
    return sorted(expected_paths), tree_bytes, reasons


def _observation_to_source(
    request: dict[str, Any],
    observation: Any,
    *,
    artifact_store: _StagingArtifactStore,
    policy: dict[str, Any],
    expected_paths: list[str] | None,
    tree_evidence: bytes | None,
    tree_reasons: set[str],
) -> dict[str, Any]:
    observation = _object(observation, _OBSERVATION_FIELDS, "retrieval observation")
    _timestamp(observation["retrieved_at"], "retrieved_at")
    _string(observation["acquisition"], "acquisition", maximum=64)
    if observation["acquisition"] != "https-get":
        raise ExternalSkillSourceError("unsupported acquisition")
    files = observation["files"]
    if (
        not isinstance(files, list)
        or not files
        or len(files) > MAX_RESOURCES_PER_SOURCE
    ):
        raise ExternalSkillSourceError("files must be a bounded non-empty list")
    declared_expected_paths = _bounded_strings(
        observation["expected_paths"], "expected_paths", required=True
    )
    if expected_paths is None:
        expected_paths = declared_expected_paths
    capabilities = _bounded_strings(
        observation["declared_capabilities"],
        "declared_capabilities",
        pattern=_CAPABILITY_RE,
    )
    total_size = 0
    prepared_resources = []
    unsafe_kind = False
    for index, item in enumerate(files):
        item = _object(item, _FILE_FIELDS, f"files[{index}]")
        path = _string(item["path"], f"files[{index}].path", maximum=2048)
        kind = _string(item["kind"], f"files[{index}].kind", maximum=32)
        content = item["content"]
        if not isinstance(content, bytes):
            raise ExternalSkillSourceError("resource content must be bytes")
        total_size += len(content)
        if total_size > MAX_RETRIEVAL_BYTES:
            raise ExternalSkillSourceError("retrieval exceeds byte bound")
        if kind != "file" or not _safe_path(path, f"files[{index}].path"):
            unsafe_kind = True
        prepared_resources.append((path, kind, content))
    for field in ("publisher_evidence", "license_evidence"):
        payload = observation[field]
        if payload is not None and (
            not isinstance(payload, bytes)
            or not payload
            or len(payload) > MAX_RETRIEVAL_BYTES
        ):
            raise ExternalSkillSourceError(f"{field} must be bounded non-empty bytes")
        if payload is not None:
            total_size += len(payload)
    if total_size > MAX_RETRIEVAL_BYTES:
        raise ExternalSkillSourceError("retrieval observation exceeds byte bound")
    if len({path for path, _, _ in prepared_resources}) != len(prepared_resources):
        raise ExternalSkillSourceError("resource paths must be unique")
    if observation["license_ref"] != policy["license_ref"]:
        raise ExternalSkillSourceError("license_ref does not match adapter policy")

    resources = []
    for path, kind, content in prepared_resources:
        ref = artifact_store.put_bytes(content)
        resources.append(
            {
                "path": path,
                "kind": kind,
                "size_bytes": len(content),
                "sha256": ref.digest,
                "artifact_ref": ref.uri,
            }
        )
    resources.sort(key=lambda item: item["path"])
    publisher_refs = _put_optional_evidence(
        artifact_store, observation["publisher_evidence"], "publisher_evidence"
    )
    license_refs = _put_optional_evidence(
        artifact_store, observation["license_evidence"], "license_evidence"
    )
    tree_ref = artifact_store.put_bytes(tree_evidence).uri if tree_evidence else None
    manifest = {
        "schema_version": RESOURCE_MANIFEST_VERSION,
        "source_id": request["source_id"],
        "source_revision": request["source_revision"],
        "final_url": observation["final_url"],
        "retrieved_at": observation["retrieved_at"],
        "acquisition": observation["acquisition"],
        "expected_paths": sorted(expected_paths),
        "tree_evidence_ref": tree_ref,
        "license_ref": policy["license_ref"],
        "license_evidence_refs": license_refs,
        "publisher_verification_refs": publisher_refs,
        "declared_capabilities": sorted(capabilities),
        "resources": resources,
    }
    manifest_bytes = _canonical(manifest)
    manifest_ref = artifact_store.put_bytes(manifest_bytes)
    primary = next(
        (item for item in resources if item["path"] == request["source_path"]), None
    )
    if primary is None:
        primary = resources[0]

    reasons = set(tree_reasons)
    if policy["revision_kind"] == "dynamic-index":
        reasons.add("mutable-source-revision")
    if policy["license_ref"] is None or not license_refs:
        reasons.add("license-unverified")
    if not publisher_refs:
        reasons.add("publisher-unverified")
    expected_url = _expected_retrieval_url(request, policy)
    if expected_url is not None and request["retrieval_url"] != expected_url:
        reasons.add("retrieval-url-not-revision-bound")
    if not _path_allowed(request["source_path"], policy):
        reasons.add("source-path-outside-policy")
    if not _valid_https_url(request["retrieval_url"]):
        reasons.add("retrieval-url-not-revision-bound")
    final_url = observation["final_url"]
    if (
        not _valid_https_url(final_url)
        or expected_url is not None
        and final_url != request["retrieval_url"]
        or expected_url is None
        and urlsplit(final_url).hostname != policy["retrieval_host"]
    ):
        reasons.add("redirect-outside-policy")
    actual_paths = {item["path"] for item in resources}
    expected_path_set = set(expected_paths)
    if expected_path_set - actual_paths:
        reasons.add("resource-missing")
    if actual_paths - expected_path_set:
        reasons.add("resource-unlisted")
    if unsafe_kind:
        reasons.add("unsafe-resource-kind")
    if request["resource_kind"] == "skill":
        skill_root = PurePosixPath(request["source_path"]).parent
        if any(
            PurePosixPath(path) != skill_root
            and skill_root not in PurePosixPath(path).parents
            for path in actual_paths
        ):
            reasons.add("resource-outside-scope")
    if primary["path"] != request["source_path"]:
        reasons.add("resource-missing")
    provenance = sorted(
        {
            primary["artifact_ref"],
            manifest_ref.uri,
            *([tree_ref] if tree_ref is not None else []),
            *publisher_refs,
            *license_refs,
        }
    )
    return {
        "source_id": request["source_id"],
        "source_class": policy["source_class"],
        "resource_kind": request["resource_kind"],
        "adapter_id": request["adapter_id"],
        "adapter_version": request["adapter_version"],
        "publisher_id": policy["publisher_id"],
        "publisher_verification_refs": publisher_refs,
        "canonical_url": policy["canonical_url"],
        "retrieval_url": request["retrieval_url"],
        "final_url": final_url,
        "revision_kind": policy["revision_kind"],
        "source_revision": request["source_revision"],
        "retrieved_at": observation["retrieved_at"],
        "acquisition": observation["acquisition"],
        "source_path": request["source_path"],
        "content_size_bytes": primary["size_bytes"],
        "content_sha256": primary["sha256"],
        "content_artifact_ref": primary["artifact_ref"],
        "resource_count": len(resources),
        "resource_tree_sha256": manifest_ref.digest,
        "resource_manifest_size_bytes": len(manifest_bytes),
        "resource_manifest_ref": manifest_ref.uri,
        "tree_evidence_ref": tree_ref,
        "license_ref": policy["license_ref"],
        "license_evidence_refs": license_refs,
        "declared_capabilities": sorted(capabilities),
        "provenance_refs": provenance,
        "trust_tier": policy["trust_tier"],
        "status": "quarantined" if reasons else "candidate",
        "quarantine_reasons": sorted(reasons),
    }


def _snapshot_identity(document: dict[str, Any]) -> str:
    identity = copy.deepcopy(document)
    identity.pop("snapshot_id", None)
    digest = hashlib.sha256(_canonical(identity)).hexdigest()
    return f"external-skill-source-snapshot/{digest}"


def create_external_skill_source_snapshot(
    sources: list[dict[str, Any]],
    *,
    retriever: Callable[..., dict[str, Any]],
    tree_retriever: Callable[..., dict[str, Any]] | None = None,
    artifact_store: LocalArtifactStore,
    generator_version: str,
    generated_at: str,
) -> dict[str, Any]:
    """Retrieve each bounded source once and freeze its bytes in the local CAS."""
    policy_sha256, policies = _policy_snapshot()
    requests = _request_preflight(sources, policy_snapshot=policies)
    if not callable(retriever):
        raise ExternalSkillSourceError("retriever must be callable")
    if tree_retriever is not None and not callable(tree_retriever):
        raise ExternalSkillSourceError("tree_retriever must be callable")
    if not isinstance(artifact_store, LocalArtifactStore):
        raise ExternalSkillSourceError("artifact_store must be LocalArtifactStore")
    if not _SEMVER_RE.fullmatch(_string(generator_version, "generator_version")):
        raise ExternalSkillSourceError("generator_version must be SemVer")
    _timestamp(generated_at, "generated_at")
    staging = _StagingArtifactStore()
    items = []
    for request in requests:
        policy = policies[request["adapter_id"]]
        expected_paths = None
        tree_evidence = None
        tree_reasons: set[str] = set()
        if request["resource_kind"] == "skill":
            tree_observation = None
            if tree_retriever is not None:
                tree_request = {
                    "source_id": request["source_id"],
                    "source_revision": request["source_revision"],
                    "source_path": request["source_path"],
                    "tree_url": policy["tree_url_template"].format(
                        revision=request["source_revision"]
                    ),
                }
                tree_observation = tree_retriever(
                    tree_request, max_bytes=MAX_RETRIEVAL_BYTES
                )
            expected_paths, tree_evidence, tree_reasons = _skill_tree_observation(
                request, tree_observation, policy=policy
            )
        observation = retriever(copy.deepcopy(request), max_bytes=MAX_RETRIEVAL_BYTES)
        item = _observation_to_source(
            request,
            observation,
            artifact_store=staging,
            policy=policy,
            expected_paths=expected_paths,
            tree_evidence=tree_evidence,
            tree_reasons=tree_reasons,
        )
        if _timestamp_value(item["retrieved_at"], "retrieved_at") > _timestamp_value(
            generated_at, "generated_at"
        ):
            raise ExternalSkillSourceError("retrieved_at cannot follow generated_at")
        items.append(item)
    document = {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": "external-skill-source-snapshot/pending",
        "generator_version": generator_version,
        "generated_at": generated_at,
        "input_sha256": _input_sha256(requests),
        "adapter_policy_sha256": policy_sha256,
        "sources": sorted(items, key=lambda item: item["source_id"]),
    }
    document["snapshot_id"] = _snapshot_identity(document)
    validate_external_skill_source_snapshot_metadata(document)
    if len(_canonical(document)) > MAX_SNAPSHOT_BYTES:
        raise ExternalSkillSourceError("snapshot exceeds byte bound")
    staging.publish(artifact_store)
    validate_external_skill_source_snapshot(document, artifact_store=artifact_store)
    return document


def _read_uri(store: LocalArtifactStore, uri: str) -> bytes:
    if not isinstance(uri, str) or not _ARTIFACT_RE.fullmatch(uri):
        raise ExternalSkillSourceError("artifact URI is invalid")
    try:
        probe = ArtifactRef.from_uri(uri, 0)
        path = store.object_path(probe)
        size = path.stat(follow_symlinks=False).st_size
        return store.read(ArtifactRef.from_uri(uri, size))
    except (ArtifactStoreError, OSError, ValueError) as exc:
        raise ExternalSkillSourceError("artifact is missing or corrupt") from exc


def _validate_resource_manifest(
    source: dict[str, Any], *, artifact_store: LocalArtifactStore
) -> dict[str, Any]:
    payload = _read_uri(artifact_store, source["resource_manifest_ref"])
    if len(payload) != source["resource_manifest_size_bytes"]:
        raise ExternalSkillSourceError("resource manifest size mismatch")
    if hashlib.sha256(payload).hexdigest() != source["resource_tree_sha256"]:
        raise ExternalSkillSourceError("resource tree digest mismatch")
    try:
        manifest = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExternalSkillSourceError("resource manifest is invalid") from exc
    _object(manifest, _MANIFEST_FIELDS, "resource manifest")
    if manifest["schema_version"] != RESOURCE_MANIFEST_VERSION:
        raise ExternalSkillSourceError("resource manifest version is invalid")
    if (
        manifest["source_id"] != source["source_id"]
        or manifest["source_revision"] != source["source_revision"]
    ):
        raise ExternalSkillSourceError("resource manifest identity mismatch")
    manifest_bindings = {
        "final_url": source["final_url"],
        "retrieved_at": source["retrieved_at"],
        "acquisition": source["acquisition"],
        "license_ref": source["license_ref"],
        "license_evidence_refs": source["license_evidence_refs"],
        "publisher_verification_refs": source["publisher_verification_refs"],
        "declared_capabilities": source["declared_capabilities"],
        "tree_evidence_ref": source["tree_evidence_ref"],
    }
    if any(
        manifest[field] != expected for field, expected in manifest_bindings.items()
    ):
        raise ExternalSkillSourceError("resource manifest observation binding mismatch")
    _bounded_strings(
        manifest["expected_paths"], "resource manifest expected_paths", required=True
    )
    resources = manifest["resources"]
    if not isinstance(resources, list) or len(resources) != source["resource_count"]:
        raise ExternalSkillSourceError("resource count mismatch")
    seen = set()
    primary = None
    for index, resource in enumerate(resources):
        resource = _object(resource, _RESOURCE_FIELDS, f"resource[{index}]")
        path = _string(resource["path"], f"resource[{index}].path", maximum=2048)
        if path in seen:
            raise ExternalSkillSourceError("resource paths must be unique")
        seen.add(path)
        if resource["kind"] not in {"file", "symlink", "submodule"}:
            raise ExternalSkillSourceError("resource kind is invalid")
        size = resource["size_bytes"]
        if type(size) is not int or size < 0 or size > MAX_RETRIEVAL_BYTES:
            raise ExternalSkillSourceError("resource size is invalid")
        if not isinstance(resource["sha256"], str) or not _SHA_RE.fullmatch(
            resource["sha256"]
        ):
            raise ExternalSkillSourceError("resource digest is invalid")
        if resource["artifact_ref"] != f"artifact://sha256/{resource['sha256']}":
            raise ExternalSkillSourceError("resource artifact identity mismatch")
        content = _read_uri(artifact_store, resource["artifact_ref"])
        if (
            len(content) != size
            or hashlib.sha256(content).hexdigest() != resource["sha256"]
        ):
            raise ExternalSkillSourceError("resource artifact mismatch")
        if path == source["source_path"]:
            primary = resource
    if primary is None:
        raise ExternalSkillSourceError("primary resource is missing")
    for field, expected in (
        ("content_size_bytes", primary["size_bytes"]),
        ("content_sha256", primary["sha256"]),
        ("content_artifact_ref", primary["artifact_ref"]),
    ):
        if source[field] != expected:
            raise ExternalSkillSourceError(f"{field} does not match primary resource")
    return manifest


def _semantic_reasons(source: dict[str, Any], *, policy: dict[str, Any]) -> set[str]:
    reasons = set()
    if policy["revision_kind"] == "dynamic-index":
        reasons.add("mutable-source-revision")
    if source["license_ref"] is None or not source["license_evidence_refs"]:
        reasons.add("license-unverified")
    if not source["publisher_verification_refs"]:
        reasons.add("publisher-unverified")
    if source["resource_kind"] == "skill" and source["tree_evidence_ref"] is None:
        reasons.add("resource-tree-unverified")
    expected_url = _expected_retrieval_url(source, policy)
    if expected_url is not None and source["retrieval_url"] != expected_url:
        reasons.add("retrieval-url-not-revision-bound")
    if not _path_allowed(source["source_path"], policy):
        reasons.add("source-path-outside-policy")
    if not _valid_https_url(source["retrieval_url"]):
        reasons.add("retrieval-url-not-revision-bound")
    if (
        not _valid_https_url(source["final_url"])
        or expected_url is not None
        and source["final_url"] != source["retrieval_url"]
        or expected_url is None
        and urlsplit(source["final_url"]).hostname != policy["retrieval_host"]
    ):
        reasons.add("redirect-outside-policy")
    return reasons


def _validate_external_skill_source_snapshot(
    document: dict[str, Any], *, artifact_store: LocalArtifactStore | None
) -> None:
    root = _object(document, _DOCUMENT_FIELDS, "document")
    if root["schema_version"] != SCHEMA_VERSION:
        raise ExternalSkillSourceError("unsupported schema_version")
    _identifier(root["snapshot_id"], "snapshot_id")
    if root["snapshot_id"] != _snapshot_identity(root):
        raise ExternalSkillSourceError("snapshot identity mismatch")
    if not _SEMVER_RE.fullmatch(
        _string(root["generator_version"], "generator_version")
    ):
        raise ExternalSkillSourceError("generator_version must be SemVer")
    _timestamp(root["generated_at"], "generated_at")
    generated_at_value = _timestamp_value(root["generated_at"], "generated_at")
    for field in ("input_sha256", "adapter_policy_sha256"):
        if not isinstance(root[field], str) or not _SHA_RE.fullmatch(root[field]):
            raise ExternalSkillSourceError(f"{field} must be lowercase SHA-256")
    _, policies = _policy_snapshot(root["adapter_policy_sha256"])
    sources = root["sources"]
    if not isinstance(sources, list) or not sources or len(sources) > MAX_SOURCES:
        raise ExternalSkillSourceError("sources must be a bounded non-empty list")
    seen = set()
    for index, source in enumerate(sources):
        source = _object(source, _SOURCE_FIELDS, f"sources[{index}]")
        source_id = _identifier(source["source_id"], f"sources[{index}].source_id")
        if source_id in seen:
            raise ExternalSkillSourceError("source_id must be unique")
        seen.add(source_id)
        adapter_id = _identifier(source["adapter_id"], "adapter_id")
        if adapter_id not in policies:
            raise ExternalSkillSourceError("unknown adapter_id")
        policy = policies[adapter_id]
        for field in (
            "source_class",
            "trust_tier",
            "publisher_id",
            "canonical_url",
            "revision_kind",
            "license_ref",
        ):
            if source[field] != policy[field]:
                raise ExternalSkillSourceError(f"{field} does not match adapter policy")
        if source["resource_kind"] not in _RESOURCE_KINDS:
            raise ExternalSkillSourceError("resource_kind is invalid")
        if source["resource_kind"] not in policy["resource_kinds"]:
            raise ExternalSkillSourceError("resource_kind is outside adapter policy")
        if source["adapter_version"] != policy["adapter_version"]:
            raise ExternalSkillSourceError(
                "adapter_version does not match adapter policy"
            )
        _string(source["source_revision"], "source_revision")
        if policy["revision_kind"] == "git-commit" and not _GIT_REVISION_RE.fullmatch(
            source["source_revision"]
        ):
            raise ExternalSkillSourceError("Git revision is invalid")
        if (
            policy["revision_kind"] == "dynamic-index"
            and source["source_revision"] != "dynamic-index"
        ):
            raise ExternalSkillSourceError("dynamic revision is invalid")
        for field in ("retrieval_url", "final_url"):
            _string(source[field], field, maximum=4096)
        _timestamp(source["retrieved_at"], "retrieved_at")
        if (
            _timestamp_value(source["retrieved_at"], "retrieved_at")
            > generated_at_value
        ):
            raise ExternalSkillSourceError("retrieved_at cannot follow generated_at")
        if source["acquisition"] != "https-get":
            raise ExternalSkillSourceError("acquisition is invalid")
        _string(source["source_path"], "source_path", maximum=2048)
        for field in (
            "content_size_bytes",
            "resource_count",
            "resource_manifest_size_bytes",
        ):
            minimum = 0 if field == "content_size_bytes" else 1
            if type(source[field]) is not int or source[field] < minimum:
                raise ExternalSkillSourceError(f"{field} is below its minimum")
        if source["content_size_bytes"] > MAX_RETRIEVAL_BYTES:
            raise ExternalSkillSourceError("content_size_bytes exceeds bound")
        if source["resource_count"] > MAX_RESOURCES_PER_SOURCE:
            raise ExternalSkillSourceError("resource_count exceeds bound")
        if source["resource_manifest_size_bytes"] > MAX_SNAPSHOT_BYTES:
            raise ExternalSkillSourceError("resource_manifest_size_bytes exceeds bound")
        for field in ("content_sha256", "resource_tree_sha256"):
            if not isinstance(source[field], str) or not _SHA_RE.fullmatch(
                source[field]
            ):
                raise ExternalSkillSourceError(f"{field} must be lowercase SHA-256")
        for field in ("content_artifact_ref", "resource_manifest_ref"):
            if not isinstance(source[field], str) or not _ARTIFACT_RE.fullmatch(
                source[field]
            ):
                raise ExternalSkillSourceError(f"{field} is invalid")
        tree_evidence_ref = source["tree_evidence_ref"]
        if tree_evidence_ref is not None and (
            not isinstance(tree_evidence_ref, str)
            or not _ARTIFACT_RE.fullmatch(tree_evidence_ref)
        ):
            raise ExternalSkillSourceError("tree_evidence_ref is invalid")
        if (
            source["content_artifact_ref"]
            != f"artifact://sha256/{source['content_sha256']}"
        ):
            raise ExternalSkillSourceError("content artifact does not match digest")
        if (
            source["resource_manifest_ref"]
            != f"artifact://sha256/{source['resource_tree_sha256']}"
        ):
            raise ExternalSkillSourceError(
                "resource manifest does not match tree digest"
            )
        if source["license_ref"] is not None and (
            not isinstance(source["license_ref"], str) or not source["license_ref"]
        ):
            raise ExternalSkillSourceError("license_ref is invalid")
        publisher_refs = _artifact_refs(
            source["publisher_verification_refs"],
            "publisher_verification_refs",
            required=False,
        )
        license_refs = _artifact_refs(
            source["license_evidence_refs"], "license_evidence_refs", required=False
        )
        provenance = _artifact_refs(
            source["provenance_refs"], "provenance_refs", required=True
        )
        capabilities = _bounded_strings(
            source["declared_capabilities"],
            "declared_capabilities",
            pattern=_CAPABILITY_RE,
        )
        if capabilities != sorted(capabilities):
            raise ExternalSkillSourceError("declared_capabilities must be sorted")
        expected_provenance = {
            source["content_artifact_ref"],
            source["resource_manifest_ref"],
            *([tree_evidence_ref] if tree_evidence_ref is not None else []),
            *publisher_refs,
            *license_refs,
        }
        if set(provenance) != expected_provenance or provenance != sorted(provenance):
            raise ExternalSkillSourceError(
                "provenance_refs do not bind source artifacts"
            )
        if source["status"] not in {"candidate", "quarantined"}:
            raise ExternalSkillSourceError("status is invalid")
        reasons = _bounded_strings(source["quarantine_reasons"], "quarantine_reasons")
        if any(reason not in _QUARANTINE_REASONS for reason in reasons):
            raise ExternalSkillSourceError("quarantine reason is invalid")
        expected_reasons = _semantic_reasons(source, policy=policy)
        resource_manifest = None
        if artifact_store is not None:
            resource_manifest = _validate_resource_manifest(
                source, artifact_store=artifact_store
            )
            resource_paths = {item["path"] for item in resource_manifest["resources"]}
            if source["source_path"] not in resource_paths:
                expected_reasons.add("resource-missing")
            if any(
                item["kind"] != "file" or not _safe_path(item["path"], "resource.path")
                for item in resource_manifest["resources"]
            ):
                expected_reasons.add("unsafe-resource-kind")
            if source["resource_kind"] == "skill":
                skill_root = PurePosixPath(source["source_path"]).parent
                if any(
                    PurePosixPath(path) != skill_root
                    and skill_root not in PurePosixPath(path).parents
                    for path in resource_paths
                ):
                    expected_reasons.add("resource-outside-scope")
            for uri in {*publisher_refs, *license_refs}:
                _read_uri(artifact_store, uri)
            if tree_evidence_ref is not None:
                tree_payload = _read_uri(artifact_store, tree_evidence_ref)
                try:
                    tree_observation = json.loads(tree_payload)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ExternalSkillSourceError("tree evidence is invalid") from exc
                tree_paths, canonical_tree, tree_reasons = _skill_tree_observation(
                    source, tree_observation, policy=policy
                )
                if canonical_tree != tree_payload:
                    raise ExternalSkillSourceError("tree evidence is not canonical")
                if tree_paths != resource_manifest["expected_paths"]:
                    raise ExternalSkillSourceError(
                        "resource manifest paths do not match pinned tree"
                    )
                expected_reasons.update(tree_reasons)
            actual_paths = {item["path"] for item in resource_manifest["resources"]}
            expected_paths = set(resource_manifest["expected_paths"])
            if expected_paths - actual_paths:
                expected_reasons.add("resource-missing")
            if actual_paths - expected_paths:
                expected_reasons.add("resource-unlisted")
        else:
            expected_reasons.update(
                set(reasons)
                & {
                    "resource-missing",
                    "resource-unlisted",
                    "unsafe-resource-kind",
                    "resource-outside-scope",
                    "resource-tree-unverified",
                }
            )
        if reasons != sorted(reasons) or set(reasons) != expected_reasons:
            raise ExternalSkillSourceError(
                "quarantine reasons do not match source evidence"
            )
        expected_status = "quarantined" if reasons else "candidate"
        if source["status"] != expected_status:
            raise ExternalSkillSourceError("status does not match quarantine reasons")
    if root["sources"] != sorted(root["sources"], key=lambda item: item["source_id"]):
        raise ExternalSkillSourceError("sources must be sorted")
    if len(_canonical(root)) > MAX_SNAPSHOT_BYTES:
        raise ExternalSkillSourceError("snapshot exceeds byte bound")


def validate_external_skill_source_snapshot_metadata(document: dict[str, Any]) -> None:
    """Validate a snapshot without granting CAS-backed admission."""
    _validate_external_skill_source_snapshot(document, artifact_store=None)


def validate_external_skill_source_snapshot(
    document: dict[str, Any], *, artifact_store: LocalArtifactStore | None = None
) -> None:
    """Validate metadata and all CAS evidence required for admission."""
    if not isinstance(artifact_store, LocalArtifactStore):
        raise ExternalSkillSourceError("artifact_store is required for admission")
    _validate_external_skill_source_snapshot(document, artifact_store=artifact_store)


def snapshot_identity_for_test(document: dict[str, Any]) -> str:
    """Return the deterministic identity used by mutation-focused contract tests."""
    return _snapshot_identity(document)


def canonical_external_skill_source_snapshot_bytes(document: dict[str, Any]) -> bytes:
    """Return canonical bytes for a structurally and semantically valid snapshot."""
    validate_external_skill_source_snapshot_metadata(document)
    return _canonical(document)


def replay_external_skill_source_snapshot(
    snapshot: dict[str, Any],
    sources: list[dict[str, Any]],
    *,
    artifact_store: LocalArtifactStore,
    generator_version: str,
    expected_generated_at: str,
) -> dict[str, Any]:
    """Verify and return an offline replay using only pinned CAS artifacts."""
    _, policies = _policy_snapshot(snapshot.get("adapter_policy_sha256"))
    requests = _request_preflight(sources, policy_snapshot=policies)
    if not isinstance(artifact_store, LocalArtifactStore):
        raise ExternalSkillSourceError("offline replay requires an artifact store")
    validate_external_skill_source_snapshot(snapshot, artifact_store=artifact_store)
    if snapshot["input_sha256"] != _input_sha256(requests):
        raise ExternalSkillSourceError("snapshot inputs do not match")
    if snapshot["generator_version"] != generator_version:
        raise ExternalSkillSourceError("generator version does not match")
    if snapshot["generated_at"] != expected_generated_at:
        raise ExternalSkillSourceError("generated_at does not match expected time")
    request_by_id = {item["source_id"]: item for item in requests}
    if set(request_by_id) != {item["source_id"] for item in snapshot["sources"]}:
        raise ExternalSkillSourceError("snapshot source set does not match inputs")
    for source in snapshot["sources"]:
        request = request_by_id[source["source_id"]]
        for field in _REQUEST_FIELDS - {"source_id"}:
            if source[field] != request[field]:
                raise ExternalSkillSourceError(f"snapshot {field} does not match input")
    return copy.deepcopy(snapshot)


def verify_external_skill_source_snapshot_inputs(
    snapshot: dict[str, Any],
    sources: list[dict[str, Any]],
    *,
    artifact_store: LocalArtifactStore,
    generator_version: str,
    expected_generated_at: str,
) -> None:
    """Fail unless the snapshot replays byte-for-byte from its bounded inputs."""
    replayed = replay_external_skill_source_snapshot(
        snapshot,
        sources,
        artifact_store=artifact_store,
        generator_version=generator_version,
        expected_generated_at=expected_generated_at,
    )
    if _canonical(replayed) != _canonical(snapshot):
        raise ExternalSkillSourceError("snapshot replay mismatch")


def project_snapshot_source_to_catalog_entry(
    snapshot: dict[str, Any],
    *,
    source_id: str,
    manifest: dict[str, Any],
    artifact_store: LocalArtifactStore | None = None,
) -> dict[str, Any]:
    """Project one eligible immutable Skill into an authority-free M4-07 entry."""
    if not isinstance(artifact_store, LocalArtifactStore):
        raise ExternalSkillSourceError("catalog projection requires an artifact store")
    validate_external_skill_source_snapshot(snapshot, artifact_store=artifact_store)
    matches = [item for item in snapshot["sources"] if item["source_id"] == source_id]
    if len(matches) != 1:
        raise ExternalSkillSourceError("source_id is missing or ambiguous")
    source = matches[0]
    if not (
        source["resource_kind"] == "skill"
        and source["status"] == "candidate"
        and source["revision_kind"] == "git-commit"
        and _GIT_REVISION_RE.fullmatch(source["source_revision"])
    ):
        raise ExternalSkillSourceError("source is not eligible for catalog projection")
    if not isinstance(manifest, dict):
        raise ExternalSkillSourceError("manifest must be an object")
    candidate_manifest = copy.deepcopy(manifest)
    if candidate_manifest.get("source_kind") != "external":
        raise ExternalSkillSourceError("manifest source_kind must be external")
    if candidate_manifest.get("status") != "proposed":
        raise ExternalSkillSourceError("manifest status must be proposed")
    bindings = {
        "content_sha256": source["content_sha256"],
        "license_ref": source["license_ref"],
        "provenance_refs": source["provenance_refs"],
    }
    if any(
        candidate_manifest.get(field) != expected
        for field, expected in bindings.items()
    ):
        raise ExternalSkillSourceError("manifest does not match source evidence")
    try:
        skill_manifest_set.validate_skill_manifest_set(
            {
                "schema_version": skill_manifest_set.SCHEMA_VERSION,
                "manifests": [candidate_manifest],
            }
        )
    except skill_manifest_set.SkillManifestSetError as exc:
        raise ExternalSkillSourceError(str(exc)) from exc
    entry = {
        "catalog_entry_id": f"external/{source['source_id']}",
        "source_kind": "external",
        "manifest": candidate_manifest,
        "source_url": source["retrieval_url"],
        "source_revision": source["source_revision"],
        "source_path": source["source_path"],
        "publisher": source["publisher_id"],
        "license_ref": source["license_ref"],
        "provenance_refs": source["provenance_refs"],
        "capabilities": source["declared_capabilities"],
        "approval_refs": [],
        "verification_refs": sorted(
            source["publisher_verification_refs"] + source["license_evidence_refs"]
        ),
        "permissions": {
            "state_write": False,
            "task_switch": False,
            "claim": False,
            "effect": False,
            "promotion": False,
            "evidence_gate": False,
        },
        "status": "candidate",
    }
    try:
        skill_catalog.validate_skill_catalog(
            {
                "schema_version": skill_catalog.SCHEMA_VERSION,
                "catalog_id": "external-skill-source-projection",
                "catalog_revision": snapshot["generated_at"],
                "entries": [entry],
            }
        )
    except skill_catalog.SkillCatalogError as exc:
        raise ExternalSkillSourceError(str(exc)) from exc
    return entry
