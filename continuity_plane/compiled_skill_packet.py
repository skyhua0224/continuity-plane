"""Compile immutable Skill bindings from a validated manifest set."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from . import skill_manifest_set


SCHEMA_VERSION = "context.compiled-skill-packet/v1alpha1"
COMPILER_VERSION = "context.skill-packet-compiler/v1alpha1"

_PACKET_FIELDS = {
    "schema_version",
    "compiler_version",
    "manifest_set_sha256",
    "selections",
    "rule_bindings",
}
_SELECTION_FIELDS = {"skill_id", "version", "content_sha256", "rule_ids"}
_RULE_BINDING_FIELDS = {
    "rule_id",
    "skill_id",
    "skill_version",
    "skill_content_sha256",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")


class CompiledSkillPacketError(ValueError):
    """Raised when a static Skill packet cannot be compiled safely."""


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CompiledSkillPacketError(f"{field} must be an object")
    keys = set(value)
    if keys != fields:
        missing = sorted(fields - keys)
        unknown = sorted(keys - fields)
        details: list[str] = []
        if missing:
            details.append(f"missing fields: {', '.join(missing)}")
        if unknown:
            details.append(f"unknown fields: {', '.join(unknown)}")
        raise CompiledSkillPacketError(f"{field} has {'; '.join(details)}")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise CompiledSkillPacketError(f"{field} must be a stable identifier")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise CompiledSkillPacketError(f"{field} must be lowercase SHA-256")
    return value


def _semver(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CompiledSkillPacketError(f"{field} must be SemVer")
    try:
        skill_manifest_set.SemanticVersion.parse(value)
    except ValueError as exc:
        raise CompiledSkillPacketError(f"{field} must be SemVer") from exc
    return value


def validate_compiled_skill_packet(packet: dict[str, Any]) -> None:
    """Validate the portable packet wire contract without resolving assets."""
    packet = _object(packet, _PACKET_FIELDS, "packet")
    if packet["schema_version"] != SCHEMA_VERSION:
        raise CompiledSkillPacketError("packet.schema_version is unsupported")
    if packet["compiler_version"] != COMPILER_VERSION:
        raise CompiledSkillPacketError("packet.compiler_version is unsupported")
    _sha256(packet["manifest_set_sha256"], "packet.manifest_set_sha256")

    selections = packet["selections"]
    if not isinstance(selections, list) or not selections:
        raise CompiledSkillPacketError("packet.selections must be a non-empty list")
    selection_ids: set[str] = set()
    rules_by_skill: dict[str, list[str]] = {}
    identity_by_skill: dict[str, tuple[str, str]] = {}
    rule_owner_by_id: dict[str, str] = {}
    for selection in selections:
        selection = _object(selection, _SELECTION_FIELDS, "packet.selection")
        skill_id = _identifier(selection["skill_id"], "packet.selection.skill_id")
        _semver(selection["version"], "packet.selection.version")
        _sha256(selection["content_sha256"], "packet.selection.content_sha256")
        rule_ids = selection["rule_ids"]
        if not isinstance(rule_ids, list) or not rule_ids:
            raise CompiledSkillPacketError(
                "packet.selection.rule_ids must be a non-empty list"
            )
        if skill_id in selection_ids:
            raise CompiledSkillPacketError("packet.selections must be unique")
        selection_ids.add(skill_id)
        normalized_rules = [
            _identifier(rule_id, "packet.selection.rule_ids") for rule_id in rule_ids
        ]
        if len(normalized_rules) != len(set(normalized_rules)):
            raise CompiledSkillPacketError("packet.selection.rule_ids must be unique")
        for rule_id in normalized_rules:
            previous_owner = rule_owner_by_id.get(rule_id)
            if previous_owner is not None and previous_owner != skill_id:
                raise CompiledSkillPacketError(
                    "packet.rule_ids must be globally unique across Skills"
                )
            rule_owner_by_id[rule_id] = skill_id
        rules_by_skill[skill_id] = normalized_rules
        identity_by_skill[skill_id] = (
            selection["version"],
            selection["content_sha256"],
        )

    bindings = packet["rule_bindings"]
    if not isinstance(bindings, list) or not bindings:
        raise CompiledSkillPacketError("packet.rule_bindings must be a non-empty list")
    bound_rule_ids: set[str] = set()
    for binding in bindings:
        binding = _object(binding, _RULE_BINDING_FIELDS, "packet.rule_binding")
        rule_id = _identifier(binding["rule_id"], "packet.rule_binding.rule_id")
        skill_id = _identifier(binding["skill_id"], "packet.rule_binding.skill_id")
        _semver(binding["skill_version"], "packet.rule_binding.skill_version")
        _sha256(
            binding["skill_content_sha256"],
            "packet.rule_binding.skill_content_sha256",
        )
        if skill_id not in selection_ids or rule_id not in rules_by_skill[skill_id]:
            raise CompiledSkillPacketError(
                "packet.rule_binding must reference a selected rule"
            )
        if (binding["skill_version"], binding["skill_content_sha256"]) != (
            identity_by_skill[skill_id]
        ):
            raise CompiledSkillPacketError(
                "packet.rule_binding must match its selected Skill version and digest"
            )
        if rule_id in bound_rule_ids:
            raise CompiledSkillPacketError("packet.rule_bindings must be unique")
        bound_rule_ids.add(rule_id)
    selected_rule_ids = {
        rule_id for rule_ids in rules_by_skill.values() for rule_id in rule_ids
    }
    if bound_rule_ids != selected_rule_ids:
        raise CompiledSkillPacketError(
            "packet.rule_bindings must bind every selected rule exactly once"
        )


def _selected_skill_ids(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise CompiledSkillPacketError("selected_skill_ids must be a non-empty list")
    if any(not isinstance(skill_id, str) or not skill_id for skill_id in value):
        raise CompiledSkillPacketError("selected_skill_ids must contain Skill IDs")
    if len(value) != len(set(value)):
        raise CompiledSkillPacketError("selected_skill_ids must be unique")
    return value


def compile_skill_packet(
    manifest_set: dict[str, Any],
    *,
    selected_skill_ids: list[str],
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Compile selected approved Skills and their dependencies deterministically."""
    try:
        skill_manifest_set.validate_skill_manifest_set(
            manifest_set,
            observed_at=observed_at,
        )
    except skill_manifest_set.SkillManifestSetError as exc:
        raise CompiledSkillPacketError(str(exc)) from exc

    requested = _selected_skill_ids(selected_skill_ids)
    by_id = {manifest["skill_id"]: manifest for manifest in manifest_set["manifests"]}
    for skill_id in requested:
        manifest = by_id.get(skill_id)
        if manifest is None:
            raise CompiledSkillPacketError(f"unknown selected Skill: {skill_id}")
        if manifest["status"] not in {"approved", "active"}:
            raise CompiledSkillPacketError(
                f"selected Skill is not selectable: {skill_id}"
            )

    ordered_ids: list[str] = []
    included: set[str] = set()

    def include(skill_id: str) -> None:
        if skill_id in included:
            return
        manifest = by_id[skill_id]
        for dependency in sorted(
            manifest["dependencies"], key=lambda item: item["skill_id"]
        ):
            include(dependency["skill_id"])
        included.add(skill_id)
        ordered_ids.append(skill_id)

    for skill_id in sorted(requested):
        include(skill_id)

    selections: list[dict[str, Any]] = []
    rule_bindings: list[dict[str, str]] = []
    seen_rule_ids: set[str] = set()
    for skill_id in ordered_ids:
        manifest = by_id[skill_id]
        selection = {
            "skill_id": skill_id,
            "version": manifest["version"],
            "content_sha256": manifest["content_sha256"],
            "rule_ids": sorted(manifest["rule_ids"]),
        }
        selections.append(selection)
        for rule_id in selection["rule_ids"]:
            if rule_id in seen_rule_ids:
                raise CompiledSkillPacketError(
                    f"rule_id resolves to multiple Skills: {rule_id}"
                )
            seen_rule_ids.add(rule_id)
            rule_bindings.append(
                {
                    "rule_id": rule_id,
                    "skill_id": skill_id,
                    "skill_version": manifest["version"],
                    "skill_content_sha256": manifest["content_sha256"],
                }
            )

    manifest_bytes = skill_manifest_set.canonical_skill_manifest_set_bytes(
        manifest_set,
        observed_at=observed_at,
    )
    packet = {
        "schema_version": SCHEMA_VERSION,
        "compiler_version": COMPILER_VERSION,
        "manifest_set_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "selections": selections,
        "rule_bindings": rule_bindings,
    }
    return json.loads(canonical_compiled_skill_packet_bytes(packet))


