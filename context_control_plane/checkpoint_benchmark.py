"""Repeatable checkpoint restore benchmark and acceptance receipt."""

from __future__ import annotations

import copy
import hashlib
import math
import platform
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from .artifact_store import ArtifactRef, LocalArtifactStore
from .checkpoint import publish_checkpoint, restore_checkpoint
from .sqlite_state_store import SQLiteStateStore
from .state_mcp import RequestContext, StateMCPService


RECEIPT_SCHEMA_VERSION = "context.checkpoint-results/v1alpha1"
RESTORE_P95_LIMIT_MS = 2_000.0
CRITICAL_FIELDS_TOTAL = 18

_RECEIPT_FIELDS = {
    "schema_version",
    "observed_at",
    "provenance",
    "environment",
    "measurement",
    "acceptance",
    "authority_boundary",
    "limitations",
    "generation",
}
_PROVENANCE_PATHS = {
    "implementation_sha256": "context_control_plane/checkpoint.py",
    "benchmark_sha256": "context_control_plane/checkpoint_benchmark.py",
    "runner_sha256": "tools/run_checkpoint_benchmark.py",
    "fixture_sha256": "experiments/state/m2-01-core-fixtures.yaml",
    "registry_sha256": "schemas/registry.yaml",
    "schema_sha256": "schemas/m2-06/checkpoint-manifest.schema.json",
    "contract_test_sha256": "tests/test_m2_06_checkpoint_canary.py",
    "benchmark_test_sha256": "tests/test_m2_06_checkpoint_benchmark.py",
}
_ENVIRONMENT_FIELDS = {
    "python_version",
    "python_implementation",
    "platform",
    "machine",
    "fixture",
    "artifact_store",
    "external_services",
    "state_backend",
    "artifact_backend",
}
_MEASUREMENT_FIELDS = {
    "samples",
    "measured_operation",
    "checkpoint_ref",
    "snapshot_ref",
    "restore_p50_ms",
    "restore_p95_ms",
    "restore_max_ms",
    "restore_p95_limit_ms",
    "critical_fields_total",
    "critical_fields_recovered",
    "critical_field_recovery_percent",
    "deterministic_same_ref",
    "manifest_bytes",
    "snapshot_bytes",
    "unique_checkpoint_refs",
    "integrity_failures",
    "critical_fields_verified",
}
_ACCEPTANCE_FIELDS = {
    "restore_p95_gate_passed",
    "critical_field_gate_passed",
    "missing_or_tampered_artifact_gate",
    "stale_authority_gate",
    "external_services",
    "restore_p95_under_2s",
    "critical_projection_complete",
}
_GENERATION_FIELDS = {
    "command",
    "arguments",
    "writes_runtime_state_to_repository",
}
_EXPECTED_BOUNDARY = {
    "trusted_checkpoint_ref_required": True,
    "arbitrary_self_consistent_ref_authority": False,
    "precompact_implemented": False,
    "execution_packet_implemented": False,
    "postcompact_write_gate_implemented": False,
}
_LIMITATIONS = [
    "Results describe one host and do not predict every device.",
    "The timed operation restores an existing checkpoint and excludes StateStore reads and publication.",
    "The checkpoint ref must come from trusted state or an authorized caller.",
    "PreCompact, Execution Packet and the PostCompact write gate remain M5 work.",
]


class _BenchmarkAuthorizer:
    def authorize(self, context, action, project_id):
        return True


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _provenance(root: Path) -> dict[str, str]:
    return {
        field: _file_sha256(root / relative_path)
        for field, relative_path in _PROVENANCE_PATHS.items()
    }


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 4)


def _generation_argument_values(arguments: Any) -> dict[str, str]:
    if (
        not isinstance(arguments, list)
        or any(not isinstance(item, str) for item in arguments)
        or len(arguments) % 2 != 0
    ):
        raise ValueError("checkpoint benchmark generation arguments are invalid")
    allowed = {"--samples", "--observed-at", "--output"}
    values: dict[str, str] = {}
    for index in range(0, len(arguments), 2):
        option = arguments[index]
        value = arguments[index + 1]
        if option not in allowed or option in values or not value:
            raise ValueError("checkpoint benchmark generation arguments are invalid")
        values[option] = value
    if "--samples" not in values or "--observed-at" not in values:
        raise ValueError("checkpoint benchmark generation arguments are incomplete")
    return values


