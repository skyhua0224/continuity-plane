"""Measured acceptance for the M9-07 Relationship and Impact projection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
)
from .idea_continuity_benchmark import build_idea_snapshot
from .project_graph_projection import build_project_graph_projection
from .relationship_impact_projection import (
    RelationshipImpactProjectionError,
    build_relationship_impact_projection,
    validate_relationship_impact_projection,
)
from .state_mcp import RequestContext

BENCHMARK_SCHEMA_VERSION = "context.relationship-impact-benchmark/v1alpha1"
BENCHMARK_ID = "m9-07-relationship-impact"
_SCALE_WORK_NODES = 2_000
_THRESHOLDS = {
    "same_revision_rate_min": 1.0,
    "complete_projection_rate_min": 1.0,
    "focus_direction_rate_min": 1.0,
    "code_clock_separation_rate_min": 1.0,
    "tamper_rejection_rate_min": 1.0,
    "filter_rejection_rate_min": 1.0,
    "scale_complete_rate_min": 1.0,
    "authority_violations_max": 0,
    "provider_invocations_max": 0,
    "external_services_max": 0,
    "projection_latency_p95_ms_max": 50.0,
    "scale_latency_p95_ms_max": 250.0,
}
_RESULT_FIELDS = {
    "same_revision_matches",
    "complete_projection_matches",
    "focus_direction_matches",
    "code_clock_separation_matches",
    "tamper_rejections",
    "filter_rejections",
    "scale_complete_matches",
    "authority_violations",
    "provider_invocations",
    "external_services",
}
_RATE_FIELDS = {
    "same_revision_rate",
    "complete_projection_rate",
    "focus_direction_rate",
    "code_clock_separation_rate",
    "tamper_rejection_rate",
    "filter_rejection_rate",
    "scale_complete_rate",
}
_TOP_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "parameters",
    "results",
    "rates",
    "latency_ms",
    "scale_latency_ms",
    "provenance",
    "authority",
    "failed_gates",
    "verdict",
    "receipt_sha256",
}
_AUTHORITY = {
    "state_write_authority": False,
    "controlled_action_authority": False,
    "approval_authority": False,
    "completion_authority": False,
    "provider_authority": 0,
    "external_effect_authority": 0,
}


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
                "capabilities": {"adapter_id": "context.benchmark"},
            },
            "error": None,
        }


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError("generated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("generated_at requires a timezone")
    return value


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 6)


def _latency(samples: list[float]) -> dict[str, Any]:
    return {
        "samples": samples,
        "p50": _percentile(samples, 0.50),
        "p95": _percentile(samples, 0.95),
        "max": round(max(samples), 6),
    }


def _project_graph(
    snapshot: dict[str, Any],
    *,
    signer: HMACExternalStateProjectionSigner,
    request_id: str,
) -> dict[str, Any]:
    provider = ExternalStateProjectionProvider(
        _Source(snapshot),
        provider_id="provider-m9-07-benchmark",
        signer=signer,
    )
    response = provider.call_tool(
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
        raise ValueError("benchmark source projection failed")
    return build_project_graph_projection(
        response["result"],
        signer=signer,
        observed_at="2026-08-17T18:00:00+08:00",
    )


def _request(
    snapshot: dict[str, Any],
    *,
    focus: list[str] | None = None,
    relations: list[str] | None = None,
    node_kinds: list[str] | None = None,
    max_nodes: int = 64,
    max_edges: int = 128,
) -> dict[str, Any]:
    return {
        "schema_version": "context.relationship-impact-view-request/v1alpha1",
        "project_id": snapshot["project"]["project_id"],
        "expected_state_revision": snapshot["project"]["revision"],
        "focus_node_ids": focus or [],
        "direction": "both",
        "max_depth": 2,
        "relation_kinds": relations or ["dependency", "parent"],
        "node_kinds": node_kinds or ["work"],
        "include_terminal_work": True,
        "max_nodes": max_nodes,
        "max_edges": max_edges,
    }


def _base_snapshot() -> dict[str, Any]:
    snapshot = build_idea_snapshot()
    by_id = {work["work_id"]: work for work in snapshot["works"]}
    by_id["work-target"]["dependency_ids"] = ["work-active"]
    return snapshot


def _scale_snapshot() -> dict[str, Any]:
    snapshot = _base_snapshot()
    template = next(
        work for work in snapshot["works"] if work["work_id"] == "work-target"
    )
    previous = "work-target"
    for index in range(_SCALE_WORK_NODES - len(snapshot["works"])):
        work = copy.deepcopy(template)
        work_id = f"scale-work-{index:04d}"
        work.update(
            {
                "work_id": work_id,
                "title": work_id,
                "status": "ready",
                "parent_work_id": "goal",
                "dependency_ids": [previous],
                "scope_refs": [{"scope_kind": "capability", "scope_ref": work_id}],
                "overlap_candidate_ids": [],
                "dedupe_status": "clear",
                "supersedes_work_id": None,
                "evidence_ids": [],
                "blocker_ids": [],
                "return_point_work_id": None,
                "promotion_target_work_id": None,
                "mainline_authority": True,
            }
        )
        snapshot["works"].append(work)
        previous = work_id
    return snapshot


def _resign(
    projection: dict[str, Any],
    *,
    signer: HMACExternalStateProjectionSigner,
) -> None:
    unsigned = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    projection["projection_sha256"] = _digest(unsigned)
    projection["signature"] = signer.sign(projection)


def _failed_gates(receipt: dict[str, Any]) -> list[str]:
    iterations = receipt["parameters"]["iterations"]
    scale_iterations = receipt["parameters"]["scale_iterations"]
    results = receipt["results"]
    failed: list[str] = []
    for field in (
        "same_revision_matches",
        "complete_projection_matches",
        "focus_direction_matches",
        "code_clock_separation_matches",
        "tamper_rejections",
        "filter_rejections",
    ):
        if results[field] != iterations:
            failed.append(field)
    if results["scale_complete_matches"] != scale_iterations:
        failed.append("scale_complete_matches")
    for field in (
        "authority_violations",
        "provider_invocations",
        "external_services",
    ):
        if results[field] != 0:
            failed.append(field)
    if receipt["latency_ms"]["p95"] > _THRESHOLDS["projection_latency_p95_ms_max"]:
        failed.append("projection_latency_p95_ms")
    if receipt["scale_latency_ms"]["p95"] > _THRESHOLDS["scale_latency_p95_ms_max"]:
        failed.append("scale_latency_p95_ms")
    return failed


def benchmark_relationship_impact_projection(
    *,
    root: Path,
    iterations: int = 1_000,
    scale_iterations: int = 25,
    generated_at: str = "2026-08-17T23:30:00+08:00",
) -> dict[str, Any]:
    """Measure correctness, fail-closed behavior and bounded graph latency."""
    root = root.resolve()
    if type(iterations) is not int or not 1 <= iterations <= 10_000:
        raise ValueError("iterations must be between 1 and 10000")
    if type(scale_iterations) is not int or not 1 <= scale_iterations <= 100:
        raise ValueError("scale_iterations must be between 1 and 100")
    _timestamp(generated_at)
    signer = HMACExternalStateProjectionSigner(
        key_id="key-m9-07-benchmark",
        secret=b"m9-07-relationship-impact-benchmark-key",
    )
    snapshot = _base_snapshot()
    project_graph = _project_graph(
        snapshot,
        signer=signer,
        request_id="request-m9-07-benchmark",
    )
    codegraph_path = root / "experiments/retrieval/m6-02-codegraph-verification.json"
    codegraph = json.loads(codegraph_path.read_text(encoding="utf-8"))
    full_request = _request(snapshot)
    focus_request = _request(snapshot, focus=["work:work-active"])
    code_request = _request(
        snapshot,
        relations=["parent", "references"],
        node_kinds=["symbol", "work"],
    )
    results = {field: 0 for field in _RESULT_FIELDS}
    latency_samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        full = build_relationship_impact_projection(
            project_graph,
            view_request=full_request,
            signer=signer,
        )
        validate_relationship_impact_projection(
            full,
            project_graph_projection=project_graph,
            view_request=full_request,
            signer=signer,
        )
        focused = build_relationship_impact_projection(
            project_graph,
            view_request=focus_request,
            signer=signer,
        )
        code = build_relationship_impact_projection(
            project_graph,
            view_request=code_request,
            signer=signer,
            codegraph_receipts=[codegraph],
            codegraph_roots={codegraph["receipt_sha256"]: root},
        )
        latency_samples.append(round((time.perf_counter_ns() - started) / 1e6, 6))
        if all(
            projection["state_revision"] == snapshot["project"]["revision"]
            and projection["state_sha256"] == project_graph["state_sha256"]
            for projection in (full, focused, code)
        ):
            results["same_revision_matches"] += 1
        if (
            full["completeness"]["returned_node_count"] == len(snapshot["works"])
            and full["completeness"]["truncated"] is False
            and full["completeness"]["impact_complete"] is True
        ):
            results["complete_projection_matches"] += 1
        if focused["impact"]["direct_dependent_work_ids"] == [
            "work-target"
        ] and "work:work-target" in {node["node_id"] for node in focused["nodes"]}:
            results["focus_direction_matches"] += 1
        if (
            len(code["code_sources"]) == 1
            and code["code_sources"][0]["state_revision_binding"] is None
            and code["code_sources"][0]["index_revisions"]
            == ["worktree:m6-codegraph-probe"]
            and any(edge["relation"] == "references" for edge in code["edges"])
        ):
            results["code_clock_separation_matches"] += 1
        tampered = copy.deepcopy(full)
        tampered["nodes"][0]["title"] = "forged"
        _resign(tampered, signer=signer)
        try:
            validate_relationship_impact_projection(
                tampered,
                project_graph_projection=project_graph,
                view_request=full_request,
                signer=signer,
            )
        except RelationshipImpactProjectionError:
            results["tamper_rejections"] += 1
        stale = copy.deepcopy(full_request)
        stale["expected_state_revision"] += 1
        try:
            build_relationship_impact_projection(
                project_graph,
                view_request=stale,
                signer=signer,
            )
        except RelationshipImpactProjectionError:
            results["filter_rejections"] += 1
        for projection in (full, focused, code):
            if projection["authority"] != _AUTHORITY:
                results["authority_violations"] += 1

    scale_snapshot = _scale_snapshot()
    scale_graph = _project_graph(
        scale_snapshot,
        signer=signer,
        request_id="request-m9-07-benchmark-scale",
    )
    scale_request = _request(
        scale_snapshot,
        max_nodes=2_000,
        max_edges=5_000,
    )
    scale_latency_samples: list[float] = []
    for _ in range(scale_iterations):
        started = time.perf_counter_ns()
        scale = build_relationship_impact_projection(
            scale_graph,
            view_request=scale_request,
            signer=signer,
        )
        validate_relationship_impact_projection(
            scale,
            project_graph_projection=scale_graph,
            view_request=scale_request,
            signer=signer,
        )
        scale_latency_samples.append(round((time.perf_counter_ns() - started) / 1e6, 6))
        if (
            scale["completeness"]["returned_node_count"] == _SCALE_WORK_NODES
            and scale["completeness"]["returned_edge_count"] <= 5_000
            and scale["completeness"]["truncated"] is False
        ):
            results["scale_complete_matches"] += 1

    rates = {
        "same_revision_rate": results["same_revision_matches"] / iterations,
        "complete_projection_rate": results["complete_projection_matches"] / iterations,
        "focus_direction_rate": results["focus_direction_matches"] / iterations,
        "code_clock_separation_rate": results["code_clock_separation_matches"]
        / iterations,
        "tamper_rejection_rate": results["tamper_rejections"] / iterations,
        "filter_rejection_rate": results["filter_rejections"] / iterations,
        "scale_complete_rate": results["scale_complete_matches"] / scale_iterations,
    }
    receipt: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "generated_at": generated_at,
        "parameters": {
            "iterations": iterations,
            "scale_iterations": scale_iterations,
            "scale_work_nodes": _SCALE_WORK_NODES,
            "thresholds": copy.deepcopy(_THRESHOLDS),
        },
        "results": results,
        "rates": rates,
        "latency_ms": _latency(latency_samples),
        "scale_latency_ms": _latency(scale_latency_samples),
        "provenance": {
            "implementation_sha256": _file_sha256(
                root / "context_control_plane/relationship_impact_projection.py"
            ),
            "benchmark_implementation_sha256": _file_sha256(
                root / "context_control_plane/relationship_impact_benchmark.py"
            ),
            "project_graph_implementation_sha256": _file_sha256(
                root / "context_control_plane/project_graph_projection.py"
            ),
            "view_request_schema_sha256": _file_sha256(
                root / "schemas/m9-07/relationship-impact-view-request.schema.json"
            ),
            "projection_schema_sha256": _file_sha256(
                root / "schemas/m9-07/relationship-impact-projection.schema.json"
            ),
            "codegraph_receipt_sha256": _file_sha256(codegraph_path),
        },
        "authority": copy.deepcopy(_AUTHORITY),
        "failed_gates": [],
        "verdict": "passed",
    }
    receipt["failed_gates"] = _failed_gates(receipt)
    receipt["verdict"] = "passed" if not receipt["failed_gates"] else "failed"
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def _validate_latency(value: Any, *, count: int, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {"samples", "p50", "p95", "max"}:
        raise ValueError(f"{field} fields are invalid")
    samples = value["samples"]
    if (
        not isinstance(samples, list)
        or len(samples) != count
        or any(
            type(sample) not in {int, float} or not math.isfinite(sample) or sample < 0
            for sample in samples
        )
    ):
        raise ValueError(f"{field} samples are invalid")
    expected = _latency([float(sample) for sample in samples])
    if value != expected:
        raise ValueError(f"{field} summary does not match samples")


def validate_relationship_impact_benchmark(
    receipt: Any,
    *,
    root: Path,
) -> None:
    """Validate a receipt against current code and recomputed gate outcomes."""
    root = root.resolve()
    if not isinstance(receipt, dict) or set(receipt) != _TOP_FIELDS:
        raise ValueError("benchmark receipt fields are invalid")
    if (
        receipt["schema_version"] != BENCHMARK_SCHEMA_VERSION
        or receipt["benchmark_id"] != BENCHMARK_ID
    ):
        raise ValueError("benchmark identity is invalid")
    _timestamp(receipt["generated_at"])
    parameters = receipt["parameters"]
    if not isinstance(parameters, dict) or set(parameters) != {
        "iterations",
        "scale_iterations",
        "scale_work_nodes",
        "thresholds",
    }:
        raise ValueError("benchmark parameters are invalid")
    iterations = parameters["iterations"]
    scale_iterations = parameters["scale_iterations"]
    if type(iterations) is not int or not 1 <= iterations <= 10_000:
        raise ValueError("benchmark iterations are invalid")
    if type(scale_iterations) is not int or not 1 <= scale_iterations <= 100:
        raise ValueError("benchmark scale iterations are invalid")
    if (
        parameters["scale_work_nodes"] != _SCALE_WORK_NODES
        or parameters["thresholds"] != _THRESHOLDS
    ):
        raise ValueError("benchmark thresholds are invalid")
    results = receipt["results"]
    if not isinstance(results, dict) or set(results) != _RESULT_FIELDS:
        raise ValueError("benchmark results are invalid")
    if any(type(value) is not int or value < 0 for value in results.values()):
        raise ValueError("benchmark result counts are invalid")
    rates = receipt["rates"]
    if not isinstance(rates, dict) or set(rates) != _RATE_FIELDS:
        raise ValueError("benchmark rates are invalid")
    expected_rates = {
        "same_revision_rate": results["same_revision_matches"] / iterations,
        "complete_projection_rate": results["complete_projection_matches"] / iterations,
        "focus_direction_rate": results["focus_direction_matches"] / iterations,
        "code_clock_separation_rate": results["code_clock_separation_matches"]
        / iterations,
        "tamper_rejection_rate": results["tamper_rejections"] / iterations,
        "filter_rejection_rate": results["filter_rejections"] / iterations,
        "scale_complete_rate": results["scale_complete_matches"] / scale_iterations,
    }
    if rates != expected_rates:
        raise ValueError("benchmark rates do not match counts")
    _validate_latency(receipt["latency_ms"], count=iterations, field="latency_ms")
    _validate_latency(
        receipt["scale_latency_ms"],
        count=scale_iterations,
        field="scale_latency_ms",
    )
    codegraph_path = root / "experiments/retrieval/m6-02-codegraph-verification.json"
    expected_provenance = {
        "implementation_sha256": _file_sha256(
            root / "context_control_plane/relationship_impact_projection.py"
        ),
        "benchmark_implementation_sha256": _file_sha256(
            root / "context_control_plane/relationship_impact_benchmark.py"
        ),
        "project_graph_implementation_sha256": _file_sha256(
            root / "context_control_plane/project_graph_projection.py"
        ),
        "view_request_schema_sha256": _file_sha256(
            root / "schemas/m9-07/relationship-impact-view-request.schema.json"
        ),
        "projection_schema_sha256": _file_sha256(
            root / "schemas/m9-07/relationship-impact-projection.schema.json"
        ),
        "codegraph_receipt_sha256": _file_sha256(codegraph_path),
    }
    if receipt["provenance"] != expected_provenance:
        raise ValueError("benchmark provenance does not match current files")
    if receipt["authority"] != _AUTHORITY:
        raise ValueError("benchmark authority must remain zero")
    expected_failed = _failed_gates(receipt)
    if receipt["failed_gates"] != expected_failed:
        raise ValueError("benchmark failed gates are inaccurate")
    expected_verdict = "passed" if not expected_failed else "failed"
    if receipt["verdict"] != expected_verdict or expected_verdict != "passed":
        raise ValueError("benchmark acceptance gates did not pass")
    if receipt["receipt_sha256"] != _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    ):
        raise ValueError("benchmark receipt digest is invalid")


__all__ = [
    "BENCHMARK_ID",
    "BENCHMARK_SCHEMA_VERSION",
    "benchmark_relationship_impact_projection",
    "validate_relationship_impact_benchmark",
]
