"""Offline replay benchmark for M5-05 context accounting."""

from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from .context_accounting import (
    ContextAccountingError,
    canonical_context_accounting_bytes,
    compose_context_accounting,
    measured_metric,
    unavailable_metric,
    validate_context_accounting,
)


SCHEMA_VERSION = "context.context-accounting-benchmark/v1alpha1"

_BENCHMARK_FIELDS = {
    "schema_version",
    "generated_at",
    "samples",
    "successful_samples",
    "replay_mismatch",
    "same_corpus_budget_failures",
    "route_configuration_failures",
    "accounting_failures",
    "provider_measured_routes",
    "provider_unavailable_routes",
    "false_provider_improvement_claims",
    "locally_measured_metrics_per_route",
    "p50_ms",
    "p95_ms",
    "max_ms",
    "external_services",
    "fixture_sha256",
    "implementation_sha256",
}
_LOCAL_METRIC_NAMES = {
    "retrieval_queries",
    "retrieval_read_bytes",
    "retrieval_output_bytes",
    "retrieval_latency_ms",
    "compaction_latency_ms",
}
_PROVIDER_METRIC_NAMES = {
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "billing_usd_micros",
    "cache_invalidated",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _unavailable_provider_metrics() -> dict[str, dict[str, Any]]:
    return {
        "input_tokens": unavailable_metric("tokens", "provider trace was not exported"),
        "output_tokens": unavailable_metric("tokens", "provider trace was not exported"),
        "cache_read_tokens": unavailable_metric("tokens", "provider cache trace was not exported"),
        "cache_write_tokens": unavailable_metric("tokens", "provider cache trace was not exported"),
        "billing_usd_micros": unavailable_metric(
            "usd_micros", "provider billing trace was not exported"
        ),
        "cache_invalidated": unavailable_metric(
            "boolean", "provider cache trace was not exported"
        ),
    }


def _route_metrics(*, provider_id: str, corpus_bytes: int) -> dict[str, dict[str, Any]]:
    evidence_ref = f"run://m5-05/{provider_id}/offline"
    metrics = _unavailable_provider_metrics()
    metrics.update(
        {
            "retrieval_queries": measured_metric(
                1, "count", source_kind="retrieval_receipt", evidence_ref=evidence_ref
            ),
            "retrieval_read_bytes": measured_metric(
                corpus_bytes,
                "bytes",
                source_kind="retrieval_receipt",
                evidence_ref=evidence_ref,
            ),
            "retrieval_output_bytes": measured_metric(
                768,
                "bytes",
                source_kind="retrieval_receipt",
                evidence_ref=evidence_ref,
            ),
            "retrieval_latency_ms": measured_metric(
                0.25,
                "milliseconds",
                source_kind="monotonic_clock",
                evidence_ref=evidence_ref,
            ),
            "compaction_latency_ms": measured_metric(
                0.5,
                "milliseconds",
                source_kind="monotonic_clock",
                evidence_ref=evidence_ref,
            ),
            "cut_point_tokens": (
                measured_metric(
                    2048,
                    "tokens",
                    source_kind="host_hook",
                    evidence_ref="hook://pi/compaction",
                )
                if provider_id == "pi"
                else unavailable_metric(
                    "tokens", "DeepSeek exposes a semantic checkpoint without a token cut point"
                )
            ),
        }
    )
    return metrics


def benchmark_context_accounting_fixture() -> dict[str, Any]:
    """Build the fixed corpus/budget comparison without synthesizing provider usage."""
    corpus = {
        "corpus_id": "corpus/m5-05/shared-v1",
        "segments": [
            {"segment_id": "active-work", "content": "M5-05 context accounting"},
            {"segment_id": "constraint", "content": "provider metrics require provider traces"},
            {"segment_id": "next-action", "content": "run the same corpus and token budget"},
        ],
    }
    corpus_bytes = _canonical(corpus)
    corpus_sha256 = hashlib.sha256(corpus_bytes).hexdigest()
    routes = [
        {
            "route_id": "route/pi",
            "provider_id": "pi",
            "route_threshold_tokens": 3072,
            "model_free_pruning": True,
            "metrics": _route_metrics(provider_id="pi", corpus_bytes=len(corpus_bytes)),
        },
        {
            "route_id": "route/deepseek",
            "provider_id": "deepseek",
            "route_threshold_tokens": 3584,
            "model_free_pruning": False,
            "metrics": _route_metrics(provider_id="deepseek", corpus_bytes=len(corpus_bytes)),
        },
    ]
    accounting = compose_context_accounting(
        accounting_id="accounting/m5-05/offline-v1",
        corpus_id=corpus["corpus_id"],
        corpus_sha256=corpus_sha256,
        budget_tokens=4096,
        routes=routes,
        observed_at="2026-08-15T02:00:00+08:00",
    )
    return {
        "fixture_version": "context.context-accounting-fixture/v1alpha1",
        "corpus": corpus,
        "corpus_sha256": corpus_sha256,
        "accounting": accounting,
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def benchmark_context_accounting(
    *, samples: int = 1000, generated_at: str = "2026-08-15T02:30:00Z"
) -> dict[str, Any]:
    """Measure local receipt overhead while retaining unavailable provider facts."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    fixture = benchmark_context_accounting_fixture()
    expected = canonical_context_accounting_bytes(fixture["accounting"])
    source = fixture["accounting"]
    durations: list[float] = []
    replay_mismatch = 0
    same_corpus_budget_failures = 0
    route_configuration_failures = 0
    accounting_failures = 0
    false_provider_improvement_claims = 0
    successful_samples = 0
    for _ in range(samples):
        started = time.perf_counter()
        try:
            accounting = compose_context_accounting(
                accounting_id=source["accounting_id"],
                corpus_id=source["corpus_id"],
                corpus_sha256=source["corpus_sha256"],
                budget_tokens=source["budget_tokens"],
                routes=source["routes"],
                observed_at=source["observed_at"],
            )
            validate_context_accounting(accounting)
            successful_samples += 1
        except ContextAccountingError:
            accounting_failures += 1
            continue
        finally:
            durations.append((time.perf_counter() - started) * 1000)
        if canonical_context_accounting_bytes(accounting) != expected:
            replay_mismatch += 1
        if (
            accounting["corpus_sha256"] != fixture["corpus_sha256"]
            or accounting["budget_tokens"] != 4096
        ):
            same_corpus_budget_failures += 1
        routes = {route["provider_id"]: route for route in accounting["routes"]}
        if (
            set(routes) != {"pi", "deepseek"}
            or routes["pi"]["route_threshold_tokens"] != 3072
            or routes["pi"]["model_free_pruning"] is not True
            or routes["deepseek"]["route_threshold_tokens"] != 3584
            or routes["deepseek"]["model_free_pruning"] is not False
        ):
            route_configuration_failures += 1
        if (
            accounting["provider_comparison_status"] == "unavailable"
            and accounting["real_provider_improvement_claimed"]
        ):
            false_provider_improvement_claims += 1
    routes = source["routes"]
    measured_routes = sum(
        all(route["metrics"][name]["status"] == "measured" for name in _PROVIDER_METRIC_NAMES)
        for route in routes
    )
    local_counts = [
        sum(route["metrics"][name]["status"] == "measured" for name in _LOCAL_METRIC_NAMES)
        for route in routes
    ]
    root = Path(__file__).parents[1]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": successful_samples,
        "replay_mismatch": replay_mismatch,
        "same_corpus_budget_failures": same_corpus_budget_failures,
        "route_configuration_failures": route_configuration_failures,
        "accounting_failures": accounting_failures,
        "provider_measured_routes": measured_routes,
        "provider_unavailable_routes": len(routes) - measured_routes,
        "false_provider_improvement_claims": false_provider_improvement_claims,
        "locally_measured_metrics_per_route": min(local_counts),
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "external_services": 0,
        "fixture_sha256": hashlib.sha256(_canonical(fixture)).hexdigest(),
        "implementation_sha256": _sha256_file(root / "context_control_plane/context_accounting.py"),
    }


def validate_context_accounting_benchmark(receipt: Any) -> None:
    """Validate the offline benchmark without treating it as provider performance evidence."""
    if not isinstance(receipt, dict) or set(receipt) != _BENCHMARK_FIELDS:
        raise ValueError("M5-05 benchmark fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("M5-05 benchmark schema_version is unsupported")
    if not isinstance(receipt["generated_at"], str) or not receipt["generated_at"].endswith("Z"):
        raise ValueError("M5-05 benchmark generated_at is invalid")
    integer_fields = {
        "samples",
        "successful_samples",
        "replay_mismatch",
        "same_corpus_budget_failures",
        "route_configuration_failures",
        "accounting_failures",
        "provider_measured_routes",
        "provider_unavailable_routes",
        "false_provider_improvement_claims",
        "locally_measured_metrics_per_route",
        "external_services",
    }
    for field in integer_fields:
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise ValueError(f"M5-05 benchmark {field} is invalid")
    if receipt["samples"] <= 0:
        raise ValueError("M5-05 benchmark samples must be positive")
    for field in ("p50_ms", "p95_ms", "max_ms"):
        if isinstance(receipt[field], bool) or not isinstance(receipt[field], (int, float)) or receipt[field] < 0:
            raise ValueError(f"M5-05 benchmark {field} is invalid")
    if receipt["successful_samples"] != receipt["samples"]:
        raise ValueError("M5-05 benchmark requires every accounting sample to pass")
    if any(
        receipt[field] != 0
        for field in (
            "replay_mismatch",
            "same_corpus_budget_failures",
            "route_configuration_failures",
            "accounting_failures",
            "false_provider_improvement_claims",
            "external_services",
        )
    ):
        raise ValueError("M5-05 benchmark correctness veto failed")
    if receipt["provider_measured_routes"] != 0 or receipt["provider_unavailable_routes"] != 2:
        raise ValueError("M5-05 offline provider availability was overstated")
    if receipt["locally_measured_metrics_per_route"] != len(_LOCAL_METRIC_NAMES):
        raise ValueError("M5-05 local accounting coverage is incomplete")
    for field in ("fixture_sha256", "implementation_sha256"):
        value = receipt[field]
        if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError(f"M5-05 benchmark {field} is invalid")
