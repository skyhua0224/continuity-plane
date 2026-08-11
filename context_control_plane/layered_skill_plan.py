"""Versioned, packet-bound plan for layered Skill composition."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .compiled_skill_packet import (
    CompiledSkillPacketError,
    canonical_compiled_skill_packet_bytes,
    validate_compiled_skill_packet,
)


SCHEMA_VERSION = "context.layered-skill-load-plan/v1alpha1"
LAYERS = ("S0", "S1", "S2", "S3")
_LAYER_ORDER = {layer: index for index, layer in enumerate(LAYERS)}
_PLAN_FIELDS = {
    "schema_version",
    "packet_sha256",
    "manifest_set_sha256",
    "observed_at",
    "requested_layers",
    "bindings",
}
_BINDING_FIELDS = {"skill_id", "layer"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)


class LayeredSkillLoadPlanError(ValueError):
    """Raised when a layer plan is not a valid M4-04 wire object."""


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise LayeredSkillLoadPlanError(f"{field} is invalid")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 256 or not _ID_RE.fullmatch(value):
        raise LayeredSkillLoadPlanError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not 20 <= len(value) <= 64
        or not _TIMESTAMP_RE.fullmatch(value)
    ):
        raise LayeredSkillLoadPlanError(f"{field} is invalid")
    if (
        int(value[11:13]) > 23
        or int(value[14:16]) > 59
        or int(value[17:19]) > 59
    ):
        raise LayeredSkillLoadPlanError(f"{field} is invalid")
    if not value.endswith("Z") and (
        int(value[-5:-3]) > 23 or int(value[-2:]) > 59
    ):
        raise LayeredSkillLoadPlanError(f"{field} is invalid")
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LayeredSkillLoadPlanError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LayeredSkillLoadPlanError(f"{field} requires a timezone")
    return value


def _requested_layers(value: Any) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise LayeredSkillLoadPlanError("requested_layers must be a non-empty sequence")
    layers = list(value)
    if any(layer not in _LAYER_ORDER for layer in layers):
        raise LayeredSkillLoadPlanError("requested_layers contains an unknown layer")
    if len(layers) != len(set(layers)):
        raise LayeredSkillLoadPlanError("requested_layers must be unique")
    if "S0" not in layers:
        raise LayeredSkillLoadPlanError("requested_layers must include S0")
    return sorted(layers, key=_LAYER_ORDER.__getitem__)


def validate_layered_skill_load_plan(plan: dict[str, Any]) -> None:
    """Validate a strict plan without consulting packet or asset state."""
    if not isinstance(plan, dict) or set(plan) != _PLAN_FIELDS:
        raise LayeredSkillLoadPlanError("layer plan fields are invalid")
    if plan["schema_version"] != SCHEMA_VERSION:
        raise LayeredSkillLoadPlanError("layer plan schema_version is unsupported")
    _sha256(plan["packet_sha256"], "layer plan packet_sha256")
    _sha256(plan["manifest_set_sha256"], "layer plan manifest_set_sha256")
    _timestamp(plan["observed_at"], "layer plan observed_at")
    _requested_layers(plan["requested_layers"])
    bindings = plan["bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise LayeredSkillLoadPlanError("layer plan bindings must be a non-empty list")
    skill_ids: set[str] = set()
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) != _BINDING_FIELDS:
            raise LayeredSkillLoadPlanError("layer plan binding fields are invalid")
        skill_id = _identifier(binding["skill_id"], "layer plan binding skill_id")
        if skill_id in skill_ids:
            raise LayeredSkillLoadPlanError("layer plan bindings must be unique")
        skill_ids.add(skill_id)
        if binding["layer"] not in _LAYER_ORDER:
            raise LayeredSkillLoadPlanError("layer plan binding layer is invalid")
    if "S0" not in {binding["layer"] for binding in bindings}:
        raise LayeredSkillLoadPlanError("layer plan must bind bootstrap content to S0")


def canonical_layered_skill_load_plan_bytes(plan: dict[str, Any]) -> bytes:
    validate_layered_skill_load_plan(plan)
    canonical = copy.deepcopy(plan)
    canonical["requested_layers"] = _requested_layers(canonical["requested_layers"])
    canonical["bindings"] = sorted(
        canonical["bindings"], key=lambda item: item["skill_id"]
    )
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def layered_skill_load_plan_digest(plan: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_layered_skill_load_plan_bytes(plan)).hexdigest()


def layer_bindings_digest(layer_bindings: Mapping[str, str]) -> str:
    """Hash the canonical binding list used by a validated plan or receipt."""
    bindings = [
        {"skill_id": skill_id, "layer": layer}
        for skill_id, layer in layer_bindings.items()
    ]
    return hashlib.sha256(
        json.dumps(
            sorted(bindings, key=lambda item: item["skill_id"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def compile_layered_skill_load_plan(
    packet: dict[str, Any],
    *,
    layer_bindings: Mapping[str, str],
    requested_layers: Sequence[str],
    observed_at: str,
) -> dict[str, Any]:
    """Create a canonical plan from a validated packet and explicit policy."""
    try:
        validate_compiled_skill_packet(packet)
        packet_bytes = canonical_compiled_skill_packet_bytes(packet)
    except (CompiledSkillPacketError, KeyError, TypeError, ValueError) as exc:
        raise LayeredSkillLoadPlanError("compiled packet failed strict validation") from exc
    if not isinstance(layer_bindings, Mapping):
        raise LayeredSkillLoadPlanError("layer_bindings must be a mapping")
    try:
        normalized_requested = _requested_layers(requested_layers)
        bindings = [
            {
                "skill_id": _identifier(skill_id, "layer binding skill_id"),
                "layer": layer,
            }
            for skill_id, layer in layer_bindings.items()
        ]
    except (LayeredSkillLoadPlanError, ValueError) as exc:
        raise LayeredSkillLoadPlanError(str(exc)) from exc
    packet_snapshot = json.loads(packet_bytes)
    plan = {
        "schema_version": SCHEMA_VERSION,
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "manifest_set_sha256": packet_snapshot["manifest_set_sha256"],
        "observed_at": observed_at,
        "requested_layers": normalized_requested,
        "bindings": bindings,
    }
    validate_layered_skill_load_plan(plan)
    selected_ids = {
        selection["skill_id"] for selection in packet_snapshot["selections"]
    }
    if {binding["skill_id"] for binding in bindings} != selected_ids:
        raise LayeredSkillLoadPlanError(
            "layer plan bindings must cover exactly the compiled packet Skills"
        )
    return json.loads(canonical_layered_skill_load_plan_bytes(plan))


def verify_layered_skill_load_plan(
    plan: dict[str, Any],
    packet: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    """Verify a plan against the immutable packet it selects from."""
    try:
        validate_layered_skill_load_plan(plan)
        validate_compiled_skill_packet(packet)
        packet_bytes = canonical_compiled_skill_packet_bytes(packet)
    except (
        LayeredSkillLoadPlanError,
        CompiledSkillPacketError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise LayeredSkillLoadPlanError("layer plan or packet failed strict validation") from exc
    canonical_plan = canonical_layered_skill_load_plan_bytes(plan)
    plan_snapshot = json.loads(canonical_plan)
    packet_snapshot = json.loads(packet_bytes)
    observed_digest = hashlib.sha256(canonical_plan).hexdigest()
    packet_sha256 = hashlib.sha256(packet_bytes).hexdigest()
    if plan_snapshot["packet_sha256"] != packet_sha256:
        raise LayeredSkillLoadPlanError("layer plan packet identity does not match packet")
    if (
        plan_snapshot["manifest_set_sha256"]
        != packet_snapshot["manifest_set_sha256"]
    ):
        raise LayeredSkillLoadPlanError("layer plan manifest-set identity does not match packet")
    selected_ids = {
        selection["skill_id"] for selection in packet_snapshot["selections"]
    }
    if {
        binding["skill_id"] for binding in plan_snapshot["bindings"]
    } != selected_ids:
        raise LayeredSkillLoadPlanError(
            "layer plan bindings must cover exactly the compiled packet Skills"
        )
    return plan_snapshot, observed_digest