def run_checkpoint_restore_benchmark(
    artifact_root: str | Path,
    read_result: dict[str, Any],
    *,
    canonical_plan_sha256: str,
    samples: int = 40,
) -> dict[str, Any]:
    """Measure verified restore without including publication or StateStore reads."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    store = LocalArtifactStore(artifact_root)
    store.initialize()
    checkpoint_ref = publish_checkpoint(
        read_result,
        store,
        canonical_plan_sha256=canonical_plan_sha256,
    )
    duplicate_ref = publish_checkpoint(
        read_result,
        store,
        canonical_plan_sha256=canonical_plan_sha256,
    )
    manifest_ref = checkpoint_ref
    first = restore_checkpoint(
        checkpoint_ref,
        store,
        expected_project_id=read_result["snapshot"]["project"]["project_id"],
        expected_revision=read_result["revision"],
        expected_event_head=read_result["event_head"],
        expected_governance_ref=read_result["snapshot"]["project"]["governance_ref"],
        expected_plan_sha256=canonical_plan_sha256,
        expected_registry_digest=read_result["registry_digest"],
    )
    snapshot_ref = ArtifactRef.from_document(first.manifest["snapshot_ref"])
    critical_fields = {
        key: value
        for key, value in first.manifest.items()
        if key
        not in {
            "schema_version",
            "snapshot_ref",
            "critical_projection_sha256",
        }
    }
    if len(critical_fields) != CRITICAL_FIELDS_TOTAL:
        raise RuntimeError("checkpoint critical field count changed")

    latencies: list[float] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        restored = restore_checkpoint(
            checkpoint_ref,
            store,
            expected_project_id=read_result["snapshot"]["project"]["project_id"],
            expected_revision=read_result["revision"],
            expected_event_head=read_result["event_head"],
            expected_governance_ref=read_result["snapshot"]["project"]["governance_ref"],
            expected_plan_sha256=canonical_plan_sha256,
            expected_registry_digest=read_result["registry_digest"],
        )
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        recovered = {
            key: restored.manifest[key] for key in critical_fields
        }
        if recovered != critical_fields or restored.snapshot != read_result["snapshot"]:
            raise RuntimeError("checkpoint benchmark restore lost critical state")

    restore_p95 = _percentile(latencies, 0.95)
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": sys.platform,
            "machine": platform.machine(),
            "fixture": "solo-active-work",
            "artifact_store": "local-content-addressed",
            "external_services": 0,
        },
        "measurement": {
            "samples": samples,
            "measured_operation": "restore_checkpoint_only",
            "checkpoint_ref": manifest_ref.to_document(),
            "snapshot_ref": snapshot_ref.to_document(),
            "restore_p50_ms": _percentile(latencies, 0.50),
            "restore_p95_ms": restore_p95,
            "restore_max_ms": round(max(latencies), 4),
            "restore_p95_limit_ms": RESTORE_P95_LIMIT_MS,
            "critical_fields_total": CRITICAL_FIELDS_TOTAL,
            "critical_fields_recovered": CRITICAL_FIELDS_TOTAL,
            "critical_field_recovery_percent": 100.0,
            "deterministic_same_ref": checkpoint_ref == duplicate_ref,
        },
        "acceptance": {
            "restore_p95_gate_passed": restore_p95 < RESTORE_P95_LIMIT_MS,
            "critical_field_gate_passed": True,
            "missing_or_tampered_artifact_gate": "covered-by-contract-tests",
            "stale_authority_gate": "covered-by-contract-tests",
            "external_services": 0,
        },
        "authority_boundary": {
            "trusted_checkpoint_ref_required": True,
            "arbitrary_self_consistent_ref_authority": False,
            "precompact_implemented": False,
            "execution_packet_implemented": False,
            "postcompact_write_gate_implemented": False,
        },
        "limitations": list(_LIMITATIONS),
    }


def run_checkpoint_benchmark(
    root: str | Path,
    snapshot: dict[str, Any],
    *,
    samples: int = 40,
    canonical_plan_sha256: str | None = None,
    registry_digest: str | None = None,
) -> dict[str, Any]:
    """Build the zero-service local path and measure its checkpoint restore."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    plan_digest = canonical_plan_sha256 or hashlib.sha256(
        b"context.m2-06-benchmark-plan/v1"
    ).hexdigest()
    state_registry_digest = registry_digest or hashlib.sha256(
        b"context.m2-06-benchmark-registry/v1"
    ).hexdigest()

    state_store = SQLiteStateStore(root / "state.sqlite")
    state_store.initialize()
    state_store.create_project(copy.deepcopy(snapshot))
    service = StateMCPService(
        state_store,
        authorizer=_BenchmarkAuthorizer(),
        registry_digest=state_registry_digest,
        clock=lambda: snapshot["project"]["updated_at"],
        event_id_factory=lambda request_id: f"event-{request_id}",
    )
    response = service.call_tool(
        "context.state.read",
        {
            "schema_version": "context.state-mcp-request/v1alpha1",
            "request_id": "request-m2-06-benchmark-read",
            "project_id": snapshot["project"]["project_id"],
        },
        context=RequestContext(
            subject_ref="actor-m2-06-benchmark",
            authorization_ref="authorization-m2-06-benchmark",
        ),
    )
    if not response["ok"]:
        raise RuntimeError("checkpoint benchmark State MCP read failed")

    result = run_checkpoint_restore_benchmark(
        root / "artifacts",
        response["result"],
        canonical_plan_sha256=plan_digest,
        samples=samples,
    )
    measurement = result["measurement"]
    result["environment"].update(
        {
            "state_backend": SQLiteStateStore.capability_manifest.adapter_id,
            "artifact_backend": "local-content-addressed",
        }
    )
    measurement.update(
        {
            "manifest_bytes": measurement["checkpoint_ref"]["size_bytes"],
            "snapshot_bytes": measurement["snapshot_ref"]["size_bytes"],
            "unique_checkpoint_refs": 1,
            "integrity_failures": 0,
            "critical_fields_verified": CRITICAL_FIELDS_TOTAL,
        }
    )
    result["acceptance"].update(
        {
            "restore_p95_under_2s": result["acceptance"]["restore_p95_gate_passed"],
            "critical_projection_complete": result["acceptance"]["critical_field_gate_passed"],
        }
    )
    return result


