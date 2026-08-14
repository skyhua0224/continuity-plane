"""Local replay and latency benchmark for M5-02 PreCompact deltas."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml

from .artifact_store import LocalArtifactStore
from .compaction_checkpoint import (
    DeepSeekCompactionHookAdapter,
    PiCompactionHookAdapter,
    build_material_event_delta,
    canonical_material_event_delta_bytes,
    publish_material_event_delta,
    validate_material_event_delta,
    validate_provider_compaction_hook_receipt,
)
from .execution_packet_benchmark import benchmark_fixture

SCHEMA_VERSION = "context.compaction-checkpoint-benchmark/v1alpha1"
PRECOMPACT_P95_LIMIT_MS = 500.0

_PROVENANCE_PATHS = {
    "implementation_sha256": "context_control_plane/compaction_checkpoint.py",
    "benchmark_sha256": "context_control_plane/compaction_checkpoint_benchmark.py",
    "runner_sha256": "tools/run_compaction_checkpoint_benchmark.py",
    "delta_schema_sha256": "schemas/m5-02/material-event-delta.schema.json",
    "hook_schema_sha256": "schemas/m5-02/provider-compaction-hook.schema.json",
    "test_sha256": "tests/test_m5_02_compaction_checkpoint.py",
    "fixture_sha256": "experiments/skills/m5-01-execution-packet-replay-v1alpha1.json",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry_entry_sha256(root: Path, schema_id: str) -> str:
    registry = yaml.safe_load((root / "schemas/registry.yaml").read_text(encoding="utf-8"))
    entry = [item for item in registry["schemas"] if item["schema_id"] == schema_id]
    if len(entry) != 1:
        raise ValueError(f"registry entry is missing: {schema_id}")
    return hashlib.sha256(
        json.dumps(entry[0], ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _event(project_id: str) -> dict[str, Any]:
    event = {
        "schema_version": "context.state-event/v4alpha1",
        "event_id": "event-m5-02-benchmark",
        "event_type": "state-transition",
        "project_id": project_id,
        "sequence_no": 1,
        "revision_before": 7,
        "revision_after": 7,
        "occurred_at": "2026-08-14T20:00:00+08:00",
        "actor_ref": "actor://benchmark",
        "causation_ref": "run:m5-02-benchmark",
        "correlation_ref": "task:m5-02",
        "previous_event_sha256": None,
        "supersedes_event_id": None,
        "changes": [],
        "project_after": {"project_id": project_id, "revision": 7},
        "task_transition": None,
        "experiment_transition": None,
    }
    event["event_sha256"] = hashlib.sha256(_canonical(event)).hexdigest()
    return event


def benchmark_compaction_checkpoint(
    *, root: Path, samples: int = 1000, generated_at: str = "2026-08-14T20:30:00Z"
) -> dict[str, Any]:
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    fixture = benchmark_fixture(root)
    snapshot = fixture["snapshot"]
    packet = fixture["packet"]
    event = _event(snapshot["project"]["project_id"])
    durations: list[float] = []
    delta_sizes: list[int] = []
    delta_mismatch = 0
    hook_mismatch = 0
    watermark_mismatch = 0
    authority_violations = 0
    pi = PiCompactionHookAdapter()
    deepseek = DeepSeekCompactionHookAdapter()
    with tempfile.TemporaryDirectory(prefix="context-m5-02-benchmark-") as directory:
        store = LocalArtifactStore(Path(directory) / "artifacts")
        store.initialize()
        expected_delta_bytes: bytes | None = None
        expected_pi: dict[str, Any] | None = None
        expected_deepseek: dict[str, Any] | None = None
        for _ in range(samples):
            started = time.perf_counter()
            delta = build_material_event_delta(
                snapshot=copy.deepcopy(snapshot),
                base_checkpoint_ref=None,
                base_revision=7,
                base_event_head=None,
                events=[copy.deepcopy(event)],
                execution_packet=copy.deepcopy(packet),
                observed_at="2026-08-14T20:00:00+08:00",
            )
            delta_ref = publish_material_event_delta(delta, store)
            pi_receipt = pi.capture(
                delta,
                metadata={"cut_point": 128, "split_turn": True, "usage_tokens": 2048},
                observed_at="2026-08-14T20:00:00+08:00",
            )
            deepseek_receipt = deepseek.capture(
                delta,
                metadata={"checkpoint_kind": "semantic", "checkpoint_sequence": 4},
                observed_at="2026-08-14T20:00:00+08:00",
            )
            duration_ms = (time.perf_counter() - started) * 1000
            durations.append(duration_ms)
            delta_bytes = canonical_material_event_delta_bytes(delta)
            delta_sizes.append(len(delta_bytes))
            if expected_delta_bytes is None:
                expected_delta_bytes = delta_bytes
                expected_pi = pi_receipt
                expected_deepseek = deepseek_receipt
            if delta_bytes != expected_delta_bytes:
                delta_mismatch += 1
            if pi_receipt != expected_pi or deepseek_receipt != expected_deepseek:
                hook_mismatch += 1
            if (
                delta["task_revision"] != packet["active_leaf"]["revision"]
                or delta["effect_high_watermark"] != snapshot["project"]["effect_high_watermark"]
                or pi_receipt["task_revision"] != delta["task_revision"]
                or deepseek_receipt["effect_high_watermark"] != delta["effect_high_watermark"]
            ):
                watermark_mismatch += 1
            if (
                delta["state_write_authority"]
                or delta["provider_native_authority"]
                or pi_receipt["state_write_authority"]
                or pi_receipt["provider_native_authority"]
                or deepseek_receipt["state_write_authority"]
                or deepseek_receipt["provider_native_authority"]
            ):
                authority_violations += 1
            validate_material_event_delta(delta)
            validate_provider_compaction_hook_receipt(pi_receipt)
            validate_provider_compaction_hook_receipt(deepseek_receipt)
            if delta_ref.digest != hashlib.sha256(delta_bytes).hexdigest():
                delta_mismatch += 1
    ordered = sorted(durations)
    percentile = lambda fraction: round(
        ordered[max(0, math.ceil(fraction * len(ordered)) - 1)], 6
    )
    ordered_delta_sizes = sorted(delta_sizes)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": samples,
        "delta_replay_mismatch": delta_mismatch,
        "hook_replay_mismatch": hook_mismatch,
        "watermark_mismatch": watermark_mismatch,
        "authority_violations": authority_violations,
        "min_delta_bytes": min(delta_sizes),
        "max_delta_bytes": max(delta_sizes),
        "p50_delta_bytes": int(statistics.median(delta_sizes)),
        "p95_delta_bytes": int(
            ordered_delta_sizes[max(0, math.ceil(0.95 * len(ordered_delta_sizes)) - 1)]
        ),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": round(max(durations), 6),
        "precompact_p95_limit_ms": PRECOMPACT_P95_LIMIT_MS,
        "precompact_p95_gate_passed": percentile(0.95) < PRECOMPACT_P95_LIMIT_MS,
        "external_services": 0,
        "provenance": {
            **{field: _sha256(root / relative) for field, relative in _PROVENANCE_PATHS.items()},
            "delta_registry_entry_sha256": _registry_entry_sha256(root, "context.material-event-delta"),
            "hook_registry_entry_sha256": _registry_entry_sha256(root, "context.provider-compaction-hook"),
        },
    }


def validate_compaction_checkpoint_benchmark_receipt(receipt: dict[str, Any], *, root: Path) -> None:
    required = {
        "schema_version", "generated_at", "samples", "successful_samples", "delta_replay_mismatch",
        "hook_replay_mismatch", "watermark_mismatch", "authority_violations", "min_delta_bytes",
        "max_delta_bytes", "p50_delta_bytes", "p95_delta_bytes", "p50_ms", "p95_ms", "max_ms",
        "precompact_p95_limit_ms", "precompact_p95_gate_passed", "external_services", "provenance",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError("M5-02 benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION or not str(receipt["generated_at"]).endswith("Z"):
        raise ValueError("M5-02 benchmark identity is invalid")
    for field in (
        "samples", "successful_samples", "delta_replay_mismatch", "hook_replay_mismatch",
        "watermark_mismatch", "authority_violations", "min_delta_bytes", "max_delta_bytes",
        "p50_delta_bytes", "p95_delta_bytes", "external_services",
    ):
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise ValueError(f"M5-02 benchmark {field} is invalid")
    for field in ("p50_ms", "p95_ms", "max_ms", "precompact_p95_limit_ms"):
        if not isinstance(receipt[field], (int, float)) or receipt[field] < 0:
            raise ValueError(f"M5-02 benchmark {field} is invalid")
    if type(receipt["precompact_p95_gate_passed"]) is not bool or receipt["external_services"] != 0:
        raise ValueError("M5-02 benchmark acceptance fields are invalid")
    if receipt["successful_samples"] != receipt["samples"] or any(
        receipt[field] != 0 for field in ("delta_replay_mismatch", "hook_replay_mismatch", "watermark_mismatch", "authority_violations")
    ):
        raise ValueError("M5-02 benchmark veto failed")
    if not receipt["precompact_p95_gate_passed"] or receipt["p95_ms"] >= PRECOMPACT_P95_LIMIT_MS:
        raise ValueError("M5-02 PreCompact p95 gate failed")
    provenance = receipt["provenance"]
    expected_fields = set(_PROVENANCE_PATHS) | {"delta_registry_entry_sha256", "hook_registry_entry_sha256"}
    if not isinstance(provenance, dict) or set(provenance) != expected_fields:
        raise ValueError("M5-02 benchmark provenance fields are invalid")
    for field, relative in _PROVENANCE_PATHS.items():
        if provenance[field] != _sha256(root / relative):
            raise ValueError(f"M5-02 benchmark provenance is stale: {field}")
    if provenance["delta_registry_entry_sha256"] != _registry_entry_sha256(root, "context.material-event-delta"):
        raise ValueError("M5-02 delta registry provenance is stale")
    if provenance["hook_registry_entry_sha256"] != _registry_entry_sha256(root, "context.provider-compaction-hook"):
        raise ValueError("M5-02 hook registry provenance is stale")
