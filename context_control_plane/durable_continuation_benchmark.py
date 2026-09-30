"""Offline replay and fault benchmark for M5-08 durable continuation."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Callable

from .durable_continuation import (
    CONTINUATION_FIELD_COUNT,
    DurableContinuationError,
    canonical_durable_recovery_bytes,
    compose_durable_continuation,
    evaluate_durable_recovery,
    validate_durable_recovery_receipt,
)


SCHEMA_VERSION = "context.durable-continuation-benchmark/v1alpha1"
FAULT_NAMES = (
    "acknowledged_input",
    "event_head",
    "first_action",
    "never_replay",
    "operation",
    "phase",
    "project_revision",
    "recovery_budget",
    "task_revision",
    "unreserved_effect",
)
_BENCHMARK_FIELDS = {
    "schema_version",
    "generated_at",
    "samples",
    "successful_samples",
    "replay_mismatch",
    "first_action_mismatches",
    "acknowledged_input_replays",
    "continuation_field_mismatches",
    "continuation_fields_total",
    "continuation_fields_recovered",
    "fault_samples",
    "fault_rejections",
    "faults",
    "recovery_budget_bytes",
    "max_recovery_bytes",
    "authority_violations",
    "p50_ms",
    "p95_ms",
    "max_ms",
    "external_services",
    "fixture_sha256",
    "implementation_sha256",
    "benchmark_sha256",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def durable_continuation_fixture() -> dict[str, Any]:
    """Build a fixed provider-neutral fixture from the Pi protocol field oracle."""
    authority = {
        "operation_id": "operation/m5-08/benchmark",
        "project_id": "project-context-control-plane",
        "project_revision": 54,
        "task_id": "M5-08",
        "task_revision": 3,
        "event_head": {"sequence_no": 91, "event_sha256": "a" * 64},
    }
    state = compose_durable_continuation(
        **authority,
        phase="effect-in-flight",
        last_durable_action="effect-intent:effect/m5-08/benchmark",
        next_action="verify-effect:effect/m5-08/benchmark",
        acknowledged_input_ids=["input/m5-08/already-answered"],
        reserved_effects=[
            {
                "effect_id": "effect/m5-08/benchmark",
                "replay_policy": "never",
                "status": "started",
            }
        ],
        response_mode="continue-silently",
    )
    recovery_reads = [
        {
            "source_ref": "artifact://sha256/" + "b" * 64,
            "content_sha256": "b" * 64,
            "bytes_read": 2816,
        },
        {
            "source_ref": "state://project-context-control-plane/revision/54",
            "content_sha256": "c" * 64,
            "bytes_read": 512,
        },
    ]
    return {
        "fixture_version": "context.durable-continuation-fixture/v1alpha1",
        "authority": authority,
        "state": state,
        "proposed_first_action": state["next_action"],
        "requested_effect_id": "effect/m5-08/benchmark",
        "recovery_reads": recovery_reads,
        "recovery_bytes": sum(item["bytes_read"] for item in recovery_reads),
        "recovery_budget_bytes": 4096,
    }


def _evaluate(fixture: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    arguments = {
        "trusted_state": copy.deepcopy(fixture["state"]),
        "restored_state": copy.deepcopy(fixture["state"]),
        "trusted_authority": copy.deepcopy(fixture["authority"]),
        "proposed_first_action": fixture["proposed_first_action"],
        "response_input_id": None,
        "requested_effect_id": fixture["requested_effect_id"],
        "replay_requested": False,
        "recovery_reads": copy.deepcopy(fixture["recovery_reads"]),
        "recovery_budget_bytes": fixture["recovery_budget_bytes"],
        "observed_at": "2026-08-15T04:00:00+08:00",
    }
    arguments.update(overrides)
    return evaluate_durable_recovery(**arguments)


def _fault_cases(fixture: dict[str, Any]) -> list[tuple[str, Callable[[], None]]]:
    def authority_fault(field: str, value: Any) -> Callable[[], None]:
        def run() -> None:
            authority = copy.deepcopy(fixture["authority"])
            authority[field] = value
            _evaluate(fixture, trusted_authority=authority)

        return run

    def phase_fault() -> None:
        restored = copy.deepcopy(fixture["state"])
        restored["phase"] = "prepared"
        _evaluate(fixture, restored_state=restored)

    return [
        (
            "acknowledged_input",
            lambda: _evaluate(
                fixture, response_input_id="input/m5-08/already-answered"
            ),
        ),
        (
            "event_head",
            authority_fault(
                "event_head", {"sequence_no": 90, "event_sha256": "d" * 64}
            ),
        ),
        (
            "first_action",
            lambda: _evaluate(fixture, proposed_first_action="restart-from-plan"),
        ),
        (
            "never_replay",
            lambda: _evaluate(fixture, replay_requested=True),
        ),
        ("operation", authority_fault("operation_id", "operation/other")),
        ("phase", phase_fault),
        ("project_revision", authority_fault("project_revision", 53)),
        (
            "recovery_budget",
            lambda: _evaluate(
                fixture, recovery_budget_bytes=fixture["recovery_bytes"] - 1
            ),
        ),
        ("task_revision", authority_fault("task_revision", 2)),
        (
            "unreserved_effect",
            lambda: _evaluate(fixture, requested_effect_id="effect/unreserved"),
        ),
    ]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)


def benchmark_durable_continuation(
    *, samples: int = 1000, generated_at: str = "2026-08-15T04:30:00Z"
) -> dict[str, Any]:
    """Measure exact local recovery and a fixed anti-reset fault matrix."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    fixture = durable_continuation_fixture()
    expected: bytes | None = None
    durations: list[float] = []
    successful_samples = 0
    replay_mismatch = 0
    first_action_mismatches = 0
    acknowledged_input_replays = 0
    continuation_field_mismatches = 0
    authority_violations = 0
    max_recovery_bytes = 0
    faults = {name: 0 for name in FAULT_NAMES}
    fault_rejections = 0
    for _ in range(samples):
        started = time.perf_counter()
        receipt = _evaluate(fixture)
        durations.append((time.perf_counter() - started) * 1000)
        validate_durable_recovery_receipt(receipt)
        payload = canonical_durable_recovery_bytes(receipt)
        if expected is None:
            expected = payload
        elif payload != expected:
            replay_mismatch += 1
        if receipt["first_action"] != fixture["proposed_first_action"]:
            first_action_mismatches += 1
        acknowledged_input_replays += receipt["acknowledged_input_replays"]
        if (
            receipt["continuation_fields_recovered"]
            != receipt["continuation_fields_total"]
        ):
            continuation_field_mismatches += 1
        if receipt["state_write_authority"] or receipt["provider_native_authority"]:
            authority_violations += 1
        max_recovery_bytes = max(
            max_recovery_bytes, receipt["recovery_read_receipt"]["bytes_read"]
        )
        successful_samples += 1
        for name, invoke in _fault_cases(fixture):
            try:
                invoke()
            except DurableContinuationError:
                fault_rejections += 1
            else:
                faults[name] += 1
    root = Path(__file__).parents[1]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": successful_samples,
        "replay_mismatch": replay_mismatch,
        "first_action_mismatches": first_action_mismatches,
        "acknowledged_input_replays": acknowledged_input_replays,
        "continuation_field_mismatches": continuation_field_mismatches,
        "continuation_fields_total": samples * CONTINUATION_FIELD_COUNT,
        "continuation_fields_recovered": successful_samples * CONTINUATION_FIELD_COUNT,
        "fault_samples": samples * len(FAULT_NAMES),
        "fault_rejections": fault_rejections,
        "faults": faults,
        "recovery_budget_bytes": fixture["recovery_budget_bytes"],
        "max_recovery_bytes": max_recovery_bytes,
        "authority_violations": authority_violations,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "external_services": 0,
        "fixture_sha256": hashlib.sha256(_canonical(fixture)).hexdigest(),
        "implementation_sha256": hashlib.sha256(
            (root / "context_control_plane/durable_continuation.py").read_bytes()
        ).hexdigest(),
        "benchmark_sha256": hashlib.sha256(
            (root / "context_control_plane/durable_continuation_benchmark.py").read_bytes()
        ).hexdigest(),
    }


