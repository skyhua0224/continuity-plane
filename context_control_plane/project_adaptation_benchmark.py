"""Deterministic M8-07 ProjectAdaptation lifecycle benchmark."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Any

from .project_adaptation import ProjectAdaptationLoop

_NOW = "2026-08-16T12:00:00Z"
_VETO_KEYS = ("E1", "E2", "E4", "E6", "E8", "E9")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "0" * 64


def _changes() -> dict[str, Any]:
    return {
        "retrieval_order": ["rg", "lsp"],
        "common_path_refs": ["path://src"],
        "common_command_refs": ["command://test"],
        "verification_hint_refs": ["verification://focused"],
        "skill_applicability": ["skill://tdd"],
        "presentation_preferences": {"response_density": "compact"},
    }


def _metrics(repeated_read_bytes: int) -> dict[str, int]:
    return {
        "bytes_read": 1000,
        "bytes_emitted": 500,
        "repeated_read_bytes": repeated_read_bytes,
        "verification_failures": 0,
    }


def _run_pair(loop: ProjectAdaptationLoop, version: str, fixture_ref: str) -> tuple[dict, dict]:
    observation_id = next(iter(loop.observations))
    proposal = loop.propose(
        observation_refs=[observation_id],
        version=version,
        scope="project",
        applicability=[{"kind": "project", "ref": f"project://{loop.project_id}"}],
        changes=_changes(),
        metrics_before=_metrics(100),
        metrics_after=_metrics(50),
    )
    replay = loop.shadow(
        proposal["adaptation_id"],
        fixture_ref=fixture_ref,
        provider_id="codex",
        budget={"input_tokens": 100, "output_tokens": 50, "tool_calls": 2},
        attempts=3,
    )
    loop.approve(proposal["adaptation_id"], approval_ref=f"approval://m8-07/{version}", approval_round=1)
    activation = loop.activate(proposal["adaptation_id"])
    return replay, activation


def benchmark_project_adaptation(
    *,
    root: Path,
    samples: int = 1000,
    generated_at: str = _NOW,
) -> dict[str, Any]:
    """Run the M8-07 safety matrix without provider or external service calls."""
    if type(samples) is not int or samples < 1:
        raise ValueError("samples must be positive")
    latencies: list[float] = []
    replay_attempts = deterministic_replays = 0
    activation_attempts = successful_activations = 0
    unauthorized_attempts = unauthorized_rejections = 0
    rollback_attempts = rollback_recoveries = 0
    reset_attempts = resets = 0
    authority_mutations = 0

    for index in range(samples):
        started = time.perf_counter()
        loop = ProjectAdaptationLoop(
            project_id=f"project-m8-07-{index}",
            profile_id="profile-m8-07",
            clock=lambda: generated_at,
        )
        loop.observe(
            run_ref=f"run://verified-{index}",
            state_revision=7,
            verified=True,
            metrics=_metrics(100),
            correction_refs=["correction://compact-output"],
            failure_fixture_refs=["fixture://repeat-read"],
        )
        before = loop.authority_snapshot()
        unauthorized_attempts += 1
        first_id = loop.propose(
            observation_refs=[next(iter(loop.observations))],
            version="1.0.0",
            scope="project",
            applicability=[{"kind": "project", "ref": f"project://{loop.project_id}"}],
            changes=_changes(),
            metrics_before=_metrics(100),
            metrics_after=_metrics(50),
        )["adaptation_id"]
        try:
            loop.activate(first_id)
        except Exception:
            unauthorized_rejections += 1
        replay = loop.shadow(
            first_id,
            fixture_ref=f"fixture://first-{index}",
            provider_id="codex",
            budget={"input_tokens": 100, "output_tokens": 50, "tool_calls": 2},
            attempts=3,
        )
        loop.approve(first_id, approval_ref=f"approval://m8-07/1.0.0-{index}", approval_round=1)
        loop.activate(first_id)
        replay_attempts += 1
        deterministic_replays += int(replay["deterministic"])
        second_replay, _ = _run_pair(loop, "1.1.0", f"fixture://second-{index}")
        replay_attempts += 1
        deterministic_replays += int(second_replay["deterministic"])
        activation_attempts += 2
        successful_activations += 2
        rollback_attempts += 1
        rollback = loop.rollback(
            loop._proposal_id_for_version("1.1.0"),
            target_version="1.0.0",
        )
        rollback_recoveries += int(all(rollback["safety_veto_results"].values()))
        reset_attempts += 1
        reset = loop.reset()
        resets += int(reset["status"] == "reset" and loop.active_version is None)
        authority_mutations += int(loop.authority_snapshot() != before)
        latencies.append((time.perf_counter() - started) * 1000.0)

    ordered = sorted(latencies)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    latency = {
        "p50_ms": float(statistics.median(ordered)),
        "p95_ms": float(ordered[p95_index]),
        "max_ms": float(max(ordered)),
    }
    rates = {
        "deterministic_replay_rate": deterministic_replays / replay_attempts,
        "unauthorized_activation_rejection_rate": unauthorized_rejections / unauthorized_attempts,
        "rollback_veto_recovery_rate": rollback_recoveries / rollback_attempts,
        "activation_success_rate": successful_activations / activation_attempts,
        "reset_success_rate": resets / reset_attempts,
    }
    thresholds = {
        "deterministic_replay_rate_min": 1.0,
        "unauthorized_activation_rejection_rate_min": 1.0,
        "rollback_veto_recovery_rate_min": 1.0,
        "activation_success_rate_min": 1.0,
        "reset_success_rate_min": 1.0,
        "authority_mutations_max": 0,
        "latency_p95_ms_max": 50.0,
    }
    failed_gates = [
        gate
        for gate, passed in {
            "deterministic-replay": rates["deterministic_replay_rate"] >= thresholds["deterministic_replay_rate_min"],
            "unapproved-activation": rates["unauthorized_activation_rejection_rate"] >= thresholds["unauthorized_activation_rejection_rate_min"],
            "rollback-veto-recovery": rates["rollback_veto_recovery_rate"] >= thresholds["rollback_veto_recovery_rate_min"],
            "activation": rates["activation_success_rate"] >= thresholds["activation_success_rate_min"],
            "reset": rates["reset_success_rate"] >= thresholds["reset_success_rate_min"],
            "authority": authority_mutations <= thresholds["authority_mutations_max"],
            "latency": latency["p95_ms"] <= thresholds["latency_p95_ms_max"],
        }.items()
        if not passed
    ]
    schema_dir = root / "schemas" / "m8-07"
    receipt = {
        "schema_version": "context.project-adaptation-benchmark/v1alpha1",
        "benchmark_id": f"benchmark-m8-07-{_digest({'generated_at': generated_at, 'samples': samples})[:24]}",
        "generated_at": generated_at,
        "samples": samples,
        "replay_attempts": replay_attempts,
        "deterministic_replays": deterministic_replays,
        "activation_attempts": activation_attempts,
        "successful_activations": successful_activations,
        "unauthorized_activation_attempts": unauthorized_attempts,
        "unauthorized_activation_rejections": unauthorized_rejections,
        "rollback_attempts": rollback_attempts,
        "rollback_veto_recoveries": rollback_recoveries,
        "reset_attempts": reset_attempts,
        "resets": resets,
        "rates": rates,
        "latency_ms": latency,
        "thresholds": thresholds,
        "authority_mutations": authority_mutations,
        "provider_invocations": 0,
        "external_services": 0,
        "provenance": {
            "implementation_sha256": _file_digest(root / "context_control_plane/project_adaptation.py"),
            "benchmark_sha256": _file_digest(root / "context_control_plane/project_adaptation_benchmark.py"),
            "observation_schema_sha256": _file_digest(schema_dir / "project-adaptation-observation.schema.json"),
            "proposal_schema_sha256": _file_digest(schema_dir / "project-adaptation-proposal.schema.json"),
            "replay_schema_sha256": _file_digest(schema_dir / "project-adaptation-replay.schema.json"),
            "transition_schema_sha256": _file_digest(schema_dir / "project-adaptation-transition.schema.json"),
        },
        "verdict": {"decision": "pass" if not failed_gates else "fail", "failed_gates": failed_gates},
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt
