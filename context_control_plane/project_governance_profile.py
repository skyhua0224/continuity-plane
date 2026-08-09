"""Strict project governance profile bundle and semantic validator."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any


SCHEMA_VERSION = "context.project-governance-profile/v1alpha1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_URI_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_APPROVAL_REF_RE = re.compile(r"^approval://[^\s]+$")
_OPAQUE_REF_RE = re.compile(r"^opaque://[^\s]+$")
_PATH_REF_RE = re.compile(r"^path://[^\s]+$")
_COMMAND_REF_RE = re.compile(r"^command://[^\s]+$")
_VERIFICATION_REF_RE = re.compile(r"^verification://[^\s]+$")
_SKILL_REF_RE = re.compile(r"^skill://[^\s]+$")
_EVIDENCE_REF_RE = re.compile(r"^evidence://[^\s]+$")
_USER_REF_RE = re.compile(r"^user://[^\s]+$")
_PROVIDER_REF_RE = re.compile(r"^provider://[^\s]+$")
_REPO_REF_RE = re.compile(r"^repo://[^\s]+$")
_OPERATION_REF_RE = re.compile(r"^operation://[^\s]+$")
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

_DOCUMENT_FIELDS = {
    "schema_version",
    "profile",
    "charters",
    "work_sources",
    "obligations",
    "adaptations",
}
_PROFILE_FIELDS = {
    "profile_id",
    "project_id",
    "revision",
    "direction_state",
    "governance_owner_mode",
    "execution_worker_mode",
    "repository_topology",
    "requested_runtime_profile",
    "task_sources",
    "governance_ref",
    "updated_at",
}
_CHARTER_FIELDS = {
    "charter_id",
    "project_id",
    "profile_id",
    "status",
    "problem_space",
    "intended_users",
    "confirmed_constraint_refs",
    "prohibited_side_effects",
    "current_evidence_refs",
    "unknowns",
    "assumptions",
    "decision_owner_ref",
    "discovery_campaign_id",
    "attempt_budget",
    "expiry",
    "return_point_work_id",
    "exit_criteria",
    "mainline_authority",
    "revision",
}
_WORK_SOURCE_FIELDS = {
    "work_source_id",
    "project_id",
    "work_id",
    "source_kind",
    "source_ref",
    "source_revision",
    "governance_parent_id",
    "dependency_ids",
    "readiness",
}
_OBLIGATION_FIELDS = {
    "obligation_id",
    "work_id",
    "mode",
    "condition_ref",
    "authority",
    "automation_class",
    "verification_profile_ref",
    "evidence_refs",
    "expires_at",
    "status",
    "revision",
}
_AUTHORITY_FIELDS = {"kind", "ref"}
_ADAPTATION_FIELDS = {
    "adaptation_id",
    "profile_id",
    "version",
    "proposal_revision",
    "content_sha256",
    "scope",
    "inputs",
    "applicability",
    "changes",
    "metrics_before",
    "metrics_after",
    "safety_veto_results",
    "replay_receipts",
    "expiry",
    "rollback_to",
    "status",
    "approval_round",
    "approval_ref",
    "approved_at",
    "activation_revision",
}
_APPLICABILITY_FIELDS = {"kind", "ref"}
_CHANGES_FIELDS = {
    "retrieval_order",
    "common_path_refs",
    "common_command_refs",
    "verification_hint_refs",
    "skill_applicability",
    "presentation_preferences",
}
_METRIC_FIELDS = {
    "bytes_read",
    "bytes_emitted",
    "repeated_read_bytes",
    "verification_failures",
}
_SAFETY_VETO_FIELDS = {"E1", "E2", "E4", "E6", "E8", "E9"}
_ADAPTATION_HASH_FIELDS = (
    "profile_id",
    "version",
    "proposal_revision",
    "scope",
    "inputs",
    "applicability",
    "changes",
)

_DIRECTION_STATES = {"discovery", "governed", "operational"}
_OWNER_MODES = {"single-owner", "multi-owner"}
_WORKER_MODES = {"single-worker", "multi-worker"}
_TOPOLOGIES = {"modular", "monolith", "mixed"}
_REQUESTED_RUNTIME_PROFILES = {
    "local-embedded",
    "forge-coordinated",
    "local-coordinator",
    "shared-strong",
}
_TASK_SOURCES = {
    "master-workstream",
    "issue-backed",
    "state-native",
    "external-pm",
}
_CHARTER_STATUSES = {"candidate", "approved", "superseded"}
_READINESS = {
    "proposed",
    "blocked",
    "ready",
    "active",
    "verifying",
    "completed",
    "rejected",
    "superseded",
}
_OBLIGATION_MODES = {"required", "conditional", "optional"}
_AUTHORITY_KINDS = {"project-governance", "task-source"}
_AUTOMATION_CLASSES = {"autonomous", "approval-gated", "manual"}
_OBLIGATION_STATUSES = {"pending", "satisfied", "waived", "expired"}
_ADAPTATION_SCOPES = {"project", "user", "provider-adapter"}
_APPLICABILITY_KINDS = {
    "project",
    "user",
    "repo",
    "path",
    "operation",
    "provider",
}
_ADAPTATION_STATUSES = {
    "candidate",
    "shadow",
    "approved",
    "active",
    "quarantined",
    "rejected",
    "superseded",
}
_PRESENTATION_PREFERENCE_KEYS = {
    "language",
    "progress_reporting",
    "response_density",
    "response_format",
    "verbosity",
}


class ProjectGovernanceProfileError(ValueError):
    """Raised when a project governance bundle violates its contract."""


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProjectGovernanceProfileError(f"{field} fields are invalid")
    return value


def _objects(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ProjectGovernanceProfileError(f"{field} must be an object list")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProjectGovernanceProfileError(f"{field} must be a non-empty string")
    return value


def _optional_string(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _string(value, field)


def _strings(value: Any, field: str, *, non_empty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(item, str) or not item.strip() for item in value)
        or len(value) != len(set(value))
        or (non_empty and not value)
    ):
        raise ProjectGovernanceProfileError(
            f"{field} must contain unique non-empty strings"
        )
    return value


def _uint(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise ProjectGovernanceProfileError(f"{field} must be a {qualifier} integer")
    return value


def _optional_uint(
    value: Any,
    field: str,
    *,
    positive: bool = False,
) -> int | None:
    if value is None:
        return None
    return _uint(value, field, positive=positive)


def _enum(value: Any, allowed: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ProjectGovernanceProfileError(f"{field} is unsupported")
    return value


def _timestamp(value: Any, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    text = _string(value, field)
    if not _RFC3339_RE.fullmatch(text):
        raise ProjectGovernanceProfileError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectGovernanceProfileError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ProjectGovernanceProfileError(f"{field} must include a timezone")
    return parsed


def _semver(value: Any, field: str) -> str:
    text = _string(value, field)
    if not _SEMVER_RE.fullmatch(text):
        raise ProjectGovernanceProfileError(f"{field} must be semver")
    return text


def _semver_precedence(value: str) -> tuple[tuple[int, int, int], tuple[Any, ...]]:
    """Return the SemVer precedence components (build metadata is ignored)."""
    without_build = value.split("+", 1)[0]
    core, separator, prerelease = without_build.partition("-")
    core_parts = tuple(int(part) for part in core.split("."))
    if not separator:
        return core_parts, ()
    identifiers: list[Any] = []
    for identifier in prerelease.split("."):
        if identifier.isdigit():
            identifiers.append((0, int(identifier)))
        else:
            identifiers.append((1, identifier))
    return core_parts, tuple(identifiers)


def _semver_is_earlier(candidate: str, current: str) -> bool:
    candidate_core, candidate_pre = _semver_precedence(candidate)
    current_core, current_pre = _semver_precedence(current)
    if candidate_core != current_core:
        return candidate_core < current_core
    if not candidate_pre and current_pre:
        return False
    if candidate_pre and not current_pre:
        return True
    for candidate_identifier, current_identifier in zip(candidate_pre, current_pre):
        if candidate_identifier == current_identifier:
            continue
        if candidate_identifier[0] != current_identifier[0]:
            return candidate_identifier[0] < current_identifier[0]
        return candidate_identifier[1] < current_identifier[1]
    return len(candidate_pre) < len(current_pre)


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ProjectGovernanceProfileError(f"{field} must be lowercase SHA-256")
    return value


def _index(
    items: list[dict[str, Any]], id_field: str, field: str
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        identity = _string(item[id_field], f"{field}.{id_field}")
        if identity in result:
            raise ProjectGovernanceProfileError(f"{field} IDs must be unique")
        result[identity] = item
    return result


def _validate_work_dag(work_by_id: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(work_id: str) -> None:
        if work_id in visiting:
            raise ProjectGovernanceProfileError("work source dependency cycle")
        if work_id in visited:
            return
        visiting.add(work_id)
        for dependency_id in work_by_id[work_id]["dependency_ids"]:
            if dependency_id == work_id:
                raise ProjectGovernanceProfileError("work source dependency cannot be self")
            if dependency_id not in work_by_id:
                raise ProjectGovernanceProfileError("work source dependency is missing")
            visit(dependency_id)
        visiting.remove(work_id)
        visited.add(work_id)

    for work_id in work_by_id:
        visit(work_id)


def _canonical_adaptation_payload(adaptation: dict[str, Any]) -> dict[str, Any]:
    payload = {
        field: copy.deepcopy(adaptation[field])
        for field in _ADAPTATION_HASH_FIELDS
    }
    payload["inputs"] = sorted(payload["inputs"])
    payload["applicability"] = sorted(
        payload["applicability"],
        key=lambda item: (item["kind"], item["ref"]),
    )
    changes = payload["changes"]
    for field in (
        "common_path_refs",
        "common_command_refs",
        "verification_hint_refs",
        "skill_applicability",
    ):
        changes[field] = sorted(changes[field])
    return payload


def _adaptation_hash(adaptation: dict[str, Any]) -> str:
    payload = _canonical_adaptation_payload(adaptation)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _canonical_document(document: dict[str, Any]) -> dict[str, Any]:
    canonical = copy.deepcopy(document)
    canonical["profile"]["task_sources"] = sorted(
        canonical["profile"]["task_sources"]
    )
    canonical["charters"] = sorted(
        canonical["charters"], key=lambda item: item["charter_id"]
    )
    canonical["work_sources"] = sorted(
        canonical["work_sources"], key=lambda item: item["work_source_id"]
    )
    canonical["obligations"] = sorted(
        canonical["obligations"], key=lambda item: item["obligation_id"]
    )
    canonical["adaptations"] = sorted(
        canonical["adaptations"], key=lambda item: item["adaptation_id"]
    )
    for charter in canonical["charters"]:
        for field in (
            "intended_users",
            "confirmed_constraint_refs",
            "prohibited_side_effects",
            "current_evidence_refs",
            "unknowns",
            "assumptions",
            "exit_criteria",
        ):
            charter[field] = sorted(charter[field])
    for source in canonical["work_sources"]:
        source["dependency_ids"] = sorted(source["dependency_ids"])
    for obligation in canonical["obligations"]:
        obligation["evidence_refs"] = sorted(obligation["evidence_refs"])
    for adaptation in canonical["adaptations"]:
        adaptation["inputs"] = sorted(adaptation["inputs"])
        adaptation["applicability"] = sorted(
            adaptation["applicability"],
            key=lambda item: (item["kind"], item["ref"]),
        )
        adaptation["replay_receipts"] = sorted(adaptation["replay_receipts"])
        for field in (
            "common_path_refs",
            "common_command_refs",
            "verification_hint_refs",
            "skill_applicability",
        ):
            adaptation["changes"][field] = sorted(
                adaptation["changes"][field]
            )
    return canonical


def validate_project_governance_profile(
    document: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> None:
    """Validate a provider-neutral project governance bundle."""
    root = _object(document, _DOCUMENT_FIELDS, "document")
    if root["schema_version"] != SCHEMA_VERSION:
        raise ProjectGovernanceProfileError("unsupported schema_version")

    profile = _object(root["profile"], _PROFILE_FIELDS, "profile")
    profile_id = _string(profile["profile_id"], "profile.profile_id")
    project_id = _string(profile["project_id"], "profile.project_id")
    _uint(profile["revision"], "profile.revision")
    direction_state = _enum(
        profile["direction_state"], _DIRECTION_STATES, "profile.direction_state"
    )
    _enum(
        profile["governance_owner_mode"],
        _OWNER_MODES,
        "profile.governance_owner_mode",
    )
    _enum(
        profile["execution_worker_mode"],
        _WORKER_MODES,
        "profile.execution_worker_mode",
    )
    _enum(
        profile["repository_topology"],
        _TOPOLOGIES,
        "profile.repository_topology",
    )
    _enum(
        profile["requested_runtime_profile"],
        _REQUESTED_RUNTIME_PROFILES,
        "profile.requested_runtime_profile",
    )
    task_sources = _strings(
        profile["task_sources"], "profile.task_sources", non_empty=True
    )
    for source in task_sources:
        _enum(source, _TASK_SOURCES, "profile.task_sources")
    _string(profile["governance_ref"], "profile.governance_ref")
    profile_updated_at = _timestamp(profile["updated_at"], "profile.updated_at")
    if observed_at is None:
        raise ProjectGovernanceProfileError(
            "observed_at is required for liveness validation"
        )
    observed = _timestamp(observed_at, "observed_at")
    if profile_updated_at > observed:
        raise ProjectGovernanceProfileError(
            "profile.updated_at is in the future of observed time"
        )

    charters = _objects(root["charters"], "charters")
    if not charters:
        raise ProjectGovernanceProfileError("charters must not be empty")
    for charter in charters:
        _object(charter, _CHARTER_FIELDS, "charter")
    _index(charters, "charter_id", "charter")
    for charter in charters:
        if charter["project_id"] != project_id or charter["profile_id"] != profile_id:
            raise ProjectGovernanceProfileError("charter project/profile reference is invalid")
        status = _enum(charter["status"], _CHARTER_STATUSES, "charter.status")
        _string(charter["problem_space"], "charter.problem_space")
        _strings(charter["intended_users"], "charter.intended_users", non_empty=True)
        _strings(charter["confirmed_constraint_refs"], "charter.confirmed_constraint_refs")
        _strings(charter["prohibited_side_effects"], "charter.prohibited_side_effects")
        _strings(charter["current_evidence_refs"], "charter.current_evidence_refs")
        _strings(charter["unknowns"], "charter.unknowns")
        _strings(charter["assumptions"], "charter.assumptions")
        _string(charter["decision_owner_ref"], "charter.decision_owner_ref")
        _string(charter["discovery_campaign_id"], "charter.discovery_campaign_id")
        _uint(charter["attempt_budget"], "charter.attempt_budget", positive=True)
        expiry = _timestamp(charter["expiry"], "charter.expiry", optional=True)
        _string(charter["return_point_work_id"], "charter.return_point_work_id")
        _strings(charter["exit_criteria"], "charter.exit_criteria", non_empty=True)
        if type(charter["mainline_authority"]) is not bool:
            raise ProjectGovernanceProfileError(
                "charter.mainline_authority must be boolean"
            )
        _uint(charter["revision"], "charter.revision")
        if direction_state == "discovery":
            if charter["mainline_authority"]:
                raise ProjectGovernanceProfileError(
                    "discovery charter cannot have mainline authority"
                )
            if expiry is None:
                raise ProjectGovernanceProfileError("discovery charter requires expiry")
        elif status != "superseded" and status != "approved":
            raise ProjectGovernanceProfileError(
                "governed or operational charter must be approved"
            )
        if status != "superseded" and expiry is not None and expiry <= observed:
            raise ProjectGovernanceProfileError("charter expiry has expired")
    current_mainline_charters = [
        charter
        for charter in charters
        if charter["status"] != "superseded" and charter["mainline_authority"]
    ]
    if direction_state != "discovery" and len(current_mainline_charters) != 1:
        raise ProjectGovernanceProfileError(
            "governed or operational profile requires one current mainline charter"
        )

    work_sources = _objects(root["work_sources"], "work_sources")
    for source in work_sources:
        _object(source, _WORK_SOURCE_FIELDS, "work_source")
    _index(work_sources, "work_source_id", "work_source")
    work_by_id = _index(work_sources, "work_id", "work_source")
    for source in work_sources:
        if source["project_id"] != project_id:
            raise ProjectGovernanceProfileError("work source project reference is invalid")
        source_kind = _enum(
            source["source_kind"], _TASK_SOURCES, "work_source.source_kind"
        )
        if source_kind not in task_sources:
            raise ProjectGovernanceProfileError(
                "work source kind is not enabled by profile.task_sources"
            )
        source_ref = _string(source["source_ref"], "work_source.source_ref")
        if not _OPAQUE_REF_RE.fullmatch(source_ref):
            raise ProjectGovernanceProfileError(
                "work_source.source_ref must be an opaque ref"
            )
        _string(source["source_revision"], "work_source.source_revision")
        _string(source["governance_parent_id"], "work_source.governance_parent_id")
        _strings(source["dependency_ids"], "work_source.dependency_ids")
        _enum(source["readiness"], _READINESS, "work_source.readiness")
    _validate_work_dag(work_by_id)
    for charter in charters:
        if charter["return_point_work_id"] not in work_by_id:
            raise ProjectGovernanceProfileError(
                "charter return point work is missing"
            )

    obligations = _objects(root["obligations"], "obligations")
    for obligation in obligations:
        _object(obligation, _OBLIGATION_FIELDS, "obligation")
    _index(obligations, "obligation_id", "obligation")
    obligation_work_ids: set[str] = set()
    for obligation in obligations:
        work_id = _string(obligation["work_id"], "obligation.work_id")
        if work_id not in work_by_id:
            raise ProjectGovernanceProfileError("obligation work reference is missing")
        if work_id in obligation_work_ids:
            raise ProjectGovernanceProfileError("work can have only one obligation")
        obligation_work_ids.add(work_id)
        mode = _enum(obligation["mode"], _OBLIGATION_MODES, "obligation.mode")
        condition_ref = _optional_string(
            obligation["condition_ref"], "obligation.condition_ref"
        )
        if (mode == "conditional") != (condition_ref is not None):
            raise ProjectGovernanceProfileError(
                "conditional obligation requires condition_ref and other modes forbid it"
            )
        authority = _object(
            obligation["authority"], _AUTHORITY_FIELDS, "obligation.authority"
        )
        _enum(authority["kind"], _AUTHORITY_KINDS, "obligation.authority.kind")
        authority_ref = _string(authority["ref"], "obligation.authority.ref")
        if (
            authority["kind"] == "project-governance"
            and authority_ref != profile["governance_ref"]
        ):
            raise ProjectGovernanceProfileError(
                "obligation governance authority must match profile"
            )
        if (
            authority["kind"] == "task-source"
            and authority_ref != work_by_id[work_id]["source_ref"]
        ):
            raise ProjectGovernanceProfileError(
                "obligation task-source authority must match work source"
            )
        automation_class = _enum(
            obligation["automation_class"],
            _AUTOMATION_CLASSES,
            "obligation.automation_class",
        )
        if authority["kind"] == "task-source" and (
            mode == "required" or automation_class == "autonomous"
        ):
            raise ProjectGovernanceProfileError(
                "task-source authority cannot create required or autonomous obligations"
            )
        _string(
            obligation["verification_profile_ref"],
            "obligation.verification_profile_ref",
        )
        evidence_refs = _strings(
            obligation["evidence_refs"], "obligation.evidence_refs"
        )
        if any(
            not (_EVIDENCE_REF_RE.fullmatch(ref) or _APPROVAL_REF_RE.fullmatch(ref))
            for ref in evidence_refs
        ):
            raise ProjectGovernanceProfileError(
                "obligation evidence must use an evidence or approval ref"
            )
        expires_at = _timestamp(
            obligation["expires_at"], "obligation.expires_at", optional=True
        )
        obligation_status = _enum(
            obligation["status"], _OBLIGATION_STATUSES, "obligation.status"
        )
        if (
            obligation_status == "pending"
            and expires_at is not None
            and expires_at <= observed
        ):
            raise ProjectGovernanceProfileError(
                "pending obligation expiry has expired"
            )
        if obligation_status == "satisfied" and not any(
            _EVIDENCE_REF_RE.fullmatch(ref) for ref in evidence_refs
        ):
            raise ProjectGovernanceProfileError(
                "satisfied obligation requires evidence"
            )
        if obligation_status == "waived" and not any(
            _APPROVAL_REF_RE.fullmatch(ref) for ref in evidence_refs
        ):
            raise ProjectGovernanceProfileError(
                "waived obligation requires approval evidence"
            )
        if obligation_status == "expired" and (
            expires_at is None or expires_at > observed
        ):
            raise ProjectGovernanceProfileError(
                "expired obligation requires an elapsed expiry"
            )
        _uint(obligation["revision"], "obligation.revision")
    if obligation_work_ids != set(work_by_id):
        raise ProjectGovernanceProfileError(
            "every work source requires exactly one obligation"
        )

    adaptations = _objects(root["adaptations"], "adaptations")
    for adaptation in adaptations:
        _object(adaptation, _ADAPTATION_FIELDS, "adaptation")
    _index(adaptations, "adaptation_id", "adaptation")
    versions: set[str] = set()
    active_keys: set[str] = set()
    adaptation_lanes: dict[str, str] = {}
    adaptation_by_version: dict[str, dict[str, Any]] = {}
    rollback_links: list[tuple[str, str]] = []
    rollback_targets: list[str] = []
    for adaptation in adaptations:
        if adaptation["profile_id"] != profile_id:
            raise ProjectGovernanceProfileError("adaptation profile reference is invalid")
        version = _semver(adaptation["version"], "adaptation.version")
        if version in versions:
            raise ProjectGovernanceProfileError("adaptation versions must be unique")
        versions.add(version)
        adaptation_by_version[version] = adaptation
        proposal_revision = _uint(
            adaptation["proposal_revision"],
            "adaptation.proposal_revision",
            positive=True,
        )
        if proposal_revision > profile["revision"]:
            raise ProjectGovernanceProfileError(
                "adaptation proposal revision exceeds profile revision"
            )
        _sha256(adaptation["content_sha256"], "adaptation.content_sha256")
        scope = _enum(
            adaptation["scope"], _ADAPTATION_SCOPES, "adaptation.scope"
        )
        _strings(adaptation["inputs"], "adaptation.inputs", non_empty=True)

        applicability = _objects(adaptation["applicability"], "adaptation.applicability")
        applicability_keys: list[tuple[str, str]] = []
        for item in applicability:
            _object(item, _APPLICABILITY_FIELDS, "adaptation.applicability")
            kind = _enum(
                item["kind"], _APPLICABILITY_KINDS, "adaptation.applicability.kind"
            )
            ref = _string(item["ref"], "adaptation.applicability.ref")
            typed_ref_patterns = {
                "user": _USER_REF_RE,
                "provider": _PROVIDER_REF_RE,
                "repo": _REPO_REF_RE,
                "path": _PATH_REF_RE,
                "operation": _OPERATION_REF_RE,
            }
            if kind in typed_ref_patterns and not typed_ref_patterns[kind].fullmatch(ref):
                raise ProjectGovernanceProfileError(
                    f"adaptation {kind} applicability must use a typed ref"
                )
            applicability_keys.append(
                (kind, ref)
            )
        if not applicability_keys or len(applicability_keys) != len(
            set(applicability_keys)
        ):
            raise ProjectGovernanceProfileError(
                "adaptation applicability must be unique and non-empty"
            )
        project_refs = [
            ref for kind, ref in applicability_keys if kind == "project"
        ]
        if project_refs != [project_id]:
            raise ProjectGovernanceProfileError(
                "adaptation applicability project must match profile project"
            )
        if scope == "user":
            user_refs = [ref for kind, ref in applicability_keys if kind == "user"]
            if len(user_refs) != 1 or not _USER_REF_RE.fullmatch(user_refs[0]):
                raise ProjectGovernanceProfileError(
                    "user adaptation requires one typed user applicability ref"
                )
        elif any(kind == "user" for kind, _ in applicability_keys):
            raise ProjectGovernanceProfileError(
                "user applicability requires user adaptation scope"
            )
        if scope == "provider-adapter":
            provider_refs = [
                ref for kind, ref in applicability_keys if kind == "provider"
            ]
            if len(provider_refs) != 1 or not _PROVIDER_REF_RE.fullmatch(
                provider_refs[0]
            ):
                raise ProjectGovernanceProfileError(
                    "provider adaptation requires one typed provider applicability ref"
                )
        elif any(kind == "provider" for kind, _ in applicability_keys):
            raise ProjectGovernanceProfileError(
                "provider applicability requires provider-adapter scope"
            )
        adaptation_lanes[version] = json.dumps(
            [scope, sorted(applicability_keys)],
            ensure_ascii=False,
            separators=(",", ":"),
        )

        changes = _object(adaptation["changes"], _CHANGES_FIELDS, "adaptation.changes")
        _strings(changes["retrieval_order"], "adaptation.changes.retrieval_order")
        _strings(changes["common_path_refs"], "adaptation.changes.common_path_refs")
        if any(not _PATH_REF_RE.fullmatch(ref) for ref in changes["common_path_refs"]):
            raise ProjectGovernanceProfileError(
                "adaptation common path must use a path ref"
            )
        _strings(
            changes["common_command_refs"], "adaptation.changes.common_command_refs"
        )
        if any(
            not _COMMAND_REF_RE.fullmatch(ref)
            for ref in changes["common_command_refs"]
        ):
            raise ProjectGovernanceProfileError(
                "adaptation common command must use a command ref"
            )
        _strings(
            changes["verification_hint_refs"],
            "adaptation.changes.verification_hint_refs",
        )
        if any(
            not _VERIFICATION_REF_RE.fullmatch(ref)
            for ref in changes["verification_hint_refs"]
        ):
            raise ProjectGovernanceProfileError(
                "adaptation verification hint must use a verification ref"
            )
        _strings(
            changes["skill_applicability"],
            "adaptation.changes.skill_applicability",
        )
        if any(
            not _SKILL_REF_RE.fullmatch(ref)
            for ref in changes["skill_applicability"]
        ):
            raise ProjectGovernanceProfileError(
                "adaptation skill applicability must use a skill ref"
            )
        preferences = changes["presentation_preferences"]
        if not isinstance(preferences, dict) or any(
            not isinstance(key, str)
            or not key.strip()
            or not isinstance(value, str)
            or not value.strip()
            for key, value in preferences.items()
        ):
            raise ProjectGovernanceProfileError(
                "adaptation presentation preferences are invalid"
            )
        if any(key not in _PRESENTATION_PREFERENCE_KEYS for key in preferences):
            raise ProjectGovernanceProfileError(
                "adaptation presentation preference is protected or unsupported"
            )

        for metric_name in ("metrics_before", "metrics_after"):
            metrics = _object(
                adaptation[metric_name], _METRIC_FIELDS, f"adaptation.{metric_name}"
            )
            for name, value in metrics.items():
                _uint(value, f"adaptation.{metric_name}.{name}")
        vetoes = _object(
            adaptation["safety_veto_results"],
            _SAFETY_VETO_FIELDS,
            "adaptation.safety_veto_results",
        )
        if any(type(value) is not bool for value in vetoes.values()):
            raise ProjectGovernanceProfileError(
                "adaptation safety veto values must be boolean"
            )
        replay_receipts = _strings(
            adaptation["replay_receipts"], "adaptation.replay_receipts"
        )
        if any(not _ARTIFACT_URI_RE.fullmatch(ref) for ref in replay_receipts):
            raise ProjectGovernanceProfileError(
                "adaptation replay receipt must be an artifact ref"
            )
        expiry = _timestamp(adaptation["expiry"], "adaptation.expiry", optional=True)
        rollback_to = _optional_string(
            adaptation["rollback_to"], "adaptation.rollback_to"
        )
        if rollback_to is not None:
            _semver(rollback_to, "adaptation.rollback_to")
            if rollback_to == version:
                raise ProjectGovernanceProfileError(
                    "adaptation rollback cannot target its own version"
                )
            rollback_targets.append(rollback_to)
            rollback_links.append((version, rollback_to))
        status = _enum(
            adaptation["status"], _ADAPTATION_STATUSES, "adaptation.status"
        )
        approval_round = _uint(
            adaptation["approval_round"], "adaptation.approval_round"
        )
        approval_ref = _optional_string(
            adaptation["approval_ref"], "adaptation.approval_ref"
        )
        if approval_ref is not None and not _APPROVAL_REF_RE.fullmatch(approval_ref):
            raise ProjectGovernanceProfileError(
                "adaptation approval ref is invalid"
            )
        approved_at = _timestamp(
            adaptation["approved_at"], "adaptation.approved_at", optional=True
        )
        activation_revision = _optional_uint(
            adaptation["activation_revision"],
            "adaptation.activation_revision",
            positive=True,
        )

        if adaptation["content_sha256"] != _adaptation_hash(adaptation):
            raise ProjectGovernanceProfileError(
                "adaptation content hash does not match immutable payload"
            )
        if status in {"shadow", "approved", "active"} and not replay_receipts:
            raise ProjectGovernanceProfileError(
                "shadow or promoted adaptation requires replay receipts"
            )
        has_approval = (
            approval_round > 0 and approval_ref is not None and approved_at is not None
        )
        has_partial_approval = (
            approval_round > 0 or approval_ref is not None or approved_at is not None
        )
        if has_partial_approval and not has_approval:
            raise ProjectGovernanceProfileError(
                "adaptation approval provenance must be complete"
            )
        if has_approval:
            if approved_at > observed:
                raise ProjectGovernanceProfileError(
                    "adaptation approval timestamp is in the future of observed time"
                )
            if expiry is not None and approved_at >= expiry:
                raise ProjectGovernanceProfileError(
                    "adaptation approval timestamp must precede expiry"
                )
        if status in {"approved", "active"}:
            if not has_approval:
                raise ProjectGovernanceProfileError(
                    "approved adaptation requires approval provenance"
                )
            if not all(vetoes.values()):
                raise ProjectGovernanceProfileError(
                    "approved adaptation requires all safety vetoes to pass"
                )
        elif status in {"candidate", "shadow"} and has_partial_approval:
            raise ProjectGovernanceProfileError(
                "unapproved adaptation cannot carry approval provenance"
            )
        if (
            status in {"candidate", "shadow", "approved", "active"}
            and expiry is not None
            and expiry <= observed
        ):
            raise ProjectGovernanceProfileError("adaptation expiry has expired")
        if status == "active":
            if activation_revision is None:
                raise ProjectGovernanceProfileError(
                    "active adaptation requires activation revision"
                )
            active_key = json.dumps(
                [adaptation["scope"], sorted(applicability_keys)],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if active_key in active_keys:
                raise ProjectGovernanceProfileError(
                    "only one adaptation can be active for an applicability scope"
                )
            active_keys.add(active_key)
        elif status in {"candidate", "shadow", "approved"} and activation_revision is not None:
            raise ProjectGovernanceProfileError(
                "inactive adaptation cannot carry activation revision"
            )
        elif activation_revision is not None and not has_approval:
            raise ProjectGovernanceProfileError(
                "historical activation requires approval provenance"
            )
        if activation_revision is not None:
            if activation_revision <= proposal_revision:
                raise ProjectGovernanceProfileError(
                    "activation revision must follow proposal revision"
                )
            if activation_revision > profile["revision"]:
                raise ProjectGovernanceProfileError(
                    "activation revision exceeds current profile revision"
                )
        if has_approval and not replay_receipts:
            raise ProjectGovernanceProfileError(
                "approved adaptation history requires replay receipts"
            )

    unknown_rollback_targets = set(rollback_targets) - versions
    if unknown_rollback_targets:
        raise ProjectGovernanceProfileError(
            "adaptation rollback target version is missing"
        )
    for source_version, target_version in rollback_links:
        target = adaptation_by_version[target_version]
        if not _semver_is_earlier(target_version, source_version):
            raise ProjectGovernanceProfileError(
                "adaptation rollback target must be an earlier version"
            )
        target_has_activation_history = (
            target["status"] in {"active", "superseded"}
            and target["activation_revision"] is not None
            and target["approval_round"] > 0
            and target["approval_ref"] is not None
            and target["approved_at"] is not None
            and bool(target["replay_receipts"])
        )
        if not target_has_activation_history:
            raise ProjectGovernanceProfileError(
                "adaptation rollback target must preserve activated historical approval"
            )
        if adaptation_lanes[source_version] != adaptation_lanes[target_version]:
            raise ProjectGovernanceProfileError(
                "adaptation rollback must stay within the same scope and applicability lane"
            )
    rollback_graph = {source: target for source, target in rollback_links}
    for source in rollback_graph:
        seen: set[str] = set()
        current = source
        while current in rollback_graph:
            if current in seen:
                raise ProjectGovernanceProfileError("adaptation rollback cycle")
            seen.add(current)
            current = rollback_graph[current]


def canonical_project_governance_bytes(
    document: dict[str, Any], *, observed_at: str | None = None
) -> bytes:
    """Return deterministic JSON bytes for a valid governance bundle."""
    validate_project_governance_profile(document, observed_at=observed_at)
    return json.dumps(
        _canonical_document(document),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def round_trip_project_governance_profile(
    document: dict[str, Any],
    *,
    observed_at: str | None = None,
) -> dict[str, Any]:
    """Round-trip a bundle through canonical JSON and validate it again."""
    restored = json.loads(
        canonical_project_governance_bytes(document, observed_at=observed_at).decode(
            "utf-8"
        )
    )
    validate_project_governance_profile(restored, observed_at=observed_at)
    return restored
