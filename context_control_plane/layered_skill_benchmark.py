"""Repeatable benchmark for bounded Skill content composition."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import math
import platform
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .compiled_skill_packet import compile_skill_packet
from .layered_skill_loader import (
    canonical_layered_skill_load_bytes,
    layered_skill_load_digest,
    load_layered_skills,
    validate_layered_skill_load_receipt,
)
from .layered_skill_plan import (
    canonical_layered_skill_load_plan_bytes,
    compile_layered_skill_load_plan,
    layered_skill_load_plan_digest,
    validate_layered_skill_load_plan,
)
from .skill_drift_quarantine import assess_skill_drift


SCHEMA_VERSION = "context.layered-skill-benchmark/v1alpha1"
LOAD_P95_LIMIT_MS = 10.0
MIN_REDUCTION_BASIS_POINTS = 6000
DEFAULT_SAMPLES = 40
WARMUP_SAMPLES = 1

_RECEIPT_FIELDS = {
    "schema_version",
    "observed_at",
    "provenance",
    "environment",
    "workload",
    "measurement",
    "acceptance",
    "authority_boundary",
    "limitations",
    "generation",
}
_PROVENANCE_PATHS = {
    "implementation_sha256": "context_control_plane/layered_skill_loader.py",
    "plan_implementation_sha256": "context_control_plane/layered_skill_plan.py",
    "benchmark_sha256": "context_control_plane/layered_skill_benchmark.py",
    "compiler_sha256": "context_control_plane/compiled_skill_packet.py",
    "drift_validator_sha256": "context_control_plane/skill_drift_quarantine.py",
    "runner_sha256": "tools/run_layered_skill_benchmark.py",
    "manifest_fixture_sha256": "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json",
    "load_fixture_sha256": "experiments/skills/m4-04-layered-skill-load-v1alpha1.json",
    "plan_fixture_sha256": "experiments/skills/m4-04-layered-skill-load-plan-v1alpha1.json",
    "schema_sha256": "schemas/m4-04/layered-skill-load.schema.json",
    "plan_schema_sha256": "schemas/m4-04/layered-skill-load-plan.schema.json",
    "contract_test_sha256": "tests/test_m4_04_layered_skill_loading.py",
    "plan_contract_test_sha256": "tests/test_m4_04_layered_skill_plan.py",
    "benchmark_test_sha256": "tests/test_m4_04_layered_skill_benchmark.py",
}
_ENVIRONMENT_FIELDS = {
    "python_version",
    "python_implementation",
    "platform",
    "machine",
    "external_services",
}
_WORKLOAD_FIELDS = {
    "fixture",
    "requested_layers",
    "layer_content_bytes",
    "bootstrap_content_range_bytes",
    "packet_content_range_bytes",
}
_MEASUREMENT_FIELDS = {
    "samples",
    "warmup_samples",
    "measured_operation",
    "latency_samples_ms",
    "load_p50_ms",
    "load_p95_ms",
    "load_max_ms",
    "load_p95_limit_ms",
    "available_skill_content_bytes",
    "validated_skill_content_bytes",
    "selected_skill_content_bytes",
    "omitted_from_composition_bytes",
    "content_reduction_basis_points",
    "baseline_repeated_selected_bytes",
    "layered_repeated_selected_bytes",
    "repeated_selected_bytes_avoided",
    "repeated_reduction_basis_points",
    "canonical_receipt_sha256",
    "receipt_digests",
    "unique_receipt_digests",
    "all_layers_control_loaded_bytes",
}
_ACCEPTANCE_FIELDS = {
    "load_p95_gate_passed",
    "reduction_gate_passed",
    "bootstrap_size_gate_passed",
    "packet_size_gate_passed",
    "deterministic_receipt_gate_passed",
    "all_layers_content_gate_passed",
    "zero_external_services_gate_passed",
}
_GENERATION_FIELDS = {
    "command",
    "arguments",
    "writes_runtime_state_to_repository",
}
_AUTHORITY_BOUNDARY = {
    "skill_content_composition_bytes_measured": True,
    "loader_validation_and_hashing_in_timed_boundary": True,
    "plan_authorization_gate_measured": True,
    "state_revision_authorization_measured": False,
    "synthetic_plan_authorizer": True,
    "frozen_clock": True,
    "all_verified_assets_resident_in_memory": True,
    "source_io_read_reduction_measured": False,
    "provider_input_tokens_measured": False,
    "provider_compaction_measured": False,
    "timing_cryptographically_attested": False,
}
_LIMITATIONS = [
    "Results describe one host and do not predict every device.",
    "The timed boundary includes packet, drift, binding and content digest validation plus in-memory Skill selection.",
    "The benchmark uses a synthetic allow-list authorizer and frozen clock; State MCP revision, actor, expiry and live-time authorization remain M4-09/M8 integration work.",
    "All verified Skill bytes are resident in memory; this result does not measure source or filesystem read reduction.",
    "Selected Skill content bytes are a composition proxy, not provider input tokens, billable tokens or prompt-cache behavior.",
    "Provider adapters, end-to-end restore and live compaction remain M4-05 and M5 work.",
    "Latency samples are trusted local-runner evidence; cryptographic CI or OTel timing attestation remains M8 work.",
]
_EXPECTED_LAYER_BYTES = {"S0": 1024, "S1": 4096, "S2": 4096, "S3": 16384}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class _BenchmarkPlanAuthorizer:
    def __init__(self, plan_sha256: str):
        self.plan_sha256 = plan_sha256

    def authorize(self, *, plan_sha256, packet_sha256, manifest_set_sha256):
        if plan_sha256 == self.plan_sha256:
            return "authorization.m4-04.benchmark"
        return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _registry_entries_sha256(path: Path) -> str:
    registry = yaml.safe_load(path.read_text(encoding="utf-8"))
    expected_ids = {
        "context.layered-skill-load",
        "context.layered-skill-load-plan",
    }
    entries = [
        entry
        for entry in registry.get("schemas", [])
        if entry.get("schema_id") in expected_ids
    ]
    if len(entries) != 2 or {entry.get("schema_id") for entry in entries} != expected_ids:
        raise ValueError("layered Skill schema registry entries are missing or duplicated")
    canonical = json.dumps(
        sorted(entries, key=lambda entry: entry["schema_id"]),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _provenance(root: Path) -> dict[str, str]:
    result = {
        field: _file_sha256(root / relative_path)
        for field, relative_path in _PROVENANCE_PATHS.items()
    }
    result["registry_entries_sha256"] = _registry_entries_sha256(
        root / "schemas" / "registry.yaml"
    )
    return result


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 4)


def _benchmark_case(root: Path, observed_at: str):
    manifest_fixture = json.loads(
        (root / _PROVENANCE_PATHS["manifest_fixture_sha256"]).read_text(
            encoding="utf-8"
        )
    )
    layer_specs = (
        ("core.bootstrap", "S0", b"b" * 1024),
        ("core.manifest", "S1", b"m" * 4096),
        ("project.active", "S2", b"a" * 4096),
        ("external.reference", "S3", b"r" * 16384),
    )
    template = manifest_fixture["manifests"][0]
    manifests = []
    assets = {}
    layer_bindings = {}
    for skill_id, layer, content in layer_specs:
        manifest = copy.deepcopy(template)
        manifest["skill_id"] = skill_id
        manifest["version"] = "1.0.0"
        manifest["content_sha256"] = hashlib.sha256(content).hexdigest()
        manifest["rule_ids"] = [f"{skill_id}.rule"]
        manifest["dependencies"] = []
        manifest["conflicts"] = []
        manifest["expires_at"] = None
        manifests.append(manifest)
        assets[skill_id] = content
        layer_bindings[skill_id] = layer
    manifest_set = {
        "schema_version": manifest_fixture["schema_version"],
        "manifests": manifests,
    }
    packet = compile_skill_packet(
        manifest_set,
        selected_skill_ids=[item[0] for item in layer_specs],
        observed_at=observed_at,
    )
    assessment = assess_skill_drift(
        packet,
        manifest_set,
        asset_resolver=lambda skill_id: assets[skill_id],
        observed_at=observed_at,
    )
    if assessment["gate"] != "allow":
        raise RuntimeError("benchmark drift assessment did not allow the corpus")
    plan = compile_layered_skill_load_plan(
        packet,
        layer_bindings=layer_bindings,
        requested_layers=["S0", "S2"],
        observed_at=observed_at,
    )
    return packet, assessment, assets, layer_bindings, plan


def run_layered_skill_benchmark(
    root: str | Path,
    *,
    samples: int = DEFAULT_SAMPLES,
    observed_at: str,
    arguments: list[str],
) -> dict[str, Any]:
    """Measure deterministic S0+S2 selection from the fixed four-layer corpus."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    if not isinstance(arguments, list) or any(
        not isinstance(argument, str) for argument in arguments
    ):
        raise ValueError("benchmark arguments must be strings")
    root = Path(root)
    provenance = _provenance(root)
    packet, assessment, assets, layer_bindings, layer_plan = _benchmark_case(
        root,
        observed_at,
    )
    load_kwargs = {
        "layer_plan": layer_plan,
        "plan_authorizer": _BenchmarkPlanAuthorizer(
            layered_skill_load_plan_digest(layer_plan)
        ),
        "trusted_clock": lambda: datetime.datetime.fromisoformat(
            observed_at.replace("Z", "+00:00")
        ),
    }

    for _ in range(WARMUP_SAMPLES):
        load_layered_skills(packet, assessment, assets, **load_kwargs)
    all_layers_plan = compile_layered_skill_load_plan(
        packet,
        layer_bindings=layer_bindings,
        requested_layers=["S0", "S1", "S2", "S3"],
        observed_at=observed_at,
    )
    all_layers = load_layered_skills(
        packet,
        assessment,
        assets,
        layer_plan=all_layers_plan,
        plan_authorizer=_BenchmarkPlanAuthorizer(
            layered_skill_load_plan_digest(all_layers_plan)
        ),
        trusted_clock=lambda: datetime.datetime.fromisoformat(
            observed_at.replace("Z", "+00:00")
        ),
    )

    latencies = []
    receipt_digests = []
    final_receipt = None
    for _ in range(samples):
        started = time.perf_counter_ns()
        result = load_layered_skills(packet, assessment, assets, **load_kwargs)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        latencies.append(round(elapsed_ms, 6))
        receipt_digests.append(layered_skill_load_digest(result.receipt))
        final_receipt = result.receipt
    if final_receipt is None:
        raise RuntimeError("benchmark produced no load receipt")

    available = final_receipt["available_content_bytes"]
    selected = final_receipt["loaded_content_bytes"]
    baseline_repeated = available * samples
    layered_repeated = selected * samples
    p95 = _percentile(latencies, 0.95)
    measurement = {
        "samples": samples,
        "warmup_samples": WARMUP_SAMPLES,
        "measured_operation": "verified_in_memory_layered_skill_load",
        "latency_samples_ms": latencies,
        "load_p50_ms": _percentile(latencies, 0.50),
        "load_p95_ms": p95,
        "load_max_ms": round(max(latencies), 4),
        "load_p95_limit_ms": LOAD_P95_LIMIT_MS,
        "available_skill_content_bytes": available,
        "validated_skill_content_bytes": available,
        "selected_skill_content_bytes": selected,
        "omitted_from_composition_bytes": available - selected,
        "content_reduction_basis_points": final_receipt[
            "content_reduction_basis_points"
        ],
        "baseline_repeated_selected_bytes": baseline_repeated,
        "layered_repeated_selected_bytes": layered_repeated,
        "repeated_selected_bytes_avoided": baseline_repeated - layered_repeated,
        "repeated_reduction_basis_points": (
            (baseline_repeated - layered_repeated) * 10000 // baseline_repeated
        ),
        "canonical_receipt_sha256": layered_skill_load_digest(final_receipt),
        "receipt_digests": receipt_digests,
        "unique_receipt_digests": len(set(receipt_digests)),
        "all_layers_control_loaded_bytes": all_layers.receipt[
            "loaded_content_bytes"
        ],
    }
    acceptance = {
        "load_p95_gate_passed": p95 < LOAD_P95_LIMIT_MS,
        "reduction_gate_passed": measurement["repeated_reduction_basis_points"]
        >= MIN_REDUCTION_BASIS_POINTS,
        "bootstrap_size_gate_passed": 1024 <= _EXPECTED_LAYER_BYTES["S0"] <= 2048,
        "packet_size_gate_passed": 2048 <= _EXPECTED_LAYER_BYTES["S2"] <= 6144,
        "deterministic_receipt_gate_passed": len(set(receipt_digests)) == 1,
        "all_layers_content_gate_passed": all_layers.receipt[
            "loaded_content_bytes"
        ]
        == available,
        "zero_external_services_gate_passed": True,
    }
    if _provenance(root) != provenance:
        raise RuntimeError("benchmark source changed during measurement")
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "provenance": provenance,
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": sys.platform,
            "machine": platform.machine(),
            "external_services": 0,
        },
        "workload": {
            "fixture": "m4-04-four-layer-synthetic-corpus",
            "requested_layers": ["S0", "S2"],
            "layer_content_bytes": dict(_EXPECTED_LAYER_BYTES),
            "bootstrap_content_range_bytes": [1024, 2048],
            "packet_content_range_bytes": [2048, 6144],
        },
        "measurement": measurement,
        "acceptance": acceptance,
        "authority_boundary": dict(_AUTHORITY_BOUNDARY),
        "limitations": list(_LIMITATIONS),
        "generation": {
            "command": " ".join(
                [".venv/bin/python", "tools/run_layered_skill_benchmark.py"]
                + [shlex.quote(argument) for argument in arguments]
            ),
            "arguments": list(arguments),
            "writes_runtime_state_to_repository": False,
        },
    }


def _argument_values(arguments: Any) -> dict[str, str]:
    if (
        not isinstance(arguments, list)
        or len(arguments) % 2 != 0
        or any(not isinstance(argument, str) for argument in arguments)
    ):
        raise ValueError("benchmark generation arguments are invalid")
    allowed = {"--samples", "--observed-at", "--output"}
    values = {}
    for index in range(0, len(arguments), 2):
        option = arguments[index]
        value = arguments[index + 1]
        if option not in allowed or option in values or not value:
            raise ValueError("benchmark generation arguments are invalid")
        values[option] = value
    if set(values) != allowed:
        raise ValueError("benchmark generation arguments are incomplete")
    return values


def validate_layered_skill_benchmark_receipt(
    receipt: dict[str, Any],
    *,
    root: str | Path,
) -> None:
    """Reject malformed, stale, inconsistent, or failed M4-04 evidence."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValueError("layered Skill benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("layered Skill benchmark schema is unsupported")
    root = Path(root)
    provenance = receipt["provenance"]
    expected_provenance = _provenance(root)
    if (
        not isinstance(provenance, dict)
        or set(provenance) != set(expected_provenance)
        or any(
            not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
            for value in provenance.values()
        )
        or provenance != expected_provenance
    ):
        raise ValueError("layered Skill benchmark provenance is stale or malformed")

    environment = receipt["environment"]
    workload = receipt["workload"]
    measurement = receipt["measurement"]
    acceptance = receipt["acceptance"]
    boundary = receipt["authority_boundary"]
    generation = receipt["generation"]
    if (
        not isinstance(environment, dict)
        or set(environment) != _ENVIRONMENT_FIELDS
        or not isinstance(workload, dict)
        or set(workload) != _WORKLOAD_FIELDS
        or not isinstance(measurement, dict)
        or set(measurement) != _MEASUREMENT_FIELDS
        or not isinstance(acceptance, dict)
        or set(acceptance) != _ACCEPTANCE_FIELDS
        or not isinstance(boundary, dict)
        or set(boundary) != set(_AUTHORITY_BOUNDARY)
        or not isinstance(generation, dict)
        or set(generation) != _GENERATION_FIELDS
    ):
        raise ValueError("layered Skill benchmark nested fields are invalid")
    if (
        type(environment["external_services"]) is not int
        or environment["external_services"] != 0
        or any(
            not isinstance(environment[field], str)
            or not environment[field]
            or len(environment[field]) > 512
            for field in (
                "python_version",
                "python_implementation",
                "platform",
                "machine",
            )
        )
    ):
        raise ValueError("layered Skill benchmark environment is invalid")
    if workload != {
        "fixture": "m4-04-four-layer-synthetic-corpus",
        "requested_layers": ["S0", "S2"],
        "layer_content_bytes": _EXPECTED_LAYER_BYTES,
        "bootstrap_content_range_bytes": [1024, 2048],
        "packet_content_range_bytes": [2048, 6144],
    }:
        raise ValueError("layered Skill benchmark workload changed")
    samples = measurement.get("samples")
    latencies = measurement.get("latency_samples_ms")
    if (
        type(samples) is not int
        or samples != DEFAULT_SAMPLES
        or type(measurement.get("warmup_samples")) is not int
        or measurement["warmup_samples"] != WARMUP_SAMPLES
        or measurement.get("measured_operation")
        != "verified_in_memory_layered_skill_load"
        or not isinstance(latencies, list)
        or len(latencies) != samples
        or any(
            type(value) not in {int, float}
            or not math.isfinite(value)
            or value <= 0
            for value in latencies
        )
    ):
        raise ValueError("layered Skill benchmark latency samples are invalid")
    p50 = _percentile(latencies, 0.50)
    p95 = _percentile(latencies, 0.95)
    maximum = round(max(latencies), 4)
    if (
        any(
            type(measurement.get(field)) not in {int, float}
            or not math.isfinite(measurement[field])
            for field in (
                "load_p50_ms",
                "load_p95_ms",
                "load_max_ms",
                "load_p95_limit_ms",
            )
        )
        or measurement["load_p50_ms"] != p50
        or measurement["load_p95_ms"] != p95
        or measurement["load_max_ms"] != maximum
        or measurement["load_p95_limit_ms"] != LOAD_P95_LIMIT_MS
        or p95 >= LOAD_P95_LIMIT_MS
    ):
        raise ValueError("layered Skill benchmark latency metrics are inconsistent")

    available = sum(_EXPECTED_LAYER_BYTES.values())
    selected = _EXPECTED_LAYER_BYTES["S0"] + _EXPECTED_LAYER_BYTES["S2"]
    baseline_repeated = available * samples
    layered_repeated = selected * samples
    expected_metrics = {
        "available_skill_content_bytes": available,
        "validated_skill_content_bytes": available,
        "selected_skill_content_bytes": selected,
        "omitted_from_composition_bytes": available - selected,
        "content_reduction_basis_points": 8000,
        "baseline_repeated_selected_bytes": baseline_repeated,
        "layered_repeated_selected_bytes": layered_repeated,
        "repeated_selected_bytes_avoided": baseline_repeated - layered_repeated,
        "repeated_reduction_basis_points": 8000,
        "unique_receipt_digests": 1,
        "all_layers_control_loaded_bytes": available,
    }
    if any(
        type(measurement.get(field)) is not int or measurement[field] != value
        for field, value in expected_metrics.items()
    ):
        raise ValueError("layered Skill benchmark byte metrics are inconsistent")
    digest = measurement.get("canonical_receipt_sha256")
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise ValueError("layered Skill benchmark receipt digest is invalid")
    try:
        packet, assessment, assets, _, layer_plan = _benchmark_case(
            root,
            receipt["observed_at"],
        )
        expected_load = load_layered_skills(
            packet,
            assessment,
            assets,
            layer_plan=layer_plan,
            plan_authorizer=_BenchmarkPlanAuthorizer(
                layered_skill_load_plan_digest(layer_plan)
            ),
            trusted_clock=lambda: datetime.datetime.fromisoformat(
                receipt["observed_at"].replace("Z", "+00:00")
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("layered Skill benchmark observed_at is invalid") from exc
    if digest != layered_skill_load_digest(expected_load.receipt):
        raise ValueError("layered Skill benchmark receipt digest is inconsistent")
    receipt_digests = measurement.get("receipt_digests")
    if (
        not isinstance(receipt_digests, list)
        or len(receipt_digests) != samples
        or any(
            not isinstance(item, str) or not _SHA256_RE.fullmatch(item)
            for item in receipt_digests
        )
        or set(receipt_digests) != {digest}
    ):
        raise ValueError("layered Skill benchmark receipt digests are inconsistent")

    fixture_path = root / _PROVENANCE_PATHS["load_fixture_sha256"]
    try:
        fixture_bytes = fixture_path.read_bytes()
        fixture = json.loads(fixture_bytes)
        validate_layered_skill_load_receipt(fixture)
        fixture_packet, fixture_assessment, fixture_assets, _, fixture_plan = (
            _benchmark_case(root, fixture["loaded_at"])
        )
        expected_fixture = load_layered_skills(
            fixture_packet,
            fixture_assessment,
            fixture_assets,
            layer_plan=fixture_plan,
            plan_authorizer=_BenchmarkPlanAuthorizer(
                layered_skill_load_plan_digest(fixture_plan)
            ),
            trusted_clock=lambda: datetime.datetime.fromisoformat(
                fixture["loaded_at"].replace("Z", "+00:00")
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("layered Skill benchmark load fixture is invalid") from exc
    if (
        fixture_bytes != canonical_layered_skill_load_bytes(fixture) + b"\n"
        or layered_skill_load_digest(fixture)
        != layered_skill_load_digest(expected_fixture.receipt)
    ):
        raise ValueError("layered Skill benchmark load fixture does not match current output")

    plan_fixture_path = root / _PROVENANCE_PATHS["plan_fixture_sha256"]
    try:
        plan_fixture_bytes = plan_fixture_path.read_bytes()
        plan_fixture = json.loads(plan_fixture_bytes)
        validate_layered_skill_load_plan(plan_fixture)
        _, _, _, _, current_plan = _benchmark_case(
            root, plan_fixture["observed_at"]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("layered Skill benchmark plan fixture is invalid") from exc
    if (
        plan_fixture_bytes
        != canonical_layered_skill_load_plan_bytes(plan_fixture) + b"\n"
        or layered_skill_load_plan_digest(plan_fixture)
        != layered_skill_load_plan_digest(current_plan)
    ):
        raise ValueError(
            "layered Skill benchmark plan fixture does not match current output"
        )

    expected_acceptance = {
        "load_p95_gate_passed": True,
        "reduction_gate_passed": True,
        "bootstrap_size_gate_passed": True,
        "packet_size_gate_passed": True,
        "deterministic_receipt_gate_passed": True,
        "all_layers_content_gate_passed": True,
        "zero_external_services_gate_passed": True,
    }
    if acceptance != expected_acceptance or any(
        type(value) is not bool for value in acceptance.values()
    ):
        raise ValueError("layered Skill benchmark acceptance gates failed")
    if boundary != _AUTHORITY_BOUNDARY or any(
        type(value) is not bool for value in boundary.values()
    ):
        raise ValueError("layered Skill benchmark authority boundary changed")
    if receipt["limitations"] != _LIMITATIONS:
        raise ValueError("layered Skill benchmark limitations changed")

    values = _argument_values(generation.get("arguments"))
    if (
        values["--samples"] != str(samples)
        or values["--observed-at"] != receipt["observed_at"]
        or generation.get("writes_runtime_state_to_repository") is not False
    ):
        raise ValueError("layered Skill benchmark generation metadata is invalid")
    expected_command = " ".join(
        [".venv/bin/python", "tools/run_layered_skill_benchmark.py"]
        + [shlex.quote(argument) for argument in generation["arguments"]]
    )
    if generation.get("command") != expected_command:
        raise ValueError("layered Skill benchmark command is inconsistent")
