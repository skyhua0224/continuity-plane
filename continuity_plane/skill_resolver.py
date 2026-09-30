"""Deterministic role and operation aware Skill resolution."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import compiled_skill_packet, skill_catalog, skill_manifest_set

REQUEST_SCHEMA_VERSION = "context.skill-resolution-request/v1alpha1"
DECISION_SCHEMA_VERSION = "context.skill-resolution-decision/v1alpha1"
RESOLVER_VERSION = "context.skill-resolver/v1alpha1"

_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "catalog_sha256",
    "project_ref",
    "repo_ref",
    "path_refs",
    "task_ref",
    "role_ref",
    "operation_ref",
    "provider_contract_ref",
    "required_schema_refs",
    "required_skill_ids",
    "binding_provenance_ref",
    "observed_at",
    "state_write_authority",
}
_DECISION_FIELDS = {
    "schema_version",
    "resolver_version",
    "request_id",
    "request_sha256",
    "catalog_sha256",
    "observed_at",
    "disposition",
    "reason_code",
    "selected_skills",
    "suppressed_skills",
    "findings",
    "selected_rule_ids",
    "manifest_set_sha256",
    "packet_sha256",
    "candidate_count",
    "selected_count",
    "state_write_authority",
}
_SELECTED_FIELDS = {
    "skill_id",
    "version",
    "content_sha256",
    "manifest_sha256",
    "rule_ids",
    "selection_reason",
    "priority_score",
}
_SUPPRESSED_FIELDS = {"skill_id", "reason_code", "conflicting_with"}
_FINDING_FIELDS = {"skill_id", "code"}

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_RE = re.compile(r"^project://[a-z0-9][a-z0-9._/-]*$")
_REPO_RE = re.compile(r"^repo://[a-z0-9][a-z0-9._/-]*$")
_PATH_RE = re.compile(r"^path://[A-Za-z0-9][A-Za-z0-9._/-]*$")
_TASK_RE = re.compile(r"^task://[a-z0-9][a-z0-9._-]*$")
_ROLE_RE = re.compile(r"^role://(?:executor|reviewer|thinker|verifier)$")
_OPERATION_RE = re.compile(r"^operation://[a-z0-9][a-z0-9._-]*$")
_PROVIDER_CONTRACT_RE = re.compile(
    r"^provider://[a-z0-9][a-z0-9._-]*/v[a-z0-9._-]+$"
)
_SCHEMA_RE = re.compile(r"^context\.[a-z0-9._/-]+/v[a-z0-9._-]+$")
_ARTIFACT_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

_PRIORITY_WEIGHT = {
    "project": 800,
    "repo": 700,
    "path": 600,
    "task": 500,
    "operation": 400,
    "role": 200,
    "provider": 100,
}


class SkillResolverError(ValueError):
    """Raised when a Skill resolution input or result violates its contract."""


@dataclass(frozen=True)
class SkillResolutionOutcome:
    """Immutable resolution decision and its optional compiled artifacts."""

    _decision_bytes: bytes
    _manifest_set_bytes: bytes | None
    _packet_bytes: bytes | None

    @property
    def decision(self) -> dict[str, Any]:
        return json.loads(self._decision_bytes)

    @property
    def manifest_set(self) -> dict[str, Any] | None:
        return (
            None
            if self._manifest_set_bytes is None
            else json.loads(self._manifest_set_bytes)
        )

    @property
    def packet(self) -> dict[str, Any] | None:
        return None if self._packet_bytes is None else json.loads(self._packet_bytes)

    @property
    def manifest_set_skill_ids(self) -> tuple[str, ...]:
        manifest_set = self.manifest_set
        if manifest_set is None:
            return ()
        return tuple(item["skill_id"] for item in manifest_set["manifests"])


def _text(value: Any, field: str, pattern: re.Pattern[str] | None = None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SkillResolverError(f"{field} must be a non-empty string")
    if pattern is not None and not pattern.fullmatch(value):
        raise SkillResolverError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    _text(value, field, _RFC3339_RE)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SkillResolverError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SkillResolverError(f"{field} requires a timezone")
    return parsed


def _unique_strings(
    value: Any,
    field: str,
    *,
    pattern: re.Pattern[str],
    required: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise SkillResolverError(f"{field} must be a unique string list")
    result = [_text(item, field, pattern) for item in value]
    if len(result) != len(set(result)):
        raise SkillResolverError(f"{field} must be unique")
    return result


def validate_skill_resolution_request(request: dict[str, Any]) -> None:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise SkillResolverError("Skill resolution request fields are invalid")
    if request["schema_version"] != REQUEST_SCHEMA_VERSION:
        raise SkillResolverError("Skill resolution request schema_version is unsupported")
    _text(request["request_id"], "request_id", _ID_RE)
    _text(request["catalog_sha256"], "catalog_sha256", _SHA256_RE)
    _text(request["project_ref"], "project_ref", _PROJECT_RE)
    _text(request["repo_ref"], "repo_ref", _REPO_RE)
    _unique_strings(request["path_refs"], "path_refs", pattern=_PATH_RE)
    _text(request["task_ref"], "task_ref", _TASK_RE)
    _text(request["role_ref"], "role_ref", _ROLE_RE)
    _text(request["operation_ref"], "operation_ref", _OPERATION_RE)
    _text(
        request["provider_contract_ref"],
        "provider_contract_ref",
        _PROVIDER_CONTRACT_RE,
    )
    _unique_strings(
        request["required_schema_refs"],
        "required_schema_refs",
        pattern=_SCHEMA_RE,
        required=True,
    )
    required_skills = _unique_strings(
        request["required_skill_ids"],
        "required_skill_ids",
        pattern=_ID_RE,
    )
    binding_ref = request["binding_provenance_ref"]
    if binding_ref is not None:
        _text(binding_ref, "binding_provenance_ref", _ARTIFACT_RE)
    if bool(required_skills) != (binding_ref is not None):
        raise SkillResolverError(
            "required Skill bindings require exactly one provenance reference"
        )
    _timestamp(request["observed_at"], "observed_at")
    if request["state_write_authority"] is not False:
        raise SkillResolverError("Skill resolution request cannot claim authority")


def canonical_skill_resolution_request_bytes(request: dict[str, Any]) -> bytes:
    validate_skill_resolution_request(request)
    canonical = copy.deepcopy(request)
    for field in ("path_refs", "required_schema_refs", "required_skill_ids"):
        canonical[field] = sorted(canonical[field])
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_skill_resolution_decision(decision: dict[str, Any]) -> None:
    if not isinstance(decision, dict) or set(decision) != _DECISION_FIELDS:
        raise SkillResolverError("Skill resolution decision fields are invalid")
    if decision["schema_version"] != DECISION_SCHEMA_VERSION:
        raise SkillResolverError("Skill resolution decision schema_version is unsupported")
    if decision["resolver_version"] != RESOLVER_VERSION:
        raise SkillResolverError("Skill resolver_version is unsupported")
    _text(decision["request_id"], "request_id", _ID_RE)
    for field in ("request_sha256", "catalog_sha256"):
        _text(decision[field], field, _SHA256_RE)
    _timestamp(decision["observed_at"], "observed_at")
    if decision["disposition"] not in {"resolved", "quarantined"}:
        raise SkillResolverError("Skill resolution disposition is invalid")
    _text(decision["reason_code"], "reason_code", _ID_RE)
    if decision["state_write_authority"] is not False:
        raise SkillResolverError("Skill resolution decision cannot claim authority")

    selected = decision["selected_skills"]
    if not isinstance(selected, list):
        raise SkillResolverError("selected_skills must be a list")
    selected_ids: list[str] = []
    selected_rules: list[str] = []
    for item in selected:
        if not isinstance(item, dict) or set(item) != _SELECTED_FIELDS:
            raise SkillResolverError("selected Skill fields are invalid")
        selected_ids.append(_text(item["skill_id"], "skill_id", _ID_RE))
        _text(item["version"], "version")
        _text(item["content_sha256"], "content_sha256", _SHA256_RE)
        _text(item["manifest_sha256"], "manifest_sha256", _SHA256_RE)
        selected_rules.extend(
            _unique_strings(item["rule_ids"], "rule_ids", pattern=_ID_RE, required=True)
        )
        if item["selection_reason"] not in {"required", "applicability", "dependency"}:
            raise SkillResolverError("selection_reason is invalid")
        if (
            isinstance(item["priority_score"], bool)
            or not isinstance(item["priority_score"], int)
            or item["priority_score"] < 0
        ):
            raise SkillResolverError("priority_score is invalid")
    if len(selected_ids) != len(set(selected_ids)):
        raise SkillResolverError("selected Skill IDs must be unique")
    if len(selected_rules) != len(set(selected_rules)):
        raise SkillResolverError("selected rule IDs must be unique")

    suppressed = decision["suppressed_skills"]
    if not isinstance(suppressed, list):
        raise SkillResolverError("suppressed_skills must be a list")
    suppressed_ids: list[str] = []
    for item in suppressed:
        if not isinstance(item, dict) or set(item) != _SUPPRESSED_FIELDS:
            raise SkillResolverError("suppressed Skill fields are invalid")
        suppressed_ids.append(_text(item["skill_id"], "skill_id", _ID_RE))
        _text(item["reason_code"], "reason_code", _ID_RE)
        _text(item["conflicting_with"], "conflicting_with", _ID_RE)
    if len(suppressed_ids) != len(set(suppressed_ids)):
        raise SkillResolverError("suppressed Skill IDs must be unique")

    findings = decision["findings"]
    if not isinstance(findings, list):
        raise SkillResolverError("findings must be a list")
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != _FINDING_FIELDS:
            raise SkillResolverError("finding fields are invalid")
        _text(finding["skill_id"], "finding.skill_id", _ID_RE)
        _text(finding["code"], "finding.code", _ID_RE)

    rules = _unique_strings(
        decision["selected_rule_ids"], "selected_rule_ids", pattern=_ID_RE
    )
    if sorted(rules) != sorted(selected_rules):
        raise SkillResolverError("selected_rule_ids do not match selected Skills")
    for field in ("candidate_count", "selected_count"):
        value = decision[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise SkillResolverError(f"{field} is invalid")
    if decision["selected_count"] != len(selected):
        raise SkillResolverError("selected_count does not match selected Skills")
    if decision["candidate_count"] < decision["selected_count"]:
        raise SkillResolverError("candidate_count cannot be smaller than selected_count")

    if decision["disposition"] == "resolved":
        if not selected or findings:
            raise SkillResolverError("resolved decision must select Skills without findings")
        for field in ("manifest_set_sha256", "packet_sha256"):
            _text(decision[field], field, _SHA256_RE)
    else:
        if selected or rules or decision["selected_count"] != 0 or not findings:
            raise SkillResolverError("quarantined decision must contain only findings")
        if decision["manifest_set_sha256"] is not None or decision["packet_sha256"] is not None:
            raise SkillResolverError("quarantined decision cannot bind compiled artifacts")


def canonical_skill_resolution_decision_bytes(decision: dict[str, Any]) -> bytes:
    validate_skill_resolution_decision(decision)
    canonical = copy.deepcopy(decision)
    canonical["selected_skills"] = sorted(
        canonical["selected_skills"], key=lambda item: item["skill_id"]
    )
    for item in canonical["selected_skills"]:
        item["rule_ids"] = sorted(item["rule_ids"])
    canonical["suppressed_skills"] = sorted(
        canonical["suppressed_skills"], key=lambda item: item["skill_id"]
    )
    canonical["findings"] = sorted(
        canonical["findings"], key=lambda item: (item["code"], item["skill_id"])
    )
    canonical["selected_rule_ids"] = sorted(canonical["selected_rule_ids"])
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _catalog_bytes(catalog: dict[str, Any]) -> bytes:
    try:
        revision = catalog["catalog_revision"]
        return skill_catalog.canonical_skill_catalog_bytes(
            catalog,
            observed_at=revision,
        )
    except (KeyError, TypeError, skill_catalog.SkillCatalogError) as exc:
        raise SkillResolverError("Skill catalog failed admission validation") from exc


def _provider_ref(provider_contract_ref: str) -> str:
    return provider_contract_ref.rsplit("/", 1)[0]


def _path_prefix_match(manifest_ref: str, request_ref: str) -> bool:
    manifest_path = manifest_ref.removeprefix("path://")
    request_path = request_ref.removeprefix("path://")
    return request_path == manifest_path or request_path.startswith(manifest_path + "/")


def _applicability_match(manifest: dict[str, Any], request: dict[str, Any]) -> bool:
    signals = {
        "project": {request["project_ref"]},
        "repo": {request["repo_ref"]},
        "path": set(request["path_refs"]),
        "task": {request["task_ref"]},
        "role": {request["role_ref"]},
        "operation": {request["operation_ref"]},
        "provider": {_provider_ref(request["provider_contract_ref"])},
    }
    grouped: dict[str, set[str]] = {}
    for item in manifest["applicability"]:
        grouped.setdefault(item["kind"], set()).add(item["ref"])
    for kind, refs in grouped.items():
        if kind == "path":
            if not any(
                _path_prefix_match(manifest_ref, request_ref)
                for manifest_ref in refs
                for request_ref in signals[kind]
            ):
                return False
        elif not refs & signals[kind]:
            return False
    return True


def _compatible(manifest: dict[str, Any], request: dict[str, Any]) -> str | None:
    contracts = manifest["compatibility"]["provider_contract_refs"]
    if contracts and request["provider_contract_ref"] not in contracts:
        return "provider-contract-incompatible"
    required_schemas = set(request["required_schema_refs"])
    if not required_schemas.issubset(manifest["compatibility"]["schema_refs"]):
        return "schema-incompatible"
    return None


def _expired(manifest: dict[str, Any], observed_at: datetime) -> bool:
    expires_at = manifest["expires_at"]
    return expires_at is not None and _timestamp(expires_at, "expires_at") <= observed_at


def _priority(manifest: dict[str, Any], required: bool) -> int:
    kinds = {item["kind"] for item in manifest["applicability"]}
    return (10_000 if required else 0) + sum(_PRIORITY_WEIGHT[kind] for kind in kinds)


def _conflicts(left: dict[str, Any], right: dict[str, Any]) -> bool:
    for source, target in ((left, right), (right, left)):
        for conflict in source["conflicts"]:
            if conflict["skill_id"] == target["skill_id"] and skill_manifest_set._version_in_range(
                target["version"], conflict["version_range"]
            ):
                return True
    return False


def _manifest_digest(manifest: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _quarantined(
    request: dict[str, Any],
    request_sha256: str,
    *,
    reason_code: str,
    findings: list[dict[str, str]],
    candidate_count: int,
    suppressed: list[dict[str, str]] | None = None,
) -> SkillResolutionOutcome:
    decision = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "catalog_sha256": request["catalog_sha256"],
        "observed_at": request["observed_at"],
        "disposition": "quarantined",
        "reason_code": reason_code,
        "selected_skills": [],
        "suppressed_skills": suppressed or [],
        "findings": findings,
        "selected_rule_ids": [],
        "manifest_set_sha256": None,
        "packet_sha256": None,
        "candidate_count": candidate_count,
        "selected_count": 0,
        "state_write_authority": False,
    }
    return SkillResolutionOutcome(
        canonical_skill_resolution_decision_bytes(decision), None, None
    )


def resolve_skills(
    catalog: dict[str, Any], request: dict[str, Any]
) -> SkillResolutionOutcome:
    """Resolve the smallest deterministic dependency-closed Skill set."""
    request_bytes = canonical_skill_resolution_request_bytes(request)
    request = json.loads(request_bytes)
    request_sha256 = hashlib.sha256(request_bytes).hexdigest()
    catalog_bytes = _catalog_bytes(catalog)
    catalog_sha256 = hashlib.sha256(catalog_bytes).hexdigest()
    if request["catalog_sha256"] != catalog_sha256:
        raise SkillResolverError("request catalog_sha256 does not match current catalog")
    observed_at = _timestamp(request["observed_at"], "observed_at")
    catalog_revision = _timestamp(catalog["catalog_revision"], "catalog_revision")
    if catalog_revision > observed_at:
        raise SkillResolverError("catalog revision cannot be newer than resolution time")

    canonical_catalog = json.loads(catalog_bytes)
    entries = [
        entry
        for entry in canonical_catalog["entries"]
        if entry["status"] in {"approved", "active"}
    ]
    by_id = {entry["manifest"]["skill_id"]: entry["manifest"] for entry in entries}
    required_ids = set(request["required_skill_ids"])
    missing_required = sorted(required_ids - set(by_id))
    if missing_required:
        return _quarantined(
            request,
            request_sha256,
            reason_code="required-skill-unavailable",
            findings=[
                {"skill_id": skill_id, "code": "required-skill-unavailable"}
                for skill_id in missing_required
            ],
            candidate_count=0,
        )

    direct: list[dict[str, Any]] = []
    invalid_applicable: list[dict[str, str]] = []
    required_invalid: list[dict[str, str]] = []
    for skill_id, manifest in sorted(by_id.items()):
        matches = _applicability_match(manifest, request)
        compatibility_error = _compatible(manifest, request)
        is_expired = _expired(manifest, observed_at)
        if skill_id in required_ids and not matches:
            required_invalid.append(
                {"skill_id": skill_id, "code": "required-skill-inapplicable"}
            )
            continue
        if not matches:
            continue
        if is_expired:
            invalid_applicable.append(
                {"skill_id": skill_id, "code": "expired-applicable-skill"}
            )
            continue
        if compatibility_error is not None:
            invalid_applicable.append(
                {"skill_id": skill_id, "code": compatibility_error}
            )
            continue
        direct.append(
            {
                "manifest": manifest,
                "required": skill_id in required_ids,
                "priority": _priority(manifest, skill_id in required_ids),
            }
        )
    if required_invalid:
        return _quarantined(
            request,
            request_sha256,
            reason_code="required-skill-inapplicable",
            findings=required_invalid,
            candidate_count=len(direct),
        )
    invalid_required = [
        finding for finding in invalid_applicable if finding["skill_id"] in required_ids
    ]
    if invalid_required:
        return _quarantined(
            request,
            request_sha256,
            reason_code=invalid_required[0]["code"],
            findings=invalid_required,
            candidate_count=len(direct),
        )
    if invalid_applicable:
        return _quarantined(
            request,
            request_sha256,
            reason_code=invalid_applicable[0]["code"],
            findings=invalid_applicable,
            candidate_count=len(direct),
        )
    if not direct:
        return _quarantined(
            request,
            request_sha256,
            reason_code="no-applicable-skill",
            findings=[{"skill_id": "resolver", "code": "no-applicable-skill"}],
            candidate_count=0,
        )

    roots: list[dict[str, Any]] = []
    suppressed: list[dict[str, str]] = []
    for candidate in sorted(
        direct,
        key=lambda item: (-item["priority"], item["manifest"]["skill_id"]),
    ):
        conflicting = next(
            (
                selected
                for selected in roots
                if _conflicts(candidate["manifest"], selected["manifest"])
            ),
            None,
        )
        if conflicting is None:
            roots.append(candidate)
            continue
        if candidate["priority"] == conflicting["priority"]:
            ids = sorted(
                [
                    candidate["manifest"]["skill_id"],
                    conflicting["manifest"]["skill_id"],
                ]
            )
            return _quarantined(
                request,
                request_sha256,
                reason_code="unresolved-conflict",
                findings=[
                    {"skill_id": skill_id, "code": "unresolved-conflict"}
                    for skill_id in ids
                ],
                candidate_count=len(direct),
                suppressed=suppressed,
            )
        suppressed.append(
            {
                "skill_id": candidate["manifest"]["skill_id"],
                "reason_code": "lower-priority-conflict",
                "conflicting_with": conflicting["manifest"]["skill_id"],
            }
        )

    closure: set[str] = {item["manifest"]["skill_id"] for item in roots}
    pending = list(closure)
    while pending:
        skill_id = pending.pop()
        for dependency in by_id[skill_id]["dependencies"]:
            dependency_id = dependency["skill_id"]
            manifest = by_id.get(dependency_id)
            if manifest is None:
                raise SkillResolverError("catalog dependency is unavailable")
            failure = None
            if not _applicability_match(manifest, request):
                failure = "dependency-inapplicable"
            if failure is None:
                failure = _compatible(manifest, request)
            if _expired(manifest, observed_at):
                failure = "expired-dependency"
            if failure is not None:
                return _quarantined(
                    request,
                    request_sha256,
                    reason_code=failure,
                    findings=[{"skill_id": dependency_id, "code": failure}],
                    candidate_count=len(direct),
                    suppressed=suppressed,
                )
            if dependency_id not in closure:
                closure.add(dependency_id)
                pending.append(dependency_id)

    closure_manifests = [by_id[skill_id] for skill_id in sorted(closure)]
    for index, left in enumerate(closure_manifests):
        for right in closure_manifests[index + 1 :]:
            if _conflicts(left, right):
                return _quarantined(
                    request,
                    request_sha256,
                    reason_code="dependency-conflict",
                    findings=[
                        {"skill_id": left["skill_id"], "code": "dependency-conflict"},
                        {"skill_id": right["skill_id"], "code": "dependency-conflict"},
                    ],
                    candidate_count=len(direct),
                    suppressed=suppressed,
                )

    manifest_set = {
        "schema_version": skill_manifest_set.SCHEMA_VERSION,
        "manifests": copy.deepcopy(closure_manifests),
    }
    manifest_set_bytes = skill_manifest_set.canonical_skill_manifest_set_bytes(
        manifest_set,
        observed_at=request["observed_at"],
    )
    manifest_set = json.loads(manifest_set_bytes)
    root_ids = [item["manifest"]["skill_id"] for item in roots]
    packet = compiled_skill_packet.compile_skill_packet(
        manifest_set,
        selected_skill_ids=root_ids,
        observed_at=request["observed_at"],
    )
    packet_bytes = compiled_skill_packet.canonical_compiled_skill_packet_bytes(packet)
    priorities = {
        item["manifest"]["skill_id"]: item["priority"] for item in roots
    }
    required_root_ids = {
        item["manifest"]["skill_id"] for item in roots if item["required"]
    }
    canonical_by_id = {
        manifest["skill_id"]: manifest for manifest in manifest_set["manifests"]
    }
    selected_skills = []
    for selection in packet["selections"]:
        skill_id = selection["skill_id"]
        reason = (
            "required"
            if skill_id in required_root_ids
            else "applicability"
            if skill_id in priorities
            else "dependency"
        )
        selected_skills.append(
            {
                "skill_id": skill_id,
                "version": selection["version"],
                "content_sha256": selection["content_sha256"],
                "manifest_sha256": _manifest_digest(canonical_by_id[skill_id]),
                "rule_ids": sorted(selection["rule_ids"]),
                "selection_reason": reason,
                "priority_score": priorities.get(skill_id, 0),
            }
        )
    decision = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "resolver_version": RESOLVER_VERSION,
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "catalog_sha256": request["catalog_sha256"],
        "observed_at": request["observed_at"],
        "disposition": "resolved",
        "reason_code": "matched-minimal-set",
        "selected_skills": selected_skills,
        "suppressed_skills": suppressed,
        "findings": [],
        "selected_rule_ids": sorted(
            rule_id for item in selected_skills for rule_id in item["rule_ids"]
        ),
        "manifest_set_sha256": hashlib.sha256(manifest_set_bytes).hexdigest(),
        "packet_sha256": hashlib.sha256(packet_bytes).hexdigest(),
        "candidate_count": len(direct)
        + len(closure - {item["manifest"]["skill_id"] for item in direct}),
        "selected_count": len(selected_skills),
        "state_write_authority": False,
    }
    return SkillResolutionOutcome(
        canonical_skill_resolution_decision_bytes(decision),
        manifest_set_bytes,
        packet_bytes,
    )
