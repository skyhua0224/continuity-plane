"""Repeatable local benchmark for the M3-04 side-effect scope gate."""

from __future__ import annotations

import copy
import hashlib
import platform
import statistics
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .effect_scope_gate import evaluate_effect_scope_gate


_PROVENANCE_PATHS = {
    "implementation_sha256": "context_control_plane/effect_scope_gate.py",
    "benchmark_sha256": "context_control_plane/effect_scope_benchmark.py",
    "fixture_sha256": "experiments/state/m2-01-core-fixtures.yaml",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], index: int) -> float:
    return round(sorted(values)[index], 6)


def benchmark_effect_scope_gate(
    snapshot: dict[str, Any],
    *,
    samples: int,
    pending_effect_counts: tuple[int, ...] = (1, 100, 1_000),
    conflict_samples: int | None = None,
    observed_at: str,
    root: str | Path,
) -> dict[str, Any]:
    """Measure equal allow/deny probes without mutating the input state."""
    if type(samples) is not int or samples <= 0 or samples % 2:
        raise ValueError("samples must be a positive even integer")
    if (
        not isinstance(pending_effect_counts, tuple)
        or not pending_effect_counts
        or any(type(count) is not int or count <= 0 for count in pending_effect_counts)
        or tuple(sorted(set(pending_effect_counts))) != pending_effect_counts
    ):
        raise ValueError("pending_effect_counts must be sorted unique positive integers")
    if conflict_samples is None:
        conflict_samples = samples
    if (
        type(conflict_samples) is not int
        or conflict_samples <= 0
        or conflict_samples % 2
    ):
        raise ValueError("conflict_samples must be a positive even integer")
    parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    root = Path(root)
    before = copy.deepcopy(snapshot)
    benchmark_state = copy.deepcopy(snapshot)
    benchmark_state["effects"] = []
    benchmark_state["schema_version"] = "context.typed-state/v2alpha1"
    work = benchmark_state["works"][0]
    claim = benchmark_state["claims"][0]
    for value in (*work["scope_refs"], *claim["scope_owners"]):
        if value["scope_kind"] in {"repo", "directory", "file", "symbol"}:
            value["scope_ref"] = f"repo://benchmark/{value['scope_ref']}"
    claim["lease_expires_at"] = "2026-12-31T23:59:59+00:00"
    allowed = 0
    denied = 0
    latencies: list[float] = []
    for sample in range(samples):
        requested_scope = (
            copy.deepcopy(claim["scope_owners"][0])
            if sample % 2 == 0
            else {"scope_kind": "file", "scope_ref": "repo://benchmark/src/unrelated.py"}
        )
        started = time.perf_counter_ns()
        verdict = evaluate_effect_scope_gate(
            benchmark_state,
            actor_ref=claim["actor_ref"],
            work_id=work["work_id"],
            claim_id=claim["claim_id"],
            expected_revision=benchmark_state["project"]["revision"],
            operation="write-file",
            requested_scope=requested_scope,
            effect_id=f"effect-benchmark-{sample}",
            observed_at=observed_at,
        )
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        if verdict["decision"] == "allow":
            allowed += 1
        else:
            denied += 1
    p95_index = max(0, int(samples * 0.95) - 1)
    baseline = {
        "samples": samples,
        "allowed": allowed,
        "read_only_denied": denied,
        "state_mutations": 0 if snapshot == before else 1,
        "p50_gate_latency_ms": round(statistics.median(latencies), 6),
        "p95_gate_latency_ms": _percentile(latencies, p95_index),
        "max_gate_latency_ms": round(max(latencies), 6),
    }
    requested_scope = copy.deepcopy(claim["scope_owners"][0])
    conflict_loads = []
    conflict_p95_index = max(0, int(conflict_samples * 0.95) - 1)
    for count in pending_effect_counts:
        conflict_state = copy.deepcopy(benchmark_state)
        conflict_state["effects"] = [
            {
                "effect_id": f"effect-pending-{index}",
                "status": "authorized",
                "scope_ref": (
                    copy.deepcopy(requested_scope)
                    if index == count - 1
                    else {
                        "scope_kind": "file",
                        "scope_ref": f"repo://benchmark/src/disjoint-{index}.py",
                    }
                ),
            }
            for index in range(count)
        ]
        conflict_latencies = []
        conflict_allowed = 0
        conflict_denied = 0
        reasons = set()
        for sample in range(conflict_samples):
            started = time.perf_counter_ns()
            verdict = evaluate_effect_scope_gate(
                conflict_state,
                actor_ref=claim["actor_ref"],
                work_id=work["work_id"],
                claim_id=claim["claim_id"],
                expected_revision=conflict_state["project"]["revision"],
                operation="write-file",
                requested_scope=requested_scope,
                effect_id=f"effect-conflict-{sample}",
                observed_at=observed_at,
            )
            conflict_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
            reasons.add(verdict["reason"])
            if verdict["decision"] == "allow":
                conflict_allowed += 1
            else:
                conflict_denied += 1
        conflict_loads.append(
            {
                "pending_effects": count,
                "samples": conflict_samples,
                "allowed": conflict_allowed,
                "read_only_denied": conflict_denied,
                "p50_gate_latency_ms": round(statistics.median(conflict_latencies), 6),
                "p95_gate_latency_ms": _percentile(
                    conflict_latencies, conflict_p95_index
                ),
                "max_gate_latency_ms": round(max(conflict_latencies), 6),
                "reason": next(iter(reasons)) if len(reasons) == 1 else "mixed",
            }
        )
    return {
        "schema_version": "context.effect-scope-benchmark/v1alpha1",
        "observed_at": observed_at,
        "provenance": {
            field: _sha256(root / relative)
            for field, relative in _PROVENANCE_PATHS.items()
        },
        "environment": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "machine": platform.machine(),
            "external_services": 0,
        },
        "measurement": {
            "baseline": baseline,
            "conflict_loads": conflict_loads,
        },
        "acceptance": {
            "all_authorized_allowed": allowed == samples // 2,
            "all_out_of_scope_read_only": denied == samples // 2,
            "all_conflict_loads_read_only": all(
                item["allowed"] == 0
                and item["read_only_denied"] == conflict_samples
                and item["reason"] == "effect_scope_conflict"
                for item in conflict_loads
            ),
            "state_unchanged": snapshot == before,
            "baseline_p95_gate_latency_ms_max": 1.0,
        },
    }


