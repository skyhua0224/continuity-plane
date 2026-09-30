"""Strict semantic validators for unattended dispatcher receipts."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

STEP_SCHEMA_VERSION = "context.unattended-dispatch-step/v1alpha1"
CAMPAIGN_RECEIPT_SCHEMA_VERSION = "context.unattended-campaign-receipt/v1alpha1"
CONDITION_DECISION_SCHEMA_VERSION = "context.condition-decision/v1alpha1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STEP_FIELDS = {
    "schema_version",
    "status",
    "project_id",
    "project_revision_before",
    "project_revision_after",
    "profile_id",
    "governance_revision",
    "obligation_id",
    "work_id",
    "work_revision_before",
    "work_revision_after",
    "claim_id",
    "lease_epoch",
    "fence",
    "packet_sha256",
    "execution_sha256",
    "verification_decision_sha256",
    "claim_evidence_verdict_sha256",
    "completion_receipt_sha256",
    "evidence_ids",
    "condition_decision_sha256",
    "condition_evidence_ids",
    "state_write_authority",
    "completion_authority",
    "provider_authority",
    "external_effect_authority",
    "step_sha256",
}
_CAMPAIGN_FIELDS = {
    "schema_version",
    "status",
    "campaign_run_id",
    "project_id",
    "profile_id",
    "governance_revision",
    "start_project_revision",
    "end_project_revision",
    "completed_work_ids",
    "conditional_not_applicable_work_ids",
    "remaining_optional_work_ids",
    "steps",
    "condition_decisions",
    "blocker_id",
    "evidence_ids",
    "resume_condition",
    "state_write_authority",
    "completion_authority",
    "provider_authority",
    "external_effect_authority",
    "receipt_sha256",
}
_CONDITION_FIELDS = {
    "schema_version",
    "decision_id",
    "condition_ref",
    "project_id",
    "project_revision",
    "profile_id",
    "governance_revision",
    "obligation_id",
    "obligation_revision",
    "outcome",
    "evidence_ids",
    "observed_at",
    "state_write_authority",
    "decision_sha256",
}


class UnattendedReceiptError(ValueError):
    """A receipt does not satisfy the unattended semantic contract."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise UnattendedReceiptError("receipt is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise UnattendedReceiptError(f"{label} fields are invalid")
    return copy.deepcopy(value)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise UnattendedReceiptError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise UnattendedReceiptError(f"{field} is invalid")
    return value


