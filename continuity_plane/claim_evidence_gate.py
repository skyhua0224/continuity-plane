"""Claim-to-evidence admission for completion and path assertions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .assertion_provenance import (
    AssertionProvenanceError,
    validate_assertion_provenance,
)
from .effect_scope_gate import validate_scope

SCHEMA_VERSION = "context.claim-evidence-gate/v1alpha1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_CLAIM_KINDS = {"completion", "path", "verification", "decision", "constraint"}
_DECISIONS = {"allow", "deny"}
_REASONS = {
    "evidence_satisfied",
    "missing_evidence",
    "required_authority_missing",
    "non_bearing_evidence",
    "evidence_binding_mismatch",
    "path_evidence_mismatch",
    "claim_invalid",
}
_CURRENT_AUTHORITIES = {
    "current_code",
    "current_state",
    "industry_standard",
    "os_official",
    "software_official",
}

# Each tuple is an OR group. A claim is admitted only when every group has a
# matching authority. Completion and verification therefore require a live
# repository or state observation; documentation alone cannot close work.
_REQUIRED_AUTHORITIES: dict[str, tuple[frozenset[str], ...]] = {
    "completion": (frozenset({"current_code", "current_state"}),),
    "path": (frozenset({"current_code"}),),
    "verification": (frozenset({"current_code", "current_state"}),),
    "decision": (frozenset({"current_code", "current_state"}),),
    "constraint": (frozenset({"current_state"}),),
}

_CLAIM_FIELDS = {
    "claim_id",
    "work_id",
    "claim_kind",
    "statement",
    "evidence_assertion_ids",
    "scope_refs",
}
_VERDICT_FIELDS = {
    "schema_version",
    "gate_id",
    "claim_id",
    "claim_kind",
    "claim_sha256",
    "decision",
    "reason",
    "required_authority_groups",
    "matched_authority_kinds",
    "evidence_ids",
    "evaluated_at",
    "state_write_authority",
    "completion_authority",
    "verdict_sha256",
}


class ClaimEvidenceError(ValueError):
    """Raised when a claim or gate verdict violates the wire contract."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(value: dict[str, Any], field: str) -> str:
    candidate = value.get(field)
    if not isinstance(candidate, str) or _SHA256_RE.fullmatch(candidate) is None:
        raise ClaimEvidenceError(f"{field} is invalid")
    return candidate


def _text(value: Any, field: str, maximum: int = 16_384) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or "\x00" in value
    ):
        raise ClaimEvidenceError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ClaimEvidenceError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClaimEvidenceError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ClaimEvidenceError(f"{field} requires a timezone")
    return parsed


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ClaimEvidenceError(f"{field} is invalid")
    return value


def _validate_claim(claim: Any) -> None:
    if not isinstance(claim, dict) or set(claim) != _CLAIM_FIELDS:
        raise ClaimEvidenceError("claim fields are invalid")
    _id(claim["claim_id"], "claim_id")
    _id(claim["work_id"], "work_id")
    if claim["claim_kind"] not in _CLAIM_KINDS:
        raise ClaimEvidenceError("claim_kind is invalid")
    _text(claim["statement"], "statement")
    assertion_ids = claim["evidence_assertion_ids"]
    if (
        not isinstance(assertion_ids, list)
        or len(assertion_ids) > 256
        or len(set(assertion_ids)) != len(assertion_ids)
        or any(_ID_RE.fullmatch(item or "") is None for item in assertion_ids)
        or assertion_ids != sorted(assertion_ids)
    ):
        raise ClaimEvidenceError("evidence_assertion_ids is invalid")
    scopes = claim["scope_refs"]
    if not isinstance(scopes, list) or len(scopes) > 256:
        raise ClaimEvidenceError("scope_refs is invalid")
    for scope in scopes:
        try:
            validate_scope(scope)
        except (TypeError, ValueError) as exc:
            raise ClaimEvidenceError("scope_ref is invalid") from exc
    if claim["claim_kind"] == "path" and not any(
        scope["scope_kind"] in {"repo", "directory", "file", "symbol"}
        for scope in scopes
    ):
        raise ClaimEvidenceError("path claim requires a path scope")


def _path_scope_identity(scope: dict[str, str]) -> tuple[str, str]:
    path_ref = scope["scope_ref"].split("#", 1)[0]
    repository_and_path = path_ref.removeprefix("repo://")
    repository, separator, relative_path = repository_and_path.partition("/")
    if scope["scope_kind"] == "repo":
        relative_path = ""
    elif not separator:
        raise ClaimEvidenceError("path scope is invalid")
    return repository, relative_path


