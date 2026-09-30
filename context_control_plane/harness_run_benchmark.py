"""Deterministic M8-06 Harness Run benchmark and evidence receipt."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from pathlib import Path
from typing import Any

from .harness_run import HarnessCoordinator, HarnessRunError, create_harness_run, replay_harness_events

BENCHMARK_SCHEMA_VERSION = "context.harness-benchmark/v1alpha1"
SAMPLE_CASES = 1_000
_NOW = "2026-08-16T20:00:00+08:00"


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _run(
    *,
    run_id: str,
    claim_id: str,
    status: str = "running",
    parent_run_id: str | None = None,
    provider_contract_version: str = "provider-contract/v1",
) -> dict[str, Any]:
    return create_harness_run(
        run_id=run_id,
        project_id="project-m8-06-benchmark",
        task_id="task-m8-06-benchmark",
        task_revision=7,
        claim_id=claim_id,
        claim_lease_epoch=2,
        claim_fence=2,
        provider="codex",
        provider_contract_version=provider_contract_version,
        execution_packet_sha256="a" * 64,
        skill_set_digest="b" * 64,
        tool_grants=["repo.read", "state.commit"],
        checkpoint_id="checkpoint-m8-06",
        effect_high_watermark=11,
        verification_profile_id="profile-default",
        reference_validity_watermark=3,
        trace_id=f"trace-{run_id}",
        status=status,
        parent_run_id=parent_run_id,
        scope_refs=[{"scope_kind": "file", "scope_ref": "context_control_plane/harness_run.py"}],
        created_at=_NOW,
        updated_at=_NOW,
    )


def provider_drift_rejection_reason(index: int) -> str:
    """Return the admission error for a genuine provider-contract drift case."""
    coordinator = HarnessCoordinator(
        project_id="project-m8-06-benchmark",
        task_revision=7,
        provider_contract_version="provider-contract/v1",
        clock=lambda: _NOW,
    )
    parent_id = f"drift-parent-{index}"
    coordinator.register_run(_run(run_id=parent_id, claim_id=f"claim-{parent_id}"))
    drifted = _run(
        run_id=f"drift-worker-{index}",
        claim_id=f"claim-drift-{index}",
        provider_contract_version="provider-contract/v0",
        parent_run_id=parent_id,
    )
    try:
        coordinator.dispatch_worker(parent_id, drifted)
    except HarnessRunError as exc:
        return str(exc)
    raise AssertionError("provider drift was admitted")


def benchmark_harness_run(*, root: Path, samples: int = SAMPLE_CASES, generated_at: str = _NOW) -> dict[str, Any]:
    """Run the M8-06 failure matrix without provider or external service calls."""
    if type(samples) is not int or samples < 1:
        raise ValueError("samples must be positive")
    latencies: list[float] = []
    provider_drift_attempts = provider_drift_rejections = 0
    worker_loss_attempts = worker_loss_unchanged = 0
    effect_attempts = effect_rejections = 0
    stale_handoff_attempts = stale_handoff_rejections = 0
    first_action_attempts = first_action_rejections = 0
    replay_attempts = replay_matches = 0
    fanout_dispatches = fanin_completions = 0

    for index in range(samples):
        started = time.perf_counter()
        coordinator = HarnessCoordinator(
            project_id="project-m8-06-benchmark",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: _NOW,
            effect_dispatcher=lambda intent: {"accepted": True, "intent": intent},
        )
        parent_id = f"parent-{index}"
        coordinator.register_run(_run(run_id=parent_id, claim_id=f"claim-{parent_id}"))
        worker_ids = []
        for worker_index in range(2):
            worker_id = f"worker-{index}-{worker_index}"
            worker_ids.append(worker_id)
            coordinator.dispatch_worker(
                parent_id,
                _run(run_id=worker_id, claim_id=f"claim-{worker_id}", parent_run_id=parent_id),
            )
            fanout_dispatches += 1
            coordinator.complete_worker(worker_id, evidence_sha256="c" * 64)
        coordinator.commit_effect(
            parent_id,
            effect_id=f"effect-{index}",
            effect_key=f"effect:write:{index}",
            operation="write-file",
            scope_ref={"scope_kind": "file", "scope_ref": "context_control_plane/harness_run.py"},
            expected_project_revision=7,
        )
        receipt = coordinator.fan_in(parent_id, worker_ids)
        fanin_completions += receipt["status"] == "completed"
        codex = replay_harness_events(coordinator.events, provider="codex")
        claude = replay_harness_events(coordinator.events, provider="claude")
        replay_attempts += 1
        replay_matches += codex == claude

        provider_drift_attempts += 1
        if "provider contract drift" in provider_drift_rejection_reason(index):
            provider_drift_rejections += 1

        loss_coordinator = HarnessCoordinator(
            project_id="project-m8-06-benchmark",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: _NOW,
        )
        loss_coordinator.register_run(_run(run_id=f"loss-parent-{index}", claim_id=f"loss-claim-{index}"))
        loss_worker = f"loss-worker-{index}"
        loss_coordinator.dispatch_worker(
            f"loss-parent-{index}",
            _run(run_id=loss_worker, claim_id=f"loss-worker-claim-{index}", parent_run_id=f"loss-parent-{index}"),
        )
        before = loss_coordinator.authority_snapshot()
        loss_coordinator.mark_worker_lost(loss_worker)
        worker_loss_attempts += 1
        worker_loss_unchanged += loss_coordinator.authority_snapshot() == before

        effect_attempts += 1
        try:
            coordinator.commit_effect(
                parent_id,
                effect_id=f"outside-{index}",
                effect_key=f"effect:outside:{index}",
                operation="write-file",
                scope_ref={"scope_kind": "file", "scope_ref": "outside.py"},
                expected_project_revision=7,
            )
        except HarnessRunError:
            effect_rejections += 1

        handoff_coordinator = HarnessCoordinator(
            project_id="project-m8-06-benchmark",
            task_revision=7,
            provider_contract_version="provider-contract/v1",
            clock=lambda: _NOW,
        )
        source = f"source-{index}"
        target = f"target-{index}"
        handoff_coordinator.register_run(_run(run_id=source, claim_id=f"source-claim-{index}", status="waiting"))
        handoff_coordinator.register_run(_run(run_id=target, claim_id=f"target-claim-{index}", status="proposed"))
        stale_handoff_attempts += 1
        try:
            handoff_coordinator.create_handoff(
                source,
                target,
                checkpoint_id="checkpoint-m8-06",
                next_action="run verifier",
                expected_task_revision=6,
            )
        except HarnessRunError:
            stale_handoff_rejections += 1
        handoff = handoff_coordinator.create_handoff(
            source,
            target,
            checkpoint_id="checkpoint-m8-06",
            next_action="run verifier",
            expected_task_revision=7,
        )
        first_action_attempts += 1
        try:
            handoff_coordinator.acknowledge_handoff(handoff["handoff_id"], "edit code")
        except HarnessRunError:
            first_action_rejections += 1
        latencies.append((time.perf_counter() - started) * 1000.0)

    ordered = sorted(latencies)
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95) - 1))
    latency = {
        "p50_ms": float(statistics.median(ordered)),
        "p95_ms": float(ordered[p95_index]),
        "max_ms": float(max(ordered)),
    }
    metrics = {
        "provider_drift_rejections": provider_drift_rejections,
        "worker_loss_authority_unchanged": worker_loss_unchanged,
        "effect_scope_rejections": effect_rejections,
        "stale_handoff_rejections": stale_handoff_rejections,
        "first_action_mismatch_rejections": first_action_rejections,
        "replay_matches": replay_matches,
        "fanout_dispatches": fanout_dispatches,
        "fanin_completions": fanin_completions,
    }
    thresholds = {
        "provider_drift_rejection_rate_min": 1.0,
        "worker_loss_unchanged_rate_min": 1.0,
        "effect_scope_rejection_rate_min": 1.0,
        "stale_handoff_rejection_rate_min": 1.0,
        "first_action_rejection_rate_min": 1.0,
        "replay_match_rate_min": 1.0,
        "latency_p95_ms_max": 50.0,
    }
    rates = {
        "provider_drift_rejection_rate": provider_drift_rejections / provider_drift_attempts,
        "worker_loss_unchanged_rate": worker_loss_unchanged / worker_loss_attempts,
        "effect_scope_rejection_rate": effect_rejections / effect_attempts,
        "stale_handoff_rejection_rate": stale_handoff_rejections / stale_handoff_attempts,
        "first_action_rejection_rate": first_action_rejections / first_action_attempts,
        "replay_match_rate": replay_matches / replay_attempts,
    }
    failed = [
        key for key, value in rates.items()
        if value < thresholds.get(key.replace("_rate", "_rate_min"), 1.0)
    ]
    if latency["p95_ms"] > thresholds["latency_p95_ms_max"]:
        failed.append("latency_p95_ms")
    result = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": "benchmark-m8-06-" + _sha([generated_at, samples])[:24],
        "generated_at": generated_at,
        "samples": samples,
        "provider_drift_attempts": provider_drift_attempts,
        "worker_loss_attempts": worker_loss_attempts,
        "effect_attempts": effect_attempts,
        "stale_handoff_attempts": stale_handoff_attempts,
        "first_action_attempts": first_action_attempts,
        "replay_attempts": replay_attempts,
        "metrics": metrics,
        "rates": rates,
        "latency_ms": latency,
        "thresholds": thresholds,
        "provider_invocations": 0,
        "external_services": 0,
        "shared_authority_claim": False,
        "provenance": {
            "implementation_sha256": hashlib.sha256((root / "context_control_plane/harness_run.py").read_bytes()).hexdigest(),
            "benchmark_sha256": hashlib.sha256((root / "context_control_plane/harness_run_benchmark.py").read_bytes()).hexdigest(),
            "run_schema_sha256": hashlib.sha256((root / "schemas/m8-06/harness-run.schema.json").read_bytes()).hexdigest(),
            "event_schema_sha256": hashlib.sha256((root / "schemas/m8-06/harness-event.schema.json").read_bytes()).hexdigest(),
            "handoff_schema_sha256": hashlib.sha256((root / "schemas/m8-06/harness-handoff.schema.json").read_bytes()).hexdigest(),
        },
        "verdict": {"decision": "pass" if not failed else "fail", "failed_gates": failed},
        "receipt_sha256": "",
    }
    result["receipt_sha256"] = _sha({k: v for k, v in result.items() if k != "receipt_sha256"})
    return result
