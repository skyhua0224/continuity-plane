"""Deterministic M8-02 Work/claim/lease interleaving benchmark."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from .effect_scope_gate import scopes_overlap
from .shared_work_ledger import ClaimLifecycleError, WorkLedger

SCHEMA_VERSION = "context.shared-work-benchmark/v1alpha1"
SHARED_WORK_SCENARIOS = (
    "same-work-double-claim",
    "overlap-scope-double-claim",
    "non-overlap-cas-loser-retry",
    "heartbeat-stale-token",
    "exact-expiry-reclaim",
    "revoke-before-dispatch",
    "dispatch-before-revoke",
    "old-worker-after-reclaim",
    "duplicate-terminal-active-work",
    "exact-request-replay-changed-payload",
)

_BASE_TIME = datetime.fromisoformat("2026-08-16T10:00:00+00:00")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_BENCHMARK_ID_RE = re.compile(r"^benchmark-m8-02-[0-9a-f]{24}$")
_METRIC_FIELDS = {
    "silent_overwrites",
    "duplicate_claims",
    "duplicate_effects",
    "post_revoke_effects",
    "orphan_reclaims",
    "expected_orphan_reclaims",
    "old_worker_admissions",
    "event_mismatches",
    "revision_mismatches",
    "hash_mismatches",
}
_ZERO_GATE_FIELDS = (
    "silent_overwrites",
    "duplicate_claims",
    "duplicate_effects",
    "post_revoke_effects",
    "old_worker_admissions",
    "event_mismatches",
    "revision_mismatches",
    "hash_mismatches",
)
_THRESHOLD_FIELDS = {
    *(f"{field}_max" for field in _ZERO_GATE_FIELDS),
    "scenario_pass_rate_min",
    "orphan_reclaim_rate_min",
    "latency_p95_ms_max",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "samples_per_scenario",
    "scenario_count",
    "sample_count",
    "scenarios",
    "metrics",
    "latency_ms",
    "thresholds",
    "verdict",
    "authority_backend",
    "shared_authority_claim",
    "provider_invocations",
    "receipt_sha256",
}


class SharedWorkBenchmarkError(RuntimeError):
    """Raised when a benchmark configuration or receipt fails closed."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SharedWorkBenchmarkError("benchmark data is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _receipt_digest(receipt: dict[str, Any]) -> str:
    body = copy.deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return _digest(body)


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise SharedWorkBenchmarkError("generated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SharedWorkBenchmarkError("generated_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SharedWorkBenchmarkError("generated_at requires a timezone")
    return value


def _time_after(milliseconds: int) -> str:
    return (_BASE_TIME + timedelta(milliseconds=milliseconds)).isoformat()


def _scope(ref: str) -> dict[str, str]:
    return {"scope_kind": "capability", "scope_ref": ref}


def _work(
    work_id: str,
    scope_ref: str,
    *,
    status: str = "ready",
    identity_key: str | None = None,
) -> dict[str, Any]:
    return {
        "work_id": work_id,
        "status": status,
        "identity_key": identity_key or f"identity-{work_id}",
        "scope_refs": [_scope(scope_ref)],
    }


def _ledger(*works: dict[str, Any]) -> WorkLedger:
    return WorkLedger(
        project_id="project-m8-02-benchmark",
        project_revision=7,
        works=list(works) or [_work("work-a", "capability/a")],
        max_ttl_ms=1_000,
    )


def _acquire(
    ledger: WorkLedger,
    *,
    work_id: str = "work-a",
    actor_ref: str = "actor-a",
    claim_id: str = "claim-a",
    scope_ref: str = "capability/a",
    expected_revision: int | None = None,
    ttl_ms: int = 500,
) -> dict[str, Any]:
    return ledger.acquire_claim(
        work_id=work_id,
        actor_ref=actor_ref,
        expected_project_revision=(
            ledger.project_revision if expected_revision is None else expected_revision
        ),
        observed_at=_time_after(0),
        requested_ttl_ms=ttl_ms,
        claim_id=claim_id,
        scope_owners=[_scope(scope_ref)],
    )["claim"]


def _current_claim(ledger: WorkLedger, claim_id: str) -> dict[str, Any]:
    return next(
        claim for claim in ledger.snapshot()["claims"] if claim["claim_id"] == claim_id
    )


def _dispatch_args(
    ledger: WorkLedger,
    claim: dict[str, Any],
    *,
    request_id: str,
    effect_id: str,
    effect_key: str,
    observed_ms: int,
    request_sha256: str | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "effect_id": effect_id,
        "effect_key": effect_key,
        "request_sha256": request_sha256 or _digest({"request_id": request_id}),
        "claim_id": claim["claim_id"],
        "work_id": claim["work_id"],
        "actor_ref": claim["actor_ref"],
        "expected_project_revision": ledger.project_revision,
        "expected_claim_revision": claim["claim_revision"],
        "lease_epoch": claim["lease_epoch"],
        "fence": claim["lease_epoch"],
        "observed_at": _time_after(observed_ms),
        "operation": "write-artifact",
        "scope_ref": copy.deepcopy(claim["scope_owners"][0]),
    }


def _expected_denial(
    ledger: WorkLedger,
    expected_code: str,
    operation: Callable[[], Any],
) -> tuple[bool, int]:
    before = ledger.snapshot()
    code: str | None = None
    try:
        operation()
    except ClaimLifecycleError as exc:
        code = exc.code
    after = ledger.snapshot()
    mutated = int(after != before)
    return code == expected_code and mutated == 0, mutated


def _empty_result(*ledgers: WorkLedger) -> dict[str, Any]:
    return {
        "passed": True,
        "ledgers": list(ledgers),
        **{field: 0 for field in _METRIC_FIELDS},
    }


def _same_work_double_claim(_: int) -> dict[str, Any]:
    ledger = _ledger()
    _acquire(ledger)
    result = _empty_result(ledger)
    denied, mutated = _expected_denial(
        ledger,
        "work_already_claimed",
        lambda: _acquire(
            ledger,
            actor_ref="actor-b",
            claim_id="claim-b",
        ),
    )
    result["passed"] = denied
    result["silent_overwrites"] = mutated
    return result


def _overlap_scope_double_claim(_: int) -> dict[str, Any]:
    ledger = _ledger(
        _work("work-a", "capability/shared"),
        _work("work-b", "capability/shared"),
    )
    _acquire(ledger, scope_ref="capability/shared")
    result = _empty_result(ledger)
    denied, mutated = _expected_denial(
        ledger,
        "scope_overlap",
        lambda: _acquire(
            ledger,
            work_id="work-b",
            actor_ref="actor-b",
            claim_id="claim-b",
            scope_ref="capability/shared",
        ),
    )
    result["passed"] = denied
    result["silent_overwrites"] = mutated
    return result


def _non_overlap_cas_loser_retry(_: int) -> dict[str, Any]:
    ledger = _ledger(
        _work("work-a", "capability/a"),
        _work("work-b", "capability/b"),
    )
    original_revision = ledger.project_revision
    _acquire(ledger)
    result = _empty_result(ledger)
    denied, mutated = _expected_denial(
        ledger,
        "stale_revision",
        lambda: _acquire(
            ledger,
            work_id="work-b",
            actor_ref="actor-b",
            claim_id="claim-b",
            scope_ref="capability/b",
            expected_revision=original_revision,
        ),
    )
    retry = _acquire(
        ledger,
        work_id="work-b",
        actor_ref="actor-b",
        claim_id="claim-b",
        scope_ref="capability/b",
    )
    active = [
        claim for claim in ledger.snapshot()["claims"] if claim["status"] == "active"
    ]
    result["passed"] = denied and retry["status"] == "active" and len(active) == 2
    result["silent_overwrites"] = mutated
    return result


def _heartbeat_stale_token(_: int) -> dict[str, Any]:
    ledger = _ledger()
    original = _acquire(ledger)
    renewed = ledger.heartbeat_claim(
        claim_id=original["claim_id"],
        actor_ref=original["actor_ref"],
        expected_project_revision=ledger.project_revision,
        expected_claim_revision=original["claim_revision"],
        lease_epoch=original["lease_epoch"],
        fence=original["lease_epoch"],
        observed_at=_time_after(100),
        requested_ttl_ms=500,
    )["claim"]
    result = _empty_result(ledger)
    denied, mutated = _expected_denial(
        ledger,
        "claim_revision_mismatch",
        lambda: ledger.heartbeat_claim(
            claim_id=original["claim_id"],
            actor_ref=original["actor_ref"],
            expected_project_revision=ledger.project_revision,
            expected_claim_revision=original["claim_revision"],
            lease_epoch=original["lease_epoch"],
            fence=original["lease_epoch"],
            observed_at=_time_after(200),
            requested_ttl_ms=500,
        ),
    )
    result["passed"] = denied and renewed["claim_revision"] == 2
    result["silent_overwrites"] = mutated
    return result


def _exact_expiry_reclaim(_: int) -> dict[str, Any]:
    ledger = _ledger()
    old = _acquire(ledger)
    reclaimed = ledger.reclaim_claim(
        old_claim_id=old["claim_id"],
        new_claim_id="claim-b",
        new_actor_ref="actor-b",
        expected_project_revision=ledger.project_revision,
        expected_claim_revision=old["claim_revision"],
        lease_epoch=old["lease_epoch"],
        fence=old["lease_epoch"],
        observed_at=old["lease_expires_at"],
        requested_ttl_ms=500,
        scope_owners=copy.deepcopy(old["scope_owners"]),
    )
    old_after = _current_claim(ledger, old["claim_id"])
    new_after = _current_claim(ledger, "claim-b")
    result = _empty_result(ledger)
    result["orphan_reclaims"] = 1
    result["expected_orphan_reclaims"] = 1
    result["passed"] = (
        old_after["status"] == "expired"
        and new_after["status"] == "active"
        and new_after["reclaimed_from_claim_id"] == old["claim_id"]
        and reclaimed["claim"]["lease_epoch"] > old["lease_epoch"]
    )
    return result


def _revoke_before_dispatch(_: int) -> dict[str, Any]:
    ledger = _ledger()
    claim = _acquire(ledger)
    ledger.revoke_claim(
        claim_id=claim["claim_id"],
        revoker_ref="admin-a",
        expected_project_revision=ledger.project_revision,
        expected_claim_revision=claim["claim_revision"],
        lease_epoch=claim["lease_epoch"],
        fence=claim["lease_epoch"],
        observed_at=_time_after(100),
        reason="benchmark revoke",
    )
    result = _empty_result(ledger)
    denied, mutated = _expected_denial(
        ledger,
        "claim_not_active",
        lambda: ledger.start_effect_dispatch(
            **_dispatch_args(
                ledger,
                claim,
                request_id="request-revoked",
                effect_id="effect-revoked",
                effect_key="effect-key-revoked",
                observed_ms=200,
            )
        ),
    )
    post_revoke = len(ledger.snapshot()["effects"])
    result["passed"] = denied and post_revoke == 0
    result["silent_overwrites"] = mutated
    result["post_revoke_effects"] = post_revoke
    return result


def _dispatch_before_revoke(_: int) -> dict[str, Any]:
    ledger = _ledger()
    claim = _acquire(ledger)
    ledger.start_effect_dispatch(
        **_dispatch_args(
            ledger,
            claim,
            request_id="request-before-revoke",
            effect_id="effect-before-revoke",
            effect_key="effect-key-before-revoke",
            observed_ms=100,
        )
    )
    current = _current_claim(ledger, claim["claim_id"])
    ledger.revoke_claim(
        claim_id=current["claim_id"],
        revoker_ref="admin-a",
        expected_project_revision=ledger.project_revision,
        expected_claim_revision=current["claim_revision"],
        lease_epoch=current["lease_epoch"],
        fence=current["lease_epoch"],
        observed_at=_time_after(200),
        reason="benchmark revoke",
    )
    effects_before = len(ledger.snapshot()["effects"])
    revoked = _current_claim(ledger, claim["claim_id"])
    result = _empty_result(ledger)
    denied, mutated = _expected_denial(
        ledger,
        "claim_not_active",
        lambda: ledger.start_effect_dispatch(
            **_dispatch_args(
                ledger,
                revoked,
                request_id="request-after-revoke",
                effect_id="effect-after-revoke",
                effect_key="effect-key-after-revoke",
                observed_ms=300,
            )
        ),
    )
    effects_after = len(ledger.snapshot()["effects"])
    result["passed"] = denied and effects_before == effects_after == 1
    result["silent_overwrites"] = mutated
    result["post_revoke_effects"] = max(effects_after - effects_before, 0)
    return result


def _old_worker_after_reclaim(_: int) -> dict[str, Any]:
    ledger = _ledger()
    old = _acquire(ledger)
    ledger.reclaim_claim(
        old_claim_id=old["claim_id"],
        new_claim_id="claim-b",
        new_actor_ref="actor-b",
        expected_project_revision=ledger.project_revision,
        expected_claim_revision=old["claim_revision"],
        lease_epoch=old["lease_epoch"],
        fence=old["lease_epoch"],
        observed_at=old["lease_expires_at"],
        requested_ttl_ms=500,
        scope_owners=copy.deepcopy(old["scope_owners"]),
    )
    old_after = _current_claim(ledger, old["claim_id"])
    result = _empty_result(ledger)
    denied, mutated = _expected_denial(
        ledger,
        "claim_not_active",
        lambda: ledger.start_effect_dispatch(
            **_dispatch_args(
                ledger,
                old_after,
                request_id="request-old-worker",
                effect_id="effect-old-worker",
                effect_key="effect-key-old-worker",
                observed_ms=600,
            )
        ),
    )
    new_claim = _current_claim(ledger, "claim-b")
    new_dispatch = ledger.start_effect_dispatch(
        **_dispatch_args(
            ledger,
            new_claim,
            request_id="request-new-worker",
            effect_id="effect-new-worker",
            effect_key="effect-key-new-worker",
            observed_ms=600,
        )
    )
    old_effects = [
        effect
        for effect in ledger.snapshot()["effects"]
        if effect["claim_id"] == old["claim_id"]
    ]
    result["passed"] = (
        denied and new_dispatch["status"] == "accepted" and not old_effects
    )
    result["silent_overwrites"] = mutated
    result["old_worker_admissions"] = len(old_effects)
    return result


def _duplicate_terminal_active_work(_: int) -> dict[str, Any]:
    terminal = _ledger(
        _work(
            "work-terminal",
            "capability/terminal",
            status="completed",
            identity_key="identity-duplicate",
        ),
        _work(
            "work-new",
            "capability/new",
            identity_key="identity-duplicate",
        ),
    )
    terminal_denied, terminal_mutated = _expected_denial(
        terminal,
        "duplicate_terminal_work",
        lambda: _acquire(
            terminal,
            work_id="work-new",
            claim_id="claim-new",
            scope_ref="capability/new",
        ),
    )
    active = _ledger(
        _work("work-a", "capability/a", identity_key="identity-duplicate"),
        _work("work-b", "capability/b", identity_key="identity-duplicate"),
    )
    _acquire(active)
    active_denied, active_mutated = _expected_denial(
        active,
        "duplicate_work_identity",
        lambda: _acquire(
            active,
            work_id="work-b",
            actor_ref="actor-b",
            claim_id="claim-b",
            scope_ref="capability/b",
        ),
    )
    result = _empty_result(terminal, active)
    result["passed"] = terminal_denied and active_denied
    result["silent_overwrites"] = terminal_mutated + active_mutated
    return result


def _exact_request_replay_changed_payload(_: int) -> dict[str, Any]:
    ledger = _ledger()
    claim = _acquire(ledger)
    arguments = _dispatch_args(
        ledger,
        claim,
        request_id="request-replay",
        effect_id="effect-replay",
        effect_key="effect-key-replay",
        observed_ms=100,
    )
    first = ledger.start_effect_dispatch(**arguments)
    before_replay = ledger.snapshot()
    replay = ledger.start_effect_dispatch(**arguments)
    replay_read_only = before_replay == ledger.snapshot()
    changed = copy.deepcopy(arguments)
    changed["request_sha256"] = "f" * 64
    result = _empty_result(ledger)
    denied, mutated = _expected_denial(
        ledger,
        "request_replay_conflict",
        lambda: ledger.start_effect_dispatch(**changed),
    )
    result["passed"] = denied and replay_read_only and replay == first
    result["silent_overwrites"] = mutated + int(not replay_read_only)
    return result


_RUNNERS: dict[str, Callable[[int], dict[str, Any]]] = {
    "same-work-double-claim": _same_work_double_claim,
    "overlap-scope-double-claim": _overlap_scope_double_claim,
    "non-overlap-cas-loser-retry": _non_overlap_cas_loser_retry,
    "heartbeat-stale-token": _heartbeat_stale_token,
    "exact-expiry-reclaim": _exact_expiry_reclaim,
    "revoke-before-dispatch": _revoke_before_dispatch,
    "dispatch-before-revoke": _dispatch_before_revoke,
    "old-worker-after-reclaim": _old_worker_after_reclaim,
    "duplicate-terminal-active-work": _duplicate_terminal_active_work,
    "exact-request-replay-changed-payload": _exact_request_replay_changed_payload,
}


def _ledger_mismatches(ledger: WorkLedger) -> dict[str, int]:
    snapshot = ledger.snapshot()
    transitions = snapshot["transitions"]
    event_mismatches = 0
    revision_mismatches = 0
    hash_mismatches = 0
    seen_ids: set[str] = set()
    previous_hash: str | None = None
    expected_revision = (
        transitions[0]["project_revision_before"] if transitions else None
    )
    for transition in transitions:
        transition_id = transition.get("transition_id")
        if (
            transition.get("schema_version") != "context.work-claim-transition/v1alpha1"
            or not isinstance(transition.get("operation"), str)
            or not transition.get("operation")
            or not isinstance(transition_id, str)
            or transition_id in seen_ids
        ):
            event_mismatches += 1
        if isinstance(transition_id, str):
            seen_ids.add(transition_id)
        before = transition.get("project_revision_before")
        after = transition.get("project_revision_after")
        if before != expected_revision or type(after) is not int or after != before + 1:
            revision_mismatches += 1
        expected_revision = after
        body = copy.deepcopy(transition)
        recorded_hash = body.pop("transition_sha256", None)
        body.pop("transition_id", None)
        computed_hash = _digest(body)
        if (
            recorded_hash != computed_hash
            or transition.get("previous_transition_sha256") != previous_hash
            or transition_id != f"transition-{computed_hash[:32]}"
        ):
            hash_mismatches += 1
        previous_hash = recorded_hash
    if transitions and snapshot["project_revision"] != expected_revision:
        revision_mismatches += 1

    active_claims = [
        claim for claim in snapshot["claims"] if claim["status"] == "active"
    ]
    duplicate_claims = 0
    for index, left in enumerate(active_claims):
        for right in active_claims[index + 1 :]:
            if left["work_id"] == right["work_id"] or any(
                scopes_overlap(left_scope, right_scope)
                for left_scope in left["scope_owners"]
                for right_scope in right["scope_owners"]
            ):
                duplicate_claims += 1
    effect_keys: set[str] = set()
    duplicate_effects = 0
    for effect in snapshot["effects"]:
        if effect["effect_key"] in effect_keys:
            duplicate_effects += 1
        effect_keys.add(effect["effect_key"])
    return {
        "duplicate_claims": duplicate_claims,
        "duplicate_effects": duplicate_effects,
        "event_mismatches": event_mismatches,
        "revision_mismatches": revision_mismatches,
        "hash_mismatches": hash_mismatches,
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 6)


def _expected_thresholds(latency_p95_threshold_ms: float) -> dict[str, Any]:
    return {
        **{f"{field}_max": 0 for field in _ZERO_GATE_FIELDS},
        "scenario_pass_rate_min": 1.0,
        "orphan_reclaim_rate_min": 1.0,
        "latency_p95_ms_max": latency_p95_threshold_ms,
    }


def _expected_failed_gates(
    metrics: dict[str, int],
    *,
    scenario_pass_rate: float,
    orphan_reclaim_rate: float,
    latency_p95_ms: float,
    thresholds: dict[str, Any],
) -> list[str]:
    failures = [
        field
        for field in _ZERO_GATE_FIELDS
        if metrics[field] > thresholds[f"{field}_max"]
    ]
    if scenario_pass_rate < thresholds["scenario_pass_rate_min"]:
        failures.append("scenario_pass_rate")
    if orphan_reclaim_rate < thresholds["orphan_reclaim_rate_min"]:
        failures.append("orphan_reclaim_rate")
    if latency_p95_ms > thresholds["latency_p95_ms_max"]:
        failures.append("latency_p95_ms")
    return failures


def benchmark_shared_work(
    *,
    samples: int = 10,
    generated_at: str,
    latency_p95_threshold_ms: float = 50.0,
) -> dict[str, Any]:
    """Run each deterministic interleaving and return a measured receipt."""
    if type(samples) is not int or not 1 <= samples <= 1000:
        raise SharedWorkBenchmarkError("samples is invalid")
    if (
        type(latency_p95_threshold_ms) not in {int, float}
        or not math.isfinite(latency_p95_threshold_ms)
        or latency_p95_threshold_ms <= 0
    ):
        raise SharedWorkBenchmarkError("latency_p95_threshold_ms is invalid")
    generated_at = _timestamp(generated_at)
    threshold = float(latency_p95_threshold_ms)
    metrics = {field: 0 for field in _METRIC_FIELDS}
    latencies: list[float] = []
    scenario_results: list[dict[str, Any]] = []

    for scenario in SHARED_WORK_SCENARIOS:
        passed = 0
        for sample_index in range(samples):
            started = time.perf_counter_ns()
            result = _RUNNERS[scenario](sample_index)
            elapsed_ms = max((time.perf_counter_ns() - started) / 1_000_000, 0.000001)
            latencies.append(elapsed_ms)
            for field in _METRIC_FIELDS:
                metrics[field] += result[field]
            for ledger in result["ledgers"]:
                mismatches = _ledger_mismatches(ledger)
                for field, value in mismatches.items():
                    metrics[field] += value
            passed += int(result["passed"])
        scenario_results.append(
            {"scenario": scenario, "attempted": samples, "passed": passed}
        )

    sample_count = samples * len(SHARED_WORK_SCENARIOS)
    total_passed = sum(item["passed"] for item in scenario_results)
    scenario_pass_rate = round(total_passed / sample_count, 6)
    expected_reclaims = metrics["expected_orphan_reclaims"]
    orphan_reclaim_rate = round(
        metrics["orphan_reclaims"] / expected_reclaims if expected_reclaims else 0.0,
        6,
    )
    latency = {
        "p50": _percentile(latencies, 0.50),
        "p95": _percentile(latencies, 0.95),
        "max": round(max(latencies), 6),
    }
    thresholds = _expected_thresholds(threshold)
    failed_gates = _expected_failed_gates(
        metrics,
        scenario_pass_rate=scenario_pass_rate,
        orphan_reclaim_rate=orphan_reclaim_rate,
        latency_p95_ms=latency["p95"],
        thresholds=thresholds,
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": "benchmark-m8-02-"
        + _digest(
            {
                "samples_per_scenario": samples,
                "scenarios": list(SHARED_WORK_SCENARIOS),
                "thresholds": thresholds,
            }
        )[:24],
        "generated_at": generated_at,
        "samples_per_scenario": samples,
        "scenario_count": len(SHARED_WORK_SCENARIOS),
        "sample_count": sample_count,
        "scenarios": scenario_results,
        "metrics": metrics,
        "latency_ms": latency,
        "thresholds": thresholds,
        "verdict": {
            "decision": "pass" if not failed_gates else "fail",
            "scenario_pass_rate": scenario_pass_rate,
            "orphan_reclaim_rate": orphan_reclaim_rate,
            "latency_p95_within_threshold": latency["p95"] <= threshold,
            "failed_gates": failed_gates,
        },
        "authority_backend": "in-memory-reference",
        "shared_authority_claim": False,
        "provider_invocations": 0,
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    validate_shared_work_benchmark(receipt)
    return receipt


def validate_shared_work_benchmark(receipt: Any) -> None:
    """Validate field closure, metric consistency, thresholds, and digest."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise SharedWorkBenchmarkError("benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise SharedWorkBenchmarkError("benchmark schema version is invalid")
    if (
        not isinstance(receipt["benchmark_id"], str)
        or _BENCHMARK_ID_RE.fullmatch(receipt["benchmark_id"]) is None
    ):
        raise SharedWorkBenchmarkError("benchmark_id is invalid")
    _timestamp(receipt["generated_at"])
    samples = receipt["samples_per_scenario"]
    if type(samples) is not int or not 1 <= samples <= 1000:
        raise SharedWorkBenchmarkError("samples_per_scenario is invalid")
    if receipt["scenario_count"] != len(SHARED_WORK_SCENARIOS):
        raise SharedWorkBenchmarkError("scenario_count is inconsistent")
    if receipt["sample_count"] != samples * len(SHARED_WORK_SCENARIOS):
        raise SharedWorkBenchmarkError("sample_count is inconsistent")
    scenarios = receipt["scenarios"]
    if (
        not isinstance(scenarios, list)
        or len(scenarios) != len(SHARED_WORK_SCENARIOS)
        or any(not isinstance(item, dict) for item in scenarios)
        or [item.get("scenario") for item in scenarios] != list(SHARED_WORK_SCENARIOS)
    ):
        raise SharedWorkBenchmarkError("scenario coverage is invalid")
    for item in scenarios:
        if not isinstance(item, dict) or set(item) != {
            "scenario",
            "attempted",
            "passed",
        }:
            raise SharedWorkBenchmarkError("scenario fields are invalid")
        if item["attempted"] != samples or type(item["passed"]) is not int:
            raise SharedWorkBenchmarkError("scenario counts are invalid")
        if not 0 <= item["passed"] <= item["attempted"]:
            raise SharedWorkBenchmarkError("scenario pass count is invalid")

    metrics = receipt["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != _METRIC_FIELDS:
        raise SharedWorkBenchmarkError("benchmark metrics fields are invalid")
    if any(type(value) is not int or value < 0 for value in metrics.values()):
        raise SharedWorkBenchmarkError("benchmark metrics are invalid")
    if metrics["expected_orphan_reclaims"] != samples:
        raise SharedWorkBenchmarkError("orphan reclaim coverage is incomplete")
    if metrics["orphan_reclaims"] > metrics["expected_orphan_reclaims"]:
        raise SharedWorkBenchmarkError("orphan reclaim count is inconsistent")

    latency = receipt["latency_ms"]
    if not isinstance(latency, dict) or set(latency) != {"p50", "p95", "max"}:
        raise SharedWorkBenchmarkError("latency fields are invalid")
    if any(
        type(value) not in {int, float} or not math.isfinite(value) or value <= 0
        for value in latency.values()
    ):
        raise SharedWorkBenchmarkError("latency values are invalid")
    if not latency["p50"] <= latency["p95"] <= latency["max"]:
        raise SharedWorkBenchmarkError("latency percentiles are inconsistent")

    thresholds = receipt["thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) != _THRESHOLD_FIELDS:
        raise SharedWorkBenchmarkError("threshold fields are invalid")
    if any(
        type(thresholds[f"{field}_max"]) is not int or thresholds[f"{field}_max"] != 0
        for field in _ZERO_GATE_FIELDS
    ):
        raise SharedWorkBenchmarkError("zero-loss thresholds are invalid")
    for field in ("scenario_pass_rate_min", "orphan_reclaim_rate_min"):
        if type(thresholds[field]) not in {int, float} or thresholds[field] != 1.0:
            raise SharedWorkBenchmarkError("rate thresholds are invalid")
    threshold = thresholds["latency_p95_ms_max"]
    if (
        type(threshold) not in {int, float}
        or not math.isfinite(threshold)
        or threshold <= 0
        or thresholds != _expected_thresholds(float(threshold))
    ):
        raise SharedWorkBenchmarkError("thresholds are invalid")

    total_passed = sum(item["passed"] for item in scenarios)
    expected_pass_rate = round(total_passed / receipt["sample_count"], 6)
    expected_reclaim_rate = round(
        metrics["orphan_reclaims"] / metrics["expected_orphan_reclaims"], 6
    )
    verdict = receipt["verdict"]
    if not isinstance(verdict, dict) or set(verdict) != {
        "decision",
        "scenario_pass_rate",
        "orphan_reclaim_rate",
        "latency_p95_within_threshold",
        "failed_gates",
    }:
        raise SharedWorkBenchmarkError("verdict fields are invalid")
    if (
        type(verdict["scenario_pass_rate"]) not in {int, float}
        or type(verdict["orphan_reclaim_rate"]) not in {int, float}
        or type(verdict["latency_p95_within_threshold"]) is not bool
        or not isinstance(verdict["failed_gates"], list)
        or any(not isinstance(item, str) for item in verdict["failed_gates"])
    ):
        raise SharedWorkBenchmarkError("verdict values are invalid")
    expected_failures = _expected_failed_gates(
        metrics,
        scenario_pass_rate=expected_pass_rate,
        orphan_reclaim_rate=expected_reclaim_rate,
        latency_p95_ms=latency["p95"],
        thresholds=thresholds,
    )
    expected_decision = "pass" if not expected_failures else "fail"
    if verdict != {
        "decision": expected_decision,
        "scenario_pass_rate": expected_pass_rate,
        "orphan_reclaim_rate": expected_reclaim_rate,
        "latency_p95_within_threshold": latency["p95"] <= threshold,
        "failed_gates": expected_failures,
    }:
        raise SharedWorkBenchmarkError("verdict is inconsistent")
    if receipt["authority_backend"] != "in-memory-reference":
        raise SharedWorkBenchmarkError("authority_backend is invalid")
    if receipt["shared_authority_claim"] is not False:
        raise SharedWorkBenchmarkError("benchmark cannot claim shared authority")
    if receipt["provider_invocations"] != 0:
        raise SharedWorkBenchmarkError("benchmark invoked a provider")
    expected_benchmark_id = (
        "benchmark-m8-02-"
        + _digest(
            {
                "samples_per_scenario": samples,
                "scenarios": list(SHARED_WORK_SCENARIOS),
                "thresholds": thresholds,
            }
        )[:24]
    )
    if receipt["benchmark_id"] != expected_benchmark_id:
        raise SharedWorkBenchmarkError("benchmark_id does not bind the configuration")
    if (
        not isinstance(receipt["receipt_sha256"], str)
        or _SHA256_RE.fullmatch(receipt["receipt_sha256"]) is None
    ):
        raise SharedWorkBenchmarkError("benchmark receipt digest is invalid")
    if receipt["receipt_sha256"] != _receipt_digest(receipt):
        raise SharedWorkBenchmarkError("benchmark receipt digest mismatch")
