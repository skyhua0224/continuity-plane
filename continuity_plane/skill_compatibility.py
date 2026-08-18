"""Lock an active task to one immutable Skill rule set."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from typing import Any

from continuity_plane import compiled_skill_packet, skill_manifest_set

LOCK_SCHEMA_VERSION = "context.skill-compatibility-lock/v1alpha1"
DECISION_SCHEMA_VERSION = "context.skill-compatibility-decision/v1alpha1"
MIGRATION_SCHEMA_VERSION = "context.skill-compatibility-migration/v1alpha1"

_LOCK_FIELDS = {
    "schema_version",
    "task_id",
    "operation_id",
    "manifest_set_sha256",
    "packet_sha256",
    "packet_schema_version",
    "provider_contract_refs",
    "skills",
    "selected_rule_ids",
}
_SKILL_FIELDS = {
    "skill_id",
    "version",
    "content_sha256",
    "manifest_sha256",
    "rule_ids",
}
_DECISION_FIELDS = {
    "schema_version",
    "task_id",
    "operation_id",
    "lock_sha256",
    "candidate_lock_sha256",
    "candidate_manifest_set_sha256",
    "candidate_packet_sha256",
    "provider_contract_refs",
    "classification",
    "reason_codes",
    "delivery_allowed_without_migration",
}
_MIGRATION_FIELDS = {
    "schema_version",
    "migration_id",
    "reason",
    "task_id",
    "operation_id",
    "old_lock_sha256",
    "new_lock_sha256",
    "approval_ref",
    "replay_proof_ref",
    "rollback_proof_ref",
    "old_lock",
    "new_lock",
}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_OPERATION_RE = re.compile(r"^operation://[a-z0-9][a-z0-9._-]*$")
_PROVIDER_CONTRACT_RE = re.compile(r"^provider://[a-z0-9][a-z0-9._-]*/v[a-z0-9._-]+$")
_ADAPTER_SURFACE_RE = re.compile(
    r"^context\.adapter-surface/[a-z0-9][a-z0-9._/-]*/v[a-z0-9._-]+$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_CLASSIFICATIONS = {"unchanged", "compatible", "migration_required", "rejected"}
_COMPATIBLE_REASON = "unselected-manifest-metadata-changed"
_MIGRATION_REASON_CODES = {
    "provider-contract-changed",
    "selected-skill-missing",
    "selected-skill-identity-changed",
    "selected-skill-manifest-changed",
    "compiled-packet-changed",
    "compiled-rule-set-changed",
}
_REJECTING_REASON_CODES = {
    "selected-skill-missing",
    "selected-skill-provider-incompatible",
    "selected-skill-unavailable",
}


class SkillCompatibilityError(ValueError):
    """Raised when a compatibility lock or migration is not trustworthy."""


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise SkillCompatibilityError(f"{field} fields are invalid")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 256 or not _ID_RE.fullmatch(value):
        raise SkillCompatibilityError(f"{field} is invalid")
    return value


def _operation(value: Any, field: str = "operation_id") -> str:
    if not isinstance(value, str) or not _OPERATION_RE.fullmatch(value):
        raise SkillCompatibilityError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SkillCompatibilityError(f"{field} is invalid")
    return value


def _artifact_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _ARTIFACT_RE.fullmatch(value):
        raise SkillCompatibilityError(f"{field} is invalid")
    return value


def _provider_contract_refs(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SkillCompatibilityError("provider_contract_refs must be non-empty")
    if any(
        not isinstance(item, str) or not _PROVIDER_CONTRACT_RE.fullmatch(item)
        for item in value
    ):
        raise SkillCompatibilityError("provider_contract_refs are invalid")
    if len(value) != len(set(value)):
        raise SkillCompatibilityError("provider_contract_refs must be unique")
    return value


def _rule_ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SkillCompatibilityError(f"{field} must be non-empty")
    normalized = [_identifier(item, field) for item in value]
    if len(normalized) != len(set(normalized)):
        raise SkillCompatibilityError(f"{field} must be unique")
    return normalized


def _validate_skill(skill: Any) -> dict[str, Any]:
    skill = _object(skill, _SKILL_FIELDS, "lock.skill")
    _identifier(skill["skill_id"], "lock.skill.skill_id")
    try:
        skill_manifest_set.SemanticVersion.parse(skill["version"])
    except (TypeError, ValueError) as exc:
        raise SkillCompatibilityError("lock.skill.version is invalid") from exc
    _sha256(skill["content_sha256"], "lock.skill.content_sha256")
    _sha256(skill["manifest_sha256"], "lock.skill.manifest_sha256")
    _rule_ids(skill["rule_ids"], "lock.skill.rule_ids")
    return skill


def validate_skill_compatibility_lock(lock: dict[str, Any]) -> None:
    """Validate a lock without consulting mutable Skill sources."""
    lock = _object(lock, _LOCK_FIELDS, "lock")
    if lock["schema_version"] != LOCK_SCHEMA_VERSION:
        raise SkillCompatibilityError("lock.schema_version is unsupported")
    _identifier(lock["task_id"], "lock.task_id")
    _operation(lock["operation_id"], "lock.operation_id")
    _sha256(lock["manifest_set_sha256"], "lock.manifest_set_sha256")
    _sha256(lock["packet_sha256"], "lock.packet_sha256")
    if lock["packet_schema_version"] != compiled_skill_packet.SCHEMA_VERSION:
        raise SkillCompatibilityError("lock.packet_schema_version is unsupported")
    _provider_contract_refs(lock["provider_contract_refs"])
    skills = lock["skills"]
    if not isinstance(skills, list) or not skills:
        raise SkillCompatibilityError("lock.skills must be non-empty")
    skill_ids: set[str] = set()
    selected_rules: set[str] = set()
    for skill in skills:
        skill = _validate_skill(skill)
        if skill["skill_id"] in skill_ids:
            raise SkillCompatibilityError("lock.skills must be unique")
        skill_ids.add(skill["skill_id"])
        for rule_id in skill["rule_ids"]:
            if rule_id in selected_rules:
                raise SkillCompatibilityError("lock rule IDs must be globally unique")
            selected_rules.add(rule_id)
    declared_rules = set(_rule_ids(lock["selected_rule_ids"], "lock.selected_rule_ids"))
    if declared_rules != selected_rules:
        raise SkillCompatibilityError("lock.selected_rule_ids do not match skills")


def canonical_skill_compatibility_lock_bytes(lock: dict[str, Any]) -> bytes:
    """Return deterministic bytes for a validated compatibility lock."""
    validate_skill_compatibility_lock(lock)
    canonical = copy.deepcopy(lock)
    canonical["provider_contract_refs"] = sorted(canonical["provider_contract_refs"])
    canonical["selected_rule_ids"] = sorted(canonical["selected_rule_ids"])
    canonical["skills"] = sorted(canonical["skills"], key=lambda item: item["skill_id"])
    for skill in canonical["skills"]:
        skill["rule_ids"] = sorted(skill["rule_ids"])
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def skill_compatibility_lock_digest(lock: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_skill_compatibility_lock_bytes(lock)).hexdigest()


def _manifest_set_digest(manifest_set: dict[str, Any]) -> str:
    return hashlib.sha256(
        skill_manifest_set.canonical_skill_manifest_set_bytes(manifest_set)
    ).hexdigest()


def _manifest_digests(manifest_set: dict[str, Any]) -> dict[str, str]:
    canonical = json.loads(
        skill_manifest_set.canonical_skill_manifest_set_bytes(manifest_set)
    )
    return {
        manifest["skill_id"]: hashlib.sha256(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for manifest in canonical["manifests"]
    }


def _selected_provider_contract_errors(
    manifest_set: dict[str, Any],
    selected_skill_ids: set[str],
    provider_contract_refs: list[str],
) -> list[str]:
    by_id = {manifest["skill_id"]: manifest for manifest in manifest_set["manifests"]}
    invalid: list[str] = []
    for skill_id in sorted(selected_skill_ids):
        manifest = by_id.get(skill_id)
        if manifest is None:
            continue
        supported_contracts = set(manifest["compatibility"]["provider_contract_refs"])
        provider_applicability = {
            item["ref"]
            for item in manifest["applicability"]
            if item["kind"] == "provider"
        }
        for contract_ref in provider_contract_refs:
            provider_ref = contract_ref.rsplit("/", 1)[0]
            if contract_ref not in supported_contracts or (
                provider_applicability and provider_ref not in provider_applicability
            ):
                invalid.append(skill_id)
                break
    return invalid


def _lock_skills_from_packet(
    packet: dict[str, Any], manifest_set: dict[str, Any]
) -> list[dict[str, Any]]:
    manifest_digests = _manifest_digests(manifest_set)
    return [
        {
            "skill_id": selection["skill_id"],
            "version": selection["version"],
            "content_sha256": selection["content_sha256"],
            "manifest_sha256": manifest_digests[selection["skill_id"]],
            "rule_ids": sorted(selection["rule_ids"]),
        }
        for selection in sorted(packet["selections"], key=lambda item: item["skill_id"])
    ]


def create_skill_compatibility_lock(
    manifest_set: dict[str, Any],
    packet: dict[str, Any],
    *,
    task_id: str,
    operation_id: str,
    provider_contract_refs: list[str],
) -> dict[str, Any]:
    """Bind a task and operation to a verified compiled Skill packet."""
    _identifier(task_id, "task_id")
    _operation(operation_id)
    _provider_contract_refs(provider_contract_refs)
    try:
        compiled_skill_packet.verify_compiled_skill_packet(packet, manifest_set)
    except (
        compiled_skill_packet.CompiledSkillPacketError,
        skill_manifest_set.SkillManifestSetError,
    ) as exc:
        raise SkillCompatibilityError(str(exc)) from exc
    selected_skill_ids = {selection["skill_id"] for selection in packet["selections"]}
    if _selected_provider_contract_errors(
        manifest_set, selected_skill_ids, provider_contract_refs
    ):
        raise SkillCompatibilityError(
            "selected Skill provider compatibility does not allow lock contracts"
        )
    skills = _lock_skills_from_packet(packet, manifest_set)
    lock = {
        "schema_version": LOCK_SCHEMA_VERSION,
        "task_id": task_id,
        "operation_id": operation_id,
        "manifest_set_sha256": _manifest_set_digest(manifest_set),
        "packet_sha256": compiled_skill_packet.compiled_skill_packet_digest(packet),
        "packet_schema_version": packet["schema_version"],
        "provider_contract_refs": sorted(provider_contract_refs),
        "skills": skills,
        "selected_rule_ids": sorted(
            rule_id for skill in skills for rule_id in skill["rule_ids"]
        ),
    }
    return json.loads(canonical_skill_compatibility_lock_bytes(lock))


def _source_skills_for_lock(
    lock: dict[str, Any], manifest_set: dict[str, Any]
) -> list[dict[str, Any]] | None:
    by_id = {manifest["skill_id"]: manifest for manifest in manifest_set["manifests"]}
    manifest_digests = _manifest_digests(manifest_set)
    result: list[dict[str, Any]] = []
    for locked in lock["skills"]:
        manifest = by_id.get(locked["skill_id"])
        if manifest is None:
            return None
        result.append(
            {
                "skill_id": manifest["skill_id"],
                "version": manifest["version"],
                "content_sha256": manifest["content_sha256"],
                "manifest_sha256": manifest_digests[manifest["skill_id"]],
                "rule_ids": sorted(manifest["rule_ids"]),
            }
        )
    return sorted(result, key=lambda item: item["skill_id"])


def validate_skill_compatibility_decision(decision: dict[str, Any]) -> None:
    decision = _object(decision, _DECISION_FIELDS, "decision")
    if decision["schema_version"] != DECISION_SCHEMA_VERSION:
        raise SkillCompatibilityError("decision.schema_version is unsupported")
    _identifier(decision["task_id"], "decision.task_id")
    _operation(decision["operation_id"], "decision.operation_id")
    _sha256(decision["lock_sha256"], "decision.lock_sha256")
    if decision["candidate_lock_sha256"] is not None:
        _sha256(decision["candidate_lock_sha256"], "decision.candidate_lock_sha256")
    _sha256(
        decision["candidate_manifest_set_sha256"],
        "decision.candidate_manifest_set_sha256",
    )
    _sha256(decision["candidate_packet_sha256"], "decision.candidate_packet_sha256")
    _provider_contract_refs(decision["provider_contract_refs"])
    if decision["classification"] not in _CLASSIFICATIONS:
        raise SkillCompatibilityError("decision.classification is invalid")
    reasons = decision["reason_codes"]
    if (
        not isinstance(reasons, list)
        or any(
            not isinstance(reason, str) or not _ID_RE.fullmatch(reason)
            for reason in reasons
        )
        or len(reasons) != len(set(reasons))
    ):
        raise SkillCompatibilityError("decision.reason_codes are invalid")
    if not isinstance(decision["delivery_allowed_without_migration"], bool):
        raise SkillCompatibilityError("decision delivery gate is invalid")
    expected_allowed = decision["classification"] in {"unchanged", "compatible"}
    if decision["delivery_allowed_without_migration"] != expected_allowed:
        raise SkillCompatibilityError(
            "decision delivery gate contradicts classification"
        )
    if decision["classification"] == "unchanged" and reasons:
        raise SkillCompatibilityError("unchanged decision cannot contain reasons")
    if decision["classification"] == "compatible" and reasons != [_COMPATIBLE_REASON]:
        raise SkillCompatibilityError("compatible decision reason is invalid")
    if decision["classification"] == "migration_required" and not set(reasons).issubset(
        _MIGRATION_REASON_CODES
    ):
        raise SkillCompatibilityError("migration decision reason is invalid")
    if decision["classification"] in {"migration_required", "rejected"} and not reasons:
        raise SkillCompatibilityError("blocked decision requires reasons")


def canonical_skill_compatibility_decision_bytes(decision: dict[str, Any]) -> bytes:
    validate_skill_compatibility_decision(decision)
    canonical = copy.deepcopy(decision)
    canonical["provider_contract_refs"] = sorted(canonical["provider_contract_refs"])
    canonical["reason_codes"] = sorted(canonical["reason_codes"])
    return json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def assess_skill_compatibility(
    lock: dict[str, Any],
    candidate_manifest_set: dict[str, Any],
    candidate_packet: dict[str, Any],
    *,
    provider_contract_refs: list[str],
) -> dict[str, Any]:
    """Classify a candidate without activating it."""
    validate_skill_compatibility_lock(lock)
    _provider_contract_refs(provider_contract_refs)
    try:
        skill_manifest_set.validate_skill_manifest_set(candidate_manifest_set)
        compiled_skill_packet.validate_compiled_skill_packet(candidate_packet)
    except (
        skill_manifest_set.SkillManifestSetError,
        compiled_skill_packet.CompiledSkillPacketError,
    ) as exc:
        raise SkillCompatibilityError(str(exc)) from exc

    manifest_digest = _manifest_set_digest(candidate_manifest_set)
    packet_digest = compiled_skill_packet.compiled_skill_packet_digest(candidate_packet)
    source_skills = _source_skills_for_lock(lock, candidate_manifest_set)
    try:
        packet_skills = _lock_skills_from_packet(
            candidate_packet, candidate_manifest_set
        )
    except KeyError:
        packet_skills = []
    locked_skills = sorted(
        copy.deepcopy(lock["skills"]), key=lambda item: item["skill_id"]
    )
    for skill in locked_skills:
        skill["rule_ids"] = sorted(skill["rule_ids"])

    reasons: set[str] = set()
    selected_skill_ids = {skill["skill_id"] for skill in lock["skills"]}
    candidate_by_id = {
        manifest["skill_id"]: manifest
        for manifest in candidate_manifest_set["manifests"]
    }
    if sorted(provider_contract_refs) != sorted(lock["provider_contract_refs"]):
        reasons.add("provider-contract-changed")
    if source_skills is None:
        reasons.add("selected-skill-missing")
    elif source_skills != locked_skills:
        reasons.add("selected-skill-manifest-changed")
    if any(
        candidate_by_id[skill_id]["status"] not in {"approved", "active"}
        for skill_id in selected_skill_ids & set(candidate_by_id)
    ):
        reasons.add("selected-skill-unavailable")
    if _selected_provider_contract_errors(
        candidate_manifest_set, selected_skill_ids, provider_contract_refs
    ):
        reasons.add("selected-skill-provider-incompatible")
    if packet_digest != lock["packet_sha256"]:
        reasons.add("compiled-packet-changed")
    if packet_skills != locked_skills:
        reasons.add("compiled-rule-set-changed")

    candidate_lock_sha256: str | None = None
    try:
        candidate_lock = create_skill_compatibility_lock(
            candidate_manifest_set,
            candidate_packet,
            task_id=lock["task_id"],
            operation_id=lock["operation_id"],
            provider_contract_refs=provider_contract_refs,
        )
        candidate_lock_sha256 = skill_compatibility_lock_digest(candidate_lock)
    except SkillCompatibilityError:
        if packet_digest != lock["packet_sha256"]:
            raise SkillCompatibilityError(
                "changed candidate packet is not bound to its manifest set"
            )

    if reasons & _REJECTING_REASON_CODES:
        classification = "rejected"
    elif reasons:
        classification = "migration_required"
    elif manifest_digest == lock["manifest_set_sha256"]:
        classification = "unchanged"
    else:
        classification = "compatible"
        reasons.add(_COMPATIBLE_REASON)

    decision = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "task_id": lock["task_id"],
        "operation_id": lock["operation_id"],
        "lock_sha256": skill_compatibility_lock_digest(lock),
        "candidate_lock_sha256": candidate_lock_sha256,
        "candidate_manifest_set_sha256": manifest_digest,
        "candidate_packet_sha256": packet_digest,
        "provider_contract_refs": sorted(provider_contract_refs),
        "classification": classification,
        "reason_codes": sorted(reasons),
        "delivery_allowed_without_migration": classification
        in {"unchanged", "compatible"},
    }
    return json.loads(canonical_skill_compatibility_decision_bytes(decision))


def validate_skill_compatibility_migration(migration: dict[str, Any]) -> None:
    migration = _object(migration, _MIGRATION_FIELDS, "migration")
    if migration["schema_version"] != MIGRATION_SCHEMA_VERSION:
        raise SkillCompatibilityError("migration.schema_version is unsupported")
    _identifier(migration["migration_id"], "migration.migration_id")
    if (
        not isinstance(migration["reason"], str)
        or not migration["reason"].strip()
        or len(migration["reason"]) > 1024
    ):
        raise SkillCompatibilityError("migration.reason is invalid")
    _identifier(migration["task_id"], "migration.task_id")
    _operation(migration["operation_id"], "migration.operation_id")
    _sha256(migration["old_lock_sha256"], "migration.old_lock_sha256")
    _sha256(migration["new_lock_sha256"], "migration.new_lock_sha256")
    for field in ("approval_ref", "replay_proof_ref", "rollback_proof_ref"):
        _artifact_ref(migration[field], f"migration.{field}")
    validate_skill_compatibility_lock(migration["old_lock"])
    validate_skill_compatibility_lock(migration["new_lock"])
    if migration["old_lock_sha256"] != skill_compatibility_lock_digest(
        migration["old_lock"]
    ):
        raise SkillCompatibilityError("migration old lock digest mismatch")
    if migration["new_lock_sha256"] != skill_compatibility_lock_digest(
        migration["new_lock"]
    ):
        raise SkillCompatibilityError("migration new lock digest mismatch")
    if migration["old_lock_sha256"] == migration["new_lock_sha256"]:
        raise SkillCompatibilityError("migration must change the lock")
    for lock in (migration["old_lock"], migration["new_lock"]):
        if (lock["task_id"], lock["operation_id"]) != (
            migration["task_id"],
            migration["operation_id"],
        ):
            raise SkillCompatibilityError("migration task binding mismatch")


def canonical_skill_compatibility_migration_bytes(migration: dict[str, Any]) -> bytes:
    validate_skill_compatibility_migration(migration)
    canonical = copy.deepcopy(migration)
    canonical["old_lock"] = json.loads(
        canonical_skill_compatibility_lock_bytes(canonical["old_lock"])
    )
    canonical["new_lock"] = json.loads(
        canonical_skill_compatibility_lock_bytes(canonical["new_lock"])
    )
    return json.dumps(
        canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def create_skill_compatibility_migration(
    old_lock: dict[str, Any],
    new_lock: dict[str, Any],
    *,
    migration_id: str,
    reason: str,
    approval_ref: str,
    replay_proof_ref: str,
    rollback_proof_ref: str,
) -> dict[str, Any]:
    """Build a replayable, reversible approval for a breaking rule-set change."""
    validate_skill_compatibility_lock(old_lock)
    validate_skill_compatibility_lock(new_lock)
    migration = {
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "migration_id": migration_id,
        "reason": reason,
        "task_id": old_lock["task_id"],
        "operation_id": old_lock["operation_id"],
        "old_lock_sha256": skill_compatibility_lock_digest(old_lock),
        "new_lock_sha256": skill_compatibility_lock_digest(new_lock),
        "approval_ref": approval_ref,
        "replay_proof_ref": replay_proof_ref,
        "rollback_proof_ref": rollback_proof_ref,
        "old_lock": copy.deepcopy(old_lock),
        "new_lock": copy.deepcopy(new_lock),
    }
    return json.loads(canonical_skill_compatibility_migration_bytes(migration))


def authorize_skill_delivery(
    decision: dict[str, Any],
    *,
    migration: dict[str, Any] | None = None,
    evidence_verifier: Callable[..., bool] | None = None,
) -> bool:
    """Fail closed until a breaking change has complete migration evidence."""
    validate_skill_compatibility_decision(decision)
    if decision["delivery_allowed_without_migration"]:
        return True
    if decision["classification"] != "migration_required" or migration is None:
        return False
    try:
        validate_skill_compatibility_migration(migration)
    except SkillCompatibilityError:
        return False
    if not callable(evidence_verifier):
        return False
    evidence_fields = (
        ("approval", "approval_ref"),
        ("replay", "replay_proof_ref"),
        ("rollback", "rollback_proof_ref"),
    )
    try:
        verified = all(
            evidence_verifier(
                migration[field],
                purpose=purpose,
                migration=copy.deepcopy(migration),
            )
            is True
            for purpose, field in evidence_fields
        )
    except Exception:  # noqa: BLE001 - untrusted verifier failures fail closed
        return False
    if not verified:
        return False
    return (
        migration["task_id"] == decision["task_id"]
        and migration["operation_id"] == decision["operation_id"]
        and migration["old_lock_sha256"] == decision["lock_sha256"]
        and decision["candidate_lock_sha256"] is not None
        and migration["new_lock_sha256"] == decision["candidate_lock_sha256"]
    )


def rollback_skill_compatibility_migration(migration: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical pre-migration lock."""
    validate_skill_compatibility_migration(migration)
    return json.loads(canonical_skill_compatibility_lock_bytes(migration["old_lock"]))


