"""Deterministic, bounded composition of the current Execution Packet."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any

from .compiled_skill_packet import (
    CompiledSkillPacketError,
    compiled_skill_packet_digest,
    validate_compiled_skill_packet,
)
from .skill_resolver import (
    SkillResolverError,
    canonical_skill_resolution_decision_bytes,
    canonical_skill_resolution_request_bytes,
    validate_skill_resolution_decision,
    validate_skill_resolution_request,
)
from .typed_state import TypedStateError, canonical_state_bytes, validate_typed_state

SCHEMA_VERSION = "context.execution-packet/v1alpha1"
COMPOSER_VERSION = "context.execution-packet-composer/v1alpha1"
MAX_PACKET_BYTES = 12 * 1024

_PACKET_FIELDS = {
    "schema_version",
    "composer_version",
    "project_id",
    "project_revision",
    "governance_ref",
    "canonical_plan_sha256",
    "state_sha256",
    "observed_at",
    "active_leaf",
    "claims",
    "decisions",
    "constraints",
    "blockers",
    "evidence_refs",
    "idea_refs",
    "next_action",
    "skill_lock",
    "continuation_cursor",
    "packet_sha256",
    "state_write_authority",
}
_ACTIVE_LEAF_FIELDS = {
    "work_id",
    "kind",
    "title",
    "status",
    "revision",
    "parent_work_id",
    "dependency_ids",
    "owner_refs",
    "scope_refs",
    "return_point_work_id",
    "exit_criteria",
    "promotion_target_work_id",
    "mainline_authority",
}
_CLAIM_FIELDS = {
    "claim_id",
    "work_id",
    "actor_ref",
    "status",
    "lease_expires_at",
    "scope_owners",
}
_DECISION_FIELDS = {"decision_id", "work_id", "status", "statement", "evidence_ids"}
_CONSTRAINT_FIELDS = {
    "constraint_id",
    "status",
    "statement",
    "scope_work_ids",
    "evidence_ids",
}
_BLOCKER_FIELDS = {"blocker_id", "status", "reason", "blocked_work_ids", "evidence_ids"}
_EVIDENCE_FIELDS = {
    "evidence_id",
    "artifact_ref",
    "content_sha256",
    "validity",
    "verified_at",
}
_IDEA_FIELDS = {
    "idea_id",
    "status",
    "summary",
    "return_work_id",
    "evidence_ids",
    "authority",
}
_SKILL_LOCK_FIELDS = {
    "resolver_version",
    "request_id",
    "request_sha256",
    "catalog_sha256",
    "manifest_set_sha256",
    "compiled_packet_sha256",
    "decision_sha256",
    "selected_rule_ids",
    "provider_contract_ref",
    "schema_refs",
}
_CURSOR_FIELDS = {
    "last_durable_action",
    "in_flight_phase",
    "confirmed_input_refs",
    "reserved_effect_ids",
    "replay_policy",
}
_SCOPE_FIELDS = {"scope_kind", "scope_ref"}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SAFE_TEXT_RE = re.compile(r"^[^\r\n\x00]+$")
_ARTIFACT_RE = re.compile(r"^artifact://(?:sha256/)?[0-9a-f]{64}$|^artifact://[^\s]+$")


class ExecutionPacketError(ValueError):
    """Raised when a packet cannot be composed or verified safely."""


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ExecutionPacketError(f"{field} fields are invalid")
    return value


def _text(value: Any, field: str, *, max_length: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > max_length
        or _SAFE_TEXT_RE.fullmatch(value) is None
    ):
        raise ExecutionPacketError(f"{field} is invalid")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ExecutionPacketError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ExecutionPacketError(f"{field} must be a lowercase SHA-256")
    return value


def _timestamp(value: Any, field: str) -> str:
    _text(value, field, max_length=64)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExecutionPacketError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExecutionPacketError(f"{field} requires a timezone")
    return value


def _uint(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ExecutionPacketError(f"{field} must be a non-negative integer")
    return value


def _ids(value: Any, field: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise ExecutionPacketError(f"{field} must be a list of IDs")
    result = [_id(item, field) for item in value]
    if len(result) != len(set(result)):
        raise ExecutionPacketError(f"{field} must contain unique IDs")
    return result


def _scopes(value: Any, field: str, *, required: bool = False) -> list[dict[str, str]]:
    if not isinstance(value, list) or (required and not value):
        raise ExecutionPacketError(f"{field} must be a scope list")
    result: list[dict[str, str]] = []
    for item in value:
        scope = _object(item, _SCOPE_FIELDS, field)
        _text(scope["scope_kind"], f"{field}.scope_kind", max_length=64)
        _text(scope["scope_ref"], f"{field}.scope_ref", max_length=2048)
        result.append({"scope_kind": scope["scope_kind"], "scope_ref": scope["scope_ref"]})
    keys = [(item["scope_kind"], item["scope_ref"]) for item in result]
    if len(keys) != len(set(keys)):
        raise ExecutionPacketError(f"{field} must contain unique scopes")
    return sorted(result, key=lambda item: (item["scope_kind"], item["scope_ref"]))


def _canonical_json_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _packet_body(packet: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(packet)
    body.pop("packet_sha256", None)
    return body


def _packet_digest(packet: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json_bytes(_packet_body(packet))).hexdigest()


def _validate_active_leaf(value: Any) -> dict[str, Any]:
    leaf = _object(value, _ACTIVE_LEAF_FIELDS, "active_leaf")
    _id(leaf["work_id"], "active_leaf.work_id")
    _text(leaf["kind"], "active_leaf.kind", max_length=64)
    if isinstance(leaf.get("title"), str) and len(leaf["title"]) > 8192:
        raise ExecutionPacketError("execution packet size exceeds active leaf bound")
    _text(leaf["title"], "active_leaf.title", max_length=8192)
    if leaf["status"] != "active":
        raise ExecutionPacketError("active_leaf.status must be active")
    _uint(leaf["revision"], "active_leaf.revision")
    for field in ("parent_work_id", "return_point_work_id", "promotion_target_work_id"):
        if leaf[field] is not None:
            _id(leaf[field], f"active_leaf.{field}")
    _ids(leaf["dependency_ids"], "active_leaf.dependency_ids")
    _ids(leaf["owner_refs"], "active_leaf.owner_refs")
    _scopes(leaf["scope_refs"], "active_leaf.scope_refs")
    if not isinstance(leaf["exit_criteria"], list) or any(
        not isinstance(item, str) or not item.strip() for item in leaf["exit_criteria"]
    ):
        raise ExecutionPacketError("active_leaf.exit_criteria is invalid")
    if not isinstance(leaf["mainline_authority"], bool):
        raise ExecutionPacketError("active_leaf.mainline_authority is invalid")
    return leaf


def _validate_claim(value: Any) -> dict[str, Any]:
    claim = _object(value, _CLAIM_FIELDS, "claim")
    for field in ("claim_id", "work_id", "actor_ref"):
        _id(claim[field], f"claim.{field}")
    if claim["status"] not in {"active", "expired", "released"}:
        raise ExecutionPacketError("claim.status is invalid")
    _timestamp(claim["lease_expires_at"], "claim.lease_expires_at")
    _scopes(claim["scope_owners"], "claim.scope_owners")
    return claim


def _validate_evidence(value: Any) -> dict[str, Any]:
    evidence = _object(value, _EVIDENCE_FIELDS, "evidence_ref")
    _id(evidence["evidence_id"], "evidence_ref.evidence_id")
    if not isinstance(evidence["artifact_ref"], str) or _ARTIFACT_RE.fullmatch(
        evidence["artifact_ref"]
    ) is None:
        raise ExecutionPacketError("evidence_ref.artifact_ref is invalid")
    _sha(evidence["content_sha256"], "evidence_ref.content_sha256")
    if evidence["validity"] != "verified":
        raise ExecutionPacketError("evidence_ref must be verified")
    _timestamp(evidence["verified_at"], "evidence_ref.verified_at")
    return evidence


def _validate_skill_lock(value: Any) -> dict[str, Any]:
    lock = _object(value, _SKILL_LOCK_FIELDS, "skill_lock")
    _text(lock["resolver_version"], "skill_lock.resolver_version", max_length=128)
    _id(lock["request_id"], "skill_lock.request_id")
    for field in (
        "request_sha256",
        "catalog_sha256",
        "manifest_set_sha256",
        "compiled_packet_sha256",
        "decision_sha256",
    ):
        _sha(lock[field], f"skill_lock.{field}")
    _ids(lock["selected_rule_ids"], "skill_lock.selected_rule_ids", required=True)
    _text(lock["provider_contract_ref"], "skill_lock.provider_contract_ref", max_length=256)
    _ids(lock["schema_refs"], "skill_lock.schema_refs", required=True)
    return lock


def _validate_cursor(value: Any) -> dict[str, Any]:
    cursor = _object(value, _CURSOR_FIELDS, "continuation_cursor")
    _text(cursor["last_durable_action"], "continuation_cursor.last_durable_action")
    _text(cursor["in_flight_phase"], "continuation_cursor.in_flight_phase")
    _ids(cursor["confirmed_input_refs"], "continuation_cursor.confirmed_input_refs")
    _ids(cursor["reserved_effect_ids"], "continuation_cursor.reserved_effect_ids")
    if cursor["replay_policy"] not in {"resume-verified", "replay-idempotent", "verify-before-effect"}:
        raise ExecutionPacketError("continuation_cursor.replay_policy is invalid")
    return cursor


def validate_execution_packet(packet: dict[str, Any], *, verify_digest: bool = True) -> None:
    """Validate a composed packet and its content-addressed digest."""
    packet = _object(packet, _PACKET_FIELDS, "execution packet")
    if packet["schema_version"] != SCHEMA_VERSION:
        raise ExecutionPacketError("unsupported execution packet schema_version")
    if packet["composer_version"] != COMPOSER_VERSION:
        raise ExecutionPacketError("unsupported execution packet composer_version")
    _id(packet["project_id"], "project_id")
    _uint(packet["project_revision"], "project_revision")
    _text(packet["governance_ref"], "governance_ref")
    _sha(packet["canonical_plan_sha256"], "canonical_plan_sha256")
    _sha(packet["state_sha256"], "state_sha256")
    _timestamp(packet["observed_at"], "observed_at")
    _validate_active_leaf(packet["active_leaf"])
    if not isinstance(packet["claims"], list):
        raise ExecutionPacketError("claims must be a list")
    for claim in packet["claims"]:
        _validate_claim(claim)
    for field, required_fields, identifier_field, text_field in (
        ("decisions", _DECISION_FIELDS, "decision_id", "statement"),
        ("constraints", _CONSTRAINT_FIELDS, "constraint_id", "statement"),
        ("blockers", _BLOCKER_FIELDS, "blocker_id", "reason"),
    ):
        if not isinstance(packet[field], list):
            raise ExecutionPacketError(f"{field} must be a list")
        for item in packet[field]:
            obj = _object(item, required_fields, field[:-1])
            _id(obj[identifier_field], f"{field}.{identifier_field}")
            _text(obj["status"], f"{field}.status", max_length=64)
            _text(obj[text_field], f"{field}.{text_field}")
            _ids(obj["evidence_ids"], f"{field}.evidence_ids")
    if not isinstance(packet["evidence_refs"], list):
        raise ExecutionPacketError("evidence_refs must be a list")
    evidence_ids = []
    for evidence in packet["evidence_refs"]:
        evidence_ids.append(_validate_evidence(evidence)["evidence_id"])
    if len(evidence_ids) != len(set(evidence_ids)):
        raise ExecutionPacketError("evidence_refs must be unique")
    if not isinstance(packet["idea_refs"], list):
        raise ExecutionPacketError("idea_refs must be a list")
    idea_ids = []
    for idea in packet["idea_refs"]:
        idea = _object(idea, _IDEA_FIELDS, "idea_ref")
        idea_ids.append(_id(idea["idea_id"], "idea_ref.idea_id"))
        if idea["status"] not in {"candidate", "parked", "proposed", "approved"}:
            raise ExecutionPacketError("idea_ref.status is not candidate-only")
        _text(idea["summary"], "idea_ref.summary", max_length=1024)
        _id(idea["return_work_id"], "idea_ref.return_work_id")
        _ids(idea["evidence_ids"], "idea_ref.evidence_ids")
        if idea["authority"] != "candidate-only":
            raise ExecutionPacketError("idea_ref authority is invalid")
    if len(idea_ids) != len(set(idea_ids)):
        raise ExecutionPacketError("idea_refs must be unique")
    _text(packet["next_action"], "next_action", max_length=1024)
    _validate_skill_lock(packet["skill_lock"])
    _validate_cursor(packet["continuation_cursor"])
    _sha(packet["packet_sha256"], "packet_sha256")
    if packet["state_write_authority"] is not False:
        raise ExecutionPacketError("execution packet cannot claim state write authority")
    if verify_digest and _packet_digest(packet) != packet["packet_sha256"]:
        raise ExecutionPacketError("packet_sha256 does not match packet content")
    if len(_canonical_json_bytes(packet)) > MAX_PACKET_BYTES:
        raise ExecutionPacketError("execution packet size exceeds 12 KiB")


def _state_items(snapshot: dict[str, Any], field: str) -> dict[str, dict[str, Any]]:
    return {item[field[:-1] + "_id"]: item for item in snapshot[field]}


def _skill_lock(
    request: dict[str, Any], decision: dict[str, Any], compiled_packet: dict[str, Any]
) -> dict[str, Any]:
    try:
        validate_skill_resolution_request(request)
        validate_skill_resolution_decision(decision)
        validate_compiled_skill_packet(compiled_packet)
        request_hash = hashlib.sha256(canonical_skill_resolution_request_bytes(request)).hexdigest()
        decision_hash = hashlib.sha256(canonical_skill_resolution_decision_bytes(decision)).hexdigest()
        packet_hash = compiled_skill_packet_digest(compiled_packet)
    except (SkillResolverError, CompiledSkillPacketError) as exc:
        raise ExecutionPacketError(f"compiled Skill binding is invalid: {exc}") from exc
    if decision["disposition"] != "resolved":
        raise ExecutionPacketError("Skill decision is quarantined")
    if request["request_id"] != decision["request_id"]:
        raise ExecutionPacketError("Skill request and decision IDs differ")
    if request_hash != decision["request_sha256"]:
        raise ExecutionPacketError("Skill request digest does not match decision")
    if packet_hash != decision["packet_sha256"]:
        raise ExecutionPacketError("compiled Skill packet digest does not match decision")
    if compiled_packet["manifest_set_sha256"] != decision["manifest_set_sha256"]:
        raise ExecutionPacketError("compiled Skill manifest digest does not match decision")
    return {
        "resolver_version": decision["resolver_version"],
        "request_id": request["request_id"],
        "request_sha256": request_hash,
        "catalog_sha256": request["catalog_sha256"],
        "manifest_set_sha256": decision["manifest_set_sha256"],
        "compiled_packet_sha256": packet_hash,
        "decision_sha256": decision_hash,
        "selected_rule_ids": sorted(decision["selected_rule_ids"]),
        "provider_contract_ref": request["provider_contract_ref"],
        "schema_refs": sorted(request["required_schema_refs"]),
    }


def compose_execution_packet(
    *,
    snapshot: dict[str, Any],
    skill_request: dict[str, Any],
    skill_decision: dict[str, Any],
    compiled_skill_packet: dict[str, Any],
    next_action: str,
    continuation_cursor: dict[str, Any],
    canonical_plan_sha256: str,
    observed_at: str,
) -> dict[str, Any]:
    """Compose a deterministic projection of the current active Work."""
    source = copy.deepcopy(snapshot)
    try:
        validate_typed_state(source)
    except TypedStateError as exc:
        raise ExecutionPacketError(f"typed state is invalid: {exc}") from exc
    _sha(canonical_plan_sha256, "canonical_plan_sha256")
    observed = _timestamp(observed_at, "observed_at")
    _text(next_action, "next_action", max_length=1024)
    if "\n" in next_action or "\r" in next_action:
        raise ExecutionPacketError("next_action must contain exactly one action")
    cursor = _validate_cursor(copy.deepcopy(continuation_cursor))
    project = source["project"]
    active_id = project["primary_work_id"]
    if active_id is None or active_id not in project["active_work_ids"]:
        raise ExecutionPacketError("active leaf is missing")
    works = {item["work_id"]: item for item in source["works"]}
    active = works.get(active_id)
    if active is None or active["status"] != "active":
        raise ExecutionPacketError("active leaf is not active")
    if active.get("kind") in {"campaign", "goal"}:
        raise ExecutionPacketError("active leaf must be executable Work")
    skill_lock = _skill_lock(skill_request, skill_decision, compiled_skill_packet)
    if skill_request["project_ref"] != f"project://{project['project_id']}":
        raise ExecutionPacketError("Skill binding project does not match state")
    if skill_request["task_ref"] != f"task://{active_id}":
        raise ExecutionPacketError("Skill binding task does not match active leaf")

    leaf = {
        "work_id": active["work_id"],
        "kind": active["kind"],
        "title": active["title"],
        "status": active["status"],
        "revision": active["revision"],
        "parent_work_id": active.get("parent_work_id"),
        "dependency_ids": sorted(active["dependency_ids"]),
        "owner_refs": sorted(active["owner_refs"]),
        "scope_refs": _scopes(active["scope_refs"], "active_leaf.scope_refs"),
        "return_point_work_id": active.get("return_point_work_id"),
        "exit_criteria": list(active.get("exit_criteria", [])),
        "promotion_target_work_id": active.get("promotion_target_work_id"),
        "mainline_authority": active.get("mainline_authority", True),
    }
    claims = [
        {
            "claim_id": claim["claim_id"],
            "work_id": claim["work_id"],
            "actor_ref": claim["actor_ref"],
            "status": claim["status"],
            "lease_expires_at": claim["lease_expires_at"],
            "scope_owners": _scopes(claim["scope_owners"], "claim.scope_owners"),
        }
        for claim in source["claims"]
        if claim["work_id"] == active_id and claim["status"] == "active"
    ]
    claims.sort(key=lambda item: item["claim_id"])
    decisions_by_id = {item["decision_id"]: item for item in source["decisions"]}
    decisions = []
    for decision_id in sorted(project["current_decision_ids"]):
        decision = decisions_by_id[decision_id]
        if decision["status"] != "accepted" or decision["work_id"] != active_id:
            continue
        decisions.append(
            {
                "decision_id": decision["decision_id"],
                "work_id": decision["work_id"],
                "status": decision["status"],
                "statement": decision["statement"],
                "evidence_ids": sorted(decision["evidence_ids"]),
            }
        )
    constraints_by_id = {item["constraint_id"]: item for item in source["constraints"]}
    constraints = []
    for constraint_id in sorted(project["active_constraint_ids"]):
        constraint = constraints_by_id[constraint_id]
        if constraint["status"] != "active":
            continue
        scopes = constraint["scope_work_ids"]
        if scopes and active_id not in scopes:
            continue
        constraints.append(
            {
                "constraint_id": constraint["constraint_id"],
                "status": constraint["status"],
                "statement": constraint["statement"],
                "scope_work_ids": sorted(scopes),
                "evidence_ids": sorted(constraint["evidence_ids"]),
            }
        )
    blockers_by_id = {item["blocker_id"]: item for item in source["blockers"]}
    blockers = []
    for blocker_id in sorted(project["open_blocker_ids"]):
        blocker = blockers_by_id[blocker_id]
        if blocker["status"] != "open" or active_id not in blocker["blocked_work_ids"]:
            continue
        blockers.append(
            {
                "blocker_id": blocker["blocker_id"],
                "status": blocker["status"],
                "reason": blocker["reason"],
                "blocked_work_ids": sorted(blocker["blocked_work_ids"]),
                "evidence_ids": sorted(blocker["evidence_ids"]),
            }
        )
    ideas = []
    for idea in source["ideas"]:
        if idea["status"] not in {"candidate", "parked", "proposed", "approved"}:
            continue
        if idea["parent_work_id"] != active_id and idea["return_work_id"] != active_id:
            continue
        ideas.append(
            {
                "idea_id": idea["idea_id"],
                "status": idea["status"],
                "summary": idea["summary"],
                "return_work_id": idea["return_work_id"],
                "evidence_ids": sorted(idea["evidence_ids"]),
                "authority": "candidate-only",
            }
        )
    ideas.sort(key=lambda item: item["idea_id"])

    required_evidence_ids = set(active["evidence_ids"])
    for item in decisions + constraints + blockers:
        required_evidence_ids.update(item["evidence_ids"])
    evidence_by_id = {item["evidence_id"]: item for item in source["evidence"]}
    evidence_refs = []
    for evidence_id in sorted(required_evidence_ids):
        evidence = evidence_by_id.get(evidence_id)
        if evidence is None:
            raise ExecutionPacketError(f"evidence ref is missing: {evidence_id}")
        if evidence["validity"] != "verified":
            raise ExecutionPacketError(f"evidence ref is not verified: {evidence_id}")
        evidence_refs.append(
            {
                "evidence_id": evidence["evidence_id"],
                "artifact_ref": evidence["artifact_ref"],
                "content_sha256": evidence["content_sha256"],
                "validity": evidence["validity"],
                "verified_at": evidence["verified_at"],
            }
        )

    packet = {
        "schema_version": SCHEMA_VERSION,
        "composer_version": COMPOSER_VERSION,
        "project_id": project["project_id"],
        "project_revision": project["revision"],
        "governance_ref": project["governance_ref"],
        "canonical_plan_sha256": canonical_plan_sha256,
        "state_sha256": hashlib.sha256(canonical_state_bytes(source)).hexdigest(),
        "observed_at": observed,
        "active_leaf": leaf,
        "claims": claims,
        "decisions": decisions,
        "constraints": constraints,
        "blockers": blockers,
        "evidence_refs": evidence_refs,
        "idea_refs": ideas,
        "next_action": next_action,
        "skill_lock": skill_lock,
        "continuation_cursor": cursor,
        "packet_sha256": "0" * 64,
        "state_write_authority": False,
    }
    packet["packet_sha256"] = _packet_digest(packet)
    validate_execution_packet(packet)
    return json.loads(_canonical_json_bytes(packet))


def canonical_execution_packet_bytes(packet: dict[str, Any]) -> bytes:
    """Return deterministic canonical packet bytes after validation."""
    validate_execution_packet(packet)
    return _canonical_json_bytes(packet)
