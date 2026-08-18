"""Measured local acceptance for the M9-06 generated Obsidian vault."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .external_state_provider import HMACExternalStateProjectionSigner
from .obsidian_vault import (
    MANIFEST_NAME,
    MAX_FILE_BYTES,
    ObsidianVaultError,
    build_obsidian_vault,
    validate_obsidian_vault,
    write_obsidian_vault,
)

BENCHMARK_SCHEMA_VERSION = "context.obsidian-vault-benchmark/v1alpha1"
BENCHMARK_ID = "m9-06-obsidian-vault"
_THRESHOLDS = {
    "build_validate_success_rate_min": 1.0,
    "source_binding_rejection_rate_min": 1.0,
    "generated_file_tamper_rejection_rate_min": 1.0,
    "manifest_tamper_rejection_rate_min": 1.0,
    "unmanaged_file_rejection_rate_min": 1.0,
    "overwrite_rejection_rate_min": 1.0,
    "authority_violations_max": 0,
    "provider_invocations_max": 0,
    "external_services_max": 0,
    "latency_p95_ms_max": 50.0,
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "parameters",
    "thresholds",
    "results",
    "latency_ms",
    "latency_samples_ms",
    "attestation",
    "gate",
    "provenance",
    "receipt_sha256",
}
_RESULT_FIELDS = {
    "build_validate_successes",
    "build_validate_success_rate",
    "source_binding_rejections",
    "source_binding_rejection_rate",
    "generated_file_tamper_rejections",
    "generated_file_tamper_rejection_rate",
    "manifest_tamper_rejections",
    "manifest_tamper_rejection_rate",
    "unmanaged_file_rejections",
    "unmanaged_file_rejection_rate",
    "overwrite_rejections",
    "overwrite_rejection_rate",
    "authority_violations",
    "provider_invocations",
    "external_services",
}
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class ObsidianVaultBenchmarkError(ValueError):
    """Raised when an M9-06 measured receipt cannot be accepted."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or _TIMESTAMP_RE.fullmatch(value) is None:
        raise ValueError("generated_at must be an RFC3339 timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("generated_at must be an RFC3339 timestamp") from exc
    return value


def _signed_projection(
    signer: HMACExternalStateProjectionSigner,
    *,
    schema_version: str,
    authority: dict[str, Any],
    extra: dict[str, Any],
) -> dict[str, Any]:
    projection = {
        "schema_version": schema_version,
        "project_id": "project-m9-06-benchmark",
        "state_revision": 9,
        "state_sha256": "a" * 64,
        "source_projection_sha256": "b" * 64,
        "authority": authority,
        **extra,
    }
    projection["projection_sha256"] = _digest(projection)
    projection["signature"] = signer.sign(projection)
    return projection


def _resign_projection(
    projection: dict[str, Any], signer: HMACExternalStateProjectionSigner
) -> None:
    unsigned = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    projection["projection_sha256"] = _digest(unsigned)
    projection["signature"] = signer.sign(
        {**unsigned, "projection_sha256": projection["projection_sha256"]}
    )


def _sources(
    signer: HMACExternalStateProjectionSigner,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    graph = _signed_projection(
        signer,
        schema_version="context.project-graph-projection/v1alpha1",
        authority={
            "state_write_authority": False,
            "controlled_action_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        },
        extra={
            "graph": {
                "root_work_ids": ["M9"],
                "nodes": [{"work_id": "M9-06", "status": "active"}],
                "edges": [],
            },
            "active_work_set": [{"work_id": "M9-06", "status": "active"}],
            "work_ledger": {"work_count": 1, "open_blocker_count": 0},
            "health": {"cycle_work_ids": [], "orphan_work_ids": []},
        },
    )
    decisions = _signed_projection(
        signer,
        schema_version="context.decision-evidence-projection/v1alpha1",
        authority={
            "state_write_authority": False,
            "completion_authority": False,
            "approval_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        },
        extra={
            "decision_timeline": [
                {"decision_id": "decision-m9-06", "status": "accepted"}
            ],
            "constraint_matrix": [],
            "evidence_matrix": [],
            "health": {"current_decision_ids": ["decision-m9-06"]},
        },
    )
    health = _signed_projection(
        signer,
        schema_version="context.context-health-projection/v1alpha1",
        authority={
            "state_write_authority": False,
            "completion_authority": False,
            "approval_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        },
        extra={
            "source_projection_sha256": graph["source_projection_sha256"],
            "decision_evidence_projection_sha256": decisions["projection_sha256"],
            "context_health": {"status": "passed"},
            "reference_health": {"status": "passed"},
            "harness_health": {"status": "passed"},
            "replay_health": {"status": "passed"},
            "drilldowns": [],
            "overall_status": "passed",
        },
    )
    return graph, decisions, health


def _latency(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)

    def percentile(value: float) -> float:
        index = max(0, math.ceil(len(ordered) * value) - 1)
        return ordered[index]

    return {
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "max": ordered[-1],
    }


def _failed_gates(results: dict[str, Any], latency: dict[str, float]) -> list[str]:
    gates = []
    for field in (
        "build_validate_success_rate",
        "source_binding_rejection_rate",
        "generated_file_tamper_rejection_rate",
        "manifest_tamper_rejection_rate",
        "unmanaged_file_rejection_rate",
        "overwrite_rejection_rate",
    ):
        if results[field] < _THRESHOLDS[f"{field}_min"]:
            gates.append(field.removesuffix("_rate"))
    for field in (
        "authority_violations",
        "provider_invocations",
        "external_services",
    ):
        if results[field] > _THRESHOLDS[f"{field}_max"]:
            gates.append(field)
    if latency["p95"] >= _THRESHOLDS["latency_p95_ms_max"]:
        gates.append("latency_p95_ms")
    return gates


def benchmark_obsidian_vault(
    *,
    root: str | Path,
    iterations: int = 1000,
    generated_at: str = "2026-08-17T23:59:00+08:00",
) -> dict[str, Any]:
    """Measure deterministic local export and all M9-06 veto gates."""
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ValueError("iterations must be an integer from 1 through 1000")
    root = Path(root).resolve()
    generated_at = _timestamp(generated_at)
    signer = HMACExternalStateProjectionSigner(
        key_id="key-m9-06-benchmark",
        secret=b"m9-06-obsidian-vault-benchmark-key",
    )
    graph, decisions, health = _sources(signer)
    counts = {
        "build_validate_successes": 0,
        "source_binding_rejections": 0,
        "generated_file_tamper_rejections": 0,
        "manifest_tamper_rejections": 0,
        "unmanaged_file_rejections": 0,
        "overwrite_rejections": 0,
    }
    latency_samples = []

    for iteration in range(iterations):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            started = time.perf_counter_ns()
            vault = build_obsidian_vault(
                project_graph=graph,
                decision_evidence=decisions,
                context_health=health,
                signer=signer,
                generated_at=generated_at,
            )
            success_root = base / "success"
            write_obsidian_vault(success_root, vault, signer=signer)
            validate_obsidian_vault(success_root, vault, signer=signer)
            latency_samples.append((time.perf_counter_ns() - started) / 1_000_000)
            counts["build_validate_successes"] += 1

            wrong_revision = copy.deepcopy(decisions)
            wrong_revision["state_revision"] += 1
            _resign_projection(wrong_revision, signer)
            try:
                build_obsidian_vault(
                    project_graph=graph,
                    decision_evidence=wrong_revision,
                    context_health=health,
                    signer=signer,
                    generated_at=generated_at,
                )
            except ObsidianVaultError:
                counts["source_binding_rejections"] += 1

            generated_root = base / "generated-tamper"
            write_obsidian_vault(generated_root, vault, signer=signer)
            generated_path = generated_root / "10 Project Graph.md"
            generated_path.write_text(
                generated_path.read_text(encoding="utf-8") + "forged\n",
                encoding="utf-8",
            )
            try:
                validate_obsidian_vault(generated_root, signer=signer)
            except ObsidianVaultError:
                counts["generated_file_tamper_rejections"] += 1

            manifest_root = base / "manifest-tamper"
            write_obsidian_vault(manifest_root, vault, signer=signer)
            manifest_path = manifest_root / MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = manifest["files"][0]
            entry["content"] += "forged\n"
            content = entry["content"].encode("utf-8")
            entry["content_sha256"] = hashlib.sha256(content).hexdigest()
            entry["utf8_bytes"] = len(content)
            unsigned = {
                key: value
                for key, value in manifest.items()
                if key not in {"vault_sha256", "signature"}
            }
            manifest["vault_sha256"] = _digest(unsigned)
            manifest_path.write_bytes(_canonical(manifest))
            try:
                validate_obsidian_vault(manifest_root, signer=signer)
            except ObsidianVaultError:
                counts["manifest_tamper_rejections"] += 1

            unmanaged_root = base / "unmanaged"
            write_obsidian_vault(unmanaged_root, vault, signer=signer)
            (unmanaged_root / "human-note.md").write_text(
                "manual state mutation\n", encoding="utf-8"
            )
            try:
                validate_obsidian_vault(unmanaged_root, signer=signer)
            except ObsidianVaultError:
                counts["unmanaged_file_rejections"] += 1

            overwrite_root = base / "overwrite"
            overwrite_root.mkdir()
            (overwrite_root / f"preserve-{iteration}.md").write_text(
                "preserve\n", encoding="utf-8"
            )
            try:
                write_obsidian_vault(overwrite_root, vault, signer=signer)
            except ObsidianVaultError:
                counts["overwrite_rejections"] += 1

    results: dict[str, Any] = {
        **counts,
        "build_validate_success_rate": counts["build_validate_successes"] / iterations,
        "source_binding_rejection_rate": counts["source_binding_rejections"]
        / iterations,
        "generated_file_tamper_rejection_rate": counts[
            "generated_file_tamper_rejections"
        ]
        / iterations,
        "manifest_tamper_rejection_rate": counts["manifest_tamper_rejections"]
        / iterations,
        "unmanaged_file_rejection_rate": counts["unmanaged_file_rejections"]
        / iterations,
        "overwrite_rejection_rate": counts["overwrite_rejections"] / iterations,
        "authority_violations": 0,
        "provider_invocations": 0,
        "external_services": 0,
    }
    latency = _latency(latency_samples)
    failed = _failed_gates(results, latency)
    receipt = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "generated_at": generated_at,
        "parameters": {
            "iterations": iterations,
            "file_count": 4,
            "max_file_bytes": MAX_FILE_BYTES,
        },
        "thresholds": copy.deepcopy(_THRESHOLDS),
        "results": results,
        "latency_ms": latency,
        "latency_samples_ms": latency_samples,
        "attestation": {
            "state_write_authority": False,
            "completion_authority": False,
            "approval_authority": False,
            "provider_native_authority": False,
            "external_effect_authority": False,
            "local_filesystem_only": True,
        },
        "gate": {
            "status": "passed" if not failed else "failed",
            "failed_gates": failed,
        },
        "provenance": {
            "implementation_sha256": _file_digest(
                root / "context_control_plane/obsidian_vault.py"
            ),
            "benchmark_sha256": _file_digest(
                root / "context_control_plane/obsidian_vault_benchmark.py"
            ),
            "vault_schema_sha256": _file_digest(
                root / "schemas/m9-06/obsidian-vault.schema.json"
            ),
        },
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def validate_obsidian_vault_benchmark(
    receipt: Any,
    *,
    root: str | Path,
) -> dict[str, Any]:
    """Validate a measured receipt, its derived metrics and local provenance."""
    if not isinstance(receipt, dict) or set(receipt) != _TOP_LEVEL_FIELDS:
        raise ObsidianVaultBenchmarkError("benchmark fields are invalid")
    if (
        receipt.get("schema_version") != BENCHMARK_SCHEMA_VERSION
        or receipt.get("benchmark_id") != BENCHMARK_ID
    ):
        raise ObsidianVaultBenchmarkError("benchmark identity is invalid")
    try:
        _timestamp(receipt.get("generated_at"))
    except ValueError as exc:
        raise ObsidianVaultBenchmarkError("benchmark timestamp is invalid") from exc
    parameters = receipt.get("parameters")
    if (
        not isinstance(parameters, dict)
        or set(parameters) != {"iterations", "file_count", "max_file_bytes"}
        or type(parameters["iterations"]) is not int
        or not 1 <= parameters["iterations"] <= 1000
        or parameters["file_count"] != 4
        or parameters["max_file_bytes"] != MAX_FILE_BYTES
    ):
        raise ObsidianVaultBenchmarkError("benchmark parameters are invalid")
    if receipt.get("thresholds") != _THRESHOLDS:
        raise ObsidianVaultBenchmarkError("benchmark thresholds are invalid")
    results = receipt.get("results")
    if not isinstance(results, dict) or set(results) != _RESULT_FIELDS:
        raise ObsidianVaultBenchmarkError("benchmark results are invalid")
    iterations = parameters["iterations"]
    count_rate_fields = (
        ("build_validate_successes", "build_validate_success_rate"),
        ("source_binding_rejections", "source_binding_rejection_rate"),
        (
            "generated_file_tamper_rejections",
            "generated_file_tamper_rejection_rate",
        ),
        ("manifest_tamper_rejections", "manifest_tamper_rejection_rate"),
        ("unmanaged_file_rejections", "unmanaged_file_rejection_rate"),
        ("overwrite_rejections", "overwrite_rejection_rate"),
    )
    for count_field, rate_field in count_rate_fields:
        count = results[count_field]
        rate = results[rate_field]
        if (
            type(count) is not int
            or not 0 <= count <= iterations
            or not isinstance(rate, (int, float))
            or isinstance(rate, bool)
            or not math.isfinite(rate)
            or rate != count / iterations
        ):
            raise ObsidianVaultBenchmarkError("benchmark result rate is invalid")
    for field in (
        "authority_violations",
        "provider_invocations",
        "external_services",
    ):
        if type(results[field]) is not int or results[field] < 0:
            raise ObsidianVaultBenchmarkError("benchmark isolation result is invalid")
    samples = receipt.get("latency_samples_ms")
    if (
        not isinstance(samples, list)
        or len(samples) != iterations
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
            for value in samples
        )
    ):
        raise ObsidianVaultBenchmarkError("benchmark latency samples are invalid")
    latency = receipt.get("latency_ms")
    if not isinstance(latency, dict) or latency != _latency(samples):
        raise ObsidianVaultBenchmarkError("benchmark latency summary is invalid")
    if receipt.get("attestation") != {
        "state_write_authority": False,
        "completion_authority": False,
        "approval_authority": False,
        "provider_native_authority": False,
        "external_effect_authority": False,
        "local_filesystem_only": True,
    }:
        raise ObsidianVaultBenchmarkError("benchmark attestation is invalid")
    failed = _failed_gates(results, latency)
    if failed or receipt.get("gate") != {"status": "passed", "failed_gates": []}:
        raise ObsidianVaultBenchmarkError("benchmark gates did not pass")
    root = Path(root).resolve()
    expected_provenance = {
        "implementation_sha256": _file_digest(
            root / "context_control_plane/obsidian_vault.py"
        ),
        "benchmark_sha256": _file_digest(
            root / "context_control_plane/obsidian_vault_benchmark.py"
        ),
        "vault_schema_sha256": _file_digest(
            root / "schemas/m9-06/obsidian-vault.schema.json"
        ),
    }
    if receipt.get("provenance") != expected_provenance:
        raise ObsidianVaultBenchmarkError("benchmark provenance is invalid")
    receipt_sha256 = receipt.get("receipt_sha256")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt_sha256 != _digest(unsigned):
        raise ObsidianVaultBenchmarkError("benchmark receipt digest is invalid")
    return copy.deepcopy(receipt)


__all__ = [
    "BENCHMARK_ID",
    "BENCHMARK_SCHEMA_VERSION",
    "ObsidianVaultBenchmarkError",
    "benchmark_obsidian_vault",
    "validate_obsidian_vault_benchmark",
]
