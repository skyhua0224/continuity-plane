"""Pure authority gates for bounded Experiment execution and promotion."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

EXPERIMENT_LIFECYCLE_VERDICT_SCHEMA_VERSION = (
    "context.experiment-lifecycle-verdict/v1alpha1"
)
_EXPERIMENT_CONTRACT_FIELDS = (
    "return_point_work_id",
    "exit_criteria",
    "attempt_budget",
    "expires_at",
    "promotion_target_work_id",
    "mainline_authority",
)


def experiment_contract_sha256(work: dict[str, Any]) -> str:
    """Hash the Experiment fields that become immutable after the first attempt."""
    payload = {field: work[field] for field in _EXPERIMENT_CONTRACT_FIELDS}
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


def _verdict(
    *,
    decision: str,
    reason: str,
    attempt_no: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": EXPERIMENT_LIFECYCLE_VERDICT_SCHEMA_VERSION,
        "decision": decision,
        "read_only": decision != "allow",
        "reason": reason,
    }
    if attempt_no is not None:
        result["attempt_no"] = attempt_no
    return result


def experiment_time_verdict(
    snapshot: dict[str, Any],
    *,
    work_id: str,
    observed_at: str,
) -> dict[str, Any]:
    """Check the trusted clock against one Experiment's expiry boundary."""
    try:
        current = _time(observed_at)
        snapshot_time = _time(snapshot["project"]["updated_at"])
    except (KeyError, TypeError, ValueError):
        return _verdict(decision="deny", reason="trusted_time_invalid")
    if current < snapshot_time:
        return _verdict(decision="deny", reason="trusted_time_regressed")
    work = next((item for item in snapshot["works"] if item["work_id"] == work_id), None)
    if work is None or work.get("kind") != "experiment":
        return _verdict(decision="deny", reason="experiment_not_found")
    try:
        expires_at = _time(work["expires_at"])
    except (KeyError, TypeError, ValueError):
        return _verdict(decision="deny", reason="experiment_contract_invalid")
    if current >= expires_at:
        return _verdict(decision="deny", reason="experiment_expired")
    return _verdict(decision="allow", reason="authorized")


def evaluate_experiment_activation_gate(
    snapshot: dict[str, Any],
    *,
    work_id: str,
    observed_at: str,
) -> dict[str, Any]:
    """Reject activation when an Experiment has expired or exhausted its budget."""
    time_verdict = experiment_time_verdict(
        snapshot,
        work_id=work_id,
        observed_at=observed_at,
    )
    if time_verdict["decision"] != "allow":
        return time_verdict
    work = next(item for item in snapshot["works"] if item["work_id"] == work_id)
    attempt_count = sum(
        item["work_id"] == work_id
        for item in snapshot.get("experiment_attempts", [])
    )
    if attempt_count >= work["attempt_budget"]:
        return _verdict(decision="deny", reason="attempt_budget_exhausted")
    return _verdict(decision="allow", reason="authorized")


def evaluate_attempt_gate(
    snapshot: dict[str, Any],
    *,
    actor_ref: str,
    work_id: str,
    claim_id: str,
    expected_revision: int,
    observed_at: str,
) -> dict[str, Any]:
    """Authorize one immutable Experiment attempt before its external effects."""
    if snapshot["project"]["revision"] != expected_revision:
        return _verdict(decision="deny", reason="stale_revision")
    time_verdict = experiment_time_verdict(
        snapshot,
        work_id=work_id,
        observed_at=observed_at,
    )
    if time_verdict["decision"] != "allow":
        return time_verdict
    work = next(item for item in snapshot["works"] if item["work_id"] == work_id)
    if work["status"] != "active":
        return _verdict(decision="deny", reason="inactive_experiment")
    claim = next(
        (item for item in snapshot["claims"] if item["claim_id"] == claim_id), None
    )
    if (
        claim is None
        or claim["status"] != "active"
        or claim["work_id"] != work_id
        or claim["actor_ref"] != actor_ref
        or claim["expected_project_revision"] != expected_revision
    ):
        return _verdict(decision="deny", reason="claim_mismatch")
    try:
        lease_expires_at = _time(claim["lease_expires_at"])
        current = _time(observed_at)
    except (KeyError, TypeError, ValueError):
        return _verdict(decision="deny", reason="claim_lease_invalid")
    if current >= lease_expires_at:
        return _verdict(decision="deny", reason="claim_expired")
    attempts = [
        item
        for item in snapshot.get("experiment_attempts", [])
        if item["work_id"] == work_id
    ]
    if len(attempts) >= work["attempt_budget"]:
        return _verdict(decision="deny", reason="attempt_budget_exhausted")
    return _verdict(decision="allow", reason="authorized", attempt_no=len(attempts) + 1)
