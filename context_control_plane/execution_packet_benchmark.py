"""Local replay and capacity benchmark for M5-01 Execution Packets."""

from __future__ import annotations

import copy
import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Any

import yaml

from .compiled_skill_packet import canonical_compiled_skill_packet_bytes
from .execution_packet import (
    MAX_PACKET_BYTES,
    ExecutionPacketError,
    canonical_execution_packet_bytes,
    compose_execution_packet,
    validate_execution_packet,
)
from .skill_resolver import canonical_skill_resolution_request_bytes

SCHEMA_VERSION = "context.execution-packet-benchmark/v1alpha1"
MIN_TARGET_PACKET_BYTES = 4 * 1024


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _load_fixture(root: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    fixtures = yaml.safe_load(
        (root / "experiments/state/m2-01-core-fixtures.yaml").read_text(encoding="utf-8")
    )
    snapshot = copy.deepcopy(
        next(case["document"] for case in fixtures["cases"] if case["case_id"] == "solo-active-work")
    )
    skill_fixture = json.loads(
        (root / "experiments/skills/m4-09-skill-resolution-replay-v1alpha1.json").read_text(
            encoding="utf-8"
        )
    )
    request = copy.deepcopy(skill_fixture["request"])
    decision = copy.deepcopy(skill_fixture["decision"])
    request["request_id"] = "resolve-packet-benchmark"
    request["project_ref"] = "project://project-solo"
    request["task_ref"] = "task://work-solo"
    decision["request_id"] = request["request_id"]
    decision["request_sha256"] = hashlib.sha256(
        canonical_skill_resolution_request_bytes(request)
    ).hexdigest()
    compiled_packet = copy.deepcopy(skill_fixture["packet"])

    # Keep the committed canary in the documented 4-12 KiB operating band.
    for index in range(1, 7):
        evidence_id = f"evidence-packet-{index}"
        snapshot["evidence"].append(
            {
                "evidence_id": evidence_id,
                "kind": "test",
                "artifact_ref": f"artifact://fixture/{evidence_id}",
                "content_sha256": hashlib.sha256(evidence_id.encode()).hexdigest(),
                "validity": "verified",
                "observed_at": f"2026-08-14T{10 + index:02d}:00:00+08:00",
                "verified_at": f"2026-08-14T{10 + index:02d}:30:00+08:00",
            }
        )
        snapshot["works"][0]["evidence_ids"].append(evidence_id)
    cursor = {
        "last_durable_action": "m4-09-commit",
        "in_flight_phase": "ready-to-execute",
        "confirmed_input_refs": ["opaque://input/m4-09-commit"],
        "reserved_effect_ids": [],
        "replay_policy": "verify-before-effect",
    }
    return snapshot, request, decision, compiled_packet, cursor


def benchmark_fixture(root: Path) -> dict[str, Any]:
    """Return the canonical input/output fixture used by the replay canary."""
    snapshot, request, decision, compiled_packet, cursor = _load_fixture(root)
    packet = compose_execution_packet(
        snapshot=snapshot,
        skill_request=request,
        skill_decision=decision,
        compiled_skill_packet=compiled_packet,
        next_action="run the M5-01 bounded packet canary",
        continuation_cursor=cursor,
        canonical_plan_sha256="c" * 64,
        observed_at="2026-08-14T20:00:00+08:00",
    )
    return {
        "fixture_version": "context.execution-packet-replay-fixture/v1alpha1",
        "snapshot": snapshot,
        "skill_request": request,
        "skill_decision": decision,
        "compiled_skill_packet": compiled_packet,
        "next_action": "run the M5-01 bounded packet canary",
        "continuation_cursor": cursor,
        "canonical_plan_sha256": "c" * 64,
        "observed_at": "2026-08-14T20:00:00+08:00",
        "packet": packet,
    }


def canonical_benchmark_fixture_bytes(root: Path) -> bytes:
    return _canonical(benchmark_fixture(root))


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * fraction + 0.999999) - 1))
    return round(ordered[index], 6)


