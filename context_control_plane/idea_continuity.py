"""Pure M3-06 gates for candidate Idea capture without route mutation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

IDEA_CAPTURE_VERDICT_SCHEMA_VERSION = "context.idea-capture-verdict/v1alpha1"


def _deny(reason: str) -> dict[str, Any]:
    return {
        "schema_version": IDEA_CAPTURE_VERDICT_SCHEMA_VERSION,
        "decision": "deny",
        "read_only": True,
        "reason": reason,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def evaluate_idea_capture_gate(
    snapshot: dict[str, Any],
    *,
    actor_ref: str,
    expected_revision: int,
    parent_work_id: str,
    return_work_id: str,
    action: str,
    switch_target_work_id: str | None,
    expiry: str | None,
    observed_at: str,
) -> dict[str, Any]:
    """Return a deterministic allow/deny decision for one candidate-only capture."""
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") not in {
        "context.typed-state/v3alpha1",
        "context.typed-state/v4alpha1",
        "context.typed-state/v5alpha1",
    }:
        return _deny("typed_state_version")
    project = snapshot.get("project")
    if not isinstance(project, dict) or project.get("revision") != expected_revision:
        return _deny("stale_revision")
    if not isinstance(actor_ref, str) or not actor_ref.strip():
        return _deny("actor_invalid")
    observed = _parse_timestamp(observed_at)
    snapshot_updated_at = _parse_timestamp(project.get("updated_at"))
    if observed is None or snapshot_updated_at is None or observed < snapshot_updated_at:
        return _deny("trusted_time_invalid")
    active_work_id = project.get("primary_work_id")
    if not isinstance(active_work_id, str) or not active_work_id:
        return _deny("active_work_missing")
    if parent_work_id != active_work_id or return_work_id != active_work_id:
        return _deny("parent_or_return_point_mismatch")
    work_by_id = {
        item.get("work_id"): item
        for item in snapshot.get("works", [])
        if isinstance(item, dict) and isinstance(item.get("work_id"), str)
    }
    active_work = work_by_id.get(active_work_id)
    if (
        active_work is None
        or active_work.get("status") != "active"
        or actor_ref not in active_work.get("owner_refs", [])
    ):
        return _deny("active_work_not_owned")
    active_claims = [
        item
        for item in snapshot.get("claims", [])
        if isinstance(item, dict)
        and item.get("status") == "active"
        and item.get("work_id") == active_work_id
    ]
    if len(active_claims) != 1 or active_claims[0].get("actor_ref") != actor_ref:
        return _deny("active_claim_mismatch")
    if active_claims[0].get("expected_project_revision") != expected_revision:
        return _deny("active_claim_stale")
    lease_expires_at = _parse_timestamp(active_claims[0].get("lease_expires_at"))
    if lease_expires_at is None or observed >= lease_expires_at:
        return _deny("active_claim_expired")
    if action not in {"capture-and-continue", "park", "propose-switch"}:
        return _deny("action_invalid")
    if expiry is not None:
        parsed_expiry = _parse_timestamp(expiry)
        if parsed_expiry is None or parsed_expiry <= observed:
            return _deny("idea_expired")
    if action == "propose-switch":
        target = work_by_id.get(switch_target_work_id)
        if (
            target is None
            or target.get("work_id") == active_work_id
            or target.get("status") != "ready"
        ):
            return _deny("switch_target_invalid")
    elif switch_target_work_id is not None:
        return _deny("switch_target_without_proposal")
    return {
        "schema_version": IDEA_CAPTURE_VERDICT_SCHEMA_VERSION,
        "decision": "allow",
        "read_only": False,
        "reason": "candidate_only",
    }
