"""Measured process-crash recovery receipt for M8 durable operations."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "context.durable-operation-benchmark/v1alpha1"
_FIXTURE_VERSION = "context.durable-provider-oracle-fixture/v1alpha1"
_CRASH_POINTS = {
    "after-prepared",
    "after-intent-commit",
    "after-intent-record",
    "after-effect-start",
    "after-external-effect",
    "after-effect-settlement",
    "after-state-commit",
    "after-response-record",
    "after-terminal",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "samples",
    "crash_points",
    "provider_oracle_fixture_sha256",
    "scenario_count",
    "terminal_recoveries",
    "authority_backend",
    "checkpoint_backend",
    "provider_invocations",
    "deduplicated_provider_invocations",
    "effect_adapter_invocations",
    "deduplicated_effect_invocations",
    "applied_effects",
    "duplicate_semantic_effects",
    "authority_intent_commits",
    "authority_state_commits",
    "restore_latency_ms",
    "state_write_authority",
    "provider_native_authority",
    "receipt_sha256",
}
_LATENCY_FIELDS = {"p50", "p95", "max"}


class DurableOperationBenchmarkError(RuntimeError):
    """Raised when crash recovery evidence is incomplete or inconsistent."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise DurableOperationBenchmarkError("benchmark data is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _receipt_digest(receipt: dict[str, Any]) -> str:
    body = copy.deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return _digest(body)


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise DurableOperationBenchmarkError("generated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DurableOperationBenchmarkError("generated_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DurableOperationBenchmarkError("generated_at requires a timezone")
    return value


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 6)


def _provider_fixture(root: Path) -> dict[str, Any]:
    path = root / "experiments/fixtures/m8-01-provider-oracles.json"
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise DurableOperationBenchmarkError("provider oracle fixture is unavailable") from exc
    if (
        not isinstance(fixture, dict)
        or fixture.get("schema_version") != _FIXTURE_VERSION
        or not isinstance(fixture.get("oracles"), list)
        or len(fixture["oracles"]) != 2
    ):
        raise DurableOperationBenchmarkError("provider oracle fixture is invalid")
    body = copy.deepcopy(fixture)
    fixture_sha256 = body.pop("fixture_sha256", None)
    if fixture_sha256 != _digest(body):
        raise DurableOperationBenchmarkError("provider oracle fixture digest mismatch")
    return fixture


def _run_scenario(root: Path, crash_point: str) -> tuple[dict[str, Any], float]:
    tool = root / "tools/run_m8_01_crash_fixture.py"
    with tempfile.TemporaryDirectory(prefix="context-m8-01-") as directory:
        command = [
            sys.executable,
            str(tool),
            "--root",
            directory,
            "--real-authority",
        ]
        if crash_point == "after-external-effect":
            command.append("--status-unavailable")
        killed = subprocess.run(
            [*command, "--crash-point", crash_point],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if killed.returncode != -signal.SIGKILL:
            raise DurableOperationBenchmarkError(
                f"crash point {crash_point} did not terminate with SIGKILL"
            )
        started = time.perf_counter_ns()
        recovered = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        if recovered.returncode != 0:
            raise DurableOperationBenchmarkError(
                f"crash point {crash_point} failed to recover"
            )
        try:
            receipt = json.loads(recovered.stdout)
        except json.JSONDecodeError as exc:
            raise DurableOperationBenchmarkError("crash fixture returned invalid JSON") from exc
    return receipt, elapsed_ms


def benchmark_durable_operation(
    *,
    root: Path,
    samples: int = 1,
    crash_points: tuple[str, ...] = tuple(sorted(_CRASH_POINTS)),
    generated_at: str,
) -> dict[str, Any]:
    """Run real SIGKILL/restart scenarios and aggregate non-conflated metrics."""
    if not hasattr(signal, "SIGKILL"):
        raise DurableOperationBenchmarkError("SIGKILL is unavailable")
    if type(samples) is not int or samples <= 0 or samples > 1000:
        raise DurableOperationBenchmarkError("samples is invalid")
    if (
        not isinstance(crash_points, tuple)
        or not crash_points
        or len(crash_points) != len(set(crash_points))
        or any(point not in _CRASH_POINTS for point in crash_points)
    ):
        raise DurableOperationBenchmarkError("crash_points are invalid")
    root = Path(root).resolve()
    fixture = _provider_fixture(root)
    latencies: list[float] = []
    results: list[dict[str, Any]] = []
    for _ in range(samples):
        for crash_point in crash_points:
            result, latency_ms = _run_scenario(root, crash_point)
            results.append(result)
            latencies.append(latency_ms)

    scenario_count = samples * len(crash_points)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": "benchmark-m8-01-" + _digest(
            {
                "samples": samples,
                "crash_points": list(crash_points),
                "fixture_sha256": fixture["fixture_sha256"],
            }
        )[:24],
        "generated_at": _timestamp(generated_at),
        "samples": samples,
        "crash_points": list(crash_points),
        "provider_oracle_fixture_sha256": fixture["fixture_sha256"],
        "scenario_count": scenario_count,
        "terminal_recoveries": sum(item.get("phase") == "terminal" for item in results),
        "authority_backend": "state-mcp-sqlite",
        "checkpoint_backend": "content-addressed-local",
        "provider_invocations": 0,
        "deduplicated_provider_invocations": 0,
        "effect_adapter_invocations": sum(item["apply_attempts"] for item in results),
        "deduplicated_effect_invocations": sum(
            item["duplicate_effects"] for item in results
        ),
        "applied_effects": sum(item["semantic_effects"] for item in results),
        "duplicate_semantic_effects": sum(
            max(item["semantic_effects"] - 1, 0) for item in results
        ),
        "authority_intent_commits": sum(item["intent_commits"] for item in results),
        "authority_state_commits": sum(item["state_commits"] for item in results),
        "restore_latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 6),
        },
        "state_write_authority": False,
        "provider_native_authority": False,
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    validate_durable_operation_benchmark(receipt)
    return receipt