def compose_provider_skills(
    adapter: Any,
    packet: dict[str, Any],
    loaded: Any,
    *,
    candidate_manifest_set: dict[str, Any],
    compatibility_lock: dict[str, Any],
    provider_contract_refs: list[str],
    compatibility_decision: dict[str, Any],
    migration: dict[str, Any] | None = None,
    evidence_verifier: Callable[..., bool] | None = None,
    **kwargs: Any,
) -> Any:
    """Gate provider composition on the active task's compatibility decision."""
    try:
        expected_decision = assess_skill_compatibility(
            compatibility_lock,
            candidate_manifest_set,
            packet,
            provider_contract_refs=provider_contract_refs,
        )
    except ValueError as exc:
        raise SkillCompatibilityError(
            "compatibility decision does not bind live Skill input"
        ) from exc
    if canonical_skill_compatibility_decision_bytes(
        expected_decision
    ) != canonical_skill_compatibility_decision_bytes(compatibility_decision):
        raise SkillCompatibilityError(
            "compatibility decision does not bind live Skill input"
        )

    binding = getattr(adapter, "compatibility_binding", None)
    if not isinstance(binding, dict) or set(binding) != {
        "provider_contract_ref",
        "provider_id",
        "adapter_surface_contract",
    }:
        raise SkillCompatibilityError(
            "provider adapter identity does not bind compatibility contract"
        )
    expected_contract_ref = binding["provider_contract_ref"]
    expected_provider_id = binding["provider_id"]
    expected_surface = binding["adapter_surface_contract"]
    if (
        not isinstance(expected_contract_ref, str)
        or _PROVIDER_CONTRACT_RE.fullmatch(expected_contract_ref) is None
        or expected_contract_ref not in provider_contract_refs
        or not isinstance(expected_provider_id, str)
        or expected_provider_id
        != expected_contract_ref.removeprefix("provider://").rsplit("/", 1)[0]
        or not isinstance(expected_surface, str)
        or _ADAPTER_SURFACE_RE.fullmatch(expected_surface) is None
        or getattr(adapter, "provider_id", None) != expected_provider_id
        or getattr(adapter, "adapter_surface_contract", None) != expected_surface
        or expected_contract_ref not in provider_contract_refs
    ):
        raise SkillCompatibilityError(
            "provider adapter identity does not bind compatibility contract"
        )

    if not authorize_skill_delivery(
        expected_decision,
        migration=migration,
        evidence_verifier=evidence_verifier,
    ):
        raise SkillCompatibilityError("provider Skill delivery is blocked")
    compose = getattr(adapter, "compose", None)
    if not callable(compose):
        raise SkillCompatibilityError("provider adapter compose surface is invalid")
    return compose(packet, loaded, **kwargs)