def validate_effect_scope_benchmark_receipt(
    receipt: dict[str, Any], *, root: str | Path
) -> None:
    """Validate benchmark structure, provenance and acceptance counters."""
    expected_fields = {
        "schema_version",
        "observed_at",
        "provenance",
        "environment",
        "measurement",
        "acceptance",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_fields:
        raise ValueError("effect scope benchmark fields are invalid")
    if receipt["schema_version"] != "context.effect-scope-benchmark/v1alpha1":
        raise ValueError("effect scope benchmark schema_version is invalid")
    root = Path(root)
    try:
        observed_at = datetime.fromisoformat(receipt["observed_at"].replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("effect scope benchmark observed_at is invalid") from exc
    if observed_at.tzinfo is None:
        raise ValueError("effect scope benchmark observed_at must include a timezone")
    expected_provenance = {
        field: _sha256(root / relative)
        for field, relative in _PROVENANCE_PATHS.items()
    }
    if receipt["provenance"] != expected_provenance:
        raise ValueError("effect scope benchmark provenance mismatch")
    environment = receipt["environment"]
    if not isinstance(environment, dict) or environment.get("external_services") != 0:
        raise ValueError("effect scope benchmark must use zero external services")
    measurement = receipt["measurement"]
    if not isinstance(measurement, dict) or set(measurement) != {
        "baseline",
        "conflict_loads",
    }:
        raise ValueError("effect scope benchmark measurement fields are invalid")
    baseline = measurement["baseline"]
    samples = baseline.get("samples")
    if type(samples) is not int or samples <= 0 or samples % 2:
        raise ValueError("effect scope benchmark sample count is invalid")
    if (
        baseline.get("allowed") != samples // 2
        or baseline.get("read_only_denied") != samples // 2
        or baseline.get("state_mutations") != 0
        or baseline.get("p95_gate_latency_ms", 1.0) >= 1.0
        or baseline.get("p50_gate_latency_ms", -1) < 0
        or baseline.get("max_gate_latency_ms", -1)
        < baseline.get("p95_gate_latency_ms", 0)
    ):
        raise ValueError("effect scope benchmark measurement failed acceptance")
    conflict_loads = measurement["conflict_loads"]
    if not isinstance(conflict_loads, list) or not conflict_loads:
        raise ValueError("effect scope benchmark conflict loads are invalid")
    previous_count = 0
    for load in conflict_loads:
        if not isinstance(load, dict) or set(load) != {
            "pending_effects", "samples", "allowed", "read_only_denied",
            "p50_gate_latency_ms", "p95_gate_latency_ms", "max_gate_latency_ms", "reason",
        }:
            raise ValueError("effect scope benchmark conflict load fields are invalid")
        count = load["pending_effects"]
        if type(count) is not int or count <= previous_count:
            raise ValueError("effect scope benchmark pending cardinality is invalid")
        previous_count = count
        if (
            type(load["samples"]) is not int
            or load["samples"] <= 0
            or load["samples"] % 2
            or load["allowed"] != 0
            or load["read_only_denied"] != load["samples"]
            or load["reason"] != "effect_scope_conflict"
            or load["p50_gate_latency_ms"] < 0
            or load["max_gate_latency_ms"] < load["p95_gate_latency_ms"]
        ):
            raise ValueError("effect scope benchmark conflict load failed acceptance")
    if receipt["acceptance"] != {
        "all_authorized_allowed": True,
        "all_out_of_scope_read_only": True,
        "all_conflict_loads_read_only": True,
        "state_unchanged": True,
        "baseline_p95_gate_latency_ms_max": 1.0,
    }:
        raise ValueError("effect scope benchmark acceptance flags are invalid")