def validate_durable_continuation_benchmark(receipt: Any) -> None:
    """Validate M5-08 acceptance metrics and reject false success."""
    if not isinstance(receipt, dict) or set(receipt) != _BENCHMARK_FIELDS:
        raise ValueError("M5-08 benchmark fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("M5-08 benchmark schema_version is unsupported")
    if not isinstance(receipt["generated_at"], str) or not receipt["generated_at"].endswith(
        "Z"
    ):
        raise ValueError("M5-08 benchmark generated_at is invalid")
    integer_fields = _BENCHMARK_FIELDS - {
        "schema_version",
        "generated_at",
        "faults",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "fixture_sha256",
        "implementation_sha256",
        "benchmark_sha256",
    }
    if any(type(receipt[field]) is not int or receipt[field] < 0 for field in integer_fields):
        raise ValueError("M5-08 benchmark integer fields are invalid")
    if any(
        not isinstance(receipt[field], (int, float))
        or isinstance(receipt[field], bool)
        or not math.isfinite(receipt[field])
        or receipt[field] < 0
        for field in ("p50_ms", "p95_ms", "max_ms")
    ):
        raise ValueError("M5-08 benchmark latency fields are invalid")
    if not isinstance(receipt["faults"], dict) or tuple(sorted(receipt["faults"])) != FAULT_NAMES:
        raise ValueError("M5-08 benchmark fault fields are invalid")
    if any(type(value) is not int or value < 0 for value in receipt["faults"].values()):
        raise ValueError("M5-08 benchmark fault values are invalid")
    for field in ("fixture_sha256", "implementation_sha256", "benchmark_sha256"):
        if (
            not isinstance(receipt[field], str)
            or len(receipt[field]) != 64
            or any(character not in "0123456789abcdef" for character in receipt[field])
        ):
            raise ValueError(f"M5-08 benchmark {field} is invalid")
    if (
        receipt["successful_samples"] != receipt["samples"]
        or receipt["replay_mismatch"]
        or receipt["first_action_mismatches"]
        or receipt["acknowledged_input_replays"]
        or receipt["continuation_field_mismatches"]
        or receipt["continuation_fields_total"]
        != receipt["samples"] * CONTINUATION_FIELD_COUNT
        or receipt["continuation_fields_recovered"]
        != receipt["continuation_fields_total"]
        or receipt["fault_samples"] != receipt["samples"] * len(FAULT_NAMES)
        or receipt["fault_rejections"] != receipt["fault_samples"]
        or any(receipt["faults"].values())
        or receipt["max_recovery_bytes"] > receipt["recovery_budget_bytes"]
        or receipt["authority_violations"]
        or receipt["external_services"]
    ):
        raise ValueError("M5-08 benchmark completion gate failed")
