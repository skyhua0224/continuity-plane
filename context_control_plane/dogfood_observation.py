"""Deterministic validation and summaries for project dogfood observations."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any


class DogfoodObservationError(ValueError):
    """Raised when an observation cannot support a trend claim."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_TYPES = {
    "compaction",
    "input-routing",
    "skill-load",
    "plan-revision",
    "verification",
}
_INPUT_KINDS = {"idea", "context-addition", "correction", "status-query", "interrupt"}
_INPUT_ROUTES = {
    "capture-and-continue",
    "continue",
    "checkpoint-and-switch",
    "correction-write-protect",
}


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DogfoodObservationError(f"{field} must be a non-negative integer")
    return value


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise DogfoodObservationError("observed_at must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DogfoodObservationError("observed_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise DogfoodObservationError("observed_at must include timezone")


def validate_observation_document(document: dict[str, Any]) -> None:
    """Validate the v1alpha1 dogfood observation contract."""
    if not isinstance(document, dict):
        raise DogfoodObservationError("observation document must be an object")
    if document.get("schema_version") != "context.dogfood-observation/v1alpha1":
        raise DogfoodObservationError("unsupported schema_version")
    observations = document.get("observations")
    if not isinstance(observations, list) or not observations:
        raise DogfoodObservationError("observations must be a non-empty list")

    seen: set[str] = set()
    for observation in observations:
        if not isinstance(observation, dict):
            raise DogfoodObservationError("observation must be an object")
        observation_id = observation.get("observation_id")
        if not isinstance(observation_id, str) or not observation_id or observation_id in seen:
            raise DogfoodObservationError("observation_id must be unique and non-empty")
        seen.add(observation_id)
        event_type = observation.get("event_type")
        if event_type not in _EVENT_TYPES:
            raise DogfoodObservationError("unsupported event_type")
        _validate_timestamp(observation.get("observed_at"))
        if not observation.get("active_leaf_before") or not observation.get("active_leaf_after"):
            raise DogfoodObservationError("active leaf references are required")
        metrics = observation.get("metrics")
        if not isinstance(metrics, dict):
            raise DogfoodObservationError("metrics must be an object")

        if event_type == "compaction":
            total = _non_negative_int(metrics.get("critical_fields_total"), "critical_fields_total")
            recovered = _non_negative_int(
                metrics.get("critical_fields_recovered"), "critical_fields_recovered"
            )
            if total == 0 or recovered > total:
                raise DogfoodObservationError("critical field counts are invalid")
            _non_negative_int(metrics.get("stale_decisions_revived"), "stale_decisions_revived")
            _non_negative_int(
                metrics.get("unauthorized_task_switches"), "unauthorized_task_switches"
            )
            context_bytes = metrics.get("comparable_context_input_bytes")
            if context_bytes is not None:
                _non_negative_int(context_bytes, "comparable_context_input_bytes")
        elif event_type == "input-routing":
            if observation.get("authority") in {None, "", "none"}:
                raise DogfoodObservationError("input routing requires authority")
            if observation.get("input_kind") not in _INPUT_KINDS:
                raise DogfoodObservationError("unsupported input_kind")
            route = observation.get("route")
            if route not in _INPUT_ROUTES:
                raise DogfoodObservationError("unsupported input route")
            candidate_summary = observation.get("candidate_summary")
            if not isinstance(candidate_summary, str) or not candidate_summary.strip():
                raise DogfoodObservationError("input routing requires a candidate summary")
            candidate_summary_sha256 = observation.get("candidate_summary_sha256", "")
            if not _SHA256_RE.fullmatch(candidate_summary_sha256):
                raise DogfoodObservationError("input routing requires candidate summary SHA-256")
            if hashlib.sha256(candidate_summary.encode("utf-8")).hexdigest() != candidate_summary_sha256:
                raise DogfoodObservationError("candidate summary digest mismatch")
            messages = _non_negative_int(metrics.get("messages_observed"), "messages_observed")
            ideas = _non_negative_int(metrics.get("ideas_captured"), "ideas_captured")
            _non_negative_int(
                metrics.get("unauthorized_task_switches"), "unauthorized_task_switches"
            )
            if messages == 0:
                raise DogfoodObservationError("input routing requires an observed message")
            if observation["input_kind"] == "idea":
                if not observation.get("candidate_id") or ideas == 0:
                    raise DogfoodObservationError("idea routing requires a captured candidate")
            for field in ("context_window_used_tokens", "context_window_limit_tokens"):
                if metrics.get(field) is not None:
                    _non_negative_int(metrics[field], field)
            for field in (
                "return_point_preserved",
                "provider_compaction_signal_available",
                "tool_interruption_observed",
            ):
                if not isinstance(metrics.get(field), bool):
                    raise DogfoodObservationError(f"{field} must be boolean")
            if route == "capture-and-continue" and (
                observation["active_leaf_before"] != observation["active_leaf_after"]
                or not metrics["return_point_preserved"]
            ):
                raise DogfoodObservationError(
                    "capture-and-continue must preserve active leaf and return point"
                )
        elif event_type == "skill-load":
            skills = metrics.get("skills")
            if not isinstance(skills, list) or not skills:
                raise DogfoodObservationError("skill-load requires skills")
            for skill in skills:
                if not isinstance(skill, dict) or not skill.get("skill_id"):
                    raise DogfoodObservationError("skill entry requires skill_id")
                if not _SHA256_RE.fullmatch(skill.get("content_sha256", "")):
                    raise DogfoodObservationError("skill entry requires SHA-256")
                _non_negative_int(skill.get("body_bytes"), "body_bytes")
                _non_negative_int(skill.get("body_lines"), "body_lines")
            count = _non_negative_int(metrics.get("body_load_count"), "body_load_count")
            total_bytes = _non_negative_int(metrics.get("total_body_bytes"), "total_body_bytes")
            _non_negative_int(metrics.get("repeated_body_load_bytes"), "repeated_body_load_bytes")
            if count != len(skills) or total_bytes != sum(skill["body_bytes"] for skill in skills):
                raise DogfoodObservationError("skill load totals do not match entries")
        elif event_type == "plan-revision":
            if observation.get("authority") in {None, "", "none"} or not observation.get("authorized"):
                raise DogfoodObservationError("plan revision requires explicit authority")
            before = _non_negative_int(metrics.get("master_revision_before"), "master_revision_before")
            after = _non_negative_int(metrics.get("master_revision_after"), "master_revision_after")
            if after <= before:
                raise DogfoodObservationError("MASTER revision must advance")
            _non_negative_int(
                metrics.get("unauthorized_goal_changes"), "unauthorized_goal_changes"
            )
        else:
            tests_run = _non_negative_int(metrics.get("tests_run"), "tests_run")
            tests_failed = _non_negative_int(metrics.get("tests_failed"), "tests_failed")
            if tests_failed > tests_run:
                raise DogfoodObservationError("tests_failed cannot exceed tests_run")
            _non_negative_int(metrics.get("checks_failed"), "checks_failed")
            _non_negative_int(metrics.get("scope_violations"), "scope_violations")
            refs = observation.get("evidence_refs")
            if not isinstance(refs, list) or not all(isinstance(ref, str) and ref for ref in refs):
                raise DogfoodObservationError("verification requires evidence_refs")


def summarize_observations(document: dict[str, Any]) -> dict[str, Any]:
    """Summarize safety and context-cost metrics without overstating trend."""
    validate_observation_document(document)
    compactions = [
        item for item in document["observations"] if item["event_type"] == "compaction"
    ]
    skill_loads = [
        item for item in document["observations"] if item["event_type"] == "skill-load"
    ]
    input_routing = [
        item for item in document["observations"] if item["event_type"] == "input-routing"
    ]
    plan_revisions = [
        item for item in document["observations"] if item["event_type"] == "plan-revision"
    ]
    verifications = [
        item for item in document["observations"] if item["event_type"] == "verification"
    ]

    critical_total = sum(item["metrics"]["critical_fields_total"] for item in compactions)
    critical_recovered = sum(
        item["metrics"]["critical_fields_recovered"] for item in compactions
    )
    stale = sum(item["metrics"]["stale_decisions_revived"] for item in compactions)
    unauthorized_switches = sum(
        item["metrics"]["unauthorized_task_switches"] for item in compactions
    ) + sum(item["metrics"]["unauthorized_task_switches"] for item in input_routing)
    unauthorized_goal_changes = sum(
        item["metrics"]["unauthorized_goal_changes"] for item in plan_revisions
    )
    skill_count = sum(item["metrics"]["body_load_count"] for item in skill_loads)
    skill_bytes = sum(item["metrics"]["total_body_bytes"] for item in skill_loads)
    repeated_skill_bytes = sum(
        item["metrics"]["repeated_body_load_bytes"] for item in skill_loads
    )
    ideas_captured = sum(item["metrics"]["ideas_captured"] for item in input_routing)
    verification_failures = sum(
        item["metrics"]["tests_failed"] + item["metrics"]["checks_failed"]
        for item in verifications
    )
    scope_violations = sum(item["metrics"]["scope_violations"] for item in verifications)

    recovery_rate = critical_recovered / critical_total if critical_total else 0.0
    comparable = [
        item["metrics"]["comparable_context_input_bytes"]
        for item in compactions
        if item["metrics"].get("comparable_context_input_bytes") is not None
    ]
    if (
        recovery_rate < 1.0
        or stale
        or unauthorized_switches
        or unauthorized_goal_changes
        or verification_failures
        or scope_violations
    ):
        trend_status = "regressed"
    elif len(comparable) < 3:
        trend_status = "baseline-insufficient-samples"
    elif comparable[-1] < comparable[0]:
        trend_status = "improving"
    else:
        trend_status = "stable"

    return {
        "compaction_events": len(compactions),
        "compaction_recovery_rate": recovery_rate,
        "stale_decisions_revived": stale,
        "unauthorized_task_switches": unauthorized_switches,
        "unauthorized_goal_changes": unauthorized_goal_changes,
        "skill_body_load_count": skill_count,
        "skill_body_load_bytes": skill_bytes,
        "repeated_skill_body_load_bytes": repeated_skill_bytes,
        "input_routing_events": len(input_routing),
        "ideas_captured": ideas_captured,
        "plan_revision_events": len(plan_revisions),
        "verification_events": len(verifications),
        "verification_failures": verification_failures,
        "scope_violations": scope_violations,
        "trend_status": trend_status,
    }
