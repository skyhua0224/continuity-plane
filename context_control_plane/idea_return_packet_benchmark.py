"""Offline replay and fault benchmark for M5-06 Idea return packets."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Callable

from .idea_continuity_benchmark import build_idea_snapshot
from .idea_review import migrate_typed_state_v3_to_v4, upsert_idea_observation
from .idea_return_packet import (
    IdeaReturnPacketError,
    canonical_idea_return_packet_bytes,
    compose_idea_return_packet,
    validate_idea_return_packet,
)
from .typed_state import validate_typed_state

SCHEMA_VERSION = "context.idea-return-packet-benchmark/v1alpha1"
FAULT_NAMES = (
    "canary_return_work",
    "checkpoint_binding",
    "current_active_work",
    "master_digest_drift",
    "registry_digest_drift",
    "return_revision",
)

_PROVENANCE_PATHS = {
    "implementation_sha256": "context_control_plane/idea_return_packet.py",
    "benchmark_sha256": "context_control_plane/idea_return_packet_benchmark.py",
    "behavior_test_sha256": "tests/test_m5_06_idea_return_packet.py",
    "benchmark_test_sha256": "tests/test_m5_06_idea_return_packet_benchmark.py",
    "return_packet_schema_sha256": "schemas/m5-06/idea-return-packet.schema.json",
    "checkpoint_binding_schema_sha256": "schemas/m5-06/idea-return-checkpoint-binding.schema.json",
    "migration_schema_sha256": "schemas/m5-06/return-context-migration.schema.json",
    "benchmark_schema_sha256": "schemas/m5-06/idea-return-packet-benchmark.schema.json",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _checkpoint_ref(digest: str = "d" * 64) -> dict[str, Any]:
    return {
        "schema_version": "context.artifact-ref/v1alpha1",
        "digest_algorithm": "sha-256",
        "digest": digest,
        "size_bytes": 2048,
        "artifact_uri": f"artifact://sha256/{digest}",
    }


def _canary_receipt(
    *, checkpoint_digest: str = "d" * 64, active_work_id: str = "work-active"
) -> dict[str, Any]:
    receipt = {
        "schema_version": "context.postcompact-canary/v1alpha1",
        "canary_version": "context.postcompact-canary-evaluator/v1alpha1",
        "canary_id": "pending",
        "provider_id": "pi",
        "project_id": "project-idea-benchmark",
        "project_revision": 9,
        "expected_packet_sha256": "a" * 64,
        "restored_packet_sha256": "a" * 64,
        "delta_id": "delta-m5-06-benchmark",
        "delta_sha256": "b" * 64,
        "event_head": None,
        "active_work_id": active_work_id,
        "task_revision": 1,
        "effect_high_watermark": 0,
        "decision_ids": [],
        "constraint_ids": [],
        "host_metadata_sha256": "c" * 64,
        "authority_binding_sha256": "e" * 64,
        "checkpoint_sha256": checkpoint_digest,
        "expected_packet_artifact_sha256": "f" * 64,
        "status": "pass",
        "execution_gate": "allow",
        "state_write_authority": False,
        "provider_native_authority": False,
        "observed_at": "2026-08-15T03:00:00+08:00",
        "canary_sha256": "pending",
    }
    body = copy.deepcopy(receipt)
    body.pop("canary_sha256")
    identity = hashlib.sha256(_canonical(body)).hexdigest()
    receipt["canary_id"] = f"canary-{identity[:32]}"
    body = copy.deepcopy(receipt)
    body.pop("canary_sha256")
    receipt["canary_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return receipt


def benchmark_idea_return_packet_fixture() -> dict[str, Any]:
    """Build one fixed historical checkpoint/current authority comparison."""
    snapshot = migrate_typed_state_v3_to_v4(
        build_idea_snapshot(), migrated_at="2026-08-15T03:05:00+08:00"
    )
    snapshot = upsert_idea_observation(
        snapshot,
        idea_id="idea-return",
        parent_work_id="work-active",
        return_work_id="work-active",
        source_ref="rng_abcdefghijklmnopqrstuvwxyz",
        summary="This complete benchmark Idea body must not enter the packet.",
        scope_ref="work://work-active",
        urgency="next",
        review_at=None,
        occurrence_id="occurrence-return",
        observed_at="2026-08-15T03:06:00+08:00",
    )
    snapshot = upsert_idea_observation(
        snapshot,
        idea_id="idea-unrelated",
        parent_work_id="work-target",
        return_work_id="work-target",
        source_ref="rng_bcdefghijklmnopqrstuvwxyza",
        summary="Unrelated benchmark Idea body.",
        scope_ref="work://work-target",
        urgency="later",
        review_at=None,
        occurrence_id="occurrence-unrelated",
        observed_at="2026-08-15T03:07:00+08:00",
    )
    snapshot["project"]["revision"] = 11
    snapshot["project"]["updated_at"] = "2026-08-15T03:10:00+08:00"
    snapshot["claims"][0]["expected_project_revision"] = 11
    snapshot["claims"][0]["lease_expires_at"] = "2026-08-15T04:00:00+08:00"
    next(item for item in snapshot["works"] if item["work_id"] == "work-active")[
        "revision"
    ] = 2
    validate_typed_state(snapshot)
    checkpoint_ref = _checkpoint_ref()
    return_frame = {
        "checkpoint_ref": checkpoint_ref,
        "old_work_id": "work-active",
        "old_work_revision": 1,
        "proposal_sha256": "9" * 64,
        "old_project_revision": 9,
        "return_work_id": "work-active",
        "return_work_revision": 1,
        "current_work_id": "work-target",
    }
    checkpoint_binding = {
        "schema_version": "context.idea-return-checkpoint-binding/v1alpha1",
        "project_id": "project-idea-benchmark",
        "project_revision": 9,
        "return_work_id": "work-active",
        "return_work_revision": 1,
        "canonical_plan_sha256": "1" * 64,
        "registry_digest": "2" * 64,
        "checkpoint_ref": checkpoint_ref,
    }
    canary = _canary_receipt()
    arguments = {
        "snapshot": snapshot,
        "return_frame": return_frame,
        "checkpoint_binding": checkpoint_binding,
        "postcompact_canary": canary,
        "canonical_plan_sha256": "1" * 64,
        "registry_digest": "2" * 64,
        "next_action": "resume the original Work from current authority",
        "observed_at": "2026-08-15T03:11:00+08:00",
        "trusted_migration_receipt": None,
    }
    packet = compose_idea_return_packet(**arguments)
    return {
        **arguments,
        "packet": packet,
        "forbidden_idea_bodies": [
            "This complete benchmark Idea body must not enter the packet.",
            "Unrelated benchmark Idea body.",
            "rng_abcdefghijklmnopqrstuvwxyz",
            "rng_bcdefghijklmnopqrstuvwxyza",
        ],
    }


def _changed_active_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    changed = copy.deepcopy(snapshot)
    active = next(item for item in changed["works"] if item["work_id"] == "work-active")
    target = next(item for item in changed["works"] if item["work_id"] == "work-target")
    active["status"] = "ready"
    target["status"] = "active"
    changed["project"]["active_work_ids"] = ["work-target"]
    changed["project"]["primary_work_id"] = "work-target"
    changed["claims"][0]["work_id"] = "work-target"
    changed["claims"][0]["scope_owners"] = copy.deepcopy(target["scope_refs"])
    validate_typed_state(changed)
    return changed


def _fault_cases(fixture: dict[str, Any]) -> list[tuple[str, Callable[[], None]]]:
    def compose(**updates: Any) -> None:
        arguments = {
            field: copy.deepcopy(fixture[field])
            for field in (
                "snapshot",
                "return_frame",
                "checkpoint_binding",
                "postcompact_canary",
                "canonical_plan_sha256",
                "registry_digest",
                "next_action",
                "observed_at",
                "trusted_migration_receipt",
            )
        }
        arguments.update(updates)
        compose_idea_return_packet(**arguments)

    def checkpoint_fault() -> None:
        binding = copy.deepcopy(fixture["checkpoint_binding"])
        binding["checkpoint_ref"] = _checkpoint_ref("e" * 64)
        compose(checkpoint_binding=binding)

    def return_revision_fault() -> None:
        frame = copy.deepcopy(fixture["return_frame"])
        frame["return_work_revision"] = 2
        frame["old_work_revision"] = 2
        compose(return_frame=frame)

    return [
        (
            "canary_return_work",
            lambda: compose(postcompact_canary=_canary_receipt(active_work_id="work-target")),
        ),
        ("checkpoint_binding", checkpoint_fault),
        ("current_active_work", lambda: compose(snapshot=_changed_active_snapshot(fixture["snapshot"]))),
        ("master_digest_drift", lambda: compose(canonical_plan_sha256="3" * 64)),
        ("registry_digest_drift", lambda: compose(registry_digest="4" * 64)),
        ("return_revision", return_revision_fault),
    ]


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)


def _fixture_sha256() -> str:
    return hashlib.sha256(_canonical(benchmark_idea_return_packet_fixture())).hexdigest()


def benchmark_idea_return_packet(
    *, root: Path, samples: int = 1000, generated_at: str = "2026-08-15T03:30:00Z"
) -> dict[str, Any]:
    """Measure deterministic original-task recovery and fail-closed behavior."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    root = Path(root)
    fixture = benchmark_idea_return_packet_fixture()
    expected = canonical_idea_return_packet_bytes(fixture["packet"])
    durations: list[float] = []
    packet_sizes: list[int] = []
    successful_samples = 0
    replay_mismatch = 0
    original_task_recovery_mismatch = 0
    return_point_recovery_mismatch = 0
    idea_ref_recovery_mismatch = 0
    idea_body_copy_violations = 0
    candidate_authority_violations = 0
    faults = {name: 0 for name in FAULT_NAMES}
    fault_rejections = 0
    for _ in range(samples):
        started = time.perf_counter()
        packet = compose_idea_return_packet(
            **{
                field: copy.deepcopy(fixture[field])
                for field in (
                    "snapshot",
                    "return_frame",
                    "checkpoint_binding",
                    "postcompact_canary",
                    "canonical_plan_sha256",
                    "registry_digest",
                    "next_action",
                    "observed_at",
                    "trusted_migration_receipt",
                )
            }
        )
        durations.append((time.perf_counter() - started) * 1000)
        validate_idea_return_packet(packet)
        payload = canonical_idea_return_packet_bytes(packet)
        packet_sizes.append(len(payload))
        successful_samples += 1
        if payload != expected:
            replay_mismatch += 1
        if packet["active_leaf"] != fixture["packet"]["active_leaf"]:
            original_task_recovery_mismatch += 1
        if packet["return_point"] != fixture["packet"]["return_point"]:
            return_point_recovery_mismatch += 1
        if packet["idea_refs"] != fixture["packet"]["idea_refs"]:
            idea_ref_recovery_mismatch += 1
        if any(body.encode("utf-8") in payload for body in fixture["forbidden_idea_bodies"]):
            idea_body_copy_violations += 1
        if (
            packet["candidate_execution_authority"] is not False
            or packet["candidate_state_write_authority"] is not False
            or packet["state_write_authority"] is not False
            or packet["external_effect_authority"] is not False
            or any(idea["authority"] != "candidate-only" for idea in packet["idea_refs"])
        ):
            candidate_authority_violations += 1
        for name, invoke in _fault_cases(fixture):
            try:
                invoke()
            except IdeaReturnPacketError:
                fault_rejections += 1
            else:
                faults[name] += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": successful_samples,
        "replay_mismatch": replay_mismatch,
        "original_task_recovery_mismatch": original_task_recovery_mismatch,
        "return_point_recovery_mismatch": return_point_recovery_mismatch,
        "idea_ref_recovery_mismatch": idea_ref_recovery_mismatch,
        "idea_body_copy_violations": idea_body_copy_violations,
        "candidate_authority_violations": candidate_authority_violations,
        "fault_samples": samples * len(FAULT_NAMES),
        "fault_rejections": fault_rejections,
        "faults": faults,
        "task_recovery_millionths": (
            1_000_000 if original_task_recovery_mismatch == 0 else 0
        ),
        "return_point_recovery_millionths": (
            1_000_000 if return_point_recovery_mismatch == 0 else 0
        ),
        "min_packet_bytes": min(packet_sizes),
        "max_packet_bytes": max(packet_sizes),
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "external_services": 0,
        **{field: _sha256(root / relative) for field, relative in _PROVENANCE_PATHS.items()},
        "fixture_sha256": _fixture_sha256(),
    }


