"""Offline benchmark and acceptance validator for M5-04 bounded expansion."""

from __future__ import annotations

import hashlib
import json
import math
import tempfile
import time
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactRef, ArtifactStoreError, LocalArtifactStore
from .bounded_artifact_expansion import (
    BoundedArtifactExpansionError,
    BoundedArtifactBudgetError,
    expand_artifact,
)

SCHEMA_VERSION = "context.bounded-artifact-expansion-benchmark/v1alpha1"
_FIELDS = {
    "schema_version", "generated_at", "samples", "successful_samples", "replay_mismatch",
    "budget_fault_samples", "budget_rejections", "digest_fault_samples", "digest_rejections",
    "returned_byte_budget", "returned_bytes_min", "returned_bytes_max", "scanned_bytes_min",
    "scanned_bytes_max", "prompt_bytes_min", "prompt_bytes_max", "prompt_reduction_millionths",
    "p50_ms", "p95_ms", "max_ms", "external_services", "implementation_sha256",
    "benchmark_sha256",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)


def benchmark_bounded_artifact_expansion(
    *, samples: int = 1000, generated_at: str = "2026-08-15T03:00:00Z"
) -> dict[str, Any]:
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    payload = (b"active-work: M5-04 bounded expansion\n" + b"constraint: return only requested excerpts\n") * 128
    returned_budget = 256
    ranges = [
        {"purpose": "active work", "offset_bytes": 0, "length_bytes": 40},
        {"purpose": "constraint", "offset_bytes": 80, "length_bytes": 48},
    ]
    durations: list[float] = []
    prompt_sizes: list[int] = []
    returned_sizes: list[int] = []
    scanned_sizes: list[int] = []
    replay_mismatch = 0
    budget_rejections = 0
    digest_rejections = 0
    with tempfile.TemporaryDirectory(prefix="context-m5-04-benchmark-") as directory:
        store = LocalArtifactStore(Path(directory) / "artifacts", max_range_bytes=1024)
        store.initialize()
        ref = store.put_bytes(payload)
        expected_prompt: bytes | None = None
        forged_digest = "0" * 64 if ref.digest != "0" * 64 else "1" * 64
        forged_ref = ArtifactRef(digest=forged_digest, size_bytes=ref.size_bytes)
        for _ in range(samples):
            started = time.perf_counter()
            expansion = expand_artifact(
                store,
                artifact_ref=ref,
                summary="M5-04 bounded artifact fixture",
                necessary_ranges=ranges,
                returned_byte_budget=returned_budget,
                scanned_byte_budget=ref.size_bytes * len(ranges),
            )
            durations.append((time.perf_counter() - started) * 1000)
            receipt = expansion.receipt
            prompt_sizes.append(receipt["prompt_bytes"])
            returned_sizes.append(receipt["returned_bytes"])
            scanned_sizes.append(receipt["scanned_bytes"])
            if expected_prompt is None:
                expected_prompt = expansion.prompt_bytes
            elif expansion.prompt_bytes != expected_prompt:
                replay_mismatch += 1
            try:
                expand_artifact(
                    store,
                    artifact_ref=ref,
                    summary="M5-04 bounded artifact fixture",
                    necessary_ranges=ranges,
                    returned_byte_budget=64,
                    scanned_byte_budget=ref.size_bytes * len(ranges),
                )
            except BoundedArtifactBudgetError:
                budget_rejections += 1
            try:
                expand_artifact(
                    store,
                    artifact_ref=forged_ref,
                    summary="M5-04 bounded artifact fixture",
                    necessary_ranges=ranges,
                    returned_byte_budget=returned_budget,
                    scanned_byte_budget=ref.size_bytes * len(ranges),
                )
            except (ArtifactStoreError, BoundedArtifactExpansionError):
                digest_rejections += 1
    root = Path(__file__).parents[1]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": samples,
        "replay_mismatch": replay_mismatch,
        "budget_fault_samples": samples,
        "budget_rejections": budget_rejections,
        "digest_fault_samples": samples,
        "digest_rejections": digest_rejections,
        "returned_byte_budget": returned_budget,
        "returned_bytes_min": min(returned_sizes),
        "returned_bytes_max": max(returned_sizes),
        "scanned_bytes_min": min(scanned_sizes),
        "scanned_bytes_max": max(scanned_sizes),
        "prompt_bytes_min": min(prompt_sizes),
        "prompt_bytes_max": max(prompt_sizes),
        "prompt_reduction_millionths": round((len(payload) - max(prompt_sizes)) / len(payload) * 1_000_000),
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "external_services": 0,
        "implementation_sha256": _sha256_file(root / "context_control_plane/bounded_artifact_expansion.py"),
        "benchmark_sha256": _sha256_file(root / "context_control_plane/bounded_artifact_expansion_benchmark.py"),
    }


def validate_bounded_artifact_expansion_benchmark(receipt: Any, *, root: Path | None = None) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS:
        raise ValueError("M5-04 benchmark fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION or not str(receipt["generated_at"]).endswith("Z"):
        raise ValueError("M5-04 benchmark identity is invalid")
    for field in _FIELDS - {"schema_version", "generated_at", "p50_ms", "p95_ms", "max_ms", "implementation_sha256", "benchmark_sha256"}:
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise ValueError(f"M5-04 benchmark integer field is invalid: {field}")
    for field in ("p50_ms", "p95_ms", "max_ms"):
        if isinstance(receipt[field], bool) or not isinstance(receipt[field], (int, float)) or not math.isfinite(receipt[field]) or receipt[field] < 0:
            raise ValueError(f"M5-04 benchmark latency field is invalid: {field}")
    for field in ("implementation_sha256", "benchmark_sha256"):
        if not isinstance(receipt[field], str) or len(receipt[field]) != 64 or any(c not in "0123456789abcdef" for c in receipt[field]):
            raise ValueError(f"M5-04 benchmark hash is invalid: {field}")
    if (
        receipt["successful_samples"] != receipt["samples"]
        or receipt["replay_mismatch"] != 0
        or receipt["budget_rejections"] != receipt["budget_fault_samples"]
        or receipt["digest_rejections"] != receipt["digest_fault_samples"]
        or receipt["external_services"] != 0
        or receipt["returned_bytes_max"] > receipt["returned_byte_budget"]
        or receipt["prompt_reduction_millionths"] <= 0
    ):
        raise ValueError("M5-04 benchmark veto failed")
    if root is not None:
        if receipt["implementation_sha256"] != _sha256_file(root / "context_control_plane/bounded_artifact_expansion.py"):
            raise ValueError("M5-04 implementation provenance is stale")
        if receipt["benchmark_sha256"] != _sha256_file(root / "context_control_plane/bounded_artifact_expansion_benchmark.py"):
            raise ValueError("M5-04 benchmark provenance is stale")