def build_checkpoint_benchmark_receipt(
    *,
    root: Path,
    benchmark: dict[str, Any],
    observed_at: str,
    arguments: list[str],
) -> dict[str, Any]:
    """Bind a benchmark result to current implementation and runner evidence."""
    return {
        "schema_version": benchmark["schema_version"],
        "observed_at": observed_at,
        "provenance": _provenance(root),
        "environment": benchmark["environment"],
        "measurement": benchmark["measurement"],
        "acceptance": benchmark["acceptance"],
        "authority_boundary": benchmark["authority_boundary"],
        "limitations": benchmark["limitations"],
        "generation": {
            "command": " ".join(
                [".venv/bin/python", "tools/run_checkpoint_benchmark.py"]
                + [shlex.quote(argument) for argument in arguments]
            ),
            "arguments": list(arguments),
            "writes_runtime_state_to_repository": False,
        },
    }


def validate_checkpoint_benchmark_receipt(
    receipt: dict[str, Any],
    *,
    root: Path,
) -> None:
    """Reject stale evidence, failed gates, or broadened authority claims."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValueError("checkpoint benchmark receipt fields are invalid")
    if receipt["schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise ValueError("checkpoint benchmark receipt schema is unsupported")
    if receipt["provenance"] != _provenance(root):
        raise ValueError("checkpoint benchmark provenance is stale")
    environment = receipt["environment"]
    measurement = receipt["measurement"]
    acceptance = receipt["acceptance"]
    boundary = receipt["authority_boundary"]
    generation = receipt["generation"]
    if (
        not isinstance(environment, dict)
        or set(environment) != _ENVIRONMENT_FIELDS
        or not isinstance(measurement, dict)
        or set(measurement) != _MEASUREMENT_FIELDS
        or not isinstance(acceptance, dict)
        or set(acceptance) != _ACCEPTANCE_FIELDS
        or not isinstance(boundary, dict)
        or set(boundary) != set(_EXPECTED_BOUNDARY)
        or not isinstance(generation, dict)
        or set(generation) != _GENERATION_FIELDS
    ):
        raise ValueError("checkpoint benchmark receipt fields are invalid")
    if type(environment.get("external_services")) is not int or environment["external_services"] != 0:
        raise ValueError("checkpoint benchmark must use zero external services")
    if (
        environment["state_backend"] != SQLiteStateStore.capability_manifest.adapter_id
        or environment["artifact_backend"] != "local-content-addressed"
    ):
        raise ValueError("checkpoint benchmark backend changed")
    if type(measurement.get("samples")) is not int or measurement["samples"] <= 0:
        raise ValueError("checkpoint benchmark samples are invalid")
    if measurement.get("measured_operation") != "restore_checkpoint_only":
        raise ValueError("checkpoint benchmark operation boundary changed")
    checkpoint_ref = ArtifactRef.from_document(measurement["checkpoint_ref"])
    snapshot_ref = ArtifactRef.from_document(measurement["snapshot_ref"])
    if (
        type(measurement["manifest_bytes"]) is not int
        or measurement["manifest_bytes"] != checkpoint_ref.size_bytes
        or type(measurement["snapshot_bytes"]) is not int
        or measurement["snapshot_bytes"] != snapshot_ref.size_bytes
        or type(measurement["unique_checkpoint_refs"]) is not int
        or measurement["unique_checkpoint_refs"] != 1
        or type(measurement["integrity_failures"]) is not int
        or measurement["integrity_failures"] != 0
        or type(measurement["critical_fields_verified"]) is not int
        or measurement["critical_fields_verified"] != CRITICAL_FIELDS_TOTAL
    ):
        raise ValueError("checkpoint benchmark artifact accounting changed")
    if (
        measurement.get("critical_fields_total") != CRITICAL_FIELDS_TOTAL
        or measurement.get("critical_fields_recovered") != CRITICAL_FIELDS_TOTAL
        or measurement.get("critical_field_recovery_percent") != 100.0
        or measurement.get("deterministic_same_ref") is not True
        or acceptance.get("critical_field_gate_passed") is not True
    ):
        raise ValueError("checkpoint critical field gate failed")
    if (
        measurement.get("restore_p95_limit_ms") != RESTORE_P95_LIMIT_MS
        or type(measurement.get("restore_p50_ms")) not in {int, float}
        or type(measurement.get("restore_p95_ms")) not in {int, float}
        or type(measurement.get("restore_max_ms")) not in {int, float}
        or not math.isfinite(measurement["restore_p50_ms"])
        or not math.isfinite(measurement["restore_p95_ms"])
        or not math.isfinite(measurement["restore_max_ms"])
        or measurement["restore_p50_ms"] <= 0
        or measurement["restore_p50_ms"] > measurement["restore_p95_ms"]
        or measurement["restore_p95_ms"] > measurement["restore_max_ms"]
        or measurement.get("restore_p95_ms", RESTORE_P95_LIMIT_MS)
        >= RESTORE_P95_LIMIT_MS
        or acceptance.get("restore_p95_gate_passed") is not True
    ):
        raise ValueError("checkpoint restore p95 gate failed")
    if boundary != _EXPECTED_BOUNDARY:
        raise ValueError("checkpoint authority boundary changed")
    if any(type(value) is not bool for value in boundary.values()):
        raise ValueError("checkpoint authority boundary types are invalid")
    if receipt["limitations"] != _LIMITATIONS:
        raise ValueError("checkpoint benchmark limitations changed")
    arguments = generation["arguments"]
    argument_values = _generation_argument_values(arguments)
    expected_command = " ".join(
        [".venv/bin/python", "tools/run_checkpoint_benchmark.py"]
        + [shlex.quote(argument) for argument in arguments]
    )
    if generation["command"] != expected_command:
        raise ValueError("checkpoint benchmark generation command is inconsistent")
    if generation["writes_runtime_state_to_repository"] is not False:
        raise ValueError("checkpoint benchmark must not write runtime state")
    try:
        generated_samples = int(argument_values["--samples"])
    except ValueError as exc:
        raise ValueError("checkpoint benchmark generation samples are invalid") from exc
    if (
        generated_samples != measurement["samples"]
        or argument_values["--observed-at"] != receipt["observed_at"]
    ):
        raise ValueError("checkpoint benchmark generation does not match receipt")
    expected_acceptance = {
        "restore_p95_gate_passed": True,
        "critical_field_gate_passed": True,
        "missing_or_tampered_artifact_gate": "covered-by-contract-tests",
        "stale_authority_gate": "covered-by-contract-tests",
        "external_services": environment["external_services"],
        "restore_p95_under_2s": True,
        "critical_projection_complete": True,
    }
    boolean_acceptance_fields = {
        "restore_p95_gate_passed",
        "critical_field_gate_passed",
        "restore_p95_under_2s",
        "critical_projection_complete",
    }
    if (
        any(type(acceptance[field]) is not bool for field in boolean_acceptance_fields)
        or type(acceptance["external_services"]) is not int
        or acceptance != expected_acceptance
    ):
        raise ValueError("checkpoint benchmark acceptance does not match measurement")
