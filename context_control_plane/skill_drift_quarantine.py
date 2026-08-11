"""Fail-closed Skill asset drift assessment."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .compiled_skill_packet import (
    CompiledSkillPacketError,
    canonical_compiled_skill_packet_bytes,
    validate_compiled_skill_packet,
)
from .skill_manifest_set import canonical_skill_manifest_set_bytes
from .skill_manifest_set import validate_skill_manifest_set


SCHEMA_VERSION = "context.skill-drift-assessment/v1alpha1"
VALIDATOR_VERSION = "context.skill-drift-validator/v1alpha1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_CODE_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_RECEIPT_FIELDS = {
    "schema_version",
    "validator_version",
    "observed_at",
    "packet_sha256",
    "manifest_set_sha256",
    "gate",
    "findings",
    "skills",
}
_FINDING_FIELDS = {"scope", "code", "detail"}
_SKILL_FIELDS = {
    "skill_id",
    "status",
    "reason_codes",
    "observed_content_sha256",
}


class SkillDriftInputError(ValueError):
    """Raised when an assessment input cannot be evaluated safely."""


def _timestamp(value: str) -> datetime:
    if (
        not isinstance(value, str)
        or not 20 <= len(value) <= 64
        or not _RFC3339_RE.fullmatch(value)
    ):
        raise ValueError("timestamp must be RFC3339 with an explicit timezone")
    if (
        int(value[11:13]) > 23
        or int(value[14:16]) > 59
        or int(value[17:19]) > 59
    ):
        raise ValueError("timestamp contains an invalid time")
    if not value.endswith("Z") and (
        int(value[-5:-3]) > 23 or int(value[-2:]) > 59
    ):
        raise ValueError("timestamp contains an invalid offset")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp contains an invalid calendar date") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def _canonical_manifest_set_bytes_for_digest(
    manifest_set: dict[str, Any],
    *,
    observed_at: str,
) -> bytes:
    try:
        return canonical_skill_manifest_set_bytes(
            manifest_set,
            observed_at=observed_at,
        )
    except (KeyError, TypeError, ValueError):
        try:
            return canonical_skill_manifest_set_bytes(
                manifest_set,
                observed_at="1970-01-01T00:00:00+00:00",
            )
        except (KeyError, TypeError, ValueError):
            pass
        # Keep a stable digest for an invalid or expired set so the receipt
        # can identify the exact input that was denied.
        canonical = copy.deepcopy(manifest_set)
        for manifest in canonical.get("manifests", []):
            if isinstance(manifest, dict):
                for field in (
                    "applicability",
                    "rule_ids",
                    "dependencies",
                    "conflicts",
                    "provenance_refs",
                ):
                    value = manifest.get(field)
                    if isinstance(value, list):
                        manifest[field] = sorted(
                            value,
                            key=lambda item: json.dumps(
                                item,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        )
                compatibility = manifest.get("compatibility")
                if isinstance(compatibility, dict):
                    for field in ("schema_refs", "provider_contract_refs"):
                        value = compatibility.get(field)
                        if isinstance(value, list):
                            compatibility[field] = sorted(value)
        manifests = canonical.get("manifests")
        if isinstance(manifests, list):
            canonical["manifests"] = sorted(
                manifests,
                key=lambda item: (
                    item.get("skill_id", "") if isinstance(item, dict) else "",
                    item.get("version", "") if isinstance(item, dict) else "",
                ),
            )
        return json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")


def validate_skill_drift_assessment(assessment: dict[str, Any]) -> None:
    """Validate the deterministic, provider-neutral drift receipt."""
    if not isinstance(assessment, dict) or set(assessment) != _RECEIPT_FIELDS:
        raise ValueError("assessment fields are invalid")
    if assessment["schema_version"] != SCHEMA_VERSION:
        raise ValueError("assessment.schema_version is unsupported")
    if assessment["validator_version"] != VALIDATOR_VERSION:
        raise ValueError("assessment.validator_version is unsupported")
    _timestamp(assessment["observed_at"])
    for field in ("packet_sha256",):
        if not isinstance(assessment[field], str) or not _SHA256_RE.fullmatch(
            assessment[field]
        ):
            raise ValueError(f"assessment.{field} is invalid")
    if assessment["manifest_set_sha256"] is not None and (
        not isinstance(assessment["manifest_set_sha256"], str)
        or not _SHA256_RE.fullmatch(assessment["manifest_set_sha256"])
    ):
        raise ValueError("assessment.manifest_set_sha256 is invalid")
    if assessment["gate"] not in {"allow", "quarantine"}:
        raise ValueError("assessment.gate is invalid")

    findings = assessment["findings"]
    if not isinstance(findings, list):
        raise ValueError("assessment.findings must be a list")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != _FINDING_FIELDS:
            raise ValueError("assessment finding fields are invalid")
        if not all(
            isinstance(finding[field], str) and finding[field]
            for field in _FINDING_FIELDS
        ):
            raise ValueError("assessment finding values are invalid")
        for field in ("scope", "code"):
            if len(finding[field]) > 128 or not _CODE_RE.fullmatch(finding[field]):
                raise ValueError("assessment finding code is invalid")
        if len(finding["detail"]) > 1024 or any(
            terminator in finding["detail"] for terminator in ("\r", "\n")
        ):
            raise ValueError("assessment finding detail is invalid")
    if len(
        {(finding["scope"], finding["code"], finding["detail"]) for finding in findings}
    ) != len(findings):
        raise ValueError("assessment findings must be unique")

    skills = assessment["skills"]
    if not isinstance(skills, list) or not skills:
        raise ValueError("assessment.skills must be a non-empty list")
    skill_ids: set[str] = set()
    for skill in skills:
        if not isinstance(skill, dict) or set(skill) != _SKILL_FIELDS:
            raise ValueError("assessment skill fields are invalid")
        skill_id = skill["skill_id"]
        if (
            not isinstance(skill_id, str)
            or len(skill_id) > 256
            or not _ID_RE.fullmatch(skill_id)
            or skill_id in skill_ids
        ):
            raise ValueError("assessment skill IDs must be unique")
        skill_ids.add(skill_id)
        if skill["status"] not in {"verified", "quarantined"}:
            raise ValueError("assessment skill status is invalid")
        reasons = skill["reason_codes"]
        if not isinstance(reasons, list) or any(
            not isinstance(reason, str) or not reason for reason in reasons
        ):
            raise ValueError("assessment reason_codes are invalid")
        if len(reasons) != len(set(reasons)):
            raise ValueError("assessment reason_codes must be unique")
        if any(len(reason) > 128 or not _CODE_RE.fullmatch(reason) for reason in reasons):
            raise ValueError("assessment reason code is invalid")
        if skill["status"] == "verified" and reasons:
            raise ValueError("verified Skill cannot have reason codes")
        if skill["status"] == "quarantined" and not reasons:
            raise ValueError("quarantined Skill requires reason codes")
        observed_digest = skill["observed_content_sha256"]
        if observed_digest is not None and (
            not isinstance(observed_digest, str)
            or not _SHA256_RE.fullmatch(observed_digest)
        ):
            raise ValueError("assessment observed content digest is invalid")
        if skill["status"] == "verified" and observed_digest is None:
            raise ValueError("verified Skill requires an observed content digest")
    expected_gate = (
        "quarantine"
        if findings or any(skill["status"] == "quarantined" for skill in skills)
        else "allow"
    )
    if assessment["gate"] != expected_gate:
        raise ValueError("assessment gate does not match its findings")
    if assessment["gate"] == "allow" and assessment["manifest_set_sha256"] is None:
        raise ValueError("allow assessment requires a manifest set digest")


def canonical_skill_drift_assessment_bytes(assessment: dict[str, Any]) -> bytes:
    """Return canonical JSON bytes for a validated drift receipt."""
    validate_skill_drift_assessment(assessment)
    canonical = copy.deepcopy(assessment)
    canonical["findings"] = sorted(
        canonical["findings"],
        key=lambda item: (item["scope"], item["code"], item["detail"]),
    )
    canonical["skills"] = sorted(
        canonical["skills"],
        key=lambda item: item["skill_id"],
    )
    for skill in canonical["skills"]:
        skill["reason_codes"] = sorted(skill["reason_codes"])
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def skill_drift_assessment_digest(assessment: dict[str, Any]) -> str:
    """Return the SHA-256 digest of canonical receipt bytes."""
    return hashlib.sha256(canonical_skill_drift_assessment_bytes(assessment)).hexdigest()


def assess_skill_drift(
    packet: dict[str, Any],
    manifest_set: dict[str, Any],
    *,
    skill_paths: object | None = None,
    asset_resolver: Callable[[str], bytes | None] | None = None,
    observed_at: str,
) -> dict[str, Any]:
    """Assess whether every selected Skill asset is safe to activate."""
    if skill_paths is not None:
        raise SkillDriftInputError(
            "direct paths are outside the core contract; use an asset_resolver"
        )
    try:
        validate_compiled_skill_packet(packet)
        packet_bytes = canonical_compiled_skill_packet_bytes(packet)
    except (CompiledSkillPacketError, KeyError, TypeError, ValueError) as exc:
        raise SkillDriftInputError("compiled packet failed strict validation") from exc
    packet_snapshot = json.loads(packet_bytes)
    if not isinstance(manifest_set, dict):
        raise SkillDriftInputError("manifest set failed strict validation")
    manifest_snapshot = copy.deepcopy(manifest_set)
    try:
        observed = _timestamp(observed_at)
    except ValueError as exc:
        raise SkillDriftInputError("observed_at failed strict RFC3339 validation") from exc
    manifests = manifest_snapshot.get("manifests")
    if not isinstance(manifests, list) or any(
        not isinstance(manifest, dict) for manifest in manifests
    ):
        raise SkillDriftInputError("manifest set failed strict validation")
    manifests_by_id = {
        manifest["skill_id"]: manifest
        for manifest in manifests
        if isinstance(manifest.get("skill_id"), str)
    }
    findings = []
    try:
        current_manifest_set_sha256 = hashlib.sha256(
            _canonical_manifest_set_bytes_for_digest(
                manifest_snapshot,
                observed_at=observed_at,
            )
        ).hexdigest()
        canonical_validation_error = None
        try:
            # Validate structure and graph independently from selected-manifest
            # liveness. Expiry is assessed below as a receipt finding.
            validate_skill_manifest_set(
                manifest_snapshot,
            )
        except (KeyError, TypeError, ValueError) as exc:
            canonical_validation_error = exc
    except (KeyError, TypeError, ValueError) as exc:
        current_manifest_set_sha256 = None
        canonical_validation_error = exc
    if canonical_validation_error is not None:
        findings.append(
            {
                "scope": "manifest-set",
                "code": "manifest-contract-invalid",
                "detail": "current manifest set failed strict validation",
            }
        )
    if current_manifest_set_sha256 != packet_snapshot["manifest_set_sha256"]:
        findings.append(
            {
                "scope": "manifest-set",
                "code": "manifest-set-digest-mismatch",
                "detail": "current manifest set differs from the locked packet",
            }
        )
    skills = []
    for selection in packet_snapshot["selections"]:
        skill_id = selection["skill_id"]
        reason_codes = []
        observed_content_sha256 = None
        manifest = manifests_by_id.get(skill_id)
        if manifest is None or manifest.get("version") != selection["version"]:
            reason_codes.append("manifest-version-mismatch")
        if (
            manifest is None
            or manifest.get("content_sha256") != selection["content_sha256"]
        ):
            reason_codes.append("manifest-digest-mismatch")
        if manifest is not None and manifest.get("status") not in {"approved", "active"}:
            reason_codes.append("source-status-unselectable")
        if manifest is not None and manifest.get("expires_at") is not None:
            try:
                expires_at = _timestamp(manifest["expires_at"])
            except (TypeError, ValueError):
                reason_codes.append("manifest-expiry-invalid")
            else:
                if expires_at <= observed:
                    reason_codes.append("manifest-expired")
        if asset_resolver is not None:
            try:
                asset = asset_resolver(skill_id)
            except Exception:  # Resolver failures must not grant activation.
                asset = None
                reason_codes.append("asset-unreadable-or-unsafe")
            if asset is None:
                if "asset-unreadable-or-unsafe" not in reason_codes:
                    reason_codes.append("missing-path")
            elif not isinstance(asset, bytes):
                reason_codes.append("asset-observation-invalid")
            else:
                observed_content_sha256 = hashlib.sha256(asset).hexdigest()
                if observed_content_sha256 != selection["content_sha256"]:
                    reason_codes.append("content-digest-mismatch")
        else:
            reason_codes.append("missing-path")
        skills.append(
            {
                "skill_id": skill_id,
                "status": "verified" if not reason_codes else "quarantined",
                "reason_codes": reason_codes,
                "observed_content_sha256": observed_content_sha256,
            }
        )
    assessment = {
        "schema_version": SCHEMA_VERSION,
        "validator_version": VALIDATOR_VERSION,
        "observed_at": observed_at,
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "manifest_set_sha256": current_manifest_set_sha256,
        "gate": "allow"
        if not findings and all(skill["status"] == "verified" for skill in skills)
        else "quarantine",
        "findings": findings,
        "skills": skills,
    }
    validate_skill_drift_assessment(assessment)
    return assessment
