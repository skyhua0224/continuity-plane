"""Local replay and fault benchmark for the M5-03 PostCompact canary."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import yaml

from .artifact_store import LocalArtifactStore
from .checkpoint import publish_checkpoint
from .compaction_checkpoint import (
    DeepSeekCompactionHookAdapter,
    PiCompactionHookAdapter,
    build_material_event_delta,
)
from .execution_packet import canonical_execution_packet_bytes
from .execution_packet_benchmark import benchmark_fixture
from .postcompact_canary import (
    PostCompactCanaryError,
    canonical_postcompact_canary_bytes,
    evaluate_postcompact_canary,
    validate_postcompact_canary_receipt,
)

SCHEMA_VERSION = "context.postcompact-canary-benchmark/v1alpha1"
FAULT_NAMES = (
    "active_work",
    "constraint",
    "decision",
    "deepseek_checkpoint",
    "delta_binding",
    "effect_watermark",
    "pi_cut_point",
    "pi_split_turn",
)

_PROVENANCE_PATHS = {
    "implementation_sha256": "context_control_plane/postcompact_canary.py",
    "benchmark_sha256": "context_control_plane/postcompact_canary_benchmark.py",
    "runner_sha256": "tools/run_postcompact_canary_benchmark.py",
    "canary_schema_sha256": "schemas/m5-03/postcompact-canary.schema.json",
    "benchmark_schema_sha256": "schemas/m5-03/postcompact-canary-benchmark.schema.json",
    "test_sha256": "tests/test_m5_03_postcompact_canary.py",
    "benchmark_test_sha256": "tests/test_m5_03_postcompact_canary_benchmark.py",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _registry_entry_sha256(root: Path, schema_id: str) -> str:
    registry = yaml.safe_load((root / "schemas/registry.yaml").read_text(encoding="utf-8"))
    entries = [item for item in registry["schemas"] if item["schema_id"] == schema_id]
    if len(entries) != 1:
        raise ValueError(f"registry entry is missing: {schema_id}")
    return hashlib.sha256(_canonical(entries[0])).hexdigest()


def _event(snapshot: dict[str, Any]) -> dict[str, Any]:
    event = {
        "schema_version": "context.state-event/v4alpha1",
        "event_id": "event-m5-03-benchmark",
        "event_type": "state-transition",
        "project_id": snapshot["project"]["project_id"],
        "sequence_no": 1,
        "revision_before": 7,
        "revision_after": 7,
        "occurred_at": "2026-08-15T00:10:00+08:00",
        "actor_ref": "actor://benchmark",
        "causation_ref": "run:m5-03-benchmark",
        "correlation_ref": "task:m5-03",
        "previous_event_sha256": None,
        "supersedes_event_id": None,
        "changes": [],
        "project_after": copy.deepcopy(snapshot["project"]),
        "task_transition": None,
        "experiment_transition": None,
    }
    event["event_sha256"] = hashlib.sha256(_canonical(event)).hexdigest()
    return event


def _capabilities() -> dict[str, Any]:
    return {
        "schema_version": "context.state-store-capabilities/v1alpha1",
        "adapter_id": "context.sqlite",
        "adapter_version": "1.0.0-alpha.1",
        "authority_mode": "local",
        "operations": ["create_project", "read_project", "read_events", "commit_event"],
        "shared_authority": False,
        "offline_write": True,
        "unique_claim": False,
        "multi_writer": True,
        "lease_clock": "process",
        "artifact_scope": "local",
        "expected_revision": True,
        "migration_source": True,
        "migration_target": True,
    }


def _fixture(root: Path, store: LocalArtifactStore) -> dict[str, Any]:
    source = benchmark_fixture(root)
    snapshot = source["snapshot"]
    packet = source["packet"]
    event = _event(snapshot)
    event_head = {"sequence_no": 1, "event_sha256": event["event_sha256"]}
    checkpoint_ref = publish_checkpoint(
        {
            "snapshot": copy.deepcopy(snapshot),
            "revision": packet["project_revision"],
            "event_head": copy.deepcopy(event_head),
            "registry_digest": "d" * 64,
            "capabilities": _capabilities(),
        },
        store,
        canonical_plan_sha256=packet["canonical_plan_sha256"],
    )
    packet_ref = store.put_bytes(canonical_execution_packet_bytes(packet))
    binding = {
        "schema_version": "context.postcompact-authority-binding/v1alpha1",
        "project_id": packet["project_id"],
        "project_revision": packet["project_revision"],
        "event_head": event_head,
        "governance_ref": packet["governance_ref"],
        "canonical_plan_sha256": packet["canonical_plan_sha256"],
        "registry_digest": "d" * 64,
        "state_sha256": packet["state_sha256"],
        "checkpoint_ref": checkpoint_ref.to_document(),
        "expected_packet_ref": packet_ref.to_document(),
        "active_work_id": packet["active_leaf"]["work_id"],
        "task_revision": packet["active_leaf"]["revision"],
        "effect_high_watermark": snapshot["project"]["effect_high_watermark"],
    }
    delta = build_material_event_delta(
        snapshot=copy.deepcopy(snapshot),
        base_checkpoint_ref=checkpoint_ref.to_document(),
        base_revision=packet["project_revision"],
        base_event_head=None,
        events=[event],
        execution_packet=copy.deepcopy(packet),
        observed_at="2026-08-15T00:10:00+08:00",
    )
    pi_metadata = {"cut_point": 128, "split_turn": True, "usage_tokens": 2048}
    deepseek_metadata = {"checkpoint_kind": "semantic", "checkpoint_sequence": 4}
    return {
        "packet": packet,
        "binding": binding,
        "delta": delta,
        "pi_metadata": pi_metadata,
        "deepseek_metadata": deepseek_metadata,
        "pi_hook": PiCompactionHookAdapter().capture(
            delta, metadata=pi_metadata, observed_at="2026-08-15T00:10:00+08:00"
        ),
        "deepseek_hook": DeepSeekCompactionHookAdapter().capture(
            delta, metadata=deepseek_metadata, observed_at="2026-08-15T00:10:00+08:00"
        ),
    }


def _evaluate(
    store: LocalArtifactStore,
    fixture: dict[str, Any],
    *,
    provider_id: str,
    packet: dict[str, Any] | None = None,
    delta: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return evaluate_postcompact_canary(
        artifact_store=store,
        trusted_binding=copy.deepcopy(fixture["binding"]),
        restored_packet=copy.deepcopy(packet or fixture["packet"]),
        delta=copy.deepcopy(delta or fixture["delta"]),
        hook_receipt=copy.deepcopy(fixture[f"{provider_id}_hook"]),
        observed_host_metadata=copy.deepcopy(metadata or fixture[f"{provider_id}_metadata"]),
        observed_at="2026-08-15T00:11:00+08:00",
    )


def _fault_cases(fixture: dict[str, Any]) -> list[tuple[str, Callable[[], None]]]:
    def packet_fault(field: str, action: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        def run() -> None:
            packet = copy.deepcopy(fixture["packet"])
            action(packet)
            _evaluate(fixture["store"], fixture, provider_id="pi", packet=packet)

        return run

    def delta_fault(field: str, value: Any) -> Callable[[], None]:
        def run() -> None:
            delta = copy.deepcopy(fixture["delta"])
            delta[field] = value
            _evaluate(fixture["store"], fixture, provider_id="pi", delta=delta)

        return run

    return [
        ("active_work", packet_fault("active_work", lambda packet: packet["active_leaf"].__setitem__("work_id", "work-other"))),
        ("constraint", packet_fault("constraint", lambda packet: packet["constraints"].clear())),
        ("decision", packet_fault("decision", lambda packet: packet["decisions"].clear())),
        (
            "deepseek_checkpoint",
            lambda: _evaluate(
                fixture["store"], fixture, provider_id="deepseek",
                metadata={**fixture["deepseek_metadata"], "checkpoint_sequence": 5},
            ),
        ),
        ("delta_binding", delta_fault("execution_packet_sha256", "a" * 64)),
        ("effect_watermark", delta_fault("effect_high_watermark", 99)),
        (
            "pi_cut_point",
            lambda: _evaluate(
                fixture["store"], fixture, provider_id="pi",
                metadata={**fixture["pi_metadata"], "cut_point": 127},
            ),
        ),
        (
            "pi_split_turn",
            lambda: _evaluate(
                fixture["store"], fixture, provider_id="pi",
                metadata={**fixture["pi_metadata"], "split_turn": False},
            ),
        ),
    ]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 6)


def benchmark_postcompact_canary(
    *, root: Path, samples: int = 1000, generated_at: str = "2026-08-15T00:30:00Z"
) -> dict[str, Any]:
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    durations: list[float] = []
    replay_mismatch = 0
    critical_field_mismatch = 0
    authority_violations = 0
    successful_samples = 0
    faults = {name: 0 for name in FAULT_NAMES}
    fault_rejections = 0
    with tempfile.TemporaryDirectory(prefix="context-m5-03-benchmark-") as directory:
        store = LocalArtifactStore(Path(directory) / "artifacts")
        store.initialize()
        fixture = _fixture(root, store)
        fixture["store"] = store
        expected: dict[str, bytes] = {}
        expected_decisions = sorted(item["decision_id"] for item in fixture["packet"]["decisions"])
        expected_constraints = sorted(
            item["constraint_id"] for item in fixture["packet"]["constraints"]
        )
        for index in range(samples):
            provider_id = "pi" if index % 2 == 0 else "deepseek"
            started = time.perf_counter()
            receipt = _evaluate(store, fixture, provider_id=provider_id)
            durations.append((time.perf_counter() - started) * 1000)
            validate_postcompact_canary_receipt(receipt)
            payload = canonical_postcompact_canary_bytes(receipt)
            if provider_id not in expected:
                expected[provider_id] = payload
            elif payload != expected[provider_id]:
                replay_mismatch += 1
            if (
                receipt["decision_ids"] != expected_decisions
                or receipt["constraint_ids"] != expected_constraints
                or receipt["active_work_id"] != fixture["packet"]["active_leaf"]["work_id"]
            ):
                critical_field_mismatch += 1
            if receipt["state_write_authority"] or receipt["provider_native_authority"]:
                authority_violations += 1
            successful_samples += 1
            for name, invoke in _fault_cases(fixture):
                try:
                    invoke()
                except PostCompactCanaryError:
                    fault_rejections += 1
                else:
                    faults[name] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": successful_samples,
        "replay_mismatch": replay_mismatch,
        "critical_field_mismatch": critical_field_mismatch,
        "authority_violations": authority_violations,
        "fault_samples": samples * len(FAULT_NAMES),
        "fault_rejections": fault_rejections,
        "faults": faults,
        "decision_recovery_millionths": 1_000_000 if critical_field_mismatch == 0 else 0,
        "constraint_recovery_millionths": 1_000_000 if critical_field_mismatch == 0 else 0,
        "work_recovery_millionths": 1_000_000 if critical_field_mismatch == 0 else 0,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "external_services": 0,
        "provenance": {
            **{field: _sha256(root / relative) for field, relative in _PROVENANCE_PATHS.items()},
            "canary_registry_entry_sha256": _registry_entry_sha256(
                root, "context.postcompact-canary"
            ),
            "benchmark_registry_entry_sha256": _registry_entry_sha256(
                root, "context.postcompact-canary-benchmark"
            ),
        },
    }


def validate_postcompact_canary_benchmark_receipt(
    receipt: dict[str, Any], *, root: Path
) -> None:
    required = {
        "schema_version", "generated_at", "samples", "successful_samples", "replay_mismatch",
        "critical_field_mismatch", "authority_violations", "fault_samples", "fault_rejections",
        "faults", "decision_recovery_millionths", "constraint_recovery_millionths",
        "work_recovery_millionths", "p50_ms", "p95_ms", "max_ms", "external_services",
        "provenance",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError("M5-03 benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION or not str(receipt["generated_at"]).endswith("Z"):
        raise ValueError("M5-03 benchmark identity is invalid")
    integer_fields = required - {"schema_version", "generated_at", "faults", "p50_ms", "p95_ms", "max_ms", "provenance"}
    if any(type(receipt[field]) is not int or receipt[field] < 0 for field in integer_fields):
        raise ValueError("M5-03 benchmark integer fields are invalid")
    if any(not isinstance(receipt[field], (int, float)) or receipt[field] < 0 for field in ("p50_ms", "p95_ms", "max_ms")):
        raise ValueError("M5-03 benchmark latency fields are invalid")
    if not isinstance(receipt["faults"], dict) or tuple(sorted(receipt["faults"])) != FAULT_NAMES:
        raise ValueError("M5-03 benchmark fault fields are invalid")
    if any(type(value) is not int or value < 0 for value in receipt["faults"].values()):
        raise ValueError("M5-03 benchmark fault values are invalid")
    if (
        receipt["successful_samples"] != receipt["samples"]
        or receipt["replay_mismatch"]
        or receipt["critical_field_mismatch"]
        or receipt["authority_violations"]
        or receipt["fault_samples"] != receipt["samples"] * len(FAULT_NAMES)
        or receipt["fault_rejections"] != receipt["fault_samples"]
        or any(receipt["faults"].values())
        or receipt["external_services"] != 0
    ):
        raise ValueError("M5-03 benchmark veto failed")
    if any(
        receipt[field] != 1_000_000
        for field in (
            "decision_recovery_millionths",
            "constraint_recovery_millionths",
            "work_recovery_millionths",
        )
    ):
        raise ValueError("M5-03 critical recovery gate failed")
    if receipt["p95_ms"] >= 2000:
        raise ValueError("M5-03 state-only restore p95 gate failed")
    expected_fields = set(_PROVENANCE_PATHS) | {
        "canary_registry_entry_sha256",
        "benchmark_registry_entry_sha256",
    }
    provenance = receipt["provenance"]
    if not isinstance(provenance, dict) or set(provenance) != expected_fields:
        raise ValueError("M5-03 benchmark provenance fields are invalid")
    for field, relative in _PROVENANCE_PATHS.items():
        if provenance[field] != _sha256(root / relative):
            raise ValueError(f"M5-03 benchmark provenance is stale: {field}")
    if provenance["canary_registry_entry_sha256"] != _registry_entry_sha256(
        root, "context.postcompact-canary"
    ):
        raise ValueError("M5-03 canary registry provenance is stale")
    if provenance["benchmark_registry_entry_sha256"] != _registry_entry_sha256(
        root, "context.postcompact-canary-benchmark"
    ):
        raise ValueError("M5-03 benchmark registry provenance is stale")
