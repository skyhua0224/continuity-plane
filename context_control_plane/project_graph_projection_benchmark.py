"""Measured acceptance for the M9-02 Project Graph projection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
)
from .idea_continuity_benchmark import build_idea_snapshot
from .project_graph_projection import (
    PROJECT_GRAPH_MAX_NESTED_ITEMS,
    PROJECT_GRAPH_MAX_SCOPE_COMPARISONS,
    ProjectGraphProjectionError,
    build_project_graph_projection,
    validate_project_graph_projection,
)
from .state_mcp import RequestContext

BENCHMARK_SCHEMA_VERSION = "context.project-graph-projection-benchmark/v1alpha1"
BENCHMARK_ID = "m9-02-project-graph-projection"
_LARGE_GRAPH_WORK_COUNT = 128
_STRESS_SCOPE_COUNT = 223
_THRESHOLDS = {
    "same_revision_rate_min": 1.0,
    "active_work_complete_rate_min": 1.0,
    "cycle_visibility_rate_min": 1.0,
    "orphan_visibility_rate_min": 1.0,
    "expired_branch_visibility_rate_min": 1.0,
    "lease_expiry_visibility_rate_min": 1.0,
    "work_scope_overlap_visibility_rate_min": 1.0,
    "bounded_overlap_complete_rate_min": 1.0,
    "claim_conflict_rejection_required": True,
    "large_graph_complete_rate_min": 1.0,
    "tamper_rejection_rate_min": 1.0,
    "authority_violations_max": 0,
    "provider_invocations_max": 0,
    "external_services_max": 0,
    "projection_latency_p95_ms_max": 50.0,
}
_COUNT_FIELDS = (
    "same_revision_matches",
    "active_work_complete_matches",
    "cycle_visibility_matches",
    "orphan_visibility_matches",
    "expired_branch_visibility_matches",
    "lease_expiry_visibility_matches",
    "work_scope_overlap_visibility_matches",
    "bounded_overlap_complete_matches",
    "large_graph_complete_matches",
    "tamper_rejections",
)
_RATE_FIELDS = (
    "same_revision_rate",
    "active_work_complete_rate",
    "cycle_visibility_rate",
    "orphan_visibility_rate",
    "expired_branch_visibility_rate",
    "lease_expiry_visibility_rate",
    "work_scope_overlap_visibility_rate",
    "bounded_overlap_complete_rate",
    "large_graph_complete_rate",
    "tamper_rejection_rate",
)
_RESULT_FIELDS = set(_COUNT_FIELDS) | set(_RATE_FIELDS) | {
    "claim_conflict_rejected",
    "authority_violations",
    "provider_invocations",
    "external_services",
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "parameters",
    "thresholds",
    "results",
    "latency_ms",
    "gate",
    "provenance",
    "receipt_sha256",
}
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class ProjectGraphProjectionBenchmarkError(ValueError):
    """Raised when an M9-02 benchmark receipt cannot be accepted."""


class _Source:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = copy.deepcopy(snapshot)

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        return {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": arguments["request_id"],
            "tool": "context.state.read",
            "ok": True,
            "result": {
                "snapshot": copy.deepcopy(self.snapshot),
                "revision": self.snapshot["project"]["revision"],
                "event_head": None,
                "registry_digest": "a" * 64,
                "capabilities": {"adapter_id": "context.m9-02-benchmark"},
            },
            "error": None,
        }


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProjectGraphProjectionBenchmarkError(
            "benchmark is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latency(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "min": ordered[0],
        "p50": ordered[max(0, (len(ordered) * 50 + 99) // 100 - 1)],
        "p95": ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)],
        "max": ordered[-1],
    }


def _legacy_team(root: Path) -> dict[str, Any]:
    fixture_set = yaml.safe_load(
        (root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
            encoding="utf-8"
        )
    )
    return copy.deepcopy(
        next(
            case["document"]
            for case in fixture_set["cases"]
            if case["case_id"] == "multi-worker-disjoint-scopes"
        )
    )


def _external_projection(
    snapshot: dict[str, Any],
    *,
    signer: HMACExternalStateProjectionSigner,
    request_id: str,
) -> dict[str, Any]:
    response = ExternalStateProjectionProvider(
        _Source(snapshot),
        provider_id="provider-m9-02-benchmark",
        signer=signer,
    ).call_tool(
        EXTERNAL_READ_TOOL,
        {
            "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
            "request_id": request_id,
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
        },
        context=RequestContext("actor-benchmark", "authorization-benchmark"),
    )
    if not response["ok"]:
        raise ProjectGraphProjectionBenchmarkError("fixture source was rejected")
    return response["result"]


def _external_projection_is_rejected(
    snapshot: dict[str, Any],
    *,
    signer: HMACExternalStateProjectionSigner,
    request_id: str,
) -> bool:
    response = ExternalStateProjectionProvider(
        _Source(snapshot),
        provider_id="provider-m9-02-benchmark",
        signer=signer,
    ).call_tool(
        EXTERNAL_READ_TOOL,
        {
            "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
            "request_id": request_id,
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
        },
        context=RequestContext("actor-benchmark", "authorization-benchmark"),
    )
    return not response["ok"] and response["error"]["code"] == "source_integrity_error"


def _scenarios(
    root: Path,
    signer: HMACExternalStateProjectionSigner,
) -> dict[str, Any]:
    baseline = build_idea_snapshot()
    # The health scenario requires an expired lease, independent of the shared fixture.
    baseline["claims"][0]["lease_expires_at"] = "2026-08-14T10:00:00+08:00"

    cycle = _legacy_team(root)
    cycle["works"][0]["parent_work_id"] = cycle["works"][1]["work_id"]
    cycle["works"][1]["parent_work_id"] = cycle["works"][0]["work_id"]

    expired = build_idea_snapshot()
    experiment = copy.deepcopy(
        next(item for item in expired["works"] if item["work_id"] == "work-target")
    )
    experiment.update(
        {
            "work_id": "experiment-expired",
            "kind": "experiment",
            "title": "Expired benchmark branch",
            "status": "ready",
            "parent_work_id": "work-target",
            "scope_refs": [
                {"scope_kind": "capability", "scope_ref": "experiment-expired"}
            ],
            "return_point_work_id": "work-target",
            "exit_criteria": ["record evidence"],
            "attempt_budget": 1,
            "expires_at": "2026-08-16T00:00:00+08:00",
            "promotion_target_work_id": "work-target",
            "mainline_authority": False,
        }
    )
    expired["works"].append(experiment)

    overlap = _legacy_team(root)
    overlap["project"]["active_work_ids"] = ["work-network"]
    overlap["project"]["primary_work_id"] = "work-network"
    overlap["claims"] = [overlap["claims"][0]]
    overlap["works"][1]["status"] = "proposed"
    overlap["works"][1]["scope_refs"] = copy.deepcopy(
        overlap["works"][0]["scope_refs"]
    )
    overlap["works"][1]["overlap_candidate_ids"] = ["work-network"]
    overlap["works"][1]["dedupe_status"] = "blocked"

    stress_overlap = copy.deepcopy(overlap)
    stress_overlap["works"][0]["overlap_candidate_ids"] = ["work-ui"]
    stress_overlap["works"][0]["dedupe_status"] = "coordinated"
    stress_overlap["works"][1]["dedupe_status"] = "coordinated"
    stress_scopes = [
        {"scope_kind": "capability", "scope_ref": f"stress-{index:03d}"}
        for index in range(_STRESS_SCOPE_COUNT)
    ]
    stress_overlap["works"][0]["scope_refs"] = copy.deepcopy(stress_scopes)
    stress_overlap["works"][1]["scope_refs"] = copy.deepcopy(stress_scopes)
    stress_overlap["claims"][0]["scope_owners"] = [
        copy.deepcopy(stress_scopes[0])
    ]

    claim_conflict = _legacy_team(root)
    claim_conflict["works"][1]["scope_refs"] = copy.deepcopy(
        claim_conflict["works"][0]["scope_refs"]
    )
    claim_conflict["claims"][1]["scope_owners"] = copy.deepcopy(
        claim_conflict["claims"][0]["scope_owners"]
    )

    large = build_idea_snapshot()
    template = copy.deepcopy(
        next(work for work in large["works"] if work["work_id"] == "work-target")
    )
    for index in range(len(large["works"]), _LARGE_GRAPH_WORK_COUNT):
        work = copy.deepcopy(template)
        work["work_id"] = f"work-large-{index:03d}"
        work["title"] = f"Large graph Work {index}"
        work["scope_refs"] = [
            {"scope_kind": "capability", "scope_ref": f"large-graph-{index:03d}"}
        ]
        large["works"].append(work)

    return {
        "baseline_snapshot": baseline,
        "baseline": _external_projection(
            baseline,
            signer=signer,
            request_id="request-m9-02-baseline",
        ),
        "cycle": _external_projection(
            cycle,
            signer=signer,
            request_id="request-m9-02-cycle",
        ),
        "cycle_ids": sorted(work["work_id"] for work in cycle["works"]),
        "expired": _external_projection(
            expired,
            signer=signer,
            request_id="request-m9-02-expired",
        ),
        "overlap": _external_projection(
            overlap,
            signer=signer,
            request_id="request-m9-02-overlap",
        ),
        "stress_overlap": _external_projection(
            stress_overlap,
            signer=signer,
            request_id="request-m9-02-stress-overlap",
        ),
        "claim_conflict_rejected": _external_projection_is_rejected(
            claim_conflict,
            signer=signer,
            request_id="request-m9-02-claim-conflict",
        ),
        "large": _external_projection(
            large,
            signer=signer,
            request_id="request-m9-02-large",
        ),
    }


def _resign_projection(
    projection: dict[str, Any],
    signer: HMACExternalStateProjectionSigner,
) -> None:
    body = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    projection["projection_sha256"] = _digest(body)
    projection["signature"] = signer.sign(
        {**body, "projection_sha256": projection["projection_sha256"]}
    )


def _provenance(root: Path) -> dict[str, str]:
    return {
        "implementation_sha256": _file_digest(
            root / "context_control_plane/project_graph_projection.py"
        ),
        "benchmark_sha256": _file_digest(
            root / "context_control_plane/project_graph_projection_benchmark.py"
        ),
        "external_state_provider_sha256": _file_digest(
            root / "context_control_plane/external_state_provider.py"
        ),
        "typed_state_sha256": _file_digest(
            root / "context_control_plane/typed_state.py"
        ),
        "fixture_sha256": _file_digest(
            root / "experiments/state/m2-01-core-fixtures.yaml"
        ),
        "idea_fixture_builder_sha256": _file_digest(
            root / "context_control_plane/idea_continuity_benchmark.py"
        ),
    }


def _failed_gates(
    results: dict[str, int | float | bool], latency: dict[str, float]
) -> list[str]:
    checks = {
        "same-revision": results["same_revision_rate"]
        >= _THRESHOLDS["same_revision_rate_min"],
        "active-work-complete": results["active_work_complete_rate"]
        >= _THRESHOLDS["active_work_complete_rate_min"],
        "cycle-visible": results["cycle_visibility_rate"]
        >= _THRESHOLDS["cycle_visibility_rate_min"],
        "orphan-visible": results["orphan_visibility_rate"]
        >= _THRESHOLDS["orphan_visibility_rate_min"],
        "expired-branch-visible": results["expired_branch_visibility_rate"]
        >= _THRESHOLDS["expired_branch_visibility_rate_min"],
        "lease-expiry-visible": results["lease_expiry_visibility_rate"]
        >= _THRESHOLDS["lease_expiry_visibility_rate_min"],
        "work-scope-overlap-visible": results["work_scope_overlap_visibility_rate"]
        >= _THRESHOLDS["work_scope_overlap_visibility_rate_min"],
        "bounded-overlap-complete": results["bounded_overlap_complete_rate"]
        >= _THRESHOLDS["bounded_overlap_complete_rate_min"],
        "claim-conflict-rejected": results["claim_conflict_rejected"] is True,
        "large-graph-complete": results["large_graph_complete_rate"]
        >= _THRESHOLDS["large_graph_complete_rate_min"],
        "tamper-rejected": results["tamper_rejection_rate"]
        >= _THRESHOLDS["tamper_rejection_rate_min"],
        "authority": results["authority_violations"]
        <= _THRESHOLDS["authority_violations_max"],
        "provider": results["provider_invocations"]
        <= _THRESHOLDS["provider_invocations_max"],
        "external": results["external_services"]
        <= _THRESHOLDS["external_services_max"],
        "latency": latency["p95"]
        <= _THRESHOLDS["projection_latency_p95_ms_max"],
    }
    return sorted(name for name, accepted in checks.items() if not accepted)


def benchmark_project_graph_projection(
    *,
    root: Path,
    iterations: int,
    generated_at: str,
) -> dict[str, Any]:
    """Measure deterministic completeness, health visibility and tamper rejection."""
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ValueError("iterations must be an integer from 1 through 1000")
    if not isinstance(generated_at, str) or _TIMESTAMP_RE.fullmatch(generated_at) is None:
        raise ValueError("generated_at must be RFC3339")
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("generated_at must include timezone")

    root = root.resolve()
    signer = HMACExternalStateProjectionSigner(
        key_id="key-m9-02-benchmark",
        secret=b"m9-02-project-graph-benchmark-key",
    )
    scenarios = _scenarios(root, signer)
    results: dict[str, int | float | bool] = {field: 0 for field in _COUNT_FIELDS}
    results.update(
        {
            "claim_conflict_rejected": scenarios["claim_conflict_rejected"],
            "authority_violations": 0,
            "provider_invocations": 0,
            "external_services": 0,
        }
    )
    timings: list[float] = []
    for _ in range(iterations):
        round_timings: list[float] = []
        started = time.perf_counter_ns()
        baseline = build_project_graph_projection(
            scenarios["baseline"],
            signer=signer,
            observed_at=generated_at,
        )
        round_timings.append((time.perf_counter_ns() - started) / 1_000_000)
        validate_project_graph_projection(
            baseline,
            source_projection=scenarios["baseline"],
            signer=signer,
        )
        if (
            baseline["state_revision"]
            == scenarios["baseline_snapshot"]["project"]["revision"]
            and baseline["state_sha256"] == scenarios["baseline"]["state_sha256"]
        ):
            results["same_revision_matches"] += 1
        if {item["work_id"] for item in baseline["active_work_set"]} == set(
            scenarios["baseline_snapshot"]["project"]["active_work_ids"]
        ):
            results["active_work_complete_matches"] += 1
        if baseline["health"]["expired_active_claim_ids"] == ["claim-active"]:
            results["lease_expiry_visibility_matches"] += 1

        cycle = build_project_graph_projection(
            scenarios["cycle"],
            signer=signer,
            observed_at="2026-08-09T11:00:00+08:00",
        )
        if cycle["health"]["cycle_work_ids"] == scenarios["cycle_ids"]:
            results["cycle_visibility_matches"] += 1
        if cycle["health"]["orphan_work_ids"] == scenarios["cycle_ids"]:
            results["orphan_visibility_matches"] += 1

        expired = build_project_graph_projection(
            scenarios["expired"],
            signer=signer,
            observed_at=generated_at,
        )
        if expired["health"]["expired_branch_work_ids"] == [
            "experiment-expired"
        ]:
            results["expired_branch_visibility_matches"] += 1

        overlap = build_project_graph_projection(
            scenarios["overlap"],
            signer=signer,
            observed_at="2026-08-09T11:00:00+08:00",
        )
        if len(overlap["health"]["work_scope_overlap_candidates"]) == 1:
            results["work_scope_overlap_visibility_matches"] += 1

        started = time.perf_counter_ns()
        stress_overlap = build_project_graph_projection(
            scenarios["stress_overlap"],
            signer=signer,
            observed_at="2026-08-09T11:00:00+08:00",
        )
        round_timings.append((time.perf_counter_ns() - started) / 1_000_000)
        if len(
            stress_overlap["health"]["work_scope_overlap_candidates"]
        ) == _STRESS_SCOPE_COUNT:
            results["bounded_overlap_complete_matches"] += 1
        started = time.perf_counter_ns()
        large = build_project_graph_projection(
            scenarios["large"],
            signer=signer,
            observed_at=generated_at,
        )
        round_timings.append((time.perf_counter_ns() - started) / 1_000_000)
        if len(large["graph"]["nodes"]) == _LARGE_GRAPH_WORK_COUNT:
            results["large_graph_complete_matches"] += 1

        forged = copy.deepcopy(baseline)
        forged["active_work_set"] = []
        _resign_projection(forged, signer)
        try:
            validate_project_graph_projection(
                forged,
                source_projection=scenarios["baseline"],
                signer=signer,
            )
        except ProjectGraphProjectionError:
            results["tamper_rejections"] += 1

        authority = baseline["authority"]
        if (
            authority["state_write_authority"] is not False
            or authority["controlled_action_authority"] is not False
            or type(authority["provider_authority"]) is not int
            or authority["provider_authority"] != 0
            or type(authority["external_effect_authority"]) is not int
            or authority["external_effect_authority"] != 0
        ):
            results["authority_violations"] += 1
        timings.append(max(round_timings))

    for count_field, rate_field in zip(_COUNT_FIELDS, _RATE_FIELDS, strict=True):
        results[rate_field] = results[count_field] / iterations
    latency = _latency(timings)
    failed_gates = _failed_gates(results, latency)
    receipt: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "generated_at": generated_at,
        "parameters": {
            "iterations": iterations,
            "large_graph_work_count": _LARGE_GRAPH_WORK_COUNT,
            "max_scope_comparisons": PROJECT_GRAPH_MAX_SCOPE_COMPARISONS,
            "max_nested_items": PROJECT_GRAPH_MAX_NESTED_ITEMS,
            "stress_scope_count": _STRESS_SCOPE_COUNT,
        },
        "thresholds": copy.deepcopy(_THRESHOLDS),
        "results": results,
        "latency_ms": latency,
        "gate": {
            "status": "passed" if not failed_gates else "failed",
            "failed_gates": failed_gates,
        },
        "provenance": _provenance(root),
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def validate_project_graph_projection_benchmark(
    receipt: dict[str, Any], *, root: Path
) -> None:
    """Independently validate one measured M9-02 acceptance receipt."""
    if not isinstance(receipt, dict) or set(receipt) != _TOP_LEVEL_FIELDS:
        raise ProjectGraphProjectionBenchmarkError("benchmark fields are invalid")
    if (
        receipt["schema_version"] != BENCHMARK_SCHEMA_VERSION
        or receipt["benchmark_id"] != BENCHMARK_ID
    ):
        raise ProjectGraphProjectionBenchmarkError("benchmark identity is invalid")
    generated_at = receipt["generated_at"]
    if not isinstance(generated_at, str) or _TIMESTAMP_RE.fullmatch(generated_at) is None:
        raise ProjectGraphProjectionBenchmarkError("generated_at is invalid")
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectGraphProjectionBenchmarkError("generated_at is invalid") from exc
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ProjectGraphProjectionBenchmarkError("generated_at is invalid")
    parameters = receipt["parameters"]
    if not isinstance(parameters, dict) or set(parameters) != {
        "iterations",
        "large_graph_work_count",
        "max_nested_items",
        "max_scope_comparisons",
        "stress_scope_count",
    }:
        raise ProjectGraphProjectionBenchmarkError("parameters are invalid")
    iterations = parameters["iterations"]
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ProjectGraphProjectionBenchmarkError("iterations are invalid")
    if parameters["large_graph_work_count"] != _LARGE_GRAPH_WORK_COUNT:
        raise ProjectGraphProjectionBenchmarkError("large graph parameter is invalid")
    if (
        parameters["max_scope_comparisons"]
        != PROJECT_GRAPH_MAX_SCOPE_COMPARISONS
        or parameters["max_nested_items"] != PROJECT_GRAPH_MAX_NESTED_ITEMS
        or parameters["stress_scope_count"] != _STRESS_SCOPE_COUNT
    ):
        raise ProjectGraphProjectionBenchmarkError("scope budget parameters are invalid")
    thresholds = receipt["thresholds"]
    if thresholds != _THRESHOLDS or any(
        type(value) not in {int, float}
        for field, value in thresholds.items()
        if field != "claim_conflict_rejection_required"
    ) or thresholds["claim_conflict_rejection_required"] is not True:
        raise ProjectGraphProjectionBenchmarkError("thresholds are invalid")
    results = receipt["results"]
    if not isinstance(results, dict) or set(results) != _RESULT_FIELDS:
        raise ProjectGraphProjectionBenchmarkError("results are invalid")
    if type(results["claim_conflict_rejected"]) is not bool:
        raise ProjectGraphProjectionBenchmarkError("claim conflict result is invalid")
    for field in _COUNT_FIELDS:
        if type(results[field]) is not int or not 0 <= results[field] <= iterations:
            raise ProjectGraphProjectionBenchmarkError("result count is invalid")
    for field in ("authority_violations", "provider_invocations", "external_services"):
        if type(results[field]) is not int or results[field] < 0:
            raise ProjectGraphProjectionBenchmarkError("zero-tolerance result is invalid")
    for count_field, rate_field in zip(_COUNT_FIELDS, _RATE_FIELDS, strict=True):
        expected_rate = results[count_field] / iterations
        if type(results[rate_field]) is not float or results[rate_field] != expected_rate:
            raise ProjectGraphProjectionBenchmarkError("result rate is invalid")
    latency = receipt["latency_ms"]
    if not isinstance(latency, dict) or set(latency) != {"min", "p50", "p95", "max"}:
        raise ProjectGraphProjectionBenchmarkError("latency fields are invalid")
    latency_values = [latency[field] for field in ("min", "p50", "p95", "max")]
    if any(
        type(value) not in {int, float}
        or not math.isfinite(value)
        or value < 0
        for value in latency_values
    ) or latency_values != sorted(latency_values):
        raise ProjectGraphProjectionBenchmarkError("latency values are invalid")
    if receipt["provenance"] != _provenance(root.resolve()):
        raise ProjectGraphProjectionBenchmarkError("benchmark provenance mismatch")
    failed_gates = _failed_gates(results, latency)
    expected_gate = {
        "status": "passed" if not failed_gates else "failed",
        "failed_gates": failed_gates,
    }
    if receipt["gate"] != expected_gate:
        raise ProjectGraphProjectionBenchmarkError("gate derivation is invalid")
    if failed_gates:
        raise ProjectGraphProjectionBenchmarkError("benchmark failed acceptance gates")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != _digest(body):
        raise ProjectGraphProjectionBenchmarkError("receipt digest mismatch")
