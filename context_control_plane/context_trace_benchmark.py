"""Offline replay and coverage benchmark for the M8-04 trace contract."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .context_trace import (
    ContextTraceError,
    append_context_trace_event,
    canonical_context_trace_event_bytes,
    export_context_trace_to_otel,
    validate_context_trace_chain,
)


SCHEMA_VERSION = "context.context-trace-benchmark/v1alpha1"
REQUIRED_EVENT_NAMES = (
    "context.compaction.precompact",
    "context.compaction.postcompact",
    "context.routing.input",
    "context.skill.selection",
    "context.plan.revision",
    "context.multi_agent.dispatch",
    "context.multi_agent.handoff",
    "context.delivery.accepted",
)

_BENCHMARK_FIELDS = {
    "schema_version",
    "generated_at",
    "samples",
    "successful_samples",
    "total_events",
    "required_event_names",
    "required_event_coverage_millionths",
    "replay_mismatch",
    "hash_chain_failures",
    "binding_failures",
    "source_evidence_failures",
    "authority_violations",
    "otel_unavailable_samples",
    "false_otel_success",
    "p50_ms",
    "p95_ms",
    "max_ms",
    "external_services",
    "fixture_sha256",
    "implementation_sha256",
}
_BINDING_FIELDS = (
    "project_id",
    "state_revision",
    "active_work_id",
    "trace_id",
    "span_id",
    "run_id",
    "operation_id",
    "correlation_id",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _fixture_binding() -> dict[str, Any]:
    return {
        "project_id": "project/context-control-plane",
        "state_revision": 54,
        "active_work_id": "M5-07",
        "trace_id": "a" * 32,
        "span_id": "b" * 16,
        "run_id": "run/m8-04/offline",
        "operation_id": "operation/m8-04/trace",
        "correlation_id": "correlation/m5-campaign",
    }


def _fixture_source() -> dict[str, str]:
    return {
        "kind": "local_hook",
        "provider": "provider-neutral",
        "adapter": "context-trace/v1",
        "source_ref": "hook://m8-04/offline",
    }


def _build_events() -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    binding = _fixture_binding()
    source = _fixture_source()
    for index, event_name in enumerate(REQUIRED_EVENT_NAMES, start=1):
        append_context_trace_event(
            events,
            event_name=event_name,
            binding=binding,
            source=source,
            evidence_refs=[f"run://m8-04/offline/{index}"],
            observed_at=f"2026-08-15T04:00:{index:02d}Z",
            attributes={"event_family": event_name.split(".")[1], "result": "observed"},
            event_id=f"event/m8-04/offline/{index}",
        )
    return events


def benchmark_context_trace_fixture() -> dict[str, Any]:
    """Build a deterministic trace containing every M5-07 observation family."""
    return {
        "fixture_version": "context.context-trace-fixture/v1alpha1",
        "binding": _fixture_binding(),
        "source": _fixture_source(),
        "events": _build_events(),
    }


def _chain_bytes(events: list[dict[str, Any]]) -> bytes:
    return b"\n".join(canonical_context_trace_event_bytes(event) for event in events)


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 6)


def benchmark_context_trace(
    *, samples: int = 1000, generated_at: str = "2026-08-15T04:00:00Z"
) -> dict[str, Any]:
    """Measure deterministic local emission without assuming an OTel service exists."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    fixture = benchmark_context_trace_fixture()
    expected = _chain_bytes(fixture["events"])
    expected_binding = fixture["binding"]
    required_names = set(REQUIRED_EVENT_NAMES)
    observed_names: set[str] = set()
    durations: list[float] = []
    successful_samples = 0
    total_events = 0
    replay_mismatch = 0
    hash_chain_failures = 0
    binding_failures = 0
    source_evidence_failures = 0
    authority_violations = 0
    otel_unavailable_samples = 0
    false_otel_success = 0

    for _ in range(samples):
        started = time.perf_counter()
        try:
            events = _build_events()
            validate_context_trace_chain(events)
        except ContextTraceError:
            hash_chain_failures += 1
            durations.append((time.perf_counter() - started) * 1000)
            continue
        receipt = export_context_trace_to_otel(events)
        durations.append((time.perf_counter() - started) * 1000)
        successful_samples += 1
        total_events += len(events)
        observed_names.update(event["event_name"] for event in events)
        if _chain_bytes(events) != expected:
            replay_mismatch += 1
        if any(
            any(event[field] != expected_binding[field] for field in _BINDING_FIELDS)
            for event in events
        ):
            binding_failures += 1
        if any(not event["source"]["source_ref"] or not event["evidence_refs"] for event in events):
            source_evidence_failures += 1
        if any(event["authority"] is not False for event in events):
            authority_violations += 1
        if receipt["status"] == "unavailable" and receipt["configured"] is False:
            otel_unavailable_samples += 1
        if receipt["status"] == "exported":
            false_otel_success += 1

    root = Path(__file__).parents[1]
    coverage = len(observed_names & required_names) * 1_000_000 // len(required_names)
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": successful_samples,
        "total_events": total_events,
        "required_event_names": list(REQUIRED_EVENT_NAMES),
        "required_event_coverage_millionths": coverage,
        "replay_mismatch": replay_mismatch,
        "hash_chain_failures": hash_chain_failures,
        "binding_failures": binding_failures,
        "source_evidence_failures": source_evidence_failures,
        "authority_violations": authority_violations,
        "otel_unavailable_samples": otel_unavailable_samples,
        "false_otel_success": false_otel_success,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "external_services": 0,
        "fixture_sha256": hashlib.sha256(_canonical(fixture)).hexdigest(),
        "implementation_sha256": hashlib.sha256(
            (root / "context_control_plane/context_trace.py").read_bytes()
        ).hexdigest(),
    }
    validate_context_trace_benchmark(result)
    return result


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("generated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("generated_at must include a timezone")


def validate_context_trace_benchmark(receipt: Any) -> None:
    """Reject benchmark receipts that hide failures or claim unavailable export."""
    if not isinstance(receipt, dict) or set(receipt) != _BENCHMARK_FIELDS:
        raise ValueError("context trace benchmark fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("context trace benchmark schema_version is invalid")
    _validate_timestamp(receipt["generated_at"])
    samples = receipt["samples"]
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    if receipt["required_event_names"] != list(REQUIRED_EVENT_NAMES):
        raise ValueError("required_event_names are invalid")
    if receipt["successful_samples"] != samples:
        raise ValueError("all context trace samples must succeed")
    if receipt["total_events"] != samples * len(REQUIRED_EVENT_NAMES):
        raise ValueError("total_events does not cover every required event")
    if receipt["required_event_coverage_millionths"] != 1_000_000:
        raise ValueError("required context trace event coverage is incomplete")
    for field in (
        "replay_mismatch",
        "hash_chain_failures",
        "binding_failures",
        "source_evidence_failures",
        "authority_violations",
        "false_otel_success",
        "external_services",
    ):
        if type(receipt[field]) is not int or receipt[field] != 0:
            raise ValueError(f"{field} must be zero")
    if receipt["otel_unavailable_samples"] != samples:
        raise ValueError("unconfigured OTel must remain explicitly unavailable")
    latencies = [receipt[field] for field in ("p50_ms", "p95_ms", "max_ms")]
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in latencies
    ) or not (latencies[0] <= latencies[1] <= latencies[2]):
        raise ValueError("context trace latency values are invalid")
    for field in ("fixture_sha256", "implementation_sha256"):
        if not isinstance(receipt[field], str) or _SHA256_RE.fullmatch(receipt[field]) is None:
            raise ValueError(f"{field} is invalid")