def _uint(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise UnattendedReceiptError(f"{field} is invalid")
    return value


def _ids(value: Any, field: str, *, required: bool = False) -> list[str]:
    if not isinstance(value, list) or (required and not value):
        raise UnattendedReceiptError(f"{field} is invalid")
    normalized = [_identifier(item, field) for item in value]
    if normalized != sorted(set(normalized)):
        raise UnattendedReceiptError(f"{field} must be sorted and unique")
    return normalized


def validate_condition_decision(value: Any) -> dict[str, Any]:
    decision = _object(value, _CONDITION_FIELDS, "condition decision")
    if decision["schema_version"] != CONDITION_DECISION_SCHEMA_VERSION:
        raise UnattendedReceiptError("condition decision version is invalid")
    for field in ("decision_id", "project_id", "profile_id", "obligation_id"):
        _identifier(decision[field], field)
    if not isinstance(decision["condition_ref"], str) or not decision[
        "condition_ref"
    ]:
        raise UnattendedReceiptError("condition_ref is invalid")
    _uint(decision["project_revision"], "project_revision")
    _uint(decision["governance_revision"], "governance_revision", minimum=1)
    _uint(decision["obligation_revision"], "obligation_revision", minimum=1)
    if decision["outcome"] not in {"met", "not-met", "unknown"}:
        raise UnattendedReceiptError("condition outcome is invalid")
    _ids(decision["evidence_ids"], "condition evidence_ids", required=True)
    if not isinstance(decision["observed_at"], str) or not decision["observed_at"]:
        raise UnattendedReceiptError("condition observed_at is invalid")
    if decision["state_write_authority"] is not False:
        raise UnattendedReceiptError("condition decision cannot claim authority")
    digest = _sha256(decision["decision_sha256"], "decision_sha256")
    unsigned = {key: item for key, item in decision.items() if key != "decision_sha256"}
    if digest != _digest(unsigned):
        raise UnattendedReceiptError("condition decision digest mismatch")
    return decision


def validate_unattended_dispatch_step(value: Any) -> dict[str, Any]:
    step = _object(value, _STEP_FIELDS, "unattended dispatch step")
    if step["schema_version"] != STEP_SCHEMA_VERSION or step["status"] != "completed":
        raise UnattendedReceiptError("unattended dispatch step version is invalid")
    for field in ("project_id", "profile_id", "obligation_id", "work_id", "claim_id"):
        _identifier(step[field], field)
    before = _uint(step["project_revision_before"], "project_revision_before")
    after = _uint(step["project_revision_after"], "project_revision_after")
    work_before = _uint(step["work_revision_before"], "work_revision_before", minimum=1)
    work_after = _uint(step["work_revision_after"], "work_revision_after", minimum=1)
    _uint(step["governance_revision"], "governance_revision", minimum=1)
    lease_epoch = _uint(step["lease_epoch"], "lease_epoch", minimum=1)
    fence = _uint(step["fence"], "fence", minimum=1)
    if after <= before or work_after != work_before + 1 or fence != lease_epoch:
        raise UnattendedReceiptError("unattended dispatch step revision chain is invalid")
    for field in (
        "packet_sha256",
        "execution_sha256",
        "verification_decision_sha256",
        "claim_evidence_verdict_sha256",
        "completion_receipt_sha256",
    ):
        _sha256(step[field], field)
    _ids(step["evidence_ids"], "step evidence_ids", required=True)
    condition_ids = _ids(step["condition_evidence_ids"], "condition_evidence_ids")
    condition_digest = step["condition_decision_sha256"]
    if condition_digest is None:
        if condition_ids:
            raise UnattendedReceiptError("condition evidence lacks a decision")
    else:
        _sha256(condition_digest, "condition_decision_sha256")
        if not condition_ids:
            raise UnattendedReceiptError("condition decision lacks evidence")
    if (
        step["state_write_authority"] is not False
        or step["completion_authority"] is not False
        or step["provider_authority"] != 0
        or step["external_effect_authority"] != 0
    ):
        raise UnattendedReceiptError("unattended dispatch step cannot claim authority")
    digest = _sha256(step["step_sha256"], "step_sha256")
    unsigned = {key: item for key, item in step.items() if key != "step_sha256"}
    if digest != _digest(unsigned):
        raise UnattendedReceiptError("unattended dispatch step digest mismatch")
    return step


def _resume_condition(value: Any) -> dict[str, Any]:
    condition = _object(value, {"kind", "refs"}, "resume condition")
    if not isinstance(condition["kind"], str) or not condition["kind"]:
        raise UnattendedReceiptError("resume condition kind is invalid")
    if not isinstance(condition["refs"], list) or not condition["refs"]:
        raise UnattendedReceiptError("resume condition refs are invalid")
    if any(not isinstance(item, str) or not item for item in condition["refs"]):
        raise UnattendedReceiptError("resume condition refs are invalid")
    if condition["refs"] != sorted(set(condition["refs"])):
        raise UnattendedReceiptError("resume condition refs must be sorted and unique")
    return condition


def validate_unattended_campaign_receipt(value: Any) -> dict[str, Any]:
    receipt = _object(value, _CAMPAIGN_FIELDS, "unattended campaign receipt")
    if receipt["schema_version"] != CAMPAIGN_RECEIPT_SCHEMA_VERSION:
        raise UnattendedReceiptError("unattended campaign receipt version is invalid")
    if receipt["status"] not in {"completed", "blocked"}:
        raise UnattendedReceiptError("unattended campaign status is invalid")
    for field in ("campaign_run_id", "project_id", "profile_id"):
        _identifier(receipt[field], field)
    _uint(receipt["governance_revision"], "governance_revision", minimum=1)
    start = _uint(receipt["start_project_revision"], "start_project_revision")
    end = _uint(receipt["end_project_revision"], "end_project_revision")
    if end < start:
        raise UnattendedReceiptError("campaign project revision regressed")
    completed = _ids(receipt["completed_work_ids"], "completed_work_ids")
    not_applicable = _ids(
        receipt["conditional_not_applicable_work_ids"],
        "conditional_not_applicable_work_ids",
    )
    optional = _ids(receipt["remaining_optional_work_ids"], "remaining_optional_work_ids")
    if (set(completed) & set(not_applicable)) or (
        (set(completed) | set(not_applicable)) & set(optional)
    ):
        raise UnattendedReceiptError("campaign Work classifications overlap")
    if not isinstance(receipt["steps"], list):
        raise UnattendedReceiptError("campaign steps are invalid")
    steps = [validate_unattended_dispatch_step(item) for item in receipt["steps"]]
    if len({item["step_sha256"] for item in steps}) != len(steps) or len(
        {item["work_id"] for item in steps}
    ) != len(steps):
        raise UnattendedReceiptError("campaign steps are duplicated")
    previous_revision = start
    for step in steps:
        if (
            step["project_id"] != receipt["project_id"]
            or step["profile_id"] != receipt["profile_id"]
            or step["governance_revision"] != receipt["governance_revision"]
            or step["project_revision_before"] != previous_revision
            or step["work_id"] not in completed
        ):
            raise UnattendedReceiptError("campaign step chain is invalid")
        previous_revision = step["project_revision_after"]
    if receipt["status"] == "completed" and previous_revision != end:
        raise UnattendedReceiptError("completed campaign revision chain is incomplete")
    if receipt["status"] == "blocked" and previous_revision > end:
        raise UnattendedReceiptError("blocked campaign revision chain is invalid")
    if not isinstance(receipt["condition_decisions"], list):
        raise UnattendedReceiptError("campaign condition decisions are invalid")
    decisions = [
        validate_condition_decision(item) for item in receipt["condition_decisions"]
    ]
    if len({item["decision_id"] for item in decisions}) != len(decisions):
        raise UnattendedReceiptError("campaign condition decisions are duplicated")
    if any(
        item["project_id"] != receipt["project_id"]
        or item["profile_id"] != receipt["profile_id"]
        or item["governance_revision"] != receipt["governance_revision"]
        for item in decisions
    ):
        raise UnattendedReceiptError("campaign condition decision binding is invalid")
    not_met_decisions = [item for item in decisions if item["outcome"] == "not-met"]
    if len(not_met_decisions) != len(not_applicable):
        raise UnattendedReceiptError(
            "campaign condition decisions do not match non-applicable Work"
        )
    evidence_ids = _ids(receipt["evidence_ids"], "campaign evidence_ids")
    if receipt["status"] == "completed":
        if (
            receipt["blocker_id"] is not None
            or receipt["resume_condition"] is not None
            or evidence_ids
        ):
            raise UnattendedReceiptError("completed campaign cannot retain a blocker")
    else:
        _identifier(receipt["blocker_id"], "blocker_id")
        if not evidence_ids:
            raise UnattendedReceiptError("blocked campaign requires evidence")
        _resume_condition(receipt["resume_condition"])
    if (
        receipt["state_write_authority"] is not False
        or receipt["completion_authority"] is not False
        or receipt["provider_authority"] != 0
        or receipt["external_effect_authority"] != 0
    ):
        raise UnattendedReceiptError("unattended campaign receipt cannot claim authority")
    digest = _sha256(receipt["receipt_sha256"], "receipt_sha256")
    unsigned = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    if digest != _digest(unsigned):
        raise UnattendedReceiptError("unattended campaign receipt digest mismatch")
    return receipt


__all__ = [
    "CAMPAIGN_RECEIPT_SCHEMA_VERSION",
    "CONDITION_DECISION_SCHEMA_VERSION",
    "STEP_SCHEMA_VERSION",
    "UnattendedReceiptError",
    "validate_condition_decision",
    "validate_unattended_campaign_receipt",
    "validate_unattended_dispatch_step",
]
