"""Offline replay and veto benchmark for M5-07 project dogfooding."""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from .context_trace import LocalContextTraceEmitter
from .dogfood_emitter import (
    DogfoodCoverageError,
    DogfoodObservationEmitter,
    assess_dogfood_coverage,
    canonical_dogfood_observation_bytes,
    require_dogfood_coverage_gate,
)


SCHEMA_VERSION = "context.dogfood-emitter-benchmark/v1alpha1"
_COVERAGE_KEYS = {
    "eligible_ingress",
    "visible_compaction",
    "skill_selection",
    "plan_revision",
    "agent_dispatch",
    "agent_handoff",
    "delivery",
}
_VETO_KINDS = {
    "acknowledged-input-replay",
    "first-action-mismatch",
    "late-canary",
    "missing-handoff",
}
_FIELDS = {
    "schema_version",
    "generated_at",
    "samples",
    "successful_samples",
    "emitted_events",
    "replay_mismatch",
    "coverage_millionths",
    "overall_coverage_millionths",
    "veto_detection_samples",
    "veto_detections",
    "veto_detections_by_kind",
    "authority_violations",
    "p50_ms",
    "p95_ms",
    "max_ms",
    "external_services",
    "fixture_sha256",
    "implementation_sha256",
    "trace_implementation_sha256",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)


def _expectations() -> dict[str, list[Any]]:
    return {
        "eligible_ingress_ids": ["ingress-1", "idea-1"],
        "visible_compaction_ids": ["compaction-1"],
        "skill_selection_ids": ["skill-selection-1"],
        "target_revisions": [54],
        "agent_dispatch_ids": ["dispatch-1"],
        "delivery_ids": ["delivery-1"],
    }


def _sample(
    *,
    omit_handoff: bool = False,
    late_canary: bool = False,
    first_action_match: bool = True,
    acknowledged_input_replayed: bool = False,
) -> tuple[list[dict[str, Any]], tuple[dict[str, Any], ...]]:
    trace = LocalContextTraceEmitter(
        binding={
            "project_id": "project-context-control-plane",
            "state_revision": 54,
            "active_work_id": "M5-07",
            "trace_id": "1" * 32,
            "span_id": "2" * 16,
            "run_id": "run-m5-07-benchmark",
            "operation_id": "operation-m5-campaign",
            "correlation_id": "campaign-M5",
        },
        source={"kind": "local-emitter", "component": "context.dogfood"},
    )
    emitter = DogfoodObservationEmitter(trace)

    def emit(event_kind: str, subject_id: str, **overrides: Any) -> dict[str, Any]:
        values: dict[str, Any] = {
            "event_kind": event_kind,
            "subject_id": subject_id,
            "active_work_before": "M5-07",
            "active_work_after": "M5-07",
            "return_point_work_id": "M5-07",
            "route": None,
            "interrupted": False,
            "candidate_only": False,
            "acknowledged_input_replayed": False,
            "first_action_match": True,
            "target_revision": 54,
            "canary_sequence": None,
            "first_side_effect_sequence": None,
            "provider_metrics_status": "unavailable",
            "evidence_refs": [f"run://m5-07/{subject_id}"],
            "observed_at": "2026-08-15T05:00:00+08:00",
        }
        values.update(overrides)
        return emitter.emit(**values)

    observations = [
        emit("input-routing", "ingress-1", route="continue"),
        emit(
            "input-routing",
            "idea-1",
            route="capture-and-continue",
            candidate_only=True,
        ),
        emit(
            "compaction",
            "compaction-1",
            canary_sequence=21 if late_canary else 20,
            first_side_effect_sequence=21,
            first_action_match=first_action_match,
            acknowledged_input_replayed=acknowledged_input_replayed,
        ),
        emit("skill-selection", "skill-selection-1"),
        emit("plan-revision", "revision-54"),
        emit("agent-dispatch", "dispatch-1"),
    ]
    if not omit_handoff:
        observations.append(emit("agent-handoff", "dispatch-1"))
    observations.append(emit("delivery", "delivery-1"))
    return observations, trace.events


def _fault_detected(kind: str) -> bool:
    options = {
        "missing-handoff": {"omit_handoff": True},
        "late-canary": {"late_canary": True},
        "first-action-mismatch": {"first_action_match": False},
        "acknowledged-input-replay": {"acknowledged_input_replayed": True},
    }[kind]
    observations, _ = _sample(**options)
    receipt = assess_dogfood_coverage(observations, expectations=_expectations())
    try:
        require_dogfood_coverage_gate(receipt)
    except DogfoodCoverageError:
        return receipt["status"] == "regressed" and bool(receipt["veto_failures"])
    return False


