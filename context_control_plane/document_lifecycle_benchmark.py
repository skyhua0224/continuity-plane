"""Quantitative M0-10 document lifecycle benchmark."""

from __future__ import annotations

import hashlib
import math
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from context_control_plane.document_lifecycle import (
    _second_level_sections,
    _validate_document_control_manifest_content,
    validate_document_control_manifest,
)


class DocumentLifecycleBenchmarkError(ValueError):
    """Raised when benchmark evidence is invalid or stale."""


BENCHMARK_SAMPLE_COUNT = 40
BENCHMARK_CONFIG_PATH = Path("profiles/document-lifecycle-benchmark-config.yaml")
BENCHMARK_CONFIG_VERSION = "context.document-lifecycle-benchmark-config/v1alpha1"
BENCHMARK_RECEIPT_VERSION = "context.document-lifecycle-benchmark/v1alpha1"
_GIT_REF_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT_SECONDS = 2
_MAX_BASELINE_BLOB_BYTES = 8 * 1024 * 1024


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git_blob(root: Path, revision: str, path: str) -> bytes:
    try:
        object_ref = f"{revision}:{path}"
        object_type = subprocess.run(
            ["git", "cat-file", "-t", object_ref],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
        if object_type != b"blob":
            raise DocumentLifecycleBenchmarkError(
                f"governed baseline does not resolve a blob: {object_ref}"
            )
        size = int(
            subprocess.run(
                ["git", "cat-file", "-s", object_ref],
                cwd=root,
                check=True,
                capture_output=True,
                timeout=_GIT_TIMEOUT_SECONDS,
            ).stdout.strip()
        )
        if size > _MAX_BASELINE_BLOB_BYTES:
            raise DocumentLifecycleBenchmarkError(
                f"governed baseline blob exceeds 8 MiB: {object_ref}"
            )
        content = subprocess.run(
            ["git", "show", object_ref],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        ).stdout
    except (
        OSError,
        ValueError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        raise DocumentLifecycleBenchmarkError(
            "governed baseline Git lookup failed"
        ) from exc
    if len(content) != size:
        raise DocumentLifecycleBenchmarkError(
            f"governed baseline blob size drift: {object_ref}"
        )
    return content


def load_document_lifecycle_benchmark_baseline(
    root: Path, baseline_ref: str
) -> tuple[bytes, bytes]:
    """Read the two governed baseline documents through bounded Git lookups."""
    return (
        _git_blob(root.resolve(), baseline_ref, "STATUS.md"),
        _git_blob(root.resolve(), baseline_ref, "MASTER.md"),
    )


def load_document_lifecycle_benchmark_config(
    root: Path, *, document: object | None = None
) -> dict[str, Any]:
    """Load and validate the repository-owned benchmark baseline and gates."""
    root = root.resolve()
    if document is None:
        try:
            document = yaml.safe_load(
                (root / BENCHMARK_CONFIG_PATH).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise DocumentLifecycleBenchmarkError(
                "benchmark config is unreadable"
            ) from exc
    expected_fields = {
        "schema_version",
        "baseline_git_ref",
        "validator_samples",
        "validator_p95_limit_ms",
    }
    if not isinstance(document, dict) or set(document) != expected_fields:
        raise DocumentLifecycleBenchmarkError("benchmark config fields are invalid")
    if document["schema_version"] != BENCHMARK_CONFIG_VERSION:
        raise DocumentLifecycleBenchmarkError("unsupported benchmark config")
    baseline_ref = document["baseline_git_ref"]
    if not isinstance(baseline_ref, str) or not _GIT_REF_RE.fullmatch(baseline_ref):
        raise DocumentLifecycleBenchmarkError("benchmark baseline_git_ref is invalid")
    try:
        object_type = subprocess.run(
            ["git", "cat-file", "-t", baseline_ref],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
        reachable = subprocess.run(
            ["git", "merge-base", "--is-ancestor", baseline_ref, "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DocumentLifecycleBenchmarkError(
            "benchmark baseline Git lookup failed"
        ) from exc
    if object_type != b"commit" or reachable.returncode != 0:
        raise DocumentLifecycleBenchmarkError(
            "benchmark baseline must identify a reachable commit"
        )
    if document["validator_samples"] != BENCHMARK_SAMPLE_COUNT:
        raise DocumentLifecycleBenchmarkError(
            "benchmark validator_samples must be exactly 40"
        )
    p95_limit = document["validator_p95_limit_ms"]
    if p95_limit != 100 or isinstance(p95_limit, bool):
        raise DocumentLifecycleBenchmarkError(
            "benchmark p95 limit must be exactly 100 ms"
        )
    return dict(document)


def _validate_governed_inputs(
    root: Path,
    *,
    baseline_ref: str,
    baseline_status: bytes,
    baseline_master: bytes,
    samples: int,
) -> dict[str, Any]:
    config = load_document_lifecycle_benchmark_config(root)
    if baseline_ref != config["baseline_git_ref"]:
        raise DocumentLifecycleBenchmarkError("benchmark governed baseline mismatch")
    if samples < BENCHMARK_SAMPLE_COUNT:
        raise DocumentLifecycleBenchmarkError(
            "samples must be at least 40 and exactly 40"
        )
    if samples != config["validator_samples"]:
        raise DocumentLifecycleBenchmarkError("samples must be exactly 40")
    expected_status, expected_master = load_document_lifecycle_benchmark_baseline(
        root, baseline_ref
    )
    if baseline_status != expected_status:
        raise DocumentLifecycleBenchmarkError("governed baseline STATUS drift")
    if baseline_master != expected_master:
        raise DocumentLifecycleBenchmarkError("governed baseline MASTER drift")
    return config


def _percent_reduction(before: int, after: int) -> float:
    if before <= 0:
        raise DocumentLifecycleBenchmarkError("baseline bytes must be positive")
    return round((before - after) / before * 100, 4)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[rank], 4)


def _static_metrics(
    root: Path,
    manifest: dict[str, Any],
    *,
    baseline_status: bytes,
    baseline_master: bytes,
) -> dict[str, int | float]:
    status = (root / "STATUS.md").read_bytes()
    master = (root / "MASTER.md").read_bytes()
    baseline_master_sections = _second_level_sections(baseline_master.decode("utf-8"))
    current_master_sections = _second_level_sections(master.decode("utf-8"))
    validation = validate_document_control_manifest(root, manifest)
    baseline_max = max(size for _, size in baseline_master_sections)
    current_max = max(size for _, size in current_master_sections)
    return {
        "baseline_status_utf8_bytes": len(baseline_status),
        "current_status_utf8_bytes": len(status),
        "status_bytes_reduction_percent": _percent_reduction(
            len(baseline_status), len(status)
        ),
        "baseline_master_max_section_utf8_bytes": baseline_max,
        "current_master_max_section_utf8_bytes": current_max,
        "master_max_section_reduction_percent": _percent_reduction(
            baseline_max, current_max
        ),
        "recovery_fields_total": validation["recovery_fields_total"],
        "recovery_fields_recovered": validation["recovery_fields_recovered"],
        "managed_document_count": validation["document_count"],
    }


def _provenance(
    root: Path, manifest: dict[str, Any], baseline_ref: str
) -> dict[str, str]:
    manifest_path = root / "profiles" / "document-control-manifest.yaml"
    return {
        "baseline_git_ref": baseline_ref,
        "baseline_status_path": "STATUS.md",
        "baseline_master_path": "MASTER.md",
        "current_status_sha256": _sha256(root / "STATUS.md"),
        "current_master_sha256": _sha256(root / "MASTER.md"),
        "manifest_sha256": _sha256(manifest_path),
        "implementation_sha256": _sha256(
            root / "context_control_plane" / "document_lifecycle.py"
        ),
        "benchmark_implementation_sha256": _sha256(Path(__file__)),
        "benchmark_config_sha256": _sha256(root / BENCHMARK_CONFIG_PATH),
    }


def measure_document_lifecycle_validator(
    root: Path, manifest: dict[str, Any], *, samples: int
) -> dict[str, int | float]:
    """Measure document validation after a single full provenance preflight."""
    if samples < BENCHMARK_SAMPLE_COUNT:
        raise DocumentLifecycleBenchmarkError(
            "samples must be at least 40 and exactly 40"
        )
    if samples != BENCHMARK_SAMPLE_COUNT:
        raise DocumentLifecycleBenchmarkError("samples must be exactly 40")
    root = root.resolve()
    _validate_document_lifecycle_preflight(root, manifest)
    durations: list[float] = []
    failures = 0
    for _ in range(samples):
        started = time.perf_counter_ns()
        try:
            _validate_document_lifecycle_content(root, manifest)
        except ValueError:
            failures += 1
        durations.append(round((time.perf_counter_ns() - started) / 1_000_000, 4))
    return {
        "validator_samples": samples,
        "validator_failures": failures,
        "validator_p50_ms": _percentile(durations, 0.50),
        "validator_p95_ms": _percentile(durations, 0.95),
        "validator_max_ms": max(durations),
    }


def _validate_document_lifecycle_preflight(
    root: Path, manifest: dict[str, Any]
) -> None:
    """Run the complete public validator once before benchmark timing."""
    validate_document_control_manifest(root, manifest)


def _validate_document_lifecycle_content(root: Path, manifest: dict[str, Any]) -> None:
    """Validate the verified in-memory manifest without repeating Git history I/O."""
    _validate_document_control_manifest_content(root, manifest)


def benchmark_document_lifecycle(
    root: Path,
    manifest: dict[str, Any],
    *,
    baseline_ref: str,
    baseline_status: bytes,
    baseline_master: bytes,
    generated_at: str,
    samples: int,
) -> dict[str, Any]:
    """Measure current document validation and capacity against a pinned baseline."""
    root = root.resolve()
    _validate_governed_inputs(
        root,
        baseline_ref=baseline_ref,
        baseline_status=baseline_status,
        baseline_master=baseline_master,
        samples=samples,
    )
    static = _static_metrics(
        root,
        manifest,
        baseline_status=baseline_status,
        baseline_master=baseline_master,
    )
    return {
        "schema_version": BENCHMARK_RECEIPT_VERSION,
        "generated_at": generated_at,
        "metrics": static,
        "provenance": _provenance(root, manifest, baseline_ref),
    }


def validate_document_lifecycle_benchmark_receipt(
    root: Path,
    manifest: dict[str, Any],
    receipt: dict[str, Any],
    *,
    baseline_ref: str,
    baseline_status: bytes,
    baseline_master: bytes,
) -> dict[str, int | float]:
    """Recompute receipt fields that must remain exact after publication."""
    root = root.resolve()
    config = _validate_governed_inputs(
        root,
        baseline_ref=baseline_ref,
        baseline_status=baseline_status,
        baseline_master=baseline_master,
        samples=BENCHMARK_SAMPLE_COUNT,
    )
    receipt_fields = {"schema_version", "generated_at", "metrics", "provenance"}
    if not isinstance(receipt, dict) or set(receipt) != receipt_fields:
        raise DocumentLifecycleBenchmarkError("benchmark receipt fields are invalid")
    if receipt.get("schema_version") != BENCHMARK_RECEIPT_VERSION:
        raise DocumentLifecycleBenchmarkError("unsupported benchmark receipt")
    generated_at = receipt.get("generated_at")
    if not isinstance(generated_at, str):
        raise DocumentLifecycleBenchmarkError("generated_at must be RFC3339")
    try:
        parsed_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DocumentLifecycleBenchmarkError("generated_at must be RFC3339") from exc
    if parsed_at.tzinfo is None:
        raise DocumentLifecycleBenchmarkError("generated_at must be RFC3339")
    metrics = receipt.get("metrics")
    provenance = receipt.get("provenance")
    if not isinstance(metrics, dict) or not isinstance(provenance, dict):
        raise DocumentLifecycleBenchmarkError("benchmark receipt fields are invalid")
    metric_fields = {
        "baseline_status_utf8_bytes",
        "current_status_utf8_bytes",
        "status_bytes_reduction_percent",
        "baseline_master_max_section_utf8_bytes",
        "current_master_max_section_utf8_bytes",
        "master_max_section_reduction_percent",
        "recovery_fields_total",
        "recovery_fields_recovered",
        "managed_document_count",
    }
    if set(metrics) != metric_fields:
        raise DocumentLifecycleBenchmarkError("benchmark metrics fields are invalid")
    expected_static = _static_metrics(
        root,
        manifest,
        baseline_status=baseline_status,
        baseline_master=baseline_master,
    )
    for field, expected in expected_static.items():
        if metrics.get(field) != expected:
            raise DocumentLifecycleBenchmarkError(
                f"benchmark static metric mismatch: {field}"
            )
    expected_provenance = _provenance(root, manifest, baseline_ref)
    if provenance != expected_provenance:
        raise DocumentLifecycleBenchmarkError("benchmark provenance mismatch")
    if metrics.get("recovery_fields_recovered") != metrics.get("recovery_fields_total"):
        raise DocumentLifecycleBenchmarkError("recovery fields are incomplete")
    live = measure_document_lifecycle_validator(
        root, manifest, samples=BENCHMARK_SAMPLE_COUNT
    )
    if live["validator_failures"] != 0:
        raise DocumentLifecycleBenchmarkError("live validator failures must be zero")
    p95_limit = config["validator_p95_limit_ms"]
    if live["validator_p95_ms"] >= p95_limit:
        raise DocumentLifecycleBenchmarkError(
            f"live validator p95 must be less than {p95_limit:g} ms"
        )
    return live