def _validate_resolved_claim_scopes(claim: dict[str, Any], root: str | Path | None) -> None:
    path_scopes = [
        scope
        for scope in claim["scope_refs"]
        if scope["scope_kind"] in {"repo", "directory", "file", "symbol"}
    ]
    if not path_scopes:
        return
    if root is None:
        raise ClaimEvidenceError("path scope requires a repository root")
    resolved_root = Path(root).resolve()
    for scope in path_scopes:
        repository, relative_path = _path_scope_identity(scope)
        if repository != resolved_root.name:
            raise ClaimEvidenceError("path scope repository mismatch")
        target = (resolved_root / relative_path).resolve()
        if target != resolved_root and resolved_root not in target.parents:
            raise ClaimEvidenceError("path scope is outside repository")
        kind = scope["scope_kind"]
        if kind == "repo" and target != resolved_root:
            raise ClaimEvidenceError("repository scope is invalid")
        if kind == "directory" and not target.is_dir():
            raise ClaimEvidenceError("directory scope does not resolve")
        if kind in {"file", "symbol"} and not target.is_file():
            raise ClaimEvidenceError("file scope does not resolve")


def _path_evidence_matches(claim: dict[str, Any], evidence_records: list[dict[str, Any]]) -> bool:
    current_code_refs = {
        item["source_ref"].split("#", 1)[0]
        for record in evidence_records
        if record["bearing"]
        for item in record["evidence"]
        if item["authority_kind"] == "current_code"
    }
    for scope in claim["scope_refs"]:
        if scope["scope_kind"] not in {"repo", "directory", "file", "symbol"}:
            continue
        scope_ref = scope["scope_ref"].split("#", 1)[0]
        if scope["scope_kind"] in {"repo", "directory"}:
            if not any(
                ref == scope_ref or ref.startswith(scope_ref.rstrip("/") + "/")
                for ref in current_code_refs
            ):
                return False
        elif scope_ref not in current_code_refs:
            return False
    return True


