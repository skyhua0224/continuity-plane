"""Provider-neutral affected graph test selection contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from .verification_profile import validate_verification_profile

GRAPH_SCHEMA_VERSION = "context.affected-graph/v1alpha1"
INVENTORY_SCHEMA_VERSION = "context.required-test-inventory/v1alpha1"
RECEIPT_SCHEMA_VERSION = "context.affected-test-selection/v1alpha1"
CHANGE_SET_SCHEMA_VERSION = "context.affected-change-set/v1alpha1"
DERIVATION_SCHEMA_VERSION = "context.deterministic-derivation-receipt/v1alpha1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_CHANGE_SET_FIELDS = {
    "schema_version",
    "change_set_id",
    "project_id",
    "base_repository_revision",
    "repository_revision",
    "generated_at",
    "diff_artifact_ref",
    "diff_artifact_sha256",
    "changed_paths",
    "state_write_authority",
    "completion_authority",
    "change_set_sha256",
}
_DERIVATION_FIELDS = {
    "schema_version",
    "derivation_id",
    "subject_kind",
    "project_id",
    "repository_revision",
    "profile_id",
    "profile_sha256",
    "generated_at",
    "adapter_id",
    "adapter_version",
    "adapter_artifact_ref",
    "adapter_artifact_sha256",
    "config_artifact_ref",
    "config_artifact_sha256",
    "source_artifact_ref",
    "source_artifact_sha256",
    "dynamic_edge_artifact_ref",
    "dynamic_edge_artifact_sha256",
    "output_sha256",
    "state_write_authority",
    "completion_authority",
    "receipt_sha256",
}
_DERIVATION_ANCHOR_FIELDS = {
    "subject_kind",
    "adapter_id",
    "adapter_version",
    "adapter_sha256",
    "config_sha256",
    "source_sha256",
    "dynamic_edge_sha256",
}
_GRAPH_FIELDS = {
    "schema_version",
    "graph_id",
    "project_id",
    "repository_revision",
    "profile_id",
    "profile_sha256",
    "generated_at",
    "valid_until",
    "derivation",
    "completeness",
    "dynamic_edge_status",
    "nodes",
    "state_write_authority",
    "completion_authority",
    "graph_sha256",
}
_NODE_FIELDS = {"node_id", "path_prefixes", "depends_on_node_ids"}
_INVENTORY_FIELDS = {
    "schema_version",
    "inventory_id",
    "project_id",
    "repository_revision",
    "profile_id",
    "profile_sha256",
    "generated_at",
    "valid_until",
    "derivation",
    "tests",
    "state_write_authority",
    "completion_authority",
    "inventory_sha256",
}
_TEST_FIELDS = {
    "test_id",
    "gate_id",
    "node_ids",
    "always_run",
    "estimated_wall_time_ms",
    "estimated_input_bytes",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "selection_id",
    "work_id",
    "project_id",
    "project_revision",
    "repository_revision",
    "change_set_id",
    "change_set_sha256",
    "profile_id",
    "profile_sha256",
    "inventory_id",
    "inventory_sha256",
    "graph_id",
    "graph_sha256",
    "evaluated_at",
    "changed_paths",
    "selection_mode",
    "fallback_reasons",
    "affected_node_ids",
    "full_test_ids",
    "selected_test_ids",
    "required_gate_ids",
    "baseline_estimated_wall_time_ms",
    "selected_estimated_wall_time_ms",
    "baseline_input_bytes",
    "selected_input_bytes",
    "wall_time_reduction_basis_points",
    "input_bytes_reduction_basis_points",
    "external_service_calls",
    "state_write_authority",
    "completion_authority",
    "input_sha256",
    "receipt_sha256",
}
_FALLBACK_REASONS = {
    "changed-path-unmapped",
    "dynamic-edges-present",
    "dynamic-edges-unknown",
    "graph-incomplete",
    "graph-profile-mismatch",
    "graph-project-mismatch",
    "graph-provenance-digest-mismatch",
    "graph-provenance-unavailable",
    "graph-repository-revision-mismatch",
    "graph-stale",
    "inventory-node-unmapped",
    "required-gate-uncovered",
}


class AffectedTestSelectionError(ValueError):
    """Raised when affected-test selection cannot fail closed safely."""


def build_affected_change_set(
    *,
    change_set_id: str,
    project_id: str,
    base_repository_revision: str,
    repository_revision: str,
    generated_at: str,
    diff_artifact_ref: str,
    diff_artifact_sha256: str,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    change_set_resolver: Callable[[str, str, str], bytes | bytearray | memoryview],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    _id(change_set_id, "change_set_id")
    _id(project_id, "project_id")
    _git_revision(base_repository_revision, "base_repository_revision")
    _git_revision(repository_revision, "repository_revision")
    if base_repository_revision == repository_revision:
        raise AffectedTestSelectionError("change-set revisions must differ")
    _timestamp(generated_at, "generated_at")
    context = _current_context(project_id, current_context_resolver)
    if context["repository_revision"] != repository_revision:
        raise AffectedTestSelectionError("change-set head is not current")
    _artifact_binding(diff_artifact_ref, diff_artifact_sha256, "diff_artifact")
    authoritative = _resolved_change_set_bytes(
        project_id,
        base_repository_revision,
        repository_revision,
        change_set_resolver,
    )
    published = _artifact_bytes(
        diff_artifact_ref,
        diff_artifact_sha256,
        artifact_resolver,
        "diff_artifact",
    )
    if published != authoritative:
        raise AffectedTestSelectionError("change-set artifact differs from current diff")
    changed_paths = _parse_change_set_payload(
        authoritative,
        base_repository_revision=base_repository_revision,
        repository_revision=repository_revision,
    )
    change_set = {
        "schema_version": CHANGE_SET_SCHEMA_VERSION,
        "change_set_id": change_set_id,
        "project_id": project_id,
        "base_repository_revision": base_repository_revision,
        "repository_revision": repository_revision,
        "generated_at": generated_at,
        "diff_artifact_ref": diff_artifact_ref,
        "diff_artifact_sha256": diff_artifact_sha256,
        "changed_paths": changed_paths,
        "state_write_authority": False,
        "completion_authority": False,
        "change_set_sha256": "0" * 64,
    }
    change_set["change_set_sha256"] = _digest(
        change_set, "change_set_sha256"
    )
    validate_affected_change_set(
        change_set,
        artifact_resolver=artifact_resolver,
        change_set_resolver=change_set_resolver,
        current_context_resolver=current_context_resolver,
    )
    return copy.deepcopy(change_set)


def validate_affected_change_set(
    change_set: Any,
    *,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    change_set_resolver: Callable[[str, str, str], bytes | bytearray | memoryview],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
) -> None:
    if not isinstance(change_set, Mapping) or set(change_set) != _CHANGE_SET_FIELDS:
        raise AffectedTestSelectionError("change-set fields are invalid")
    if change_set["schema_version"] != CHANGE_SET_SCHEMA_VERSION:
        raise AffectedTestSelectionError("change-set version is invalid")
    _id(change_set["change_set_id"], "change_set_id")
    project_id = _id(change_set["project_id"], "project_id")
    base = _git_revision(
        change_set["base_repository_revision"], "base_repository_revision"
    )
    head = _git_revision(change_set["repository_revision"], "repository_revision")
    if base == head:
        raise AffectedTestSelectionError("change-set revisions must differ")
    _timestamp(change_set["generated_at"], "generated_at")
    _artifact_binding(
        change_set["diff_artifact_ref"],
        change_set["diff_artifact_sha256"],
        "diff_artifact",
    )
    changed_paths = _canonical_paths(change_set["changed_paths"], "changed_paths")
    context = _current_context(project_id, current_context_resolver)
    if context["repository_revision"] != head:
        raise AffectedTestSelectionError("change-set head is not current")
    published = _artifact_bytes(
        change_set["diff_artifact_ref"],
        change_set["diff_artifact_sha256"],
        artifact_resolver,
        "diff_artifact",
    )
    authoritative = _resolved_change_set_bytes(
        project_id, base, head, change_set_resolver
    )
    if published != authoritative:
        raise AffectedTestSelectionError("change-set artifact differs from current diff")
    resolved_paths = _parse_change_set_payload(
        authoritative,
        base_repository_revision=base,
        repository_revision=head,
    )
    if changed_paths != resolved_paths:
        raise AffectedTestSelectionError("change-set paths differ from current diff")
    if (
        change_set["state_write_authority"] is not False
        or change_set["completion_authority"] is not False
    ):
        raise AffectedTestSelectionError("change-set has no authority")
    digest = _sha256(change_set["change_set_sha256"], "change_set_sha256")
    if digest != _digest(change_set, "change_set_sha256"):
        raise AffectedTestSelectionError("change-set digest mismatch")


def build_derivation_receipt(
    *,
    derivation_id: str,
    subject_kind: str,
    project_id: str,
    repository_revision: str,
    profile: Mapping[str, Any],
    generated_at: str,
    adapter_id: str,
    adapter_version: str,
    adapter_artifact_ref: str,
    adapter_artifact_sha256: str,
    config_artifact_ref: str,
    config_artifact_sha256: str,
    source_artifact_ref: str,
    source_artifact_sha256: str,
    dynamic_edge_artifact_ref: str | None,
    dynamic_edge_artifact_sha256: str | None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    derivation_resolver: Callable[
        [Mapping[str, Any], bytes, bytes, bytes | None], Mapping[str, Any]
    ],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    validate_verification_profile(profile, observed_at=generated_at)
    receipt = {
        "schema_version": DERIVATION_SCHEMA_VERSION,
        "derivation_id": derivation_id,
        "subject_kind": subject_kind,
        "project_id": project_id,
        "repository_revision": repository_revision,
        "profile_id": profile["profile_id"],
        "profile_sha256": profile["profile_sha256"],
        "generated_at": generated_at,
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "adapter_artifact_ref": adapter_artifact_ref,
        "adapter_artifact_sha256": adapter_artifact_sha256,
        "config_artifact_ref": config_artifact_ref,
        "config_artifact_sha256": config_artifact_sha256,
        "source_artifact_ref": source_artifact_ref,
        "source_artifact_sha256": source_artifact_sha256,
        "dynamic_edge_artifact_ref": dynamic_edge_artifact_ref,
        "dynamic_edge_artifact_sha256": dynamic_edge_artifact_sha256,
        "output_sha256": "0" * 64,
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "0" * 64,
    }
    output = _resolve_derivation_output(
        receipt,
        artifact_resolver=artifact_resolver,
        derivation_resolver=derivation_resolver,
        current_context_resolver=current_context_resolver,
        verify_output_digest=False,
    )
    receipt["output_sha256"] = hashlib.sha256(_canonical(output)).hexdigest()
    receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
    validate_derivation_receipt(
        receipt,
        artifact_resolver=artifact_resolver,
        derivation_resolver=derivation_resolver,
        current_context_resolver=current_context_resolver,
    )
    return copy.deepcopy(receipt)


def validate_derivation_receipt(
    receipt: Any,
    *,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    derivation_resolver: Callable[
        [Mapping[str, Any], bytes, bytes, bytes | None], Mapping[str, Any]
    ],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
) -> None:
    _resolve_derivation_output(
        receipt,
        artifact_resolver=artifact_resolver,
        derivation_resolver=derivation_resolver,
        current_context_resolver=current_context_resolver,
        verify_output_digest=True,
    )
    if receipt["receipt_sha256"] != _digest(receipt, "receipt_sha256"):
        raise AffectedTestSelectionError("derivation receipt digest mismatch")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AffectedTestSelectionError("selection data must be canonical JSON") from exc


def _digest(value: Mapping[str, Any], field: str) -> str:
    body = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise AffectedTestSelectionError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AffectedTestSelectionError(f"{field} is invalid")
    return value


def _git_revision(value: Any, field: str) -> str:
    if not isinstance(value, str) or _GIT_REVISION_RE.fullmatch(value) is None:
        raise AffectedTestSelectionError(f"{field} is invalid")
    return value
    return value


def _uint(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise AffectedTestSelectionError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise AffectedTestSelectionError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AffectedTestSelectionError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise AffectedTestSelectionError(f"{field} requires a timezone")
    return parsed


def _string_list(
    value: Any,
    field: str,
    *,
    allow_empty: bool = True,
    identifiers: bool = True,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value) > 1024
    ):
        raise AffectedTestSelectionError(f"{field} is invalid")
    for item in value:
        if identifiers:
            _id(item, field)
        elif not isinstance(item, str) or not item or len(item.encode("utf-8")) > 1024:
            raise AffectedTestSelectionError(f"{field} is invalid")
    if len(value) != len(set(value)):
        raise AffectedTestSelectionError(f"{field} must be unique")
    return list(value)


def _path(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or value.startswith("/")
        or "\\" in value
    ):
        raise AffectedTestSelectionError(f"{field} is invalid")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise AffectedTestSelectionError(f"{field} is invalid")
    return value


def _artifact_binding(ref: Any, digest: Any, field: str) -> tuple[str, str]:
    checked_digest = _sha256(digest, f"{field}_sha256")
    if ref != f"artifact://sha256/{checked_digest}":
        raise AffectedTestSelectionError(f"{field}_ref is not content-addressed")
    return ref, checked_digest


def _canonical_paths(value: Any, field: str) -> list[str]:
    paths = _string_list(value, field, allow_empty=False, identifiers=False)
    normalized = sorted(_path(path, field) for path in paths)
    if paths != normalized:
        raise AffectedTestSelectionError(f"{field} must be canonical")
    return normalized


def _artifact_bytes(
    ref: str,
    expected_sha256: str,
    resolver: Callable[[str], bytes | bytearray | memoryview | None],
    field: str,
) -> bytes:
    if not callable(resolver):
        raise AffectedTestSelectionError("artifact_resolver is invalid")
    _artifact_binding(ref, expected_sha256, field)
    try:
        payload = resolver(ref)
    except Exception as exc:
        raise AffectedTestSelectionError(f"{field} is unavailable") from exc
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise AffectedTestSelectionError(f"{field} is unavailable")
    result = bytes(payload)
    if hashlib.sha256(result).hexdigest() != expected_sha256:
        raise AffectedTestSelectionError(f"{field} digest mismatch")
    return result


def _current_context(
    project_id: str,
    resolver: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    if not callable(resolver):
        raise AffectedTestSelectionError("current_context_resolver is invalid")
    try:
        source = resolver(project_id)
    except Exception as exc:
        raise AffectedTestSelectionError("current project context is unavailable") from exc
    fields = {
        "project_id",
        "repository_revision",
        "profile_id",
        "profile_sha256",
        "derivation_anchors",
    }
    if not isinstance(source, Mapping) or set(source) != fields:
        raise AffectedTestSelectionError("current project context is invalid")
    context = copy.deepcopy(dict(source))
    if _id(context["project_id"], "context.project_id") != project_id:
        raise AffectedTestSelectionError("current project context mismatch")
    _git_revision(context["repository_revision"], "context.repository_revision")
    _id(context["profile_id"], "context.profile_id")
    _sha256(context["profile_sha256"], "context.profile_sha256")
    anchors = context["derivation_anchors"]
    if not isinstance(anchors, Mapping) or set(anchors) != {
        "affected-graph",
        "required-test-inventory",
    }:
        raise AffectedTestSelectionError("current derivation anchors are invalid")
    normalized_anchors: dict[str, dict[str, Any]] = {}
    for kind, source_anchor in anchors.items():
        if not isinstance(source_anchor, Mapping) or set(source_anchor) != _DERIVATION_ANCHOR_FIELDS:
            raise AffectedTestSelectionError("current derivation anchor is invalid")
        anchor = copy.deepcopy(dict(source_anchor))
        if anchor["subject_kind"] != kind:
            raise AffectedTestSelectionError("current derivation anchor kind mismatch")
        _id(anchor["adapter_id"], "anchor.adapter_id")
        if not isinstance(anchor["adapter_version"], str) or _SEMVER_RE.fullmatch(
            anchor["adapter_version"]
        ) is None:
            raise AffectedTestSelectionError("anchor.adapter_version is invalid")
        for field in ("adapter_sha256", "config_sha256", "source_sha256"):
            _sha256(anchor[field], f"anchor.{field}")
        dynamic = anchor["dynamic_edge_sha256"]
        if kind == "affected-graph":
            _sha256(dynamic, "anchor.dynamic_edge_sha256")
        elif dynamic is not None:
            raise AffectedTestSelectionError("inventory anchor has dynamic edge evidence")
        normalized_anchors[kind] = anchor
    context["derivation_anchors"] = normalized_anchors
    return context


def _resolved_change_set_bytes(
    project_id: str,
    base_repository_revision: str,
    repository_revision: str,
    resolver: Callable[[str, str, str], bytes | bytearray | memoryview],
) -> bytes:
    if not callable(resolver):
        raise AffectedTestSelectionError("change_set_resolver is invalid")
    try:
        payload = resolver(project_id, base_repository_revision, repository_revision)
    except Exception as exc:
        raise AffectedTestSelectionError("current diff is unavailable") from exc
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise AffectedTestSelectionError("current diff is invalid")
    return bytes(payload)


def _parse_change_set_payload(
    payload: bytes,
    *,
    base_repository_revision: str,
    repository_revision: str,
) -> list[str]:
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AffectedTestSelectionError("current diff payload is invalid") from exc
    expected_fields = {
        "base_repository_revision",
        "repository_revision",
        "changed_paths",
    }
    if not isinstance(decoded, Mapping) or set(decoded) != expected_fields:
        raise AffectedTestSelectionError("current diff payload fields are invalid")
    if _canonical(decoded) != payload:
        raise AffectedTestSelectionError("current diff payload is not canonical")
    if (
        decoded["base_repository_revision"] != base_repository_revision
        or decoded["repository_revision"] != repository_revision
    ):
        raise AffectedTestSelectionError("current diff revision binding mismatch")
    return _canonical_paths(decoded["changed_paths"], "diff.changed_paths")


def _normalize_derivation_output(
    subject_kind: str, output: Any
) -> dict[str, Any]:
    if not isinstance(output, Mapping):
        raise AffectedTestSelectionError("derivation output is invalid")
    if subject_kind == "affected-graph":
        if set(output) != {"nodes", "dynamic_edge_status"}:
            raise AffectedTestSelectionError("graph derivation output fields are invalid")
        status = output["dynamic_edge_status"]
        if status not in {"none", "resolved", "present", "unknown"}:
            raise AffectedTestSelectionError("derived dynamic edge status is invalid")
        nodes = output["nodes"]
        if not isinstance(nodes, list) or not nodes or len(nodes) > 4096:
            raise AffectedTestSelectionError("derived graph nodes are invalid")
        normalized_nodes: list[dict[str, Any]] = []
        for source in nodes:
            if not isinstance(source, Mapping) or set(source) != _NODE_FIELDS:
                raise AffectedTestSelectionError("derived graph node is invalid")
            node = copy.deepcopy(dict(source))
            _id(node["node_id"], "node_id")
            node["path_prefixes"] = sorted(
                _path(path, "path_prefix")
                for path in _string_list(
                    node["path_prefixes"],
                    "path_prefixes",
                    allow_empty=False,
                    identifiers=False,
                )
            )
            node["depends_on_node_ids"] = sorted(
                _string_list(node["depends_on_node_ids"], "depends_on_node_ids")
            )
            normalized_nodes.append(node)
        return {
            "nodes": sorted(normalized_nodes, key=lambda item: item["node_id"]),
            "dynamic_edge_status": status,
        }
    if subject_kind != "required-test-inventory" or set(output) != {"tests"}:
        raise AffectedTestSelectionError("inventory derivation output fields are invalid")
    tests = output["tests"]
    if not isinstance(tests, list) or not tests or len(tests) > 4096:
        raise AffectedTestSelectionError("derived tests are invalid")
    normalized_tests: list[dict[str, Any]] = []
    for source in tests:
        if not isinstance(source, Mapping) or set(source) != _TEST_FIELDS:
            raise AffectedTestSelectionError("derived test is invalid")
        test = copy.deepcopy(dict(source))
        _id(test["test_id"], "test_id")
        _id(test["gate_id"], "gate_id")
        test["node_ids"] = sorted(_string_list(test["node_ids"], "test.node_ids"))
        if test["always_run"] not in {True, False}:
            raise AffectedTestSelectionError("derived test always_run is invalid")
        if not test["always_run"] and not test["node_ids"]:
            raise AffectedTestSelectionError("derived non-anchor test requires nodes")
        _uint(test["estimated_wall_time_ms"], "estimated_wall_time_ms", positive=True)
        _uint(test["estimated_input_bytes"], "estimated_input_bytes", positive=True)
        normalized_tests.append(test)
    return {"tests": sorted(normalized_tests, key=lambda item: item["test_id"])}


def _resolve_derivation_output(
    receipt: Any,
    *,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    derivation_resolver: Callable[
        [Mapping[str, Any], bytes, bytes, bytes | None], Mapping[str, Any]
    ],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
    verify_output_digest: bool,
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping) or set(receipt) != _DERIVATION_FIELDS:
        raise AffectedTestSelectionError("derivation receipt fields are invalid")
    if receipt["schema_version"] != DERIVATION_SCHEMA_VERSION:
        raise AffectedTestSelectionError("derivation receipt version is invalid")
    for field in ("derivation_id", "project_id", "profile_id", "adapter_id"):
        _id(receipt[field], field)
    subject_kind = receipt["subject_kind"]
    if subject_kind not in {"affected-graph", "required-test-inventory"}:
        raise AffectedTestSelectionError("derivation subject kind is invalid")
    _git_revision(receipt["repository_revision"], "repository_revision")
    _sha256(receipt["profile_sha256"], "profile_sha256")
    _timestamp(receipt["generated_at"], "generated_at")
    if not isinstance(receipt["adapter_version"], str) or _SEMVER_RE.fullmatch(
        receipt["adapter_version"]
    ) is None:
        raise AffectedTestSelectionError("adapter_version is invalid")
    for prefix in ("adapter_artifact", "config_artifact", "source_artifact"):
        _artifact_binding(receipt[f"{prefix}_ref"], receipt[f"{prefix}_sha256"], prefix)
    dynamic_ref = receipt["dynamic_edge_artifact_ref"]
    dynamic_sha = receipt["dynamic_edge_artifact_sha256"]
    if subject_kind == "affected-graph":
        if dynamic_ref is None or dynamic_sha is None:
            raise AffectedTestSelectionError("graph derivation requires dynamic edge evidence")
        _artifact_binding(dynamic_ref, dynamic_sha, "dynamic_edge_artifact")
    elif dynamic_ref is not None or dynamic_sha is not None:
        raise AffectedTestSelectionError("inventory derivation has dynamic edge evidence")
    _sha256(receipt["output_sha256"], "output_sha256")
    _sha256(receipt["receipt_sha256"], "receipt_sha256")
    if (
        receipt["state_write_authority"] is not False
        or receipt["completion_authority"] is not False
    ):
        raise AffectedTestSelectionError("derivation receipt has no authority")
    context = _current_context(receipt["project_id"], current_context_resolver)
    if (
        context["repository_revision"] != receipt["repository_revision"]
        or context["profile_id"] != receipt["profile_id"]
        or context["profile_sha256"] != receipt["profile_sha256"]
    ):
        raise AffectedTestSelectionError("derivation is not bound to current context")
    anchor = context["derivation_anchors"][subject_kind]
    expected_anchor = {
        "subject_kind": subject_kind,
        "adapter_id": receipt["adapter_id"],
        "adapter_version": receipt["adapter_version"],
        "adapter_sha256": receipt["adapter_artifact_sha256"],
        "config_sha256": receipt["config_artifact_sha256"],
        "source_sha256": receipt["source_artifact_sha256"],
        "dynamic_edge_sha256": receipt["dynamic_edge_artifact_sha256"],
    }
    if anchor != expected_anchor:
        raise AffectedTestSelectionError("derivation anchor is not current")
    adapter = _artifact_bytes(
        receipt["adapter_artifact_ref"],
        receipt["adapter_artifact_sha256"],
        artifact_resolver,
        "adapter_artifact",
    )
    del adapter
    config = _artifact_bytes(
        receipt["config_artifact_ref"],
        receipt["config_artifact_sha256"],
        artifact_resolver,
        "config_artifact",
    )
    source = _artifact_bytes(
        receipt["source_artifact_ref"],
        receipt["source_artifact_sha256"],
        artifact_resolver,
        "source_artifact",
    )
    dynamic = None
    if subject_kind == "affected-graph":
        dynamic = _artifact_bytes(
            dynamic_ref,
            dynamic_sha,
            artifact_resolver,
            "dynamic_edge_artifact",
        )
    if not callable(derivation_resolver):
        raise AffectedTestSelectionError("derivation_resolver is invalid")
    try:
        raw_output = derivation_resolver(receipt, source, config, dynamic)
    except Exception as exc:
        raise AffectedTestSelectionError("derivation adapter failed") from exc
    output = _normalize_derivation_output(subject_kind, raw_output)
    if verify_output_digest and hashlib.sha256(_canonical(output)).hexdigest() != receipt[
        "output_sha256"
    ]:
        raise AffectedTestSelectionError("derivation output digest mismatch")
    return output


def _validity(document: Mapping[str, Any]) -> tuple[datetime, datetime]:
    generated = _timestamp(document["generated_at"], "generated_at")
    valid_until = _timestamp(document["valid_until"], "valid_until")
    if valid_until <= generated:
        raise AffectedTestSelectionError("validity interval is invalid")
    return generated, valid_until


def _normalize_graph(graph: Any) -> dict[str, Any]:
    if not isinstance(graph, Mapping) or set(graph) != _GRAPH_FIELDS:
        raise AffectedTestSelectionError("affected graph fields are invalid")
    normalized = copy.deepcopy(dict(graph))
    if normalized["schema_version"] != GRAPH_SCHEMA_VERSION:
        raise AffectedTestSelectionError("affected graph version is invalid")
    for field in ("graph_id", "project_id", "profile_id"):
        _id(normalized[field], field)
    _git_revision(normalized["repository_revision"], "repository_revision")
    _sha256(normalized["profile_sha256"], "profile_sha256")
    _validity(normalized)
    derivation = normalized["derivation"]
    if (
        not isinstance(derivation, Mapping)
        or set(derivation) != _DERIVATION_FIELDS
        or derivation["schema_version"] != DERIVATION_SCHEMA_VERSION
        or derivation["subject_kind"] != "affected-graph"
        or derivation["project_id"] != normalized["project_id"]
        or derivation["repository_revision"] != normalized["repository_revision"]
        or derivation["profile_id"] != normalized["profile_id"]
        or derivation["profile_sha256"] != normalized["profile_sha256"]
        or derivation["receipt_sha256"] != _digest(derivation, "receipt_sha256")
    ):
        raise AffectedTestSelectionError("affected graph derivation is invalid")
    if normalized["completeness"] not in {"complete", "incomplete", "unknown"}:
        raise AffectedTestSelectionError("affected graph completeness is invalid")
    if normalized["dynamic_edge_status"] not in {
        "none",
        "resolved",
        "present",
        "unknown",
    }:
        raise AffectedTestSelectionError("dynamic_edge_status is invalid")
    nodes = normalized["nodes"]
    if not isinstance(nodes, list) or not nodes or len(nodes) > 4096:
        raise AffectedTestSelectionError("affected graph nodes are invalid")
    normalized_nodes: list[dict[str, Any]] = []
    for source in nodes:
        if not isinstance(source, Mapping) or set(source) != _NODE_FIELDS:
            raise AffectedTestSelectionError("affected graph node fields are invalid")
        node = copy.deepcopy(dict(source))
        _id(node["node_id"], "node_id")
        paths = _string_list(
            node["path_prefixes"],
            "path_prefixes",
            allow_empty=False,
            identifiers=False,
        )
        node["path_prefixes"] = sorted(_path(path, "path_prefix") for path in paths)
        node["depends_on_node_ids"] = sorted(
            _string_list(node["depends_on_node_ids"], "depends_on_node_ids")
        )
        normalized_nodes.append(node)
    node_by_id = {node["node_id"]: node for node in normalized_nodes}
    if len(node_by_id) != len(normalized_nodes):
        raise AffectedTestSelectionError("affected graph node IDs must be unique")
    for node in normalized_nodes:
        if node["node_id"] in node["depends_on_node_ids"] or any(
            dependency not in node_by_id
            for dependency in node["depends_on_node_ids"]
        ):
            raise AffectedTestSelectionError("affected graph dependency is invalid")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise AffectedTestSelectionError("affected graph dependency cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for dependency in node_by_id[node_id]["depends_on_node_ids"]:
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in node_by_id:
        visit(node_id)
    normalized["nodes"] = sorted(normalized_nodes, key=lambda item: item["node_id"])
    if (
        normalized["state_write_authority"] is not False
        or normalized["completion_authority"] is not False
    ):
        raise AffectedTestSelectionError("affected graph has no authority")
    digest = _sha256(normalized["graph_sha256"], "graph_sha256")
    if digest != _digest(normalized, "graph_sha256"):
        raise AffectedTestSelectionError("affected graph digest mismatch")
    return normalized


def _normalize_inventory(
    inventory: Any,
    *,
    profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(inventory, Mapping) or set(inventory) != _INVENTORY_FIELDS:
        raise AffectedTestSelectionError("required test inventory fields are invalid")
    normalized = copy.deepcopy(dict(inventory))
    if normalized["schema_version"] != INVENTORY_SCHEMA_VERSION:
        raise AffectedTestSelectionError("required test inventory version is invalid")
    for field in ("inventory_id", "project_id", "profile_id"):
        _id(normalized[field], field)
    _git_revision(normalized["repository_revision"], "repository_revision")
    _sha256(normalized["profile_sha256"], "profile_sha256")
    _validity(normalized)
    derivation = normalized["derivation"]
    if (
        not isinstance(derivation, Mapping)
        or set(derivation) != _DERIVATION_FIELDS
        or derivation["schema_version"] != DERIVATION_SCHEMA_VERSION
        or derivation["subject_kind"] != "required-test-inventory"
        or derivation["project_id"] != normalized["project_id"]
        or derivation["repository_revision"] != normalized["repository_revision"]
        or derivation["profile_id"] != normalized["profile_id"]
        or derivation["profile_sha256"] != normalized["profile_sha256"]
        or derivation["receipt_sha256"] != _digest(derivation, "receipt_sha256")
    ):
        raise AffectedTestSelectionError("test inventory derivation is invalid")
    tests = normalized["tests"]
    if not isinstance(tests, list) or not tests or len(tests) > 4096:
        raise AffectedTestSelectionError("required test inventory tests are invalid")
    normalized_tests: list[dict[str, Any]] = []
    for source in tests:
        if not isinstance(source, Mapping) or set(source) != _TEST_FIELDS:
            raise AffectedTestSelectionError("required test fields are invalid")
        test = copy.deepcopy(dict(source))
        _id(test["test_id"], "test_id")
        _id(test["gate_id"], "gate_id")
        test["node_ids"] = sorted(
            _string_list(test["node_ids"], "test.node_ids")
        )
        if test["always_run"] is not True and test["always_run"] is not False:
            raise AffectedTestSelectionError("test always_run is invalid")
        if not test["always_run"] and not test["node_ids"]:
            raise AffectedTestSelectionError("non-anchor tests require graph nodes")
        _uint(test["estimated_wall_time_ms"], "estimated_wall_time_ms", positive=True)
        _uint(test["estimated_input_bytes"], "estimated_input_bytes", positive=True)
        normalized_tests.append(test)
    if len({test["test_id"] for test in normalized_tests}) != len(normalized_tests):
        raise AffectedTestSelectionError("required test IDs must be unique")
    normalized["tests"] = sorted(normalized_tests, key=lambda item: item["test_id"])
    if profile is not None:
        validate_verification_profile(profile)
        gate_by_id = {gate["gate_id"]: gate for gate in profile["gates"]}
        if (
            normalized["project_id"] != profile["project_id"]
            or normalized["profile_id"] != profile["profile_id"]
            or normalized["profile_sha256"] != profile["profile_sha256"]
        ):
            raise AffectedTestSelectionError("test inventory profile binding mismatch")
        if any(test["gate_id"] not in gate_by_id for test in normalized_tests):
            raise AffectedTestSelectionError("test inventory references an unknown gate")
        required_gate_ids = {
            gate["gate_id"] for gate in profile["gates"] if gate["mode"] == "required"
        }
        covered = {
            test["gate_id"]
            for test in normalized_tests
            if test["gate_id"] in required_gate_ids
        }
        if covered != required_gate_ids:
            raise AffectedTestSelectionError("test inventory misses a required gate")
    if (
        normalized["state_write_authority"] is not False
        or normalized["completion_authority"] is not False
    ):
        raise AffectedTestSelectionError("required test inventory has no authority")
    digest = _sha256(normalized["inventory_sha256"], "inventory_sha256")
    if digest != _digest(normalized, "inventory_sha256"):
        raise AffectedTestSelectionError("required test inventory digest mismatch")
    return normalized


def build_affected_graph(
    *,
    graph_id: str,
    profile: Mapping[str, Any],
    derivation: Mapping[str, Any],
    generated_at: str,
    valid_until: str,
    completeness: str,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    derivation_resolver: Callable[
        [Mapping[str, Any], bytes, bytes, bytes | None], Mapping[str, Any]
    ],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    validate_verification_profile(profile, observed_at=generated_at)
    output = _resolve_derivation_output(
        derivation,
        artifact_resolver=artifact_resolver,
        derivation_resolver=derivation_resolver,
        current_context_resolver=current_context_resolver,
        verify_output_digest=True,
    )
    if derivation["subject_kind"] != "affected-graph":
        raise AffectedTestSelectionError("graph derivation kind mismatch")
    graph = {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": derivation["project_id"],
        "repository_revision": derivation["repository_revision"],
        "profile_id": profile["profile_id"],
        "profile_sha256": profile["profile_sha256"],
        "generated_at": generated_at,
        "valid_until": valid_until,
        "derivation": copy.deepcopy(dict(derivation)),
        "completeness": completeness,
        "dynamic_edge_status": output["dynamic_edge_status"],
        "nodes": output["nodes"],
        "state_write_authority": False,
        "completion_authority": False,
        "graph_sha256": "0" * 64,
    }
    graph["graph_sha256"] = _digest(graph, "graph_sha256")
    normalized = _normalize_graph(graph)
    if (
        normalized["project_id"] != profile["project_id"]
        or normalized["profile_id"] != derivation["profile_id"]
        or normalized["profile_sha256"] != derivation["profile_sha256"]
    ):
        raise AffectedTestSelectionError("affected graph project does not match profile")
    return copy.deepcopy(normalized)


def build_test_inventory(
    *,
    inventory_id: str,
    profile: Mapping[str, Any],
    derivation: Mapping[str, Any],
    generated_at: str,
    valid_until: str,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    derivation_resolver: Callable[
        [Mapping[str, Any], bytes, bytes, bytes | None], Mapping[str, Any]
    ],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    validate_verification_profile(profile, observed_at=generated_at)
    output = _resolve_derivation_output(
        derivation,
        artifact_resolver=artifact_resolver,
        derivation_resolver=derivation_resolver,
        current_context_resolver=current_context_resolver,
        verify_output_digest=True,
    )
    if derivation["subject_kind"] != "required-test-inventory":
        raise AffectedTestSelectionError("inventory derivation kind mismatch")
    inventory = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "inventory_id": inventory_id,
        "project_id": derivation["project_id"],
        "repository_revision": derivation["repository_revision"],
        "profile_id": profile["profile_id"],
        "profile_sha256": profile["profile_sha256"],
        "generated_at": generated_at,
        "valid_until": valid_until,
        "derivation": copy.deepcopy(dict(derivation)),
        "tests": output["tests"],
        "state_write_authority": False,
        "completion_authority": False,
        "inventory_sha256": "0" * 64,
    }
    inventory["inventory_sha256"] = _digest(inventory, "inventory_sha256")
    return copy.deepcopy(_normalize_inventory(inventory, profile=profile))


def _resolve_artifact(
    ref: str,
    expected_sha256: str,
    resolver: Callable[[str], bytes | bytearray | memoryview | None],
) -> str | None:
    try:
        payload = resolver(ref)
    except Exception:  # noqa: BLE001 - provider failures make provenance unavailable.
        return "unavailable"
    if payload is None:
        return "unavailable"
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        return "digest-mismatch"
    if hashlib.sha256(bytes(payload)).hexdigest() != expected_sha256:
        return "digest-mismatch"
    return None


def _path_matches(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _reduction(baseline: int, selected: int) -> int:
    if baseline <= 0 or selected >= baseline:
        return 0
    return ((baseline - selected) * 10_000) // baseline


def _selection_input_digest(
    *,
    selection_id: str,
    work_id: str,
    project_revision: int,
    evaluated_at: str,
    profile: Mapping[str, Any],
    inventory: Mapping[str, Any],
    graph: Mapping[str, Any],
    change_set: Mapping[str, Any],
) -> str:
    return hashlib.sha256(
        _canonical(
            {
                "selection_id": selection_id,
                "work_id": work_id,
                "project_revision": project_revision,
                "evaluated_at": evaluated_at,
                "profile": profile,
                "inventory": inventory,
                "graph": graph,
                "change_set": change_set,
            }
        )
    ).hexdigest()


def select_affected_tests(
    *,
    selection_id: str,
    work_id: str,
    project_revision: int,
    profile: Mapping[str, Any],
    inventory: Mapping[str, Any],
    graph: Mapping[str, Any],
    change_set: Mapping[str, Any],
    evaluated_at: str,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    change_set_resolver: Callable[[str, str, str], bytes | bytearray | memoryview],
    derivation_resolver: Callable[
        [Mapping[str, Any], bytes, bytes, bytes | None], Mapping[str, Any]
    ],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
) -> dict[str, Any]:
    _id(selection_id, "selection_id")
    _id(work_id, "work_id")
    _uint(project_revision, "project_revision")
    validate_verification_profile(profile, observed_at=evaluated_at)
    context = _current_context(profile["project_id"], current_context_resolver)
    if (
        context["profile_id"] != profile["profile_id"]
        or context["profile_sha256"] != profile["profile_sha256"]
    ):
        raise AffectedTestSelectionError("verification profile is not current")
    validate_affected_change_set(
        change_set,
        artifact_resolver=artifact_resolver,
        change_set_resolver=change_set_resolver,
        current_context_resolver=current_context_resolver,
    )
    repository_revision = change_set["repository_revision"]
    if context["repository_revision"] != repository_revision:
        raise AffectedTestSelectionError("selection repository is not current")
    normalized_inventory = _normalize_inventory(inventory, profile=profile)
    normalized_graph = _normalize_graph(graph)
    inventory_output = _resolve_derivation_output(
        normalized_inventory["derivation"],
        artifact_resolver=artifact_resolver,
        derivation_resolver=derivation_resolver,
        current_context_resolver=current_context_resolver,
        verify_output_digest=True,
    )
    if inventory_output["tests"] != normalized_inventory["tests"]:
        raise AffectedTestSelectionError("test inventory differs from derivation output")
    graph_fallback_reasons: list[str] = []
    try:
        graph_output = _resolve_derivation_output(
            normalized_graph["derivation"],
            artifact_resolver=artifact_resolver,
            derivation_resolver=derivation_resolver,
            current_context_resolver=current_context_resolver,
            verify_output_digest=True,
        )
    except AffectedTestSelectionError as exc:
        message = str(exc)
        if "unavailable" in message:
            graph_fallback_reasons.append("graph-provenance-unavailable")
            graph_output = None
        elif "digest mismatch" in message:
            graph_fallback_reasons.append("graph-provenance-digest-mismatch")
            graph_output = None
        else:
            raise
    if graph_output is not None and (
        graph_output["nodes"] != normalized_graph["nodes"]
        or graph_output["dynamic_edge_status"]
        != normalized_graph["dynamic_edge_status"]
    ):
        raise AffectedTestSelectionError("affected graph differs from derivation output")
    evaluated = _timestamp(evaluated_at, "evaluated_at")
    change_generated = _timestamp(change_set["generated_at"], "change_set.generated_at")
    if change_generated > evaluated:
        raise AffectedTestSelectionError("change-set is from the future")
    paths = list(change_set["changed_paths"])
    if (
        normalized_inventory["project_id"] != profile["project_id"]
        or normalized_inventory["repository_revision"] != repository_revision
    ):
        raise AffectedTestSelectionError("test inventory revision binding mismatch")
    inventory_generated, inventory_expires = _validity(normalized_inventory)
    if inventory_generated > evaluated or inventory_expires <= evaluated:
        raise AffectedTestSelectionError("required test inventory is not current")
    required_gate_ids = sorted(
        gate["gate_id"] for gate in profile["gates"] if gate["mode"] == "required"
    )
    required_gate_set = set(required_gate_ids)
    tests = [
        test
        for test in normalized_inventory["tests"]
        if test["gate_id"] in required_gate_set
    ]
    test_by_id = {test["test_id"]: test for test in tests}
    full_test_ids = sorted(test_by_id)
    if not full_test_ids:
        raise AffectedTestSelectionError("required test inventory is empty")

    fallback_reasons: list[str] = list(graph_fallback_reasons)
    if normalized_graph["project_id"] != profile["project_id"]:
        fallback_reasons.append("graph-project-mismatch")
    if normalized_graph["repository_revision"] != repository_revision:
        fallback_reasons.append("graph-repository-revision-mismatch")
    if (
        normalized_graph["profile_id"] != profile["profile_id"]
        or normalized_graph["profile_sha256"] != profile["profile_sha256"]
    ):
        fallback_reasons.append("graph-profile-mismatch")
    graph_generated, graph_expires = _validity(normalized_graph)
    if graph_generated > evaluated or graph_expires <= evaluated:
        fallback_reasons.append("graph-stale")
    if normalized_graph["completeness"] != "complete":
        fallback_reasons.append("graph-incomplete")
    if normalized_graph["dynamic_edge_status"] == "unknown":
        fallback_reasons.append("dynamic-edges-unknown")
    elif normalized_graph["dynamic_edge_status"] == "present":
        fallback_reasons.append("dynamic-edges-present")
    node_by_id = {
        node["node_id"]: node for node in normalized_graph["nodes"]
    }
    inventory_node_ids = {
        node_id for test in tests for node_id in test["node_ids"]
    }
    if not inventory_node_ids.issubset(node_by_id):
        fallback_reasons.append("inventory-node-unmapped")
    directly_changed: set[str] = set()
    for path in paths:
        owners = {
            node["node_id"]
            for node in normalized_graph["nodes"]
            if any(_path_matches(path, prefix) for prefix in node["path_prefixes"])
        }
        if not owners:
            fallback_reasons.append("changed-path-unmapped")
        directly_changed.update(owners)

    affected = set(directly_changed)
    if not fallback_reasons:
        changed = True
        while changed:
            changed = False
            for node in normalized_graph["nodes"]:
                if node["node_id"] in affected:
                    continue
                if any(dependency in affected for dependency in node["depends_on_node_ids"]):
                    affected.add(node["node_id"])
                    changed = True
    selected_test_ids = sorted(
        test["test_id"]
        for test in tests
        if test["always_run"] or affected.intersection(test["node_ids"])
    )
    selected_gate_ids = {test_by_id[test_id]["gate_id"] for test_id in selected_test_ids}
    if not fallback_reasons and selected_gate_ids != required_gate_set:
        fallback_reasons.append("required-gate-uncovered")
    fallback_reasons = sorted(set(fallback_reasons))
    selection_mode = "full-validation" if fallback_reasons else "selected"
    if fallback_reasons:
        affected_node_ids: list[str] = []
        selected_test_ids = full_test_ids
    else:
        affected_node_ids = sorted(affected)

    baseline_wall = sum(test["estimated_wall_time_ms"] for test in tests)
    selected_wall = sum(
        test_by_id[test_id]["estimated_wall_time_ms"] for test_id in selected_test_ids
    )
    baseline_bytes = sum(test["estimated_input_bytes"] for test in tests)
    selected_bytes = sum(
        test_by_id[test_id]["estimated_input_bytes"] for test_id in selected_test_ids
    )
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "selection_id": selection_id,
        "work_id": work_id,
        "project_id": profile["project_id"],
        "project_revision": project_revision,
        "repository_revision": repository_revision,
        "change_set_id": change_set["change_set_id"],
        "change_set_sha256": change_set["change_set_sha256"],
        "profile_id": profile["profile_id"],
        "profile_sha256": profile["profile_sha256"],
        "inventory_id": normalized_inventory["inventory_id"],
        "inventory_sha256": normalized_inventory["inventory_sha256"],
        "graph_id": normalized_graph["graph_id"],
        "graph_sha256": normalized_graph["graph_sha256"],
        "evaluated_at": evaluated_at,
        "changed_paths": paths,
        "selection_mode": selection_mode,
        "fallback_reasons": fallback_reasons,
        "affected_node_ids": affected_node_ids,
        "full_test_ids": full_test_ids,
        "selected_test_ids": selected_test_ids,
        "required_gate_ids": required_gate_ids,
        "baseline_estimated_wall_time_ms": baseline_wall,
        "selected_estimated_wall_time_ms": selected_wall,
        "baseline_input_bytes": baseline_bytes,
        "selected_input_bytes": selected_bytes,
        "wall_time_reduction_basis_points": _reduction(baseline_wall, selected_wall),
        "input_bytes_reduction_basis_points": _reduction(baseline_bytes, selected_bytes),
        "external_service_calls": 0,
        "state_write_authority": False,
        "completion_authority": False,
        "input_sha256": _selection_input_digest(
            selection_id=selection_id,
            work_id=work_id,
            project_revision=project_revision,
            evaluated_at=evaluated_at,
            profile=profile,
            inventory=normalized_inventory,
            graph=normalized_graph,
            change_set=change_set,
        ),
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
    _validate_affected_test_selection_receipt_structure(receipt)
    return copy.deepcopy(receipt)


def _validate_affected_test_selection_receipt_structure(receipt: Any) -> None:
    if not isinstance(receipt, Mapping) or set(receipt) != _RECEIPT_FIELDS:
        raise AffectedTestSelectionError("selection receipt fields are invalid")
    if receipt["schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise AffectedTestSelectionError("selection receipt version is invalid")
    for field in (
        "selection_id",
        "work_id",
        "project_id",
        "change_set_id",
        "profile_id",
        "inventory_id",
        "graph_id",
    ):
        _id(receipt[field], field)
    _uint(receipt["project_revision"], "project_revision")
    _git_revision(receipt["repository_revision"], "repository_revision")
    for field in (
        "profile_sha256",
        "change_set_sha256",
        "inventory_sha256",
        "graph_sha256",
        "input_sha256",
        "receipt_sha256",
    ):
        _sha256(receipt[field], field)
    _timestamp(receipt["evaluated_at"], "evaluated_at")
    changed_paths = _string_list(
        receipt["changed_paths"],
        "changed_paths",
        allow_empty=False,
        identifiers=False,
    )
    if changed_paths != sorted(changed_paths):
        raise AffectedTestSelectionError("changed_paths must be canonical")
    for path in changed_paths:
        _path(path, "changed_path")
    for field in (
        "fallback_reasons",
        "affected_node_ids",
        "full_test_ids",
        "selected_test_ids",
        "required_gate_ids",
    ):
        values = _string_list(receipt[field], field, allow_empty=(field in {"fallback_reasons", "affected_node_ids"}))
        if values != sorted(values):
            raise AffectedTestSelectionError(f"{field} must be canonical")
    if any(reason not in _FALLBACK_REASONS for reason in receipt["fallback_reasons"]):
        raise AffectedTestSelectionError("selection fallback reason is invalid")
    if receipt["selection_mode"] not in {"selected", "full-validation"}:
        raise AffectedTestSelectionError("selection mode is invalid")
    full = set(receipt["full_test_ids"])
    selected = set(receipt["selected_test_ids"])
    if not selected.issubset(full):
        raise AffectedTestSelectionError("selected tests exceed full inventory")
    if receipt["selection_mode"] == "selected":
        if receipt["fallback_reasons"] or not receipt["affected_node_ids"]:
            raise AffectedTestSelectionError("selected mode has fallback state")
    elif (
        not receipt["fallback_reasons"]
        or receipt["affected_node_ids"]
        or selected != full
    ):
        raise AffectedTestSelectionError("full validation mode is inconsistent")
    for field in (
        "baseline_estimated_wall_time_ms",
        "selected_estimated_wall_time_ms",
        "baseline_input_bytes",
        "selected_input_bytes",
        "wall_time_reduction_basis_points",
        "input_bytes_reduction_basis_points",
        "external_service_calls",
    ):
        _uint(receipt[field], field)
    if (
        receipt["selected_estimated_wall_time_ms"]
        > receipt["baseline_estimated_wall_time_ms"]
        or receipt["selected_input_bytes"] > receipt["baseline_input_bytes"]
        or receipt["wall_time_reduction_basis_points"]
        != _reduction(
            receipt["baseline_estimated_wall_time_ms"],
            receipt["selected_estimated_wall_time_ms"],
        )
        or receipt["input_bytes_reduction_basis_points"]
        != _reduction(
            receipt["baseline_input_bytes"], receipt["selected_input_bytes"]
        )
    ):
        raise AffectedTestSelectionError("selection accounting is inconsistent")
    if receipt["external_service_calls"] != 0:
        raise AffectedTestSelectionError("selection must remain local")
    if (
        receipt["state_write_authority"] is not False
        or receipt["completion_authority"] is not False
    ):
        raise AffectedTestSelectionError("selection receipts have no authority")
    if receipt["receipt_sha256"] != _digest(receipt, "receipt_sha256"):
        raise AffectedTestSelectionError("selection receipt digest mismatch")


def validate_affected_test_selection_receipt(
    receipt: Any,
    *,
    profile: Mapping[str, Any],
    inventory: Mapping[str, Any],
    graph: Mapping[str, Any],
    change_set: Mapping[str, Any],
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
    change_set_resolver: Callable[[str, str, str], bytes | bytearray | memoryview],
    derivation_resolver: Callable[
        [Mapping[str, Any], bytes, bytes, bytes | None], Mapping[str, Any]
    ],
    current_context_resolver: Callable[[str], Mapping[str, Any]],
) -> None:
    _validate_affected_test_selection_receipt_structure(receipt)
    expected = select_affected_tests(
        selection_id=receipt["selection_id"],
        work_id=receipt["work_id"],
        project_revision=receipt["project_revision"],
        profile=profile,
        inventory=inventory,
        graph=graph,
        change_set=change_set,
        evaluated_at=receipt["evaluated_at"],
        artifact_resolver=artifact_resolver,
        change_set_resolver=change_set_resolver,
        derivation_resolver=derivation_resolver,
        current_context_resolver=current_context_resolver,
    )
    if expected != receipt:
        raise AffectedTestSelectionError(
            "selection receipt differs from trusted replay"
        )
