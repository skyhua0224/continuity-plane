"""Deterministic, candidate-only Skill proposals from bounded project inputs."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from context_control_plane import skill_manifest_set
from context_control_plane.schema_governance import (
    SchemaGovernanceError,
    SemanticVersion,
)

SCHEMA_VERSION = "context.skill-proposal/v1alpha1"
MAX_INPUT_BYTES = 64 * 1024
MAX_PROPOSAL_BYTES = 128 * 1024
MAX_IDENTIFIER_CHARS = 256
MAX_INPUT_LIST_ITEMS = 4096
MAX_INPUT_STRING_CHARS = 65_536
MAX_SKILL_CONTENT_CHARS = 16_384
MAX_SKILL_CONTENT_BYTES = 64 * 1024

_PROJECT_FACT_FIELDS = {
    "repo_id",
    "topology",
    "languages",
    "build_commands",
    "test_commands",
    "source_refs",
}
_VERIFICATION_PROFILE_FIELDS = {
    "profile_id",
    "required_gates",
    "generated_skill_license_ref",
    "source_refs",
}
_USER_PREFERENCE_FIELDS = {
    "preference_ids",
    "tool_preferences",
    "style_preferences",
    "source_refs",
}
_PROPOSAL_FIELDS = {
    "schema_version",
    "proposal_id",
    "generated_at",
    "generator_version",
    "input_fingerprint",
    "input_refs",
    "status",
    "activation",
    "candidates",
}
_ACTIVATION_FIELDS = {"status", "approved_revision"}
_CANDIDATE_FIELDS = {
    "candidate_id",
    "source_kind",
    "manifest",
    "content",
    "provenance_refs",
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
_ARTIFACT_REF_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_RFC3339_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:"
    r"[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_TOPOLOGIES = {"modular", "monolith", "mixed"}


class SkillProposalError(ValueError):
    """Raised when bounded proposal input or output is invalid."""


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SkillProposalError(f"{field} fields are invalid")
    return value


def _preflight_input_object(
    value: Any,
    fields: set[str],
    list_fields: set[str],
    field: str,
) -> int:
    """Bound typed input before copying, sorting, or duplicate detection."""
    root = _object(value, fields, field)
    total_bytes = 0
    for name, item in root.items():
        values = item if name in list_fields else [item]
        if name in list_fields and (
            not isinstance(item, list) or len(item) > MAX_INPUT_LIST_ITEMS
        ):
            raise SkillProposalError(f"{field}.{name} exceeds the input item limit")
        for value_item in values:
            if not isinstance(value_item, str):
                raise SkillProposalError(f"{field}.{name} must contain strings")
            if len(value_item) > MAX_INPUT_STRING_CHARS:
                raise SkillProposalError(f"{field}.{name} exceeds the input string limit")
            try:
                total_bytes += len(value_item.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise SkillProposalError(f"{field}.{name} must be valid UTF-8") from exc
            if total_bytes > MAX_INPUT_BYTES:
                raise SkillProposalError("proposal input exceeds the 64 KiB preflight limit")
    return total_bytes


def _preflight_inputs(
    project_facts: Any,
    verification_profile: Any,
    user_preferences: Any,
) -> None:
    total_bytes = _preflight_input_object(
        project_facts,
        _PROJECT_FACT_FIELDS,
        {"languages", "build_commands", "test_commands", "source_refs"},
        "project_facts",
    )
    total_bytes += _preflight_input_object(
        verification_profile,
        _VERIFICATION_PROFILE_FIELDS,
        {"required_gates", "source_refs"},
        "verification_profile",
    )
    total_bytes += _preflight_input_object(
        user_preferences,
        _USER_PREFERENCE_FIELDS,
        {"preference_ids", "tool_preferences", "style_preferences", "source_refs"},
        "user_preferences",
    )
    if total_bytes > MAX_INPUT_BYTES:
        raise SkillProposalError("proposal input exceeds the 64 KiB preflight limit")


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillProposalError(f"{field} must be a non-empty string")
    if "\r" in value or "\n" in value:
        raise SkillProposalError(f"{field} cannot contain newlines")
    return value


def _identifier(value: Any, field: str) -> str:
    value = _string(value, field)
    if len(value) > MAX_IDENTIFIER_CHARS or not _ID_RE.fullmatch(value):
        raise SkillProposalError(f"{field} is invalid")
    return value


def _semver(value: Any, field: str) -> str:
    value = _string(value, field)
    try:
        SemanticVersion.parse(value)
    except SchemaGovernanceError as exc:
        raise SkillProposalError(f"{field} must be strict SemVer") from exc
    return value


def _artifact_refs(value: Any, field: str, *, required: bool) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise SkillProposalError(f"{field} must be a {'non-empty ' if required else ''}list")
    if any(not isinstance(item, str) or not _ARTIFACT_REF_RE.fullmatch(item) for item in value):
        raise SkillProposalError(f"{field} contains an invalid artifact reference")
    if len(value) != len(set(value)):
        raise SkillProposalError(f"{field} must be unique")
    return value


def _string_list(value: Any, field: str, *, required: bool) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise SkillProposalError(f"{field} must be a {'non-empty ' if required else ''}list")
    result = [_string(item, field) for item in value]
    if len(result) != len(set(result)):
        raise SkillProposalError(f"{field} must be unique")
    return result


def _timestamp(value: Any, field: str) -> str:
    value = _string(value, field)
    if not _RFC3339_RE.fullmatch(value):
        raise SkillProposalError(f"{field} must be RFC3339")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SkillProposalError(f"{field} must be RFC3339") from exc
    return value


def _validate_project_facts(value: Any) -> dict[str, Any]:
    facts = copy.deepcopy(_object(value, _PROJECT_FACT_FIELDS, "project_facts"))
    _identifier(facts["repo_id"], "project_facts.repo_id")
    if facts["topology"] not in _TOPOLOGIES:
        raise SkillProposalError("project_facts.topology is invalid")
    facts["languages"] = sorted(
        _string_list(facts["languages"], "project_facts.languages", required=True)
    )
    _string_list(facts["build_commands"], "project_facts.build_commands", required=True)
    _string_list(facts["test_commands"], "project_facts.test_commands", required=True)
    facts["source_refs"] = sorted(
        _artifact_refs(facts["source_refs"], "project_facts.source_refs", required=True)
    )
    return facts


def _validate_verification_profile(value: Any) -> dict[str, Any]:
    profile = copy.deepcopy(
        _object(value, _VERIFICATION_PROFILE_FIELDS, "verification_profile")
    )
    _identifier(profile["profile_id"], "verification_profile.profile_id")
    _string(
        profile["generated_skill_license_ref"],
        "verification_profile.generated_skill_license_ref",
    )
    profile["required_gates"] = sorted(
        _string_list(
            profile["required_gates"],
            "verification_profile.required_gates",
            required=True,
        )
    )
    profile["source_refs"] = sorted(
        _artifact_refs(
            profile["source_refs"],
            "verification_profile.source_refs",
            required=True,
        )
    )
    return profile


def _validate_user_preferences(value: Any) -> dict[str, Any]:
    preferences = copy.deepcopy(
        _object(value, _USER_PREFERENCE_FIELDS, "user_preferences")
    )
    for field in ("preference_ids", "tool_preferences", "style_preferences"):
        preferences[field] = sorted(
            _string_list(
                preferences[field],
                f"user_preferences.{field}",
                required=False,
            )
        )
    refs = sorted(
        _artifact_refs(
            preferences["source_refs"],
            "user_preferences.source_refs",
            required=False,
        )
    )
    preferences["source_refs"] = refs
    has_preferences = any(
        preferences[field]
        for field in ("preference_ids", "tool_preferences", "style_preferences")
    )
    if has_preferences and not refs:
        raise SkillProposalError("user preferences require input provenance")
    return preferences


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _input_refs(
    project_facts: dict[str, Any],
    verification_profile: dict[str, Any],
    user_preferences: dict[str, Any],
) -> list[str]:
    return sorted(
        set(project_facts["source_refs"])
        | set(verification_profile["source_refs"])
        | set(user_preferences["source_refs"])
    )


def _markdown_list(values: list[str]) -> list[str]:
    encoded = [json.dumps(value, ensure_ascii=False).replace("`", r"\u0060") for value in values]
    return [f"- `{value}`" for value in encoded]


def _candidate_content_bytes(
    source_kind: str,
    project_facts: dict[str, Any],
    verification_profile: dict[str, Any],
    user_preferences: dict[str, Any],
    input_refs: list[str],
) -> bytes:
    title = "Project Verification Skill Candidate"
    lines = [
        f"# {title}",
        "",
        f"Repository: `{project_facts['repo_id']}`",
        f"Topology: `{project_facts['topology']}`",
        "",
    ]
    if source_kind == "project":
        lines.extend(["## Languages", "", *_markdown_list(project_facts["languages"]), ""])
        lines.extend(
            ["## Build Commands", "", *_markdown_list(project_facts["build_commands"]), ""]
        )
        lines.extend(
            ["## Test Commands", "", *_markdown_list(project_facts["test_commands"]), ""]
        )
        lines.extend(
            [
                "## Required Gates",
                "",
                *_markdown_list(verification_profile["required_gates"]),
                "",
            ]
        )
    else:
        lines[0] = "# User Preference Skill Candidate"
        lines.extend(
            [
                "Apply these explicit preferences only when they do not conflict with project rules.",
                "",
                "## Preference IDs",
                "",
                *_markdown_list(user_preferences["preference_ids"]),
                "",
                "## Tool Preferences",
                "",
                *_markdown_list(user_preferences["tool_preferences"]),
                "",
                "## Style Preferences",
                "",
                *_markdown_list(user_preferences["style_preferences"]),
                "",
            ]
        )
    lines.extend(["## Provenance", "", *_markdown_list(input_refs), ""])
    return "\n".join(lines).encode("utf-8")


def _candidate(
    source_kind: str,
    project_facts: dict[str, Any],
    verification_profile: dict[str, Any],
    user_preferences: dict[str, Any],
    input_refs: list[str],
) -> dict[str, Any]:
    repo_id = project_facts["repo_id"]
    skill_id = f"proposal.{source_kind}.{repo_id}"
    candidate_refs = (
        input_refs
        if source_kind == "user"
        else sorted(
            set(project_facts["source_refs"])
            | set(verification_profile["source_refs"])
        )
    )
    content = _candidate_content_bytes(
        source_kind,
        project_facts,
        verification_profile,
        user_preferences,
        candidate_refs,
    )
    content_sha256 = hashlib.sha256(content).hexdigest()
    manifest = {
        "skill_id": skill_id,
        "version": "0.1.0",
        "content_sha256": content_sha256,
        "source_kind": source_kind,
        "license_ref": verification_profile["generated_skill_license_ref"],
        "applicability": [{"kind": "repo", "ref": f"repo://{repo_id}"}],
        "rule_ids": [f"rule.{source_kind}.{repo_id}.proposal"],
        "dependencies": [],
        "conflicts": [],
        "expires_at": None,
        "compatibility": {
            "schema_refs": ["context.compiled-skill-packet/v1alpha1"],
            "provider_contract_refs": ["provider://codex/v1", "provider://claude/v1"],
        },
        "provenance_refs": candidate_refs,
        "status": "proposed",
    }
    return {
        "candidate_id": f"proposal/{source_kind}/{repo_id}",
        "source_kind": source_kind,
        "manifest": manifest,
        "content": content.decode("utf-8"),
        "provenance_refs": candidate_refs,
        "permissions": {field: False for field in sorted(_PERMISSION_FIELDS)},
        "status": "candidate",
    }


def generate_skill_proposal(
    project_facts: dict[str, Any],
    verification_profile: dict[str, Any],
    user_preferences: dict[str, Any],
    *,
    generator_version: str,
    generated_at: str,
) -> dict[str, Any]:
    """Generate a reproducible proposal without activating any Skill."""
    _preflight_inputs(project_facts, verification_profile, user_preferences)
    project_facts = _validate_project_facts(project_facts)
    verification_profile = _validate_verification_profile(verification_profile)
    user_preferences = _validate_user_preferences(user_preferences)
    bounded_inputs = {
        "project_facts": project_facts,
        "verification_profile": verification_profile,
        "user_preferences": user_preferences,
    }
    if len(_canonical_json_bytes(bounded_inputs)) > MAX_INPUT_BYTES:
        raise SkillProposalError("proposal input exceeds the 64 KiB canonical limit")
    generator_version = _semver(generator_version, "generator_version")
    generated_at = _timestamp(generated_at, "generated_at")
    input_refs = _input_refs(project_facts, verification_profile, user_preferences)
    fingerprint_material = {**bounded_inputs, "generator_version": generator_version}
    input_fingerprint = hashlib.sha256(_canonical_json_bytes(fingerprint_material)).hexdigest()
    candidates = [
        _candidate(
            "project",
            project_facts,
            verification_profile,
            user_preferences,
            input_refs,
        )
    ]
    if any(
        user_preferences[field]
        for field in ("preference_ids", "tool_preferences", "style_preferences")
    ):
        candidates.append(
            _candidate(
                "user",
                project_facts,
                verification_profile,
                user_preferences,
                input_refs,
            )
        )
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "proposal_id": f"skill-proposal/{project_facts['repo_id']}",
        "generated_at": generated_at,
        "generator_version": generator_version,
        "input_fingerprint": input_fingerprint,
        "input_refs": input_refs,
        "status": "proposed",
        "activation": {"status": "not_active", "approved_revision": None},
        "candidates": candidates,
    }
    validate_skill_proposal(proposal)
    return json.loads(canonical_skill_proposal_bytes(proposal))


def _validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise SkillProposalError("candidate.manifest must be an object")
    try:
        skill_manifest_set.validate_skill_manifest_set(
            {"schema_version": skill_manifest_set.SCHEMA_VERSION, "manifests": [manifest]}
        )
    except skill_manifest_set.SkillManifestSetError as exc:
        raise SkillProposalError(str(exc)) from exc
    if manifest["status"] != "proposed":
        raise SkillProposalError("proposal manifests must remain proposed")
    _identifier(manifest["skill_id"], "candidate.manifest.skill_id")
    for rule_id in manifest["rule_ids"]:
        _identifier(rule_id, "candidate.manifest.rule_id")
    for relation in (*manifest["dependencies"], *manifest["conflicts"]):
        _identifier(relation["skill_id"], "candidate.manifest.relation.skill_id")
    return manifest


def validate_skill_proposal(proposal: dict[str, Any]) -> None:
    """Validate proposal structure and keep activation fail-closed."""
    root = _object(proposal, _PROPOSAL_FIELDS, "proposal")
    if len(_canonical_json_bytes(root)) > MAX_PROPOSAL_BYTES:
        raise SkillProposalError("proposal exceeds the 128 KiB canonical limit")
    if root["schema_version"] != SCHEMA_VERSION:
        raise SkillProposalError("unsupported schema_version")
    _identifier(root["proposal_id"], "proposal_id")
    _timestamp(root["generated_at"], "generated_at")
    _semver(root["generator_version"], "generator_version")
    if not isinstance(root["input_fingerprint"], str) or not _SHA256_RE.fullmatch(
        root["input_fingerprint"]
    ):
        raise SkillProposalError("input_fingerprint must be lowercase SHA-256")
    input_refs = _artifact_refs(root["input_refs"], "input_refs", required=True)
    if root["status"] != "proposed":
        raise SkillProposalError("proposals cannot be activated by mutation")
    activation = _object(root["activation"], _ACTIVATION_FIELDS, "activation")
    if activation["status"] != "not_active" or activation["approved_revision"] is not None:
        raise SkillProposalError("proposal activation must remain not_active")
    candidates = root["candidates"]
    if not isinstance(candidates, list) or not candidates:
        raise SkillProposalError("candidates must be a non-empty list")
    seen: set[str] = set()
    source_kinds: set[str] = set()
    for candidate in candidates:
        candidate = _object(candidate, _CANDIDATE_FIELDS, "candidate")
        candidate_id = _identifier(candidate["candidate_id"], "candidate.candidate_id")
        if candidate_id in seen:
            raise SkillProposalError("candidate IDs must be unique")
        seen.add(candidate_id)
        source_kind = candidate["source_kind"]
        if source_kind not in {"project", "user"}:
            raise SkillProposalError("proposal source kind is invalid")
        if source_kind in source_kinds:
            raise SkillProposalError("proposal source kinds must be unique")
        source_kinds.add(source_kind)
        manifest = _validate_manifest(candidate["manifest"])
        if manifest["source_kind"] != source_kind:
            raise SkillProposalError("candidate source kind must match manifest")
        skill_prefix = f"proposal.{source_kind}."
        if not manifest["skill_id"].startswith(skill_prefix) or candidate_id != (
            f"proposal/{source_kind}/{manifest['skill_id'].removeprefix(skill_prefix)}"
        ):
            raise SkillProposalError("candidate ID must bind to manifest Skill ID")
        if candidate["status"] != "candidate":
            raise SkillProposalError("proposal candidates must remain candidate")
        content = candidate["content"]
        if not isinstance(content, str) or not content:
            raise SkillProposalError("candidate content must be a non-empty UTF-8 string")
        content_bytes = content.encode("utf-8")
        if (
            len(content) > MAX_SKILL_CONTENT_CHARS
            or len(content_bytes) > MAX_SKILL_CONTENT_BYTES
        ):
            raise SkillProposalError("candidate content exceeds the 64 KiB limit")
        if (
            content.startswith(("\ufeff", "---\n"))
            or "\r" in content
            or not content.endswith("\n")
        ):
            raise SkillProposalError(
                "candidate content must be body-only Markdown with LF line endings"
            )
        if hashlib.sha256(content_bytes).hexdigest() != manifest["content_sha256"]:
            raise SkillProposalError("candidate content must match manifest digest")
        refs = _artifact_refs(candidate["provenance_refs"], "candidate.provenance_refs", required=True)
        if not set(refs).issubset(input_refs):
            raise SkillProposalError("candidate provenance must reference proposal inputs")
        if set(refs) != set(manifest["provenance_refs"]):
            raise SkillProposalError("candidate provenance must match manifest provenance")
        permissions = _object(candidate["permissions"], _PERMISSION_FIELDS, "candidate.permissions")
        if any(type(permissions[field]) is not bool for field in _PERMISSION_FIELDS) or any(
            permissions.values()
        ):
            raise SkillProposalError("proposal candidates cannot grant authority")
    if "project" not in source_kinds:
        raise SkillProposalError("proposal must include a project candidate")


def canonical_skill_proposal_bytes(proposal: dict[str, Any]) -> bytes:
    """Return stable bytes for a validated proposal without mutating input."""
    validate_skill_proposal(proposal)
    canonical = copy.deepcopy(proposal)
    canonical["input_refs"] = sorted(canonical["input_refs"])
    canonical["candidates"] = sorted(
        canonical["candidates"], key=lambda item: item["candidate_id"]
    )
    for candidate in canonical["candidates"]:
        candidate["provenance_refs"] = sorted(candidate["provenance_refs"])
        candidate["permissions"] = {
            key: candidate["permissions"][key] for key in sorted(candidate["permissions"])
        }
        manifest_set = {
            "schema_version": skill_manifest_set.SCHEMA_VERSION,
            "manifests": [candidate["manifest"]],
        }
        candidate["manifest"] = json.loads(
            skill_manifest_set.canonical_skill_manifest_set_bytes(manifest_set)
        )["manifests"][0]
    return _canonical_json_bytes(canonical)


def skill_proposal_digest(proposal: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_skill_proposal_bytes(proposal)).hexdigest()


def proposal_skill_assets(proposal: dict[str, Any]) -> dict[str, bytes]:
    """Return exact candidate bytes for bytes-only Skill asset resolvers."""
    validate_skill_proposal(proposal)
    return {
        candidate["manifest"]["skill_id"]: candidate["content"].encode("utf-8")
        for candidate in proposal["candidates"]
    }


def verify_skill_proposal_inputs(
    proposal: dict[str, Any],
    project_facts: dict[str, Any],
    verification_profile: dict[str, Any],
    user_preferences: dict[str, Any],
    *,
    generator_version: str,
    expected_generated_at: str,
) -> None:
    """Verify proposal identity by replaying its original bounded inputs."""
    validate_skill_proposal(proposal)
    _timestamp(expected_generated_at, "expected_generated_at")
    if proposal["generated_at"] != expected_generated_at:
        raise SkillProposalError("proposal generated_at does not match expected timestamp")
    expected = generate_skill_proposal(
        project_facts,
        verification_profile,
        user_preferences,
        generator_version=generator_version,
        generated_at=expected_generated_at,
    )
    if canonical_skill_proposal_bytes(proposal) != canonical_skill_proposal_bytes(expected):
        raise SkillProposalError("proposal does not match the supplied bounded inputs")
