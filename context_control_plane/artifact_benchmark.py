"""Deterministic local artifact benchmark for bounded context expansion."""

from __future__ import annotations

import hashlib
import io
import platform
import sys
import time
from pathlib import Path
from typing import Any

from .artifact_store import LocalArtifactStore


_SCHEMA_VERSION = "context.artifact-store-results/v1alpha1"


def _payload(size: int) -> bytes:
    seed = hashlib.sha256(b"context-control-plane-m2-04-artifact-benchmark").digest()
    repeats, remainder = divmod(size, len(seed))
    return seed * repeats + seed[:remainder]


def run_artifact_benchmark(
    root: str | Path,
    *,
    payload_bytes: int = 1024 * 1024,
    range_bytes: int = 8192,
) -> dict[str, Any]:
    """Measure full versus bounded reads through the validated local store."""
    if type(payload_bytes) is not int or payload_bytes <= 0:
        raise ValueError("payload_bytes must be a positive integer")
    if type(range_bytes) is not int or range_bytes <= 0 or range_bytes > payload_bytes:
        raise ValueError("range_bytes must be positive and fit within payload_bytes")
    root = Path(root)
    if root.exists():
        raise ValueError("artifact benchmark root must not already exist")

    payload = _payload(payload_bytes)
    store = LocalArtifactStore(root, max_range_bytes=range_bytes)
    store.initialize()

    started = time.perf_counter_ns()
    ref = store.put_bytes(payload)
    put_ms = (time.perf_counter_ns() - started) / 1_000_000

    started = time.perf_counter_ns()
    full = store.read(ref)
    full_read_ms = (time.perf_counter_ns() - started) / 1_000_000
    if full != payload:
        raise RuntimeError("artifact full read does not match the source")

    offset = (payload_bytes - range_bytes) // 2
    started = time.perf_counter_ns()
    bounded = store.read_range(ref, offset=offset, length=range_bytes)
    range_read_ms = (time.perf_counter_ns() - started) / 1_000_000
    expected_range = payload[offset : offset + range_bytes]
    if bounded != expected_range:
        raise RuntimeError("artifact range read does not match the source")
    store.verify(ref)

    range_digest = hashlib.sha256(bounded).hexdigest()
    return {
        "schema_version": _SCHEMA_VERSION,
        "external_services": 0,
        "environment": {
            "python_version": platform.python_version(),
            "platform": sys.platform,
            "machine": platform.machine(),
            "external_services": 0,
        },
        "payload_bytes": payload_bytes,
        "artifact_ref": ref.to_document(),
        "put_ms": round(put_ms, 4),
        "full_read_ms": round(full_read_ms, 4),
        "range_read_ms": round(range_read_ms, 4),
        "full_read_bytes": len(full),
        "range_bytes": len(bounded),
        "range_offset": offset,
        "full_read_sha256": hashlib.sha256(full).hexdigest(),
        "range_sha256": range_digest,
        "range_output_sha256": range_digest,
        "context_bytes_reduction_percent": round(
            (len(full) - len(bounded)) / len(full) * 100,
            4,
        ),
        "integrity": "passed",
    }