def benchmark_execution_packet(
    *, root: Path, samples: int = 1000, generated_at: str = "2026-08-14T20:30:00Z"
) -> dict[str, Any]:
    """Run deterministic composition and replay measurements without services."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    fixture = benchmark_fixture(root)
    expected_bytes = canonical_execution_packet_bytes(fixture["packet"])
    durations: list[float] = []
    packet_sizes: list[int] = []
    replay_mismatch = 0
    canary_failures = 0
    authority_true = 0
    for _ in range(samples):
        started = time.perf_counter()
        packet = compose_execution_packet(
            snapshot=fixture["snapshot"],
            skill_request=fixture["skill_request"],
            skill_decision=fixture["skill_decision"],
            compiled_skill_packet=fixture["compiled_skill_packet"],
            next_action=fixture["next_action"],
            continuation_cursor=fixture["continuation_cursor"],
            canonical_plan_sha256=fixture["canonical_plan_sha256"],
            observed_at=fixture["observed_at"],
        )
        duration_ms = (time.perf_counter() - started) * 1000
        durations.append(duration_ms)
        packet_bytes = canonical_execution_packet_bytes(packet)
        packet_sizes.append(len(packet_bytes))
        if packet_bytes != expected_bytes:
            replay_mismatch += 1
        try:
            validate_execution_packet(packet)
        except ExecutionPacketError:
            canary_failures += 1
        if packet["state_write_authority"] is not False:
            authority_true += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": samples - canary_failures,
        "replay_mismatch": replay_mismatch,
        "canary_failures": canary_failures,
        "state_write_authority_true": authority_true,
        "min_packet_bytes": min(packet_sizes),
        "max_packet_bytes": max(packet_sizes),
        "p50_packet_bytes": int(statistics.median(packet_sizes)),
        "p95_packet_bytes": int(_percentile([float(value) for value in packet_sizes], 0.95)),
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "capacity_in_bound": int(
            min(packet_sizes) >= MIN_TARGET_PACKET_BYTES
            and max(packet_sizes) <= MAX_PACKET_BYTES
        ),
        "external_services": 0,
        "implementation_sha256": hashlib.sha256(
            (root / "context_control_plane/execution_packet.py").read_bytes()
        ).hexdigest(),
        "fixture_sha256": hashlib.sha256(canonical_benchmark_fixture_bytes(root)).hexdigest(),
        "compiled_skill_packet_sha256": hashlib.sha256(
            canonical_compiled_skill_packet_bytes(fixture["compiled_skill_packet"])
        ).hexdigest(),
    }


def validate_execution_packet_benchmark_receipt(receipt: dict[str, Any], *, root: Path) -> None:
    required = {
        "schema_version", "generated_at", "samples", "successful_samples", "replay_mismatch",
        "canary_failures", "state_write_authority_true", "min_packet_bytes", "max_packet_bytes",
        "p50_packet_bytes", "p95_packet_bytes", "p50_ms", "p95_ms", "max_ms", "capacity_in_bound",
        "external_services", "implementation_sha256", "fixture_sha256", "compiled_skill_packet_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError("Execution Packet benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION or not str(receipt["generated_at"]).endswith("Z"):
        raise ValueError("Execution Packet benchmark identity is invalid")
    for field in (
        "samples", "successful_samples", "replay_mismatch", "canary_failures", "state_write_authority_true",
        "min_packet_bytes", "max_packet_bytes", "p50_packet_bytes", "p95_packet_bytes", "capacity_in_bound",
        "external_services",
    ):
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise ValueError(f"Execution Packet benchmark {field} is invalid")
    for field in ("p50_ms", "p95_ms", "max_ms"):
        if not isinstance(receipt[field], (int, float)) or receipt[field] < 0:
            raise ValueError(f"Execution Packet benchmark {field} is invalid")
    for field in ("implementation_sha256", "fixture_sha256", "compiled_skill_packet_sha256"):
        if not isinstance(receipt[field], str) or not re_full_sha256(receipt[field]):
            raise ValueError(f"Execution Packet benchmark {field} provenance is invalid")
    if receipt["successful_samples"] != receipt["samples"]:
        raise ValueError("Execution Packet benchmark acceptance requires every sample to pass")
    if receipt["replay_mismatch"] or receipt["canary_failures"] or receipt["state_write_authority_true"]:
        raise ValueError("Execution Packet benchmark acceptance veto failed")
    if receipt["capacity_in_bound"] != 1 or receipt["min_packet_bytes"] < MIN_TARGET_PACKET_BYTES or receipt["max_packet_bytes"] > MAX_PACKET_BYTES:
        raise ValueError("Execution Packet benchmark packet capacity is out of bounds")
    implementation = hashlib.sha256((root / "context_control_plane/execution_packet.py").read_bytes()).hexdigest()
    if receipt["implementation_sha256"] != implementation:
        raise ValueError("Execution Packet benchmark implementation provenance is stale")
    fixture_hash = hashlib.sha256(canonical_benchmark_fixture_bytes(root)).hexdigest()
    if receipt["fixture_sha256"] != fixture_hash:
        raise ValueError("Execution Packet benchmark fixture provenance is stale")


def re_full_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