def validate_idea_return_packet_benchmark(
    receipt: dict[str, Any], *, root: Path
) -> None:
    required = {
        "schema_version",
        "generated_at",
        "samples",
        "successful_samples",
        "replay_mismatch",
        "original_task_recovery_mismatch",
        "return_point_recovery_mismatch",
        "idea_ref_recovery_mismatch",
        "idea_body_copy_violations",
        "candidate_authority_violations",
        "fault_samples",
        "fault_rejections",
        "faults",
        "task_recovery_millionths",
        "return_point_recovery_millionths",
        "min_packet_bytes",
        "max_packet_bytes",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "external_services",
        *set(_PROVENANCE_PATHS),
        "fixture_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError("M5-06 benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION or not str(receipt["generated_at"]).endswith("Z"):
        raise ValueError("M5-06 benchmark identity is invalid")
    integer_fields = required - {
        "schema_version",
        "generated_at",
        "faults",
        "p50_ms",
        "p95_ms",
        "max_ms",
        *set(_PROVENANCE_PATHS),
        "fixture_sha256",
    }
    if any(type(receipt[field]) is not int or receipt[field] < 0 for field in integer_fields):
        raise ValueError("M5-06 benchmark integer fields are invalid")
    if any(
        not isinstance(receipt[field], (int, float)) or receipt[field] < 0
        for field in ("p50_ms", "p95_ms", "max_ms")
    ):
        raise ValueError("M5-06 benchmark latency fields are invalid")
    if not isinstance(receipt["faults"], dict) or tuple(sorted(receipt["faults"])) != FAULT_NAMES:
        raise ValueError("M5-06 benchmark fault fields are invalid")
    if any(type(value) is not int or value < 0 for value in receipt["faults"].values()):
        raise ValueError("M5-06 benchmark fault values are invalid")
    if (
        receipt["successful_samples"] != receipt["samples"]
        or receipt["replay_mismatch"]
        or receipt["original_task_recovery_mismatch"]
        or receipt["return_point_recovery_mismatch"]
        or receipt["idea_ref_recovery_mismatch"]
        or receipt["idea_body_copy_violations"]
        or receipt["candidate_authority_violations"]
        or receipt["fault_samples"] != receipt["samples"] * len(FAULT_NAMES)
        or receipt["fault_rejections"] != receipt["fault_samples"]
        or any(receipt["faults"].values())
        or receipt["external_services"] != 0
    ):
        raise ValueError("M5-06 benchmark veto failed")
    if (
        receipt["task_recovery_millionths"] != 1_000_000
        or receipt["return_point_recovery_millionths"] != 1_000_000
    ):
        raise ValueError("M5-06 recovery gate failed")
    if receipt["min_packet_bytes"] <= 0 or receipt["max_packet_bytes"] > 8 * 1024:
        raise ValueError("M5-06 packet capacity gate failed")
    if receipt["p95_ms"] >= 2000:
        raise ValueError("M5-06 restore latency gate failed")
    root = Path(root)
    for field, relative in _PROVENANCE_PATHS.items():
        if receipt[field] != _sha256(root / relative):
            raise ValueError(f"M5-06 {field} provenance is stale")
    if receipt["fixture_sha256"] != _fixture_sha256():
        raise ValueError("M5-06 fixture provenance is stale")
