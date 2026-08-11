"""Provider Skill delivery plans with a neutral, bounded replay contract."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from .compiled_skill_packet import (
    CompiledSkillPacketError,
    canonical_compiled_skill_packet_bytes,
    validate_compiled_skill_packet,
)
from .layered_skill_loader import (
    LayeredSkillLoad,
    LayeredSkillLoadInputError,
    canonical_layered_skill_load_bytes,
    validate_layered_skill_load_receipt,
)
from .layered_skill_plan import (
    LayeredSkillLoadPlanError,
    verify_layered_skill_load_plan,
)
from .skill_drift_quarantine import (
    canonical_skill_drift_assessment_bytes,
    skill_drift_assessment_digest,
    validate_skill_drift_assessment,
)


SCHEMA_VERSION = "context.provider-skill-adapter-effect/v1alpha1"
ADAPTER_VERSION = "context.provider-skill-adapter/v1alpha1"
PROVIDER_SURFACE_CONTRACTS = {
    "codex": "context.adapter-surface/codex-app-server-skill-input/v1alpha1",
    "claude": "context.adapter-surface/claude-agent-sdk-filesystem-skill/v1alpha1",
}
MAX_SKILLS = 1024
MAX_RULE_IDS = 65536
MAX_COMPOSITION_BYTES = 8 * 1024 * 1024
MAX_PROVIDER_PAYLOAD_BYTES = 16 * 1024 * 1024
MAX_SECTION_B64_CHARS = ((MAX_COMPOSITION_BYTES + 2) // 3) * 4
_LAYER_ORDER = {"S0": 0, "S1": 1, "S2": 2, "S3": 3}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(
    r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$"
)
_EFFECT_FIELDS = {
    "schema_version",
    "provider_id",
    "adapter_version",
    "adapter_surface_contract",
    "packet_sha256",
    "layer_plan_sha256",
    "load_receipt_sha256",
    "selected_skill_ids",
    "selected_rule_ids",
    "composition_sha256",
    "composition_sections",
    "composition_body_bytes",
    "provider_payload_sha256",
    "provider_payload_bytes",
    "writes_runtime_state",
    "state_revision",
}
_SECTION_FIELDS = {
    "skill_id",
    "layer",
    "rule_ids",
    "content_sha256",
    "content_bytes",
    "content_b64",
}
_PROVIDER_ASSET_FIELDS = {
    "provider_skill_name",
    "source_skill_id",
    "layer",
    "rule_ids",
    "source_content_sha256",
    "source_content_bytes",
    "materialized_content_sha256",
    "materialized_content_bytes",
    "materialized_content_b64",
    "path",
}
_NEUTRAL_FIELDS = (
    "schema_version",
    "packet_sha256",
    "layer_plan_sha256",
    "load_receipt_sha256",
    "selected_skill_ids",
    "selected_rule_ids",
    "composition_sha256",
    "composition_sections",
    "composition_body_bytes",
    "writes_runtime_state",
    "state_revision",
)


class ProviderSkillAdapterError(ValueError):
    """Raised when a provider delivery plan cannot be verified."""


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProviderSkillAdapterError(f"{field} is invalid")
    return value


def _identifier(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 256
        or not _ID_RE.fullmatch(value)
    ):
        raise ProviderSkillAdapterError(f"{field} is invalid")
    return value


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")


def _bounded_identifiers(
    value: Any,
    field: str,
    limit: int,
    *,
    require_sorted: bool = False,
) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or len(value) > limit
        or (require_sorted and value != sorted(value))
        or len(value) != len(set(value))
    ):
        raise ProviderSkillAdapterError(f"{field} are invalid")
    for identifier in value:
        _identifier(identifier, field)
    return value


def _validated_sections(
    sections: Any,
    *,
    selected_skill_ids: list[str] | None = None,
    selected_rule_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(sections, list) or not sections or len(sections) > MAX_SKILLS:
        raise ProviderSkillAdapterError("composition sections are invalid")
    canonical_sections: list[dict[str, Any]] = []
    total_bytes = 0
    all_rule_ids: list[str] = []
    for section in sections:
        if not isinstance(section, dict) or set(section) != _SECTION_FIELDS:
            raise ProviderSkillAdapterError("composition section fields are invalid")
        skill_id = _identifier(section["skill_id"], "composition section skill_id")
        layer = section["layer"]
        if layer not in _LAYER_ORDER:
            raise ProviderSkillAdapterError("composition section layer is invalid")
        rule_ids = _bounded_identifiers(
            section["rule_ids"],
            "composition section rule_ids",
            MAX_RULE_IDS,
            require_sorted=True,
        )
        _sha256(section["content_sha256"], "composition section content_sha256")
        content_b64 = section["content_b64"]
        if (
            not isinstance(content_b64, str)
            or not content_b64
            or len(content_b64) > MAX_SECTION_B64_CHARS
        ):
            raise ProviderSkillAdapterError("composition section content_b64 is invalid")
        try:
            content = base64.b64decode(content_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ProviderSkillAdapterError(
                "composition section content_b64 is invalid"
            ) from exc
        content_bytes = section["content_bytes"]
        if (
            type(content_bytes) is not int
            or content_bytes <= 0
            or content_bytes != len(content)
        ):
            raise ProviderSkillAdapterError("composition section content bytes are invalid")
        if hashlib.sha256(content).hexdigest() != section["content_sha256"]:
            raise ProviderSkillAdapterError("composition section content digest is invalid")
        total_bytes += content_bytes
        if total_bytes > MAX_COMPOSITION_BYTES:
            raise ProviderSkillAdapterError("composition exceeds maximum body bytes")
        canonical_sections.append(
            {
                "skill_id": skill_id,
                "layer": layer,
                "rule_ids": list(rule_ids),
                "content_sha256": section["content_sha256"],
                "content_bytes": content_bytes,
                "content_b64": content_b64,
            }
        )
        all_rule_ids.extend(rule_ids)
    if canonical_sections != sorted(
        canonical_sections,
        key=lambda item: (_LAYER_ORDER[item["layer"]], item["skill_id"]),
    ):
        raise ProviderSkillAdapterError("composition sections are not canonical")
    if len({section["skill_id"] for section in canonical_sections}) != len(
        canonical_sections
    ):
        raise ProviderSkillAdapterError("composition section Skill IDs are not unique")
    if len(all_rule_ids) != len(set(all_rule_ids)):
        raise ProviderSkillAdapterError("composition section rule IDs are not unique")
    if len(all_rule_ids) > MAX_RULE_IDS:
        raise ProviderSkillAdapterError("composition rule count exceeds maximum")
    if selected_skill_ids is not None and [
        section["skill_id"] for section in canonical_sections
    ] != selected_skill_ids:
        raise ProviderSkillAdapterError("composition sections do not match selected Skills")
    if selected_rule_ids is not None and all_rule_ids != selected_rule_ids:
        raise ProviderSkillAdapterError("composition sections do not match selected rules")
    return canonical_sections, total_bytes


def _provider_skill_name(skill_id: str) -> str:
    return f"ccp-{hashlib.sha256(skill_id.encode('utf-8')).hexdigest()[:60]}"


def _provider_asset_path(provider_id: str, provider_skill_name: str) -> str:
    root = ".agents/skills" if provider_id == "codex" else ".claude/skills"
    return f"{root}/{provider_skill_name}/SKILL.md"


def _is_yaml_frontmatter_document(content: bytes) -> bool:
    return content.startswith(b"---\n") or content.startswith(b"---\r\n")


def _provider_assets(effect: dict[str, Any]) -> list[dict[str, Any]]:
    assets = []
    names: set[str] = set()
    for section in effect["composition_sections"]:
        provider_skill_name = _provider_skill_name(section["skill_id"])
        if provider_skill_name in names:
            raise ProviderSkillAdapterError("provider Skill names are not unique")
        names.add(provider_skill_name)
        try:
            source_content = base64.b64decode(section["content_b64"], validate=True)
            source_content.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise ProviderSkillAdapterError(
                "provider Skill content must be valid UTF-8"
            ) from exc
        if _is_yaml_frontmatter_document(source_content):
            raise ProviderSkillAdapterError(
                "provider Skill source content must be body-only"
            )
        header = (
            f"---\nname: {provider_skill_name}\n"
            f"description: Context Control Plane Skill {section['skill_id']}\n"
            "---\n"
        ).encode("ascii")
        materialized = header + source_content
        assets.append(
            {
                "provider_skill_name": provider_skill_name,
                "source_skill_id": section["skill_id"],
                "layer": section["layer"],
                "rule_ids": section["rule_ids"],
                "source_content_sha256": section["content_sha256"],
                "source_content_bytes": section["content_bytes"],
                "materialized_content_sha256": hashlib.sha256(materialized).hexdigest(),
                "materialized_content_bytes": len(materialized),
                "materialized_content_b64": base64.b64encode(materialized).decode(
                    "ascii"
                ),
                "path": _provider_asset_path(effect["provider_id"], provider_skill_name),
            }
        )
    return assets


def _render_codex_delivery_plan(effect: dict[str, Any]) -> bytes:
    assets = _provider_assets(effect)
    return _canonical_json_bytes(
        {
            "adapter_surface_contract": effect["adapter_surface_contract"],
            "filesystem_assets": assets,
            "turn_start": {
                "input": [
                    item
                    for asset in assets
                    for item in (
                        {"type": "text", "text": f"${asset['provider_skill_name']}"},
                        {
                            "type": "skill",
                            "name": asset["provider_skill_name"],
                            "path": asset["path"],
                        },
                    )
                ]
            },
        }
    )


def _render_claude_delivery_plan(effect: dict[str, Any]) -> bytes:
    assets = _provider_assets(effect)
    return _canonical_json_bytes(
        {
            "adapter_surface_contract": effect["adapter_surface_contract"],
            "agent_sdk_init": {
                "allowed_tools": ["Skill"],
                "skills": [asset["provider_skill_name"] for asset in assets],
                "setting_sources": ["project"],
            },
            "filesystem_assets": assets,
        }
    )


def _render_provider_payload(effect: dict[str, Any]) -> bytes:
    if effect["provider_id"] == "codex":
        return _render_codex_delivery_plan(effect)
    if effect["provider_id"] == "claude":
        return _render_claude_delivery_plan(effect)
    raise ProviderSkillAdapterError("provider_id is unsupported")


def _recover_payload_object(provider_payload: bytes) -> dict[str, Any]:
    if not isinstance(provider_payload, bytes) or not provider_payload:
        raise ProviderSkillAdapterError("provider payload is invalid")
    if len(provider_payload) > MAX_PROVIDER_PAYLOAD_BYTES:
        raise ProviderSkillAdapterError("provider payload exceeds maximum bytes")
    try:
        value = json.loads(provider_payload)
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProviderSkillAdapterError("provider payload is invalid") from exc
    if _canonical_json_bytes(value) != provider_payload:
        raise ProviderSkillAdapterError("provider payload is not canonical")
    if not isinstance(value, dict):
        raise ProviderSkillAdapterError("provider payload is invalid")
    return value


def recover_provider_composition_sections(provider_payload: bytes) -> list[dict[str, Any]]:
    """Recover bounded neutral sections from one provider delivery plan."""
    payload = _recover_payload_object(provider_payload)
    surface = payload.get("adapter_surface_contract")
    if surface == PROVIDER_SURFACE_CONTRACTS["codex"]:
        if set(payload) != {
            "adapter_surface_contract",
            "filesystem_assets",
            "turn_start",
        }:
            raise ProviderSkillAdapterError("Codex delivery plan fields are invalid")
        if not isinstance(payload["turn_start"], dict) or set(
            payload["turn_start"]
        ) != {"input"}:
            raise ProviderSkillAdapterError("Codex delivery plan fields are invalid")
        declared_inputs = payload["turn_start"]["input"]
        provider_id = "codex"
    elif surface == PROVIDER_SURFACE_CONTRACTS["claude"]:
        if set(payload) != {
            "adapter_surface_contract",
            "agent_sdk_init",
            "filesystem_assets",
        }:
            raise ProviderSkillAdapterError("Claude delivery plan fields are invalid")
        init = payload["agent_sdk_init"]
        if (
            not isinstance(init, dict)
            or set(init) != {"allowed_tools", "skills", "setting_sources"}
            or init["allowed_tools"] != ["Skill"]
            or init["setting_sources"] != ["project"]
            or not isinstance(init["skills"], list)
            or not init["skills"]
            or any(
                not isinstance(name, str) or not name
                for name in init["skills"]
            )
        ):
            raise ProviderSkillAdapterError("Claude delivery plan fields are invalid")
        declared_inputs = init["skills"]
        provider_id = "claude"
    else:
        raise ProviderSkillAdapterError("provider delivery plan surface is unsupported")
    assets = payload["filesystem_assets"]
    if not isinstance(assets, list) or not isinstance(declared_inputs, list):
        raise ProviderSkillAdapterError("provider delivery plan assets are invalid")
    sections: list[dict[str, Any]] = []
    aliases: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != _PROVIDER_ASSET_FIELDS:
            raise ProviderSkillAdapterError("provider delivery plan asset fields are invalid")
        source_skill_id = _identifier(
            asset["source_skill_id"], "provider asset source_skill_id"
        )
        provider_skill_name = asset["provider_skill_name"]
        if provider_skill_name != _provider_skill_name(source_skill_id):
            raise ProviderSkillAdapterError("provider Skill alias is invalid")
        if asset["path"] != _provider_asset_path(provider_id, provider_skill_name):
            raise ProviderSkillAdapterError("provider delivery plan asset path is invalid")
        try:
            materialized = base64.b64decode(
                asset["materialized_content_b64"], validate=True
            )
        except (ValueError, TypeError) as exc:
            raise ProviderSkillAdapterError(
                "provider materialized Skill content is invalid"
            ) from exc
        if (
            type(asset["materialized_content_bytes"]) is not int
            or asset["materialized_content_bytes"] != len(materialized)
            or hashlib.sha256(materialized).hexdigest()
            != asset["materialized_content_sha256"]
        ):
            raise ProviderSkillAdapterError(
                "provider materialized Skill identity is invalid"
            )
        header = (
            f"---\nname: {provider_skill_name}\n"
            f"description: Context Control Plane Skill {source_skill_id}\n"
            "---\n"
        ).encode("ascii")
        if not materialized.startswith(header):
            raise ProviderSkillAdapterError("provider Skill frontmatter is invalid")
        source_content = materialized[len(header) :]
        try:
            source_content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProviderSkillAdapterError("provider Skill body is not UTF-8") from exc
        if (
            type(asset["source_content_bytes"]) is not int
            or asset["source_content_bytes"] != len(source_content)
            or hashlib.sha256(source_content).hexdigest()
            != asset["source_content_sha256"]
        ):
            raise ProviderSkillAdapterError("provider Skill source identity is invalid")
        aliases.append(provider_skill_name)
        sections.append(
            {
                "skill_id": source_skill_id,
                "layer": asset["layer"],
                "rule_ids": asset["rule_ids"],
                "content_sha256": asset["source_content_sha256"],
                "content_bytes": asset["source_content_bytes"],
                "content_b64": base64.b64encode(source_content).decode("ascii"),
            }
        )
    if len(aliases) != len(set(aliases)):
        raise ProviderSkillAdapterError("provider Skill aliases are not unique")
    if provider_id == "codex":
        expected_inputs = [
            item
            for alias, asset in zip(aliases, assets, strict=True)
            for item in (
                {"type": "text", "text": f"${alias}"},
                {"type": "skill", "name": alias, "path": asset["path"]},
            )
        ]
        if declared_inputs != expected_inputs:
            raise ProviderSkillAdapterError("Codex delivery plan Skill input is invalid")
    elif declared_inputs != aliases:
        raise ProviderSkillAdapterError("Claude delivery plan Skill input is invalid")
    canonical_sections, _ = _validated_sections(sections)
    return canonical_sections


def validate_provider_skill_effect(effect: dict[str, Any]) -> None:
    """Validate a bounded provider delivery effect and neutral replay identity."""
    if not isinstance(effect, dict) or set(effect) != _EFFECT_FIELDS:
        raise ProviderSkillAdapterError("provider Skill effect fields are invalid")
    if effect["schema_version"] != SCHEMA_VERSION:
        raise ProviderSkillAdapterError("provider Skill effect schema is unsupported")
    provider_id = _identifier(effect["provider_id"], "provider_id")
    if provider_id not in PROVIDER_SURFACE_CONTRACTS:
        raise ProviderSkillAdapterError("provider_id is unsupported")
    _identifier(effect["adapter_version"], "adapter_version")
    if effect["adapter_version"] != ADAPTER_VERSION:
        raise ProviderSkillAdapterError("adapter_version is unsupported")
    if effect["adapter_surface_contract"] != PROVIDER_SURFACE_CONTRACTS[provider_id]:
        raise ProviderSkillAdapterError("adapter surface does not match provider")
    for field in (
        "packet_sha256",
        "layer_plan_sha256",
        "load_receipt_sha256",
        "composition_sha256",
        "provider_payload_sha256",
    ):
        _sha256(effect[field], field)
    selected_skill_ids = _bounded_identifiers(
        effect["selected_skill_ids"], "selected_skill_ids", MAX_SKILLS
    )
    selected_rule_ids = _bounded_identifiers(
        effect["selected_rule_ids"], "selected_rule_ids", MAX_RULE_IDS
    )
    sections, body_bytes = _validated_sections(
        effect["composition_sections"],
        selected_skill_ids=selected_skill_ids,
        selected_rule_ids=selected_rule_ids,
    )
    if (
        type(effect["composition_body_bytes"]) is not int
        or effect["composition_body_bytes"] != body_bytes
    ):
        raise ProviderSkillAdapterError("composition body bytes are invalid")
    if hashlib.sha256(_canonical_json_bytes(sections)).hexdigest() != effect[
        "composition_sha256"
    ]:
        raise ProviderSkillAdapterError("composition digest is inconsistent")
    if effect["writes_runtime_state"] is not False:
        raise ProviderSkillAdapterError("provider adapter cannot write runtime state")
    if effect["state_revision"] is not None:
        raise ProviderSkillAdapterError("provider adapter cannot assert state revision")
    provider_payload = _render_provider_payload(effect)
    if (
        len(provider_payload) > MAX_PROVIDER_PAYLOAD_BYTES
        or effect["provider_payload_bytes"] != len(provider_payload)
        or hashlib.sha256(provider_payload).hexdigest()
        != effect["provider_payload_sha256"]
    ):
        raise ProviderSkillAdapterError("provider payload digest is inconsistent")
    if recover_provider_composition_sections(provider_payload) != sections:
        raise ProviderSkillAdapterError("provider payload does not replay composition")


def canonical_provider_skill_effect_bytes(effect: dict[str, Any]) -> bytes:
    validate_provider_skill_effect(effect)
    return _canonical_json_bytes(effect)


def provider_effect_digest(effect: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_provider_skill_effect_bytes(effect)).hexdigest()


def provider_neutral_effect_digest(effect: dict[str, Any]) -> str:
    validate_provider_skill_effect(effect)
    neutral = {field: effect[field] for field in _NEUTRAL_FIELDS}
    return hashlib.sha256(_canonical_json_bytes(neutral)).hexdigest()


def materialize_provider_payload(effect: dict[str, Any]) -> bytes:
    """Materialize the provider-specific local delivery plan after validation."""
    validate_provider_skill_effect(effect)
    return _render_provider_payload(effect)


def _verified_composition(
    packet: dict[str, Any],
    loaded: LayeredSkillLoad,
    layer_plan: dict[str, Any],
    plan_authorizer: Any,
    drift_assessment: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], list[str], list[str], list[dict[str, Any]]]:
    if type(loaded) is not LayeredSkillLoad:
        raise ProviderSkillAdapterError("controlled layered Skill loader result is required")
    try:
        validate_compiled_skill_packet(packet)
        packet_bytes = canonical_compiled_skill_packet_bytes(packet)
        receipt = loaded.receipt
        validate_layered_skill_load_receipt(receipt)
        receipt_bytes = canonical_layered_skill_load_bytes(receipt)
        plan_snapshot, layer_plan_sha256 = verify_layered_skill_load_plan(
            layer_plan, packet
        )
        validate_skill_drift_assessment(drift_assessment)
        drift_assessment_bytes = canonical_skill_drift_assessment_bytes(
            drift_assessment
        )
    except (
        CompiledSkillPacketError,
        LayeredSkillLoadInputError,
        LayeredSkillLoadPlanError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProviderSkillAdapterError("packet or load receipt is invalid") from exc
    packet_sha256 = hashlib.sha256(packet_bytes).hexdigest()
    if receipt["packet_sha256"] != packet_sha256:
        raise ProviderSkillAdapterError("packet identity does not match load receipt")
    drift_snapshot = json.loads(drift_assessment_bytes)
    if drift_snapshot["gate"] != "allow":
        raise ProviderSkillAdapterError("drift assessment does not allow composition")
    if drift_snapshot["packet_sha256"] != packet_sha256:
        raise ProviderSkillAdapterError("drift assessment packet does not match packet")
    if drift_snapshot["manifest_set_sha256"] != packet["manifest_set_sha256"]:
        raise ProviderSkillAdapterError(
            "drift assessment manifest set does not match packet"
        )
    if receipt["drift_assessment_sha256"] != skill_drift_assessment_digest(
        drift_snapshot
    ):
        raise ProviderSkillAdapterError(
            "load receipt drift assessment does not match assessment"
        )
    if receipt["assessment_observed_at"] != drift_snapshot["observed_at"]:
        raise ProviderSkillAdapterError("load receipt time does not match drift assessment")
    if receipt["layer_plan_sha256"] != layer_plan_sha256:
        raise ProviderSkillAdapterError("load receipt plan does not match layer plan")
    if receipt["requested_layers"] != plan_snapshot["requested_layers"]:
        raise ProviderSkillAdapterError("load receipt requested layers do not match plan")
    if receipt["assessment_observed_at"] != plan_snapshot["observed_at"]:
        raise ProviderSkillAdapterError("load receipt time does not match plan")
    if not hasattr(plan_authorizer, "authorize") or not callable(
        plan_authorizer.authorize
    ):
        raise ProviderSkillAdapterError("layer plan authorizer is required")
    try:
        authorization_ref = plan_authorizer.authorize(
            plan_sha256=layer_plan_sha256,
            packet_sha256=packet_sha256,
            manifest_set_sha256=packet["manifest_set_sha256"],
        )
    except Exception as exc:
        raise ProviderSkillAdapterError("layer plan authorization failed") from exc
    if authorization_ref != receipt["layer_plan_authorization_ref"]:
        raise ProviderSkillAdapterError("load receipt authorization does not match plan")
    packet_snapshot = json.loads(packet_bytes)
    receipt_snapshot = json.loads(receipt_bytes)
    selected_by_id = {
        selection["skill_id"]: selection for selection in packet_snapshot["selections"]
    }
    receipt_by_id = {skill["skill_id"]: skill for skill in receipt_snapshot["skills"]}
    if set(receipt_by_id) != set(selected_by_id):
        raise ProviderSkillAdapterError("load receipt Skill set does not match packet")
    for skill_id, selection in selected_by_id.items():
        receipt_skill = receipt_by_id[skill_id]
        if receipt_skill["content_sha256"] != selection["content_sha256"]:
            raise ProviderSkillAdapterError("load receipt content does not match packet")
    plan_layers = {
        binding["skill_id"]: binding["layer"]
        for binding in plan_snapshot["bindings"]
    }
    receipt_layers = {
        skill["skill_id"]: skill["layer"] for skill in receipt_snapshot["skills"]
    }
    if receipt_layers != plan_layers:
        raise ProviderSkillAdapterError("load receipt Skill layers do not match plan")
    loaded_ids = [
        skill["skill_id"] for skill in receipt_snapshot["skills"] if skill["loaded"]
    ]
    if not loaded_ids:
        raise ProviderSkillAdapterError("load receipt has no composed Skill content")
    if set(loaded.assets) != set(loaded_ids):
        raise ProviderSkillAdapterError("load assets do not match receipt")
    sections: list[dict[str, Any]] = []
    for skill_id in loaded_ids:
        content = loaded.assets[skill_id]
        receipt_skill = receipt_by_id[skill_id]
        if not isinstance(content, bytes) or not content:
            raise ProviderSkillAdapterError(f"Skill content is invalid for {skill_id}")
        if len(content) != receipt_skill["content_bytes"]:
            raise ProviderSkillAdapterError(f"Skill content length is invalid for {skill_id}")
        if hashlib.sha256(content).hexdigest() != receipt_skill["content_sha256"]:
            raise ProviderSkillAdapterError(f"Skill content digest is invalid for {skill_id}")
        sections.append(
            {
                "skill_id": skill_id,
                "layer": receipt_skill["layer"],
                "rule_ids": sorted(selected_by_id[skill_id]["rule_ids"]),
                "content_sha256": receipt_skill["content_sha256"],
                "content_bytes": len(content),
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        )
    sections, _ = _validated_sections(sections)
    rule_ids = [rule_id for section in sections for rule_id in section["rule_ids"]]
    return packet_snapshot, receipt_snapshot, loaded_ids, rule_ids, sections


@dataclass(frozen=True)
class ProviderSkillAdapter:
    """Provider-specific delivery-plan builder with zero state commit authority."""

    provider_id: str
    adapter_surface_contract: str

    def compose(
        self,
        packet: dict[str, Any],
        loaded: LayeredSkillLoad,
        *,
        layer_plan: dict[str, Any],
        plan_authorizer: Any,
        drift_assessment: dict[str, Any],
    ) -> dict[str, Any]:
        packet_snapshot, receipt, skill_ids, rule_ids, sections = _verified_composition(
            packet,
            loaded,
            layer_plan,
            plan_authorizer,
            drift_assessment,
        )
        packet_bytes = canonical_compiled_skill_packet_bytes(packet_snapshot)
        receipt_bytes = canonical_layered_skill_load_bytes(receipt)
        effect = {
            "schema_version": SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "adapter_version": ADAPTER_VERSION,
            "adapter_surface_contract": self.adapter_surface_contract,
            "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
            "layer_plan_sha256": receipt["layer_plan_sha256"],
            "load_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "selected_skill_ids": skill_ids,
            "selected_rule_ids": rule_ids,
            "composition_sha256": hashlib.sha256(
                _canonical_json_bytes(sections)
            ).hexdigest(),
            "composition_sections": sections,
            "composition_body_bytes": sum(
                section["content_bytes"] for section in sections
            ),
            "provider_payload_sha256": "0" * 64,
            "provider_payload_bytes": 0,
            "writes_runtime_state": False,
            "state_revision": None,
        }
        payload = _render_provider_payload(effect)
        if len(payload) > MAX_PROVIDER_PAYLOAD_BYTES:
            raise ProviderSkillAdapterError("provider payload exceeds maximum bytes")
        effect["provider_payload_sha256"] = hashlib.sha256(payload).hexdigest()
        effect["provider_payload_bytes"] = len(payload)
        validate_provider_skill_effect(effect)
        return effect


def get_provider_skill_adapter(provider_id: str) -> ProviderSkillAdapter:
    """Return a known adapter; unknown providers remain unavailable."""
    if provider_id not in PROVIDER_SURFACE_CONTRACTS:
        raise ProviderSkillAdapterError("provider_id is unsupported")
    return ProviderSkillAdapter(provider_id, PROVIDER_SURFACE_CONTRACTS[provider_id])