def validate_durable_operation_benchmark(receipt: Any) -> None:
    """Validate one measured crash benchmark and its zero-duplicate gate."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise DurableOperationBenchmarkError("benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise DurableOperationBenchmarkError("benchmark receipt version is invalid")
    if not isinstance(receipt["benchmark_id"], str) or not receipt["benchmark_id"].startswith(
        "benchmark-m8-01-"
    ):
        raise DurableOperationBenchmarkError("benchmark_id is invalid")
    _timestamp(receipt["generated_at"])
    crash_points = receipt["crash_points"]
    if (
        not isinstance(crash_points, list)
        or not crash_points
        or len(crash_points) != len(set(crash_points))
        or any(point not in _CRASH_POINTS for point in crash_points)
    ):
        raise DurableOperationBenchmarkError("benchmark crash_points are invalid")
    integer_fields = (
        "samples",
        "scenario_count",
        "terminal_recoveries",
        "provider_invocations",
        "deduplicated_provider_invocations",
        "effect_adapter_invocations",
        "deduplicated_effect_invocations",
        "applied_effects",
        "duplicate_semantic_effects",
        "authority_intent_commits",
        "authority_state_commits",
    )
    for field in integer_fields:
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise DurableOperationBenchmarkError(f"benchmark {field} is invalid")
    if receipt["samples"] <= 0:
        raise DurableOperationBenchmarkError("benchmark samples must be positive")
    expected_scenarios = receipt["samples"] * len(crash_points)
    if receipt["scenario_count"] != expected_scenarios:
        raise DurableOperationBenchmarkError("benchmark scenario count is inconsistent")
    if receipt["terminal_recoveries"] != expected_scenarios:
        raise DurableOperationBenchmarkError("not every crash scenario recovered")
    if receipt["authority_backend"] != "state-mcp-sqlite":
        raise DurableOperationBenchmarkError("benchmark authority backend is invalid")
    if receipt["checkpoint_backend"] != "content-addressed-local":
        raise DurableOperationBenchmarkError("benchmark checkpoint backend is invalid")
    if receipt["provider_invocations"] != 0:
        raise DurableOperationBenchmarkError("local crash fixture invoked a provider")
    if receipt["deduplicated_provider_invocations"] > receipt["provider_invocations"]:
        raise DurableOperationBenchmarkError("provider deduplication metric is inconsistent")
    if receipt["deduplicated_effect_invocations"] > receipt["effect_adapter_invocations"]:
        raise DurableOperationBenchmarkError("effect deduplication metric is inconsistent")
    expected_deduplications = (
        receipt["samples"] if "after-external-effect" in crash_points else 0
    )
    if receipt["deduplicated_effect_invocations"] != expected_deduplications:
        raise DurableOperationBenchmarkError("same-key retry coverage is incomplete")
    if receipt["effect_adapter_invocations"] != expected_scenarios + expected_deduplications:
        raise DurableOperationBenchmarkError("effect invocation count is inconsistent")
    if receipt["applied_effects"] != expected_scenarios:
        raise DurableOperationBenchmarkError("each scenario must apply one semantic effect")
    if receipt["duplicate_semantic_effects"] != 0:
        raise DurableOperationBenchmarkError("duplicate semantic effects violate M8-01")
    if receipt["authority_intent_commits"] != expected_scenarios:
        raise DurableOperationBenchmarkError("intent commit count is inconsistent")
    if receipt["authority_state_commits"] != expected_scenarios:
        raise DurableOperationBenchmarkError("state commit count is inconsistent")
    latency = receipt["restore_latency_ms"]
    if not isinstance(latency, dict) or set(latency) != _LATENCY_FIELDS:
        raise DurableOperationBenchmarkError("restore latency fields are invalid")
    if any(type(latency[field]) not in {int, float} or latency[field] <= 0 for field in latency):
        raise DurableOperationBenchmarkError("restore latency values are invalid")
    if not latency["p50"] <= latency["p95"] <= latency["max"]:
        raise DurableOperationBenchmarkError("restore latency percentiles are inconsistent")
    digest = receipt["provider_oracle_fixture_sha256"]
    if not isinstance(digest, str) or len(digest) != 64:
        raise DurableOperationBenchmarkError("provider fixture digest is invalid")
    if receipt["state_write_authority"] is not False:
        raise DurableOperationBenchmarkError("benchmark cannot claim State authority")
    if receipt["provider_native_authority"] is not False:
        raise DurableOperationBenchmarkError("benchmark cannot claim provider authority")
    if receipt["receipt_sha256"] != _receipt_digest(receipt):
        raise DurableOperationBenchmarkError("benchmark receipt digest mismatch")
