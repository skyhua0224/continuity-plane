"""Load verified Skill assets through a deterministic S0-S3 boundary."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Protocol

from .compiled_skill_packet import (
    CompiledSkillPacketError,
    canonical_compiled_skill_packet_bytes,
    validate_compiled_skill_packet,
)
from .layered_skill_plan import (
    LayeredSkillLoadPlanError,
    layer_bindings_digest,
    verify_layered_skill_load_plan,
)
from .skill_drift_quarantine import (
    canonical_skill_drift_assessment_bytes,
    skill_drift_assessment_digest,
    validate_skill_drift_assessment,
)


SCHEMA_VERSION = "context.layered-skill-load/v1alpha1"
LOADER_VERSION = "context.layered-skill-loader/v1alpha1"
LAYERS = ("S0", "S1", "S2", "S3")
_LAYER_ORDER = {layer: index for index, layer in enumerate(LAYERS)}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_RECEIPT_FIELDS = {
    "schema_version",
    "loader_version",
    "packet_sha256",
    "drift_assessment_sha256",
    "layer_plan_sha256",
    "layer_plan_authorization_ref",
    "layer_bindings_sha256",
    "assessment_observed_at",
    "loaded_at",
    "requested_layers",
    "skills",
    "layers",
    "available_content_bytes",
    "loaded_content_bytes",
    "omitted_content_bytes",
    "content_reduction_basis_points",
}
_SKILL_FIELDS = {
    "skill_id",
    "layer",
    "content_sha256",
    "content_bytes",
    "loaded",
}
_LAYER_FIELDS = {"layer", "skill_count", "content_bytes", "loaded"}


class LayeredSkillLoadInputError(ValueError):
    """Raised when verified Skill assets cannot be loaded safely."""


class LayeredSkillPlanAuthorizer(Protocol):
    """Trusted boundary for activating a compiled layer plan."""

    def authorize(
        self,
        *,
        plan_sha256: str,
        packet_sha256: str,
        manifest_set_sha256: str,
    ) -> str | None:
        """Return an opaque authorization reference, or deny with None."""


@dataclass(frozen=True)
class LayeredSkillLoad:
    """Receipt plus immutable bytes selected for provider composition."""

    _receipt_bytes: bytes
    assets: Mapping[str, bytes]

    @property
    def receipt(self) -> dict[str, Any]:
        """Return a defensive copy of the verified receipt."""
        return json.loads(self._receipt_bytes)


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} is invalid")
    return value


def _identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 256
        or not _ID_RE.fullmatch(value)
    ):
        raise ValueError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not 20 <= len(value) <= 64
        or not _RFC3339_RE.fullmatch(value)
    ):
        raise ValueError(f"{field} is invalid")
    if (
        int(value[11:13]) > 23
        or int(value[14:16]) > 59
        or int(value[17:19]) > 59
    ):
        raise ValueError(f"{field} is invalid")
    if not value.endswith("Z") and (
        int(value[-5:-3]) > 23 or int(value[-2:]) > 59
    ):
        raise ValueError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} requires an explicit timezone")
    return value


def _requested_layers(value: Any) -> list[str]:
    if (
        isinstance(value, (str, bytes))
        or not isinstance(value, Sequence)
        or not value
    ):
        raise ValueError("requested layers must be a non-empty sequence")
    layers = list(value)
    if any(layer not in _LAYER_ORDER for layer in layers):
        raise ValueError("requested layers contain an unknown layer")
    if len(layers) != len(set(layers)):
        raise ValueError("requested layers must be unique")
    if "S0" not in layers:
        raise ValueError("requested layers must include S0")
    return sorted(layers, key=_LAYER_ORDER.__getitem__)


def _skill_sort_key(skill: dict[str, Any]) -> tuple[int, str]:
    return _LAYER_ORDER[skill["layer"]], skill["skill_id"]


def validate_layered_skill_load_receipt(receipt: dict[str, Any]) -> None:
    """Validate the strict, provider-neutral layer-load receipt."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValueError("layered Skill receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("layered Skill schema_version is unsupported")
    if receipt["loader_version"] != LOADER_VERSION:
        raise ValueError("layered Skill loader_version is unsupported")
    _sha256(receipt["packet_sha256"], "packet_sha256")
    _sha256(receipt["drift_assessment_sha256"], "drift_assessment_sha256")
    _sha256(receipt["layer_plan_sha256"], "layer_plan_sha256")
    _identifier(
        receipt["layer_plan_authorization_ref"],
        "layer_plan_authorization_ref",
    )
    _sha256(receipt["layer_bindings_sha256"], "layer_bindings_sha256")
    assessment_observed_at = _timestamp(
        receipt["assessment_observed_at"],
        "assessment_observed_at",
    )
    loaded_at = _timestamp(receipt["loaded_at"], "loaded_at")
    if assessment_observed_at != loaded_at:
        raise ValueError("load time must match the drift assessment time")
    requested = _requested_layers(receipt["requested_layers"])

    skills = receipt["skills"]
    if not isinstance(skills, list) or not skills:
        raise ValueError("layered Skill skills must be a non-empty list")
    skill_ids: set[str] = set()
    for skill in skills:
        if not isinstance(skill, dict) or set(skill) != _SKILL_FIELDS:
            raise ValueError("layered Skill skill fields are invalid")
        skill_id = skill["skill_id"]
        if skill_id in skill_ids:
            raise ValueError("layered Skill IDs must be unique")
        _identifier(skill_id, "layered Skill ID")
        skill_ids.add(skill_id)
        if skill["layer"] not in _LAYER_ORDER:
            raise ValueError("layered Skill layer is invalid")
        _sha256(skill["content_sha256"], "skill content_sha256")
        if (
            isinstance(skill["content_bytes"], bool)
            or not isinstance(skill["content_bytes"], int)
            or skill["content_bytes"] <= 0
        ):
            raise ValueError("skill content_bytes must be a positive integer")
        if not isinstance(skill["loaded"], bool):
            raise ValueError("skill loaded must be boolean")
        if skill["loaded"] != (skill["layer"] in requested):
            raise ValueError("skill loaded flag disagrees with requested layers")
    if not any(skill["layer"] == "S0" for skill in skills):
        raise ValueError("layered Skill receipt must contain S0 bootstrap content")
    receipt_binding_digest = layer_bindings_digest(
        {skill["skill_id"]: skill["layer"] for skill in skills}
    )
    if receipt["layer_bindings_sha256"] != receipt_binding_digest:
        raise ValueError("layer binding digest does not match Skill entries")

    layers = receipt["layers"]
    if not isinstance(layers, list) or len(layers) != len(LAYERS):
        raise ValueError("layer summaries must contain S0-S3")
    layer_names: set[str] = set()
    for summary in layers:
        if not isinstance(summary, dict) or set(summary) != _LAYER_FIELDS:
            raise ValueError("layer summary fields are invalid")
        layer = summary["layer"]
        if layer not in _LAYER_ORDER or layer in layer_names:
            raise ValueError("layer summaries must contain unique S0-S3")
        layer_names.add(layer)
        for field in ("skill_count", "content_bytes"):
            value = summary[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValueError(f"layer {field} must be a non-negative integer")
        if not isinstance(summary["loaded"], bool):
            raise ValueError("layer loaded must be boolean")
        if summary["loaded"] != (layer in requested):
            raise ValueError("layer loaded flag disagrees with requested layers")
    if layer_names != set(LAYERS):
        raise ValueError("layer summaries must contain exactly S0-S3")

    expected_layers = {
        layer: {"skill_count": 0, "content_bytes": 0}
        for layer in LAYERS
    }
    for skill in skills:
        expected = expected_layers[skill["layer"]]
        expected["skill_count"] += 1
        expected["content_bytes"] += skill["content_bytes"]
    for summary in layers:
        expected = expected_layers[summary["layer"]]
        if (
            summary["skill_count"] != expected["skill_count"]
            or summary["content_bytes"] != expected["content_bytes"]
        ):
            raise ValueError("layer summary does not match Skill entries")

    available = sum(item["content_bytes"] for item in skills)
    loaded = sum(item["content_bytes"] for item in skills if item["loaded"])
    if available <= 0:
        raise ValueError("available content bytes must be positive")
    if (
        receipt["available_content_bytes"] != available
        or receipt["loaded_content_bytes"] != loaded
        or receipt["omitted_content_bytes"] != available - loaded
    ):
        raise ValueError("layered Skill byte totals are inconsistent")
    expected_reduction = (available - loaded) * 10000 // available
    if receipt["content_reduction_basis_points"] != expected_reduction:
        raise ValueError("layered Skill reduction metric is inconsistent")
    for field in (
        "available_content_bytes",
        "loaded_content_bytes",
        "omitted_content_bytes",
        "content_reduction_basis_points",
    ):
        value = receipt[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field} must be a non-negative integer")
    if receipt["content_reduction_basis_points"] > 10000:
        raise ValueError("content reduction metric is out of range")


def canonical_layered_skill_load_bytes(receipt: dict[str, Any]) -> bytes:
    """Return deterministic canonical JSON bytes for a load receipt."""
    validate_layered_skill_load_receipt(receipt)
    canonical = copy.deepcopy(receipt)
    canonical["requested_layers"] = sorted(
        canonical["requested_layers"],
        key=_LAYER_ORDER.__getitem__,
    )
    canonical["skills"] = sorted(canonical["skills"], key=_skill_sort_key)
    canonical["layers"] = sorted(
        canonical["layers"],
        key=lambda item: _LAYER_ORDER[item["layer"]],
    )
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def layered_skill_load_digest(receipt: dict[str, Any]) -> str:
    """Return the SHA-256 digest of canonical load receipt bytes."""
    return hashlib.sha256(canonical_layered_skill_load_bytes(receipt)).hexdigest()


def load_layered_skills(
    packet: dict[str, Any],
    drift_assessment: dict[str, Any],
    assets: Mapping[str, bytes],
    *,
    layer_plan: dict[str, Any],
    plan_authorizer: LayeredSkillPlanAuthorizer,
    trusted_clock: Callable[[], datetime],
) -> LayeredSkillLoad:
    """Load only requested layers from assets bound to an allow receipt."""
    try:
        validate_compiled_skill_packet(packet)
        packet_bytes = canonical_compiled_skill_packet_bytes(packet)
    except (CompiledSkillPacketError, KeyError, TypeError, ValueError) as exc:
        raise LayeredSkillLoadInputError(
            "compiled packet failed strict validation"
        ) from exc
    try:
        validate_skill_drift_assessment(drift_assessment)
        assessment_bytes = canonical_skill_drift_assessment_bytes(drift_assessment)
    except (KeyError, TypeError, ValueError) as exc:
        raise LayeredSkillLoadInputError(
            "drift assessment failed strict validation"
        ) from exc
    packet_snapshot = json.loads(packet_bytes)
    assessment_snapshot = json.loads(assessment_bytes)
    if assessment_snapshot["gate"] != "allow":
        raise LayeredSkillLoadInputError(
            "layered Skill loading requires an allow drift assessment"
        )
    packet_sha256 = hashlib.sha256(packet_bytes).hexdigest()
    if assessment_snapshot["packet_sha256"] != packet_sha256:
        raise LayeredSkillLoadInputError(
            "drift assessment packet identity does not match packet"
        )
    if (
        assessment_snapshot["manifest_set_sha256"]
        != packet_snapshot["manifest_set_sha256"]
    ):
        raise LayeredSkillLoadInputError(
            "drift assessment manifest-set identity does not match packet"
        )
    try:
        plan_snapshot, layer_plan_sha256 = verify_layered_skill_load_plan(
            layer_plan,
            packet_snapshot,
        )
    except LayeredSkillLoadPlanError as exc:
        raise LayeredSkillLoadInputError(str(exc)) from exc
    if plan_snapshot["observed_at"] != assessment_snapshot["observed_at"]:
        raise LayeredSkillLoadInputError(
            "layer plan time does not match the drift assessment time"
        )
    if not hasattr(plan_authorizer, "authorize") or not callable(
        plan_authorizer.authorize
    ):
        raise LayeredSkillLoadInputError("layer plan authorizer is required")
    try:
        authorization_ref = plan_authorizer.authorize(
            plan_sha256=layer_plan_sha256,
            packet_sha256=packet_sha256,
            manifest_set_sha256=packet_snapshot["manifest_set_sha256"],
        )
    except Exception as exc:
        raise LayeredSkillLoadInputError("layer plan authorization failed") from exc
    try:
        authorization_ref = _identifier(
            authorization_ref,
            "layer plan authorization reference",
        )
    except ValueError as exc:
        raise LayeredSkillLoadInputError("layer plan is not authorized") from exc
    if not callable(trusted_clock):
        raise LayeredSkillLoadInputError("trusted clock must be callable")
    try:
        trusted_now = trusted_clock()
    except Exception as exc:
        raise LayeredSkillLoadInputError("trusted clock failed") from exc
    if (
        not isinstance(trusted_now, datetime)
        or trusted_now.tzinfo is None
        or trusted_now.utcoffset() is None
    ):
        raise LayeredSkillLoadInputError(
            "trusted clock must return a timezone-aware datetime"
        )
    assessment_time = datetime.fromisoformat(
        assessment_snapshot["observed_at"].replace("Z", "+00:00")
    )
    if trusted_now != assessment_time:
        raise LayeredSkillLoadInputError(
            "load time must match the drift assessment time"
        )
    trusted_load_time = assessment_snapshot["observed_at"]
    requested = plan_snapshot["requested_layers"]
    if not isinstance(assets, Mapping):
        raise LayeredSkillLoadInputError("assets must be a mapping of bytes")
    binding_snapshot = {
        item["skill_id"]: item["layer"] for item in plan_snapshot["bindings"]
    }
    observed_binding_digest = layer_bindings_digest(binding_snapshot)
    selections = packet_snapshot["selections"]
    expected_order = [selection["skill_id"] for selection in selections]
    expected_ids = set(expected_order)
    if set(binding_snapshot) != expected_ids:
        raise LayeredSkillLoadInputError(
            "layer bindings must cover exactly the compiled packet Skills"
        )
    if "S0" not in binding_snapshot.values():
        raise LayeredSkillLoadInputError(
            "layer bindings must assign bootstrap content to S0"
        )
    if set(assets) != expected_ids:
        raise LayeredSkillLoadInputError(
            "assets must cover exactly the compiled packet Skills"
        )
    try:
        asset_snapshot = {skill_id: assets[skill_id] for skill_id in expected_order}
    except (KeyError, TypeError) as exc:
        raise LayeredSkillLoadInputError(
            "assets changed while the verified snapshot was created"
        ) from exc
    assessment_by_id = {
        skill["skill_id"]: skill for skill in assessment_snapshot["skills"]
    }
    if set(assessment_by_id) != expected_ids or any(
        skill["status"] != "verified" for skill in assessment_by_id.values()
    ):
        raise LayeredSkillLoadInputError(
            "drift assessment does not verify exactly the compiled packet Skills"
        )

    receipt_skills = []
    loaded_assets: dict[str, bytes] = {}
    for selection in selections:
        skill_id = selection["skill_id"]
        layer = binding_snapshot[skill_id]
        if layer not in _LAYER_ORDER:
            raise LayeredSkillLoadInputError(
                f"layer binding is invalid for {skill_id}"
            )
        raw_asset = asset_snapshot[skill_id]
        if not isinstance(raw_asset, bytes):
            raise LayeredSkillLoadInputError(
                f"asset for {skill_id} must be bytes; direct paths are forbidden"
            )
        asset = memoryview(raw_asset).tobytes()
        if not asset:
            raise LayeredSkillLoadInputError(
                f"asset for {skill_id} must contain Skill content"
            )
        observed_digest = hashlib.sha256(asset).hexdigest()
        if observed_digest != selection["content_sha256"]:
            raise LayeredSkillLoadInputError(
                f"asset digest does not match packet for {skill_id}"
            )
        assessment_digest = assessment_by_id[skill_id]["observed_content_sha256"]
        if assessment_digest != observed_digest:
            raise LayeredSkillLoadInputError(
                f"asset digest does not match drift receipt for {skill_id}"
            )
        loaded = layer in requested
        if loaded:
            loaded_assets[skill_id] = asset
        receipt_skills.append(
            {
                "skill_id": skill_id,
                "layer": layer,
                "content_sha256": observed_digest,
                "content_bytes": len(asset),
                "loaded": loaded,
            }
        )

    receipt_skills.sort(key=_skill_sort_key)
    layer_summaries = []
    for layer in LAYERS:
        members = [skill for skill in receipt_skills if skill["layer"] == layer]
        layer_summaries.append(
            {
                "layer": layer,
                "skill_count": len(members),
                "content_bytes": sum(
                    skill["content_bytes"] for skill in members
                ),
                "loaded": layer in requested,
            }
        )
    available_bytes = sum(skill["content_bytes"] for skill in receipt_skills)
    loaded_bytes = sum(
        skill["content_bytes"] for skill in receipt_skills if skill["loaded"]
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "loader_version": LOADER_VERSION,
        "packet_sha256": packet_sha256,
        "drift_assessment_sha256": skill_drift_assessment_digest(
            assessment_snapshot
        ),
        "layer_plan_sha256": layer_plan_sha256,
        "layer_plan_authorization_ref": authorization_ref,
        "layer_bindings_sha256": observed_binding_digest,
        "assessment_observed_at": assessment_snapshot["observed_at"],
        "loaded_at": trusted_load_time,
        "requested_layers": requested,
        "skills": receipt_skills,
        "layers": layer_summaries,
        "available_content_bytes": available_bytes,
        "loaded_content_bytes": loaded_bytes,
        "omitted_content_bytes": available_bytes - loaded_bytes,
        "content_reduction_basis_points": (
            (available_bytes - loaded_bytes) * 10000 // available_bytes
        ),
    }
    validate_layered_skill_load_receipt(receipt)
    return LayeredSkillLoad(
        _receipt_bytes=canonical_layered_skill_load_bytes(receipt),
        assets=MappingProxyType(dict(loaded_assets)),
    )
