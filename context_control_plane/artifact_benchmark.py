"""Deterministic local artifact benchmark for bounded context expansion."""

from __future__ import annotations

import hashlib
import io
import platform
import shlex
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactRef, LocalArtifactStore


_SCHEMA_VERSION = "context.artifact-store-results/v1alpha1"
MAX_ARTIFACT_BENCHMARK_PAYLOAD_BYTES = 64 * 1024 * 1024
_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "observed_at",
        "provenance",
        "environment",
        "measurement",
        "acceptance",
        "generation",
    }
)
_PROVENANCE_FIELDS = frozenset(
    {"implementation_sha256", "benchmark_sha256", "runner_sha256"}
)
_ENVIRONMENT_FIELDS = frozenset(
    {"python_version", "platform", "machine", "external_services"}
)
_MEASUREMENT_FIELDS = frozenset(
    {
        "external_services",
        "payload_bytes",
        "artifact_ref",
        "put_ms",
        "full_read_ms",
        "range_read_ms",
        "full_read_bytes",
        "range_bytes",
        "range_offset",
        "full_read_sha256",
        "range_sha256",
        "range_output_sha256",
        "context_bytes_reduction_percent",
        "integrity",
    }
)
_ACCEPTANCE_FIELDS = frozenset(
    {"checksum_verified", "bounded_range_read", "external_services"}
)
_GENERATION_FIELDS = frozenset(
    {"command", "arguments", "writes_runtime_state_to_repository"}
)


def _require_exact_fields(
    value: Any,
    expected: frozenset[str],
    field_name: str,
) -> dict[str, Any]:
    if not isinstance(value, dict) or frozenset(value) != expected:
        raise ValueError(f"artifact benchmark {field_name} fields are invalid")
    return value


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_artifact_benchmark_receipt(
    receipt: dict[str, Any],
    *,
    root: str | Path,
) -> None:
    """Fail closed when a committed benchmark receipt or its inputs drift."""
    receipt = _require_exact_fields(receipt, _RECEIPT_FIELDS, "receipt")
    if receipt["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("artifact benchmark schema_version is unsupported")
    try:
        observed_at = datetime.fromisoformat(receipt["observed_at"])
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact benchmark observed_at is invalid") from exc
    if observed_at.utcoffset() is None:
        raise ValueError("artifact benchmark observed_at requires an offset")

    root = Path(root)
    provenance = _require_exact_fields(
        receipt["provenance"], _PROVENANCE_FIELDS, "provenance"
    )
    expected_hashes = {
        "implementation_sha256": _file_sha256(
            root / "context_control_plane/artifact_store.py"
        ),
        "benchmark_sha256": _file_sha256(
            root / "context_control_plane/artifact_benchmark.py"
        ),
        "runner_sha256": _file_sha256(root / "tools/run_artifact_benchmark.py"),
    }
    if provenance != expected_hashes:
        raise ValueError("artifact benchmark provenance hash mismatch")

    environment = _require_exact_fields(
        receipt["environment"], _ENVIRONMENT_FIELDS, "environment"
    )
    if environment["external_services"] != 0:
        raise ValueError("artifact benchmark must use zero external services")
    for field_name in ("python_version", "platform", "machine"):
        if not isinstance(environment[field_name], str):
            raise ValueError(f"artifact benchmark {field_name} is invalid")

    measurement = _require_exact_fields(
        receipt["measurement"], _MEASUREMENT_FIELDS, "measurement"
    )
    integer_fields = (
        "payload_bytes",
        "full_read_bytes",
        "range_bytes",
        "range_offset",
    )
    if any(type(measurement[field]) is not int for field in integer_fields):
        raise ValueError("artifact benchmark byte measurements are invalid")
    if measurement["payload_bytes"] > MAX_ARTIFACT_BENCHMARK_PAYLOAD_BYTES:
        raise ValueError("artifact benchmark payload exceeds maximum size")
    if (
        measurement["payload_bytes"] <= 0
        or measurement["full_read_bytes"] != measurement["payload_bytes"]
        or measurement["range_bytes"] <= 0
        or measurement["range_bytes"] > measurement["full_read_bytes"]
        or measurement["range_offset"] < 0
        or measurement["range_offset"] + measurement["range_bytes"]
        > measurement["full_read_bytes"]
    ):
        raise ValueError("artifact benchmark byte range is invalid")
    ref = ArtifactRef.from_document(measurement["artifact_ref"])
    expected_payload = _payload(measurement["payload_bytes"])
    expected_full_digest = hashlib.sha256(expected_payload).hexdigest()
    expected_range_digest = hashlib.sha256(
        expected_payload[
            measurement["range_offset"] : measurement["range_offset"]
            + measurement["range_bytes"]
        ]
    ).hexdigest()
    if (
        ref.size_bytes != measurement["payload_bytes"]
        or ref.digest != measurement["full_read_sha256"]
        or ref.digest != expected_full_digest
        or measurement["range_sha256"] != measurement["range_output_sha256"]
        or measurement["range_sha256"] != expected_range_digest
    ):
        raise ValueError("artifact benchmark digest evidence is inconsistent")
    for field_name in ("range_sha256", "range_output_sha256"):
        digest = measurement[field_name]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("artifact benchmark range digest is invalid")
        try:
            int(digest, 16)
        except ValueError as exc:
            raise ValueError("artifact benchmark range digest is invalid") from exc
    for field_name in ("put_ms", "full_read_ms", "range_read_ms"):
        value = measurement[field_name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError("artifact benchmark latency is invalid")
    expected_reduction = round(
        (measurement["full_read_bytes"] - measurement["range_bytes"])
        / measurement["full_read_bytes"]
        * 100,
        4,
    )
    if measurement["context_bytes_reduction_percent"] != expected_reduction:
        raise ValueError("artifact benchmark context byte reduction is invalid")
    if measurement["external_services"] != 0 or measurement["integrity"] != "passed":
        raise ValueError("artifact benchmark acceptance evidence is invalid")

    acceptance = _require_exact_fields(
        receipt["acceptance"], _ACCEPTANCE_FIELDS, "acceptance"
    )
    if acceptance != {
        "checksum_verified": True,
        "bounded_range_read": True,
        "external_services": 0,
    }:
        raise ValueError("artifact benchmark acceptance flags are invalid")

    generation = _require_exact_fields(
        receipt["generation"], _GENERATION_FIELDS, "generation"
    )
    expected_arguments = [
        "--payload-bytes",
        str(measurement["payload_bytes"]),
        "--range-bytes",
        str(measurement["range_bytes"]),
        "--observed-at",
        receipt["observed_at"],
        "--output",
        "experiments/state/m2-04-artifact-store-results.yaml",
    ]
    if generation["arguments"] != expected_arguments:
        raise ValueError("artifact benchmark generation arguments are invalid")
    if generation["writes_runtime_state_to_repository"] is not False:
        raise ValueError("artifact benchmark must not write runtime state to Git")
    if shlex.split(generation["command"]) != [
        ".venv/bin/python",
        "tools/run_artifact_benchmark.py",
        *expected_arguments,
    ]:
        raise ValueError("artifact benchmark generation command is invalid")


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
    if payload_bytes > MAX_ARTIFACT_BENCHMARK_PAYLOAD_BYTES:
        raise ValueError("payload_bytes exceeds benchmark maximum")
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
