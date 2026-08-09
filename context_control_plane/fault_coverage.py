"""M1-05 E0-E9 fault coverage contract validation and summaries."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any


SCHEMA_VERSION = "context.fault-coverage/v1alpha1"
REQUIRED_EXPERIMENTS = {f"E{index}" for index in range(10)}
REQUIRED_SCENARIOS = {
    "task-switch-without-checkpoint",
    "parallel-ready-work",
    "parallel-path-owner-conflict",
    "issue-backed-ready-work",
    "duplicate-work-detection",
    "stale-skill-digest",
    "sigkill-commit-boundary",
    "provider-503",
    "checkpoint-corruption",
    "concurrent-cas",
}

_SOURCE_KINDS = {"synthetic-contract", "admitted-replay"}
_PHASES = {"contract", "runtime-verified"}
_GATES = {"allow", "block", "read-only", "quarantine", "retry"}
_COMPLETION_CLAIMS = {"pending", "accepted", "rejected"}
_INJECTION_KINDS = {
    "none",
    "compaction",
    "task-switch",
    "parallel-claim",
    "path-conflict",
    "skill-load",
    "skill-drift",
    "retrieval-stale",
    "memory-stale",
    "verification-gap",
    "cas-conflict",
    "sigkill",
    "provider-503",
    "checkpoint-corruption",
}
_DOCUMENT_FIELDS = {
    "schema_version",
    "generated_at",
    "status",
    "evidence_refs",
    "fixtures",
}
_FIXTURE_FIELDS = {
    "fixture_id",
    "experiment_id",
    "scenario",
    "source_kind",
    "phase",
    "initial",
    "injection",
    "expected",
    "runtime_dependencies",
    "evidence_refs",
}


class FaultCoverageError(ValueError):
    """Raised when an M1-05 coverage document is incomplete or unsafe."""


def _non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FaultCoverageError(f"{field} must be a non-empty string")
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FaultCoverageError(f"{field} must be a non-negative integer")
    return value


def _validate_timestamp(value: Any) -> None:
    value = _non_empty_string(value, "generated_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FaultCoverageError("generated_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise FaultCoverageError("generated_at must include timezone")


def _validate_refs(refs: Any, field: str) -> list[str]:
    if not isinstance(refs, list) or not refs:
        raise FaultCoverageError(f"{field} must be a non-empty list")
    if not all(
        isinstance(ref, str)
        and (ref.startswith("artifact://") or ref.startswith("assertion:"))
        for ref in refs
    ):
        raise FaultCoverageError(f"{field} contains an invalid evidence reference")
    return refs


def _validate_initial(initial: Any, expected_gate: str) -> None:
    fields = {"active_tasks", "state_revision", "effect_watermark", "claims"}
    if not isinstance(initial, dict) or set(initial) != fields:
        raise FaultCoverageError("initial fields do not match the contract")
    active_tasks = initial["active_tasks"]
    if (
        not isinstance(active_tasks, list)
        or not active_tasks
        or not all(isinstance(task, str) and task for task in active_tasks)
        or len(active_tasks) != len(set(active_tasks))
    ):
        raise FaultCoverageError("initial.active_tasks must be unique and non-empty")
    _non_negative_int(initial["state_revision"], "initial.state_revision")
    _non_negative_int(initial["effect_watermark"], "initial.effect_watermark")
    claims = initial["claims"]
    if not isinstance(claims, list):
        raise FaultCoverageError("initial.claims must be a list")
    claimed_paths: list[str] = []
    claim_refs: set[str] = set()
    for claim in claims:
        if not isinstance(claim, dict) or set(claim) != {
            "task_id",
            "claim_ref",
            "owner_ref",
            "paths",
        }:
            raise FaultCoverageError("claim fields do not match the contract")
        if claim["task_id"] not in active_tasks:
            raise FaultCoverageError("claim task must be active")
        claim_ref = _non_empty_string(claim["claim_ref"], "claim.claim_ref")
        if claim_ref in claim_refs:
            raise FaultCoverageError("claim_ref must be unique")
        claim_refs.add(claim_ref)
        _non_empty_string(claim["owner_ref"], "claim.owner_ref")
        paths = claim["paths"]
        if not isinstance(paths, list) or not paths or not all(
            isinstance(path, str) and path for path in paths
        ):
            raise FaultCoverageError("claim.paths must be non-empty")
        claimed_paths.extend(paths)
    has_path_conflict = len(claimed_paths) != len(set(claimed_paths))
    if has_path_conflict and expected_gate != "block":
        raise FaultCoverageError("path ownership conflict must block")


def _validate_expected(expected: Any, initial: dict[str, Any]) -> None:
    fields = {
        "gate",
        "active_tasks",
        "state_revision",
        "effect_watermark",
        "duplicate_effects",
        "unauthorized_effects",
        "stale_decisions_revived",
        "completion_claim",
    }
    if not isinstance(expected, dict) or set(expected) != fields:
        raise FaultCoverageError("expected fields do not match the contract")
    if expected["gate"] not in _GATES:
        raise FaultCoverageError("unsupported expected gate")
    active_tasks = expected["active_tasks"]
    if not isinstance(active_tasks, list) or not active_tasks or len(active_tasks) != len(
        set(active_tasks)
    ):
        raise FaultCoverageError("expected.active_tasks must be unique and non-empty")
    state_revision = _non_negative_int(expected["state_revision"], "expected.state_revision")
    effect_watermark = _non_negative_int(
        expected["effect_watermark"], "expected.effect_watermark"
    )
    if state_revision < initial["state_revision"] or effect_watermark < initial["effect_watermark"]:
        raise FaultCoverageError("expected state or effect watermark cannot move backward")
    safety_fields = (
        "duplicate_effects",
        "unauthorized_effects",
        "stale_decisions_revived",
    )
    if any(_non_negative_int(expected[field], field) != 0 for field in safety_fields):
        raise FaultCoverageError("effect safety metrics must remain zero")
    if expected["completion_claim"] not in _COMPLETION_CLAIMS:
        raise FaultCoverageError("unsupported completion claim")


def validate_fault_coverage(document: dict[str, Any]) -> None:
    """Validate E0-E9 contract coverage without claiming deferred runtime evidence."""
    if not isinstance(document, dict) or set(document) != _DOCUMENT_FIELDS:
        raise FaultCoverageError("coverage document fields do not match the contract")
    if document["schema_version"] != SCHEMA_VERSION:
        raise FaultCoverageError("unsupported schema_version")
    _validate_timestamp(document["generated_at"])
    if document["status"] != "contract-verified-runtime-deferred":
        raise FaultCoverageError("coverage status must preserve the runtime evidence boundary")
    document_refs = set(_validate_refs(document["evidence_refs"], "evidence_refs"))
    fixtures = document["fixtures"]
    if not isinstance(fixtures, list) or not fixtures:
        raise FaultCoverageError("fixtures must be a non-empty list")

    fixture_ids: set[str] = set()
    scenarios: set[str] = set()
    experiments: set[str] = set()
    for fixture in fixtures:
        if not isinstance(fixture, dict) or set(fixture) != _FIXTURE_FIELDS:
            raise FaultCoverageError("fixture fields do not match the contract")
        fixture_id = _non_empty_string(fixture["fixture_id"], "fixture_id")
        if fixture_id in fixture_ids:
            raise FaultCoverageError("fixture_id must be unique")
        fixture_ids.add(fixture_id)
        experiment_id = fixture["experiment_id"]
        if experiment_id not in REQUIRED_EXPERIMENTS:
            raise FaultCoverageError("unsupported experiment_id")
        experiments.add(experiment_id)
        scenario = _non_empty_string(fixture["scenario"], "scenario")
        if scenario in scenarios:
            raise FaultCoverageError("scenario must be unique")
        scenarios.add(scenario)
        if fixture["source_kind"] not in _SOURCE_KINDS:
            raise FaultCoverageError("unsupported source_kind")
        if fixture["phase"] not in _PHASES:
            raise FaultCoverageError("unsupported phase")
        injection = fixture["injection"]
        if not isinstance(injection, dict) or set(injection) != {"kind", "target", "boundary"}:
            raise FaultCoverageError("injection fields do not match the contract")
        if injection["kind"] not in _INJECTION_KINDS:
            raise FaultCoverageError("unsupported injection kind")
        _non_empty_string(injection["target"], "injection.target")
        _non_empty_string(injection["boundary"], "injection.boundary")
        initial = fixture["initial"]
        if not isinstance(initial, dict) or set(initial) != {
            "active_tasks",
            "state_revision",
            "effect_watermark",
            "claims",
        }:
            raise FaultCoverageError("initial fields do not match the contract")
        _validate_expected(fixture["expected"], fixture["initial"])
        _validate_initial(fixture["initial"], fixture["expected"]["gate"])
        runtime_dependencies = fixture["runtime_dependencies"]
        if not isinstance(runtime_dependencies, list) or not all(
            isinstance(dependency, str) and dependency.startswith("M")
            for dependency in runtime_dependencies
        ):
            raise FaultCoverageError("runtime_dependencies must contain phase IDs")
        fixture_refs = set(_validate_refs(fixture["evidence_refs"], "fixture.evidence_refs"))
        if not fixture_refs.issubset(document_refs):
            raise FaultCoverageError("fixture evidence must be declared by the document")
        if fixture["phase"] == "runtime-verified" and runtime_dependencies:
            raise FaultCoverageError("runtime-verified fixtures cannot retain runtime dependencies")

    if experiments != REQUIRED_EXPERIMENTS:
        raise FaultCoverageError("coverage matrix must include E0-E9")
    if not REQUIRED_SCENARIOS.issubset(scenarios):
        raise FaultCoverageError("coverage matrix is missing required M1-05 scenarios")


def summarize_fault_coverage(document: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic contract and runtime coverage counts."""
    validate_fault_coverage(document)
    fixtures = document["fixtures"]
    experiments = sorted(
        {fixture["experiment_id"] for fixture in fixtures}, key=lambda value: int(value[1:])
    )
    phases = Counter(fixture["phase"] for fixture in fixtures)
    fixture_count = len(fixtures)
    return {
        "status": document["status"],
        "fixture_count": fixture_count,
        "experiments_covered": experiments,
        "experiment_coverage_rate": len(experiments) / len(REQUIRED_EXPERIMENTS),
        "contract_verified_fixture_count": phases["contract"],
        "contract_fixture_coverage_rate": phases["contract"] / fixture_count,
        "runtime_verified_fixture_count": phases["runtime-verified"],
        "runtime_fixture_coverage_rate": phases["runtime-verified"] / fixture_count,
    }