def _verdict_digest(verdict: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(verdict)
    unsigned.pop("verdict_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def validate_claim_evidence_verdict(verdict: Any) -> None:
    """Validate a gate receipt without granting completion or State authority."""
    if not isinstance(verdict, dict) or set(verdict) != _VERDICT_FIELDS:
        raise ClaimEvidenceError("gate verdict fields are invalid")
    if verdict["schema_version"] != SCHEMA_VERSION:
        raise ClaimEvidenceError("gate verdict schema_version is invalid")
    _id(verdict["gate_id"], "gate_id")
    _id(verdict["claim_id"], "claim_id")
    if verdict["claim_kind"] not in _CLAIM_KINDS:
        raise ClaimEvidenceError("gate verdict claim_kind is invalid")
    _digest(verdict, "claim_sha256")
    if verdict["decision"] not in _DECISIONS:
        raise ClaimEvidenceError("gate verdict decision is invalid")
    if verdict["reason"] not in _REASONS:
        raise ClaimEvidenceError("gate verdict reason is invalid")
    groups = verdict["required_authority_groups"]
    if not isinstance(groups, list) or not groups or any(
        not isinstance(group, list)
        or not group
        or any(authority not in _CURRENT_AUTHORITIES for authority in group)
        for group in groups
    ):
        raise ClaimEvidenceError("required_authority_groups is invalid")
    expected_groups = [
        sorted(group) for group in _REQUIRED_AUTHORITIES[verdict["claim_kind"]]
    ]
    if groups != expected_groups:
        raise ClaimEvidenceError("required_authority_groups do not match claim policy")
    matched = verdict["matched_authority_kinds"]
    if not isinstance(matched, list) or len(set(matched)) != len(matched) or any(
        authority not in _CURRENT_AUTHORITIES for authority in matched
    ):
        raise ClaimEvidenceError("matched_authority_kinds is invalid")
    if matched != sorted(matched):
        raise ClaimEvidenceError("matched_authority_kinds must be canonical")
    evidence_ids = verdict["evidence_ids"]
    if not isinstance(evidence_ids, list) or len(set(evidence_ids)) != len(evidence_ids) or any(
        _ID_RE.fullmatch(item or "") is None for item in evidence_ids
    ):
        raise ClaimEvidenceError("evidence_ids is invalid")
    if evidence_ids != sorted(evidence_ids):
        raise ClaimEvidenceError("evidence_ids must be canonical")
    requirements_satisfied = all(set(group) & set(matched) for group in groups)
    if verdict["decision"] == "allow" and (
        verdict["reason"] != "evidence_satisfied"
        or not evidence_ids
        or not requirements_satisfied
    ):
        raise ClaimEvidenceError("allow verdict lacks required evidence")
    if verdict["decision"] == "deny" and verdict["reason"] == "evidence_satisfied":
        raise ClaimEvidenceError("deny verdict cannot report evidence_satisfied")
    if verdict["reason"] == "missing_evidence" and (matched or evidence_ids):
        raise ClaimEvidenceError("missing_evidence verdict contains evidence")
    if verdict["reason"] == "non_bearing_evidence" and evidence_ids:
        raise ClaimEvidenceError("non_bearing_evidence verdict contains bearing evidence")
    _timestamp(verdict["evaluated_at"], "evaluated_at")
    if verdict["state_write_authority"] is not False:
        raise ClaimEvidenceError("gate verdicts cannot write State")
    if verdict["completion_authority"] is not False:
        raise ClaimEvidenceError("gate verdicts cannot authorize completion")
    _digest(verdict, "verdict_sha256")
    if verdict["verdict_sha256"] != _verdict_digest(verdict):
        raise ClaimEvidenceError("gate verdict digest mismatch")


def _build_verdict(
    *, claim: dict[str, Any], decision: str, reason: str, matched: set[str], evidence_ids: list[str], evaluated_at: str
) -> dict[str, Any]:
    groups = [sorted(group) for group in _REQUIRED_AUTHORITIES[claim["claim_kind"]]]
    verdict = {
        "schema_version": SCHEMA_VERSION,
        "gate_id": f"gate/{claim['claim_id']}",
        "claim_id": claim["claim_id"],
        "claim_kind": claim["claim_kind"],
        "claim_sha256": hashlib.sha256(_canonical(claim)).hexdigest(),
        "decision": decision,
        "reason": reason,
        "required_authority_groups": groups,
        "matched_authority_kinds": sorted(matched),
        "evidence_ids": sorted(evidence_ids),
        "evaluated_at": evaluated_at,
        "state_write_authority": False,
        "completion_authority": False,
        "verdict_sha256": "",
    }
    verdict["verdict_sha256"] = _verdict_digest(verdict)
    validate_claim_evidence_verdict(verdict)
    return verdict


def evaluate_claim_evidence_gate(
    claim: dict[str, Any],
    *,
    evidence_records: list[dict[str, Any]],
    current_time: str,
    root: str | Path | None = None,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None = None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None]
    | None = None,
) -> dict[str, Any]:
    """Evaluate claim evidence; only State MCP may apply an allowed result."""
    _validate_claim(claim)
    evaluated_at = _timestamp(current_time, "current_time")
    _validate_resolved_claim_scopes(claim, root)
    if not isinstance(evidence_records, list) or len(evidence_records) > 256:
        raise ClaimEvidenceError("evidence_records is invalid")
    evidence_ids: list[str] = []
    record_ids: list[str] = []
    authorities: set[str] = set()
    for record in evidence_records:
        try:
            validate_assertion_provenance(
                record,
                current_time=current_time,
                root=root,
                evidence_resolver=evidence_resolver,
                artifact_resolver=artifact_resolver,
            )
        except (AssertionProvenanceError, TypeError, KeyError) as exc:
            raise ClaimEvidenceError("evidence provenance is invalid") from exc
        if _timestamp(record["asserted_at"], "asserted_at") > evaluated_at:
            raise ClaimEvidenceError("assertion postdates gate evaluation")
        record_ids.append(record["assertion_id"])
        if not record["bearing"]:
            continue
        evidence_ids.append(record["assertion_id"])
        authorities.update(item["authority_kind"] for item in record["evidence"])
    if len(set(record_ids)) != len(record_ids):
        raise ClaimEvidenceError("evidence assertion IDs must be unique")
    if not evidence_records:
        return _build_verdict(
            claim=claim,
            decision="deny",
            reason="missing_evidence",
            matched=set(),
            evidence_ids=[],
            evaluated_at=current_time,
        )
    if sorted(record_ids) != claim["evidence_assertion_ids"]:
        return _build_verdict(
            claim=claim,
            decision="deny",
            reason="evidence_binding_mismatch",
            matched=authorities,
            evidence_ids=evidence_ids,
            evaluated_at=current_time,
        )
    if not evidence_ids:
        return _build_verdict(
            claim=claim,
            decision="deny",
            reason="non_bearing_evidence",
            matched=authorities,
            evidence_ids=[],
            evaluated_at=current_time,
        )
    requirements = _REQUIRED_AUTHORITIES[claim["claim_kind"]]
    if not all(group & authorities for group in requirements):
        return _build_verdict(
            claim=claim,
            decision="deny",
            reason="required_authority_missing",
            matched=authorities,
            evidence_ids=evidence_ids,
            evaluated_at=current_time,
        )
    if claim["claim_kind"] == "path" and not _path_evidence_matches(
        claim, evidence_records
    ):
        return _build_verdict(
            claim=claim,
            decision="deny",
            reason="path_evidence_mismatch",
            matched=authorities,
            evidence_ids=evidence_ids,
            evaluated_at=current_time,
        )
    return _build_verdict(
        claim=claim,
        decision="allow",
        reason="evidence_satisfied",
        matched=authorities,
        evidence_ids=evidence_ids,
        evaluated_at=current_time,
    )


def canonical_claim_evidence_verdict_bytes(verdict: dict[str, Any]) -> bytes:
    validate_claim_evidence_verdict(verdict)
    return _canonical(verdict)


def validate_claim_evidence_claim(claim: Any) -> None:
    """Validate a claim returned with a resolver-backed completion verdict."""
    _validate_claim(claim)