def canonical_compiled_skill_packet_bytes(packet: dict[str, Any]) -> bytes:
    """Return deterministic canonical JSON without mutating the input."""
    validate_compiled_skill_packet(packet)
    canonical = copy.deepcopy(packet)
    canonical["selections"] = sorted(
        canonical["selections"],
        key=lambda selection: (selection["skill_id"], selection["version"]),
    )
    for selection in canonical["selections"]:
        selection["rule_ids"] = sorted(selection["rule_ids"])
    canonical["rule_bindings"] = sorted(
        canonical["rule_bindings"],
        key=lambda binding: binding["rule_id"],
    )
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def verify_compiled_skill_packet(
    packet: dict[str, Any],
    manifest_set: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> None:
    """Verify that a packet remains bound to its source manifest set."""
    validate_compiled_skill_packet(packet)
    try:
        manifest_bytes = skill_manifest_set.canonical_skill_manifest_set_bytes(
            manifest_set,
            observed_at=observed_at,
        )
    except skill_manifest_set.SkillManifestSetError as exc:
        raise CompiledSkillPacketError(str(exc)) from exc
    if packet["manifest_set_sha256"] != hashlib.sha256(manifest_bytes).hexdigest():
        raise CompiledSkillPacketError("manifest_set_sha256 does not match source")

    by_id = {manifest["skill_id"]: manifest for manifest in manifest_set["manifests"]}
    selected_ids = {selection["skill_id"] for selection in packet["selections"]}
    for selection in packet["selections"]:
        manifest = by_id.get(selection["skill_id"])
        if manifest is None:
            raise CompiledSkillPacketError("selected Skill is absent from source")
        if manifest["status"] not in {"approved", "active"}:
            raise CompiledSkillPacketError(
                "source Skill status is not selectable for this packet"
            )
        source_identity = manifest["version"], manifest["content_sha256"]
        packet_identity = selection["version"], selection["content_sha256"]
        if packet_identity != source_identity:
            raise CompiledSkillPacketError("selected Skill identity does not match source")
        if selection["rule_ids"] != sorted(manifest["rule_ids"]):
            raise CompiledSkillPacketError("selected Skill rules do not match source")
        missing_dependencies = {
            dependency["skill_id"] for dependency in manifest["dependencies"]
        } - selected_ids
        if missing_dependencies:
            raise CompiledSkillPacketError("selected Skill dependencies are incomplete")


def compiled_skill_packet_digest(packet: dict[str, Any]) -> str:
    """Return the SHA-256 digest of a canonical compiled Skill packet."""
    return hashlib.sha256(canonical_compiled_skill_packet_bytes(packet)).hexdigest()