def benchmark_dogfood_emitter(
    *, samples: int = 1000, generated_at: str = "2026-08-15T05:30:00Z"
) -> dict[str, Any]:
    """Measure deterministic local emission and all M5-07 veto conditions."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")

    expected_replay: bytes | None = None
    durations: list[float] = []
    successful_samples = 0
    emitted_events = 0
    replay_mismatch = 0
    authority_violations = 0
    coverage_floor = {key: 1_000_000 for key in _COVERAGE_KEYS}
    veto_detections_by_kind = {key: 0 for key in _VETO_KINDS}

    for _ in range(samples):
        started = time.perf_counter()
        observations, trace_events = _sample()
        coverage = assess_dogfood_coverage(observations, expectations=_expectations())
        require_dogfood_coverage_gate(coverage)
        durations.append((time.perf_counter() - started) * 1000)
        successful_samples += 1
        emitted_events += len(trace_events)
        replay = b"\n".join(canonical_dogfood_observation_bytes(item) for item in observations)
        if expected_replay is None:
            expected_replay = replay
        elif replay != expected_replay:
            replay_mismatch += 1
        for key, value in coverage["coverage_millionths"].items():
            coverage_floor[key] = min(coverage_floor[key], value)
        authority_violations += sum(
            item["state_write_authority"] is not False
            or item["provider_native_authority"] is not False
            for item in observations
        )
        authority_violations += sum(event["authority"] is not False for event in trace_events)
        for kind in sorted(_VETO_KINDS):
            veto_detections_by_kind[kind] += int(_fault_detected(kind))

    root = Path(__file__).parents[1]
    fixture = {"expectations": _expectations(), "events_per_sample": 8, "veto_kinds": sorted(_VETO_KINDS)}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": successful_samples,
        "emitted_events": emitted_events,
        "replay_mismatch": replay_mismatch,
        "coverage_millionths": dict(sorted(coverage_floor.items())),
        "overall_coverage_millionths": min(coverage_floor.values()),
        "veto_detection_samples": samples * len(_VETO_KINDS),
        "veto_detections": sum(veto_detections_by_kind.values()),
        "veto_detections_by_kind": dict(sorted(veto_detections_by_kind.items())),
        "authority_violations": authority_violations,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "external_services": 0,
        "fixture_sha256": hashlib.sha256(_canonical(fixture)).hexdigest(),
        "implementation_sha256": _sha256_file(root / "context_control_plane/dogfood_emitter.py"),
        "trace_implementation_sha256": _sha256_file(root / "context_control_plane/context_trace.py"),
    }


def validate_dogfood_emitter_benchmark(
    receipt: Any, *, root: Path | None = None
) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS:
        raise ValueError("M5-07 benchmark fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("M5-07 benchmark schema_version is unsupported")
    if not isinstance(receipt["generated_at"], str) or not receipt["generated_at"].endswith("Z"):
        raise ValueError("M5-07 benchmark generated_at is invalid")
    integer_fields = {
        "samples",
        "successful_samples",
        "emitted_events",
        "replay_mismatch",
        "overall_coverage_millionths",
        "veto_detection_samples",
        "veto_detections",
        "authority_violations",
        "external_services",
    }
    if any(type(receipt[field]) is not int or receipt[field] < 0 for field in integer_fields):
        raise ValueError("M5-07 benchmark integer fields are invalid")
    if receipt["samples"] <= 0 or receipt["successful_samples"] != receipt["samples"]:
        raise ValueError("M5-07 benchmark requires every sample to pass")
    if receipt["emitted_events"] != receipt["samples"] * 8:
        raise ValueError("M5-07 event denominator is incomplete")
    coverage = receipt["coverage_millionths"]
    if not isinstance(coverage, dict) or set(coverage) != _COVERAGE_KEYS:
        raise ValueError("M5-07 coverage fields are invalid")
    if any(value != 1_000_000 for value in coverage.values()):
        raise ValueError("M5-07 event coverage regressed")
    if receipt["overall_coverage_millionths"] != min(coverage.values()):
        raise ValueError("M5-07 overall coverage is invalid")
    detections = receipt["veto_detections_by_kind"]
    if not isinstance(detections, dict) or set(detections) != _VETO_KINDS:
        raise ValueError("M5-07 veto kinds are invalid")
    if any(value != receipt["samples"] for value in detections.values()):
        raise ValueError("M5-07 veto detection is incomplete")
    if (
        receipt["veto_detection_samples"] != receipt["samples"] * len(_VETO_KINDS)
        or receipt["veto_detections"] != sum(detections.values())
        or receipt["veto_detections"] != receipt["veto_detection_samples"]
    ):
        raise ValueError("M5-07 veto denominator is invalid")
    if any(receipt[field] != 0 for field in ("replay_mismatch", "authority_violations", "external_services")):
        raise ValueError("M5-07 correctness veto failed")
    for field in ("p50_ms", "p95_ms", "max_ms"):
        if isinstance(receipt[field], bool) or not isinstance(receipt[field], (int, float)) or receipt[field] < 0:
            raise ValueError(f"M5-07 benchmark {field} is invalid")
    if not receipt["p50_ms"] <= receipt["p95_ms"] <= receipt["max_ms"]:
        raise ValueError("M5-07 latency order is invalid")
    for field in ("fixture_sha256", "implementation_sha256", "trace_implementation_sha256"):
        value = receipt[field]
        if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"M5-07 benchmark {field} is invalid")
    if root is not None:
        expected = {
            "implementation_sha256": _sha256_file(root / "context_control_plane/dogfood_emitter.py"),
            "trace_implementation_sha256": _sha256_file(root / "context_control_plane/context_trace.py"),
        }
        for field, digest in expected.items():
            if receipt[field] != digest:
                raise ValueError(f"M5-07 benchmark {field} does not match current code")
