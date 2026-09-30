"""Measured acceptance for the M9-04 Context Health projection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import socket
import subprocess
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from typing import Any, Self
from unittest.mock import patch

from .artifact_store import LocalArtifactStore
from .context_accounting import compose_context_accounting, measured_metric
from .context_health_projection import (
    ContextHealthProjectionError,
    build_context_health_projection,
    validate_context_health_projection,
)
from .metric_evidence import compose_metric_evidence
from .reference_watcher import decide_reference_watch, observe_reference

BENCHMARK_SCHEMA_VERSION = "context.context-health-benchmark/v1alpha1"
BENCHMARK_ID = "m9-04-context-health"

_THRESHOLDS = {
    "same_revision_rate_min": 1.0,
    "source_binding_rate_min": 1.0,
    "context_binding_rate_min": 1.0,
    "stale_assertion_visibility_rate_min": 1.0,
    "harness_failure_visibility_rate_min": 1.0,
    "replay_failure_visibility_rate_min": 1.0,
    "unavailable_measurement_honesty_rate_min": 1.0,
    "tamper_rejection_rate_min": 1.0,
    "expired_claim_rejections_rate_min": 1.0,
    "reference_conflict_rejections_rate_min": 1.0,
    "terminal_digest_rejections_rate_min": 1.0,
    "partial_accounting_honesty_rate_min": 1.0,
    "read_budget_classification_rate_min": 1.0,
    "acknowledged_input_replay_classification_rate_min": 1.0,
    "metric_evidence_rejections_rate_min": 1.0,
    "canary_tamper_rejections_rate_min": 1.0,
    "effect_watermark_rejections_rate_min": 1.0,
    "capacity_rejection_required": True,
    "false_healthy_claims_max": 0,
    "false_current_assertions_max": 0,
    "authority_violations_max": 0,
    "provider_invocations_max": 0,
    "external_services_max": 0,
    "operational_latency_p95_ms_max": 50.0,
    "worst_path_latency_p95_ms_max": 500.0,
}
_COUNT_FIELDS = (
    "same_revision_matches",
    "source_binding_matches",
    "context_binding_matches",
    "stale_assertion_visibility_matches",
    "harness_failure_visibility_matches",
    "replay_failure_visibility_matches",
    "unavailable_measurement_honesty_matches",
    "tamper_rejections",
    "expired_claim_rejections",
    "reference_conflict_rejections",
    "terminal_digest_rejections",
    "partial_accounting_honesty_matches",
    "read_budget_classification_matches",
    "acknowledged_input_replay_classification_matches",
    "metric_evidence_rejections",
    "canary_tamper_rejections",
    "effect_watermark_rejections",
)
_RATE_FIELDS = tuple(field.removesuffix("_matches") + "_rate" for field in _COUNT_FIELDS)
_ZERO_FIELDS = (
    "false_healthy_claims",
    "false_current_assertions",
    "authority_violations",
    "provider_invocations",
    "external_services",
)
_RESULT_FIELDS = set(_COUNT_FIELDS) | set(_RATE_FIELDS) | set(_ZERO_FIELDS) | {
    "capacity_rejected",
    "capacity_cases_rejected",
    "capacity_cases_total",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "parameters",
    "thresholds",
    "results",
    "latency_ms",
    "latency_samples_ms",
    "scale_latency_ms",
    "scale_latency_samples_ms",
    "attestation",
    "gate",
    "provenance",
    "receipt_sha256",
}
_AUTHORITY = {
    "state_write_authority": False,
    "completion_authority": False,
    "approval_authority": False,
    "provider_authority": 0,
    "external_effect_authority": 0,
}


class ContextHealthBenchmarkError(ValueError):
    """Raised when an M9-04 measured receipt cannot be trusted."""


class _EffectProbe:
    """Count provider paths and block process/network effects during acceptance."""

    provider_probe_targets = (
        "provider-skill-adapter",
        "recall-provider",
        "reviewer-adapter",
    )
    external_probe_targets = (
        "socket.connect",
        "socket.create_connection",
        "subprocess.Popen",
    )

    def __init__(self) -> None:
        self.provider_invocations = 0
        self.external_services = 0
        self._stack = ExitStack()

    def _provider_wrapper(self, original: Any) -> Any:
        def observed(*args: Any, **kwargs: Any) -> Any:
            self.provider_invocations += 1
            return original(*args, **kwargs)

        return observed

    def _blocked_external(self, *args: Any, **kwargs: Any) -> Any:
        del args, kwargs
        self.external_services += 1
        raise ContextHealthBenchmarkError(
            "external effect attempted during isolated benchmark"
        )

    def __enter__(self) -> Self:
        from .provider_skill_adapter import ProviderSkillAdapter
        from .recall_provider import RecallCoordinator
        from .reviewer_adapter import ReviewCoordinator

        for owner, attribute in (
            (ProviderSkillAdapter, "compose"),
            (RecallCoordinator, "retrieve"),
            (ReviewCoordinator, "review"),
        ):
            original = getattr(owner, attribute)
            self._stack.enter_context(
                patch.object(owner, attribute, self._provider_wrapper(original))
            )
        self._stack.enter_context(
            patch.object(socket.socket, "connect", self._blocked_external)
        )
        self._stack.enter_context(
            patch.object(socket, "create_connection", self._blocked_external)
        )
        self._stack.enter_context(
            patch.object(subprocess, "Popen", self._blocked_external)
        )
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stack.__exit__(*exc_info)


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise ContextHealthBenchmarkError("benchmark value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ContextHealthBenchmarkError(
            f"benchmark provenance is unavailable: {path}"
        ) from exc


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise ContextHealthBenchmarkError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextHealthBenchmarkError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContextHealthBenchmarkError(f"{field} requires a timezone")
    return value


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 6)


def _latency(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 6),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "max": round(max(values), 6),
    }


def _projection_arguments(fixture: Any) -> dict[str, Any]:
    return {
        "source_projection": fixture.source_projection,
        "decision_evidence_projection": fixture.decision_projection,
        "provenance_bundle": fixture.provenance_bundle,
        "trace_events": fixture.trace_events,
        "compaction_records": fixture.compaction_records,
        "accounting_records": fixture.accounting_records,
        "reference_records": fixture.reference_records,
        "harness_runs": fixture.harness_runs,
        "harness_events": fixture.harness_events,
        "recovery_records": fixture.recovery_records,
        "artifact_store": fixture.artifact_store,
        "provider_id": "provider-docmost-health",
        "observed_at": "2026-08-17T22:00:00+08:00",
        "signer": fixture.signer,
        "evidence_resolver": fixture.evidence_resolver,
        "artifact_resolver": fixture.artifact_resolver,
        "trusted_time_verifier": fixture.trusted_time_verifier,
    }


def _validate_arguments(
    fixture: Any,
    projection: dict[str, Any],
    *,
    source_arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    arguments = dict(source_arguments or _projection_arguments(fixture))
    arguments.pop("provider_id")
    arguments.pop("observed_at")
    return {"projection": projection, **arguments}


def _rehash(document: dict[str, Any], digest_field: str) -> None:
    document[digest_field] = _digest(
        {key: value for key, value in document.items() if key != digest_field}
    )


def _scaled_trace_events(
    events: list[dict[str, Any]], *, total_events: int
) -> list[dict[str, Any]]:
    scaled = copy.deepcopy(events)
    template = copy.deepcopy(scaled[-1])
    while len(scaled) < total_events:
        sequence = len(scaled) + 1
        event = {
            **template,
            "event_id": f"event-m9-04-scale-{sequence}",
            "event_name": "context.benchmark.scale",
            "sequence": sequence,
            "previous_event_sha256": scaled[-1]["event_sha256"],
            "evidence_refs": [
                "artifact://sha256/"
                + hashlib.sha256(b"m9-04-scale-evidence").hexdigest()
            ],
            "attributes": {},
            "event_sha256": "",
        }
        _rehash(event, "event_sha256")
        scaled.append(event)
    return scaled


def _reference_conflict_records(fixture: Any) -> list[dict[str, Any]]:
    watch = copy.deepcopy(fixture.reference_records[0]["watch"])
    watch["watch_id"] = "watch-m9-04-benchmark-current"
    baseline = fixture.evidence_resolver(
        watch["source_ref"], watch["baseline_revision"]
    )
    if not isinstance(baseline, (bytes, bytearray, memoryview)):
        raise ContextHealthBenchmarkError("reference baseline evidence is unavailable")
    trusted_time = {
        "kind": "fixture-clock",
        "trusted_at": "2026-08-17T12:00:00Z",
        "evidence": b"fixture-clock:2026-08-17T12:00:00Z",
    }
    observation = observe_reference(
        watch=watch,
        mode="fixture",
        availability_status="available",
        observed_revision=watch["baseline_revision"],
        observed_content=bytes(baseline),
        unavailability_evidence=None,
        trusted_time=trusted_time,
        trusted_time_verifier=fixture.trusted_time_verifier,
    )
    fixture.artifacts[observation["current_evidence_ref"]] = bytes(baseline)
    decision = decide_reference_watch(
        observation,
        expected_watch=watch,
        artifact_resolver=fixture.artifact_resolver,
        trusted_time_verifier=fixture.trusted_time_verifier,
    )
    return [
        *copy.deepcopy(fixture.reference_records),
        {
            "reference_validity_watermark": watch["watch_revision"],
            "watch": watch,
            "observation": observation,
            "decision": decision,
            "replacement_assertion": None,
        },
    ]


def _accounting_scenario(
    fixture: Any, *, measured_route_count: int, evidence_available: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source = copy.deepcopy(fixture.accounting_records[0]["receipt"])
    token_names = (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    )
    for route_index, route in enumerate(source["routes"]):
        if route_index >= measured_route_count:
            continue
        for name in token_names:
            evidence_ref = (
                f"trace://provider/{measured_route_count}/{route['route_id']}/{name}"
            )
            route["metrics"][name] = measured_metric(
                10,
                "tokens",
                source_kind="provider_trace",
                evidence_ref=(
                    evidence_ref
                    if evidence_available
                    else f"trace://missing/{route['route_id']}/{name}"
                ),
            )
    if evidence_available:
        for route in source["routes"]:
            for name, metric in route["metrics"].items():
                if metric["status"] == "measured":
                    metric["evidence_ref"] = (
                        "trace://accounting-scenario/"
                        f"{measured_route_count}/{route['route_id']}/{name}"
                    )
    receipt = compose_context_accounting(
        accounting_id=source["accounting_id"],
        corpus_id=source["corpus_id"],
        corpus_sha256=source["corpus_sha256"],
        budget_tokens=source["budget_tokens"],
        routes=source["routes"],
        observed_at=source["observed_at"],
    )
    if evidence_available:
        for route in receipt["routes"]:
            for name, metric in route["metrics"].items():
                if metric["status"] != "measured":
                    continue
                evidence_ref = metric["evidence_ref"]
                fixture.artifacts[evidence_ref] = compose_metric_evidence(
                    accounting_id=receipt["accounting_id"],
                    accounting_sha256=receipt["accounting_sha256"],
                    corpus_id=receipt["corpus_id"],
                    corpus_sha256=receipt["corpus_sha256"],
                    route_id=route["route_id"],
                    provider_id=route["provider_id"],
                    trace_event_id="event-m9-04-8",
                    run_id="run-m9-04",
                    observed_at=receipt["observed_at"],
                    metrics={
                        name: {
                            "value": metric["value"],
                            "unit": metric["unit"],
                            "source_kind": metric["source_kind"],
                        }
                    },
                )
    receipt_ref = "artifact://sha256/" + _digest(receipt)
    records = [
        {
            "trace_event_id": "event-m9-04-8",
            "receipt": receipt,
            "receipt_ref": receipt_ref,
        }
    ]
    events = copy.deepcopy(fixture.trace_events)
    events[-1]["evidence_refs"] = [receipt_ref]
    _rehash(events[-1], "event_sha256")
    return records, events


def _is_rejected(arguments: dict[str, Any], **overrides: Any) -> bool:
    try:
        build_context_health_projection(**{**arguments, **overrides})
    except ContextHealthProjectionError:
        return True
    return False


def _capacity_rejections(arguments: dict[str, Any]) -> tuple[int, int]:
    recovery_template = arguments["recovery_records"][1]
    drilldown_records = []
    for index in range(257):
        record = copy.deepcopy(recovery_template)
        record["recovery_id"] = f"recovery-m9-04-capacity-{index:03d}"
        drilldown_records.append(record)
    cases = (
        {"trace_events": [{}] * 10_001},
        {"compaction_records": [{}] * 10_001},
        {"accounting_records": [{}] * 10_001},
        {"reference_records": [{}] * 10_001},
        {"harness_runs": [{}] * 10_001},
        {"harness_events": [{}] * 50_001},
        {"recovery_records": [{}] * 10_001},
        {
            "harness_runs": [
                {"scope_refs": ["scope://benchmark"] * 50_001, "tool_grants": []}
            ]
        },
        {"trace_events": [{"nodes": [None] * 200_001}]},
        {"trace_events": [{"oversized": "x" * 16_385}]},
        {"recovery_records": drilldown_records},
    )
    rejected = sum(_is_rejected(arguments, **case) for case in cases)
    return rejected, len(cases)


def _gate(
    results: dict[str, Any],
    latency: dict[str, float],
    scale_latency: dict[str, float],
) -> dict[str, Any]:
    checks = {
        "same-revision": results["same_revision_rate"] >= 1.0,
        "source-binding": results["source_binding_rate"] >= 1.0,
        "context-binding": results["context_binding_rate"] >= 1.0,
        "stale-assertion-visibility": results[
            "stale_assertion_visibility_rate"
        ]
        >= 1.0,
        "harness-failure-visibility": results[
            "harness_failure_visibility_rate"
        ]
        >= 1.0,
        "replay-failure-visibility": results["replay_failure_visibility_rate"]
        >= 1.0,
        "unavailable-measurement-honesty": results[
            "unavailable_measurement_honesty_rate"
        ]
        >= 1.0,
        "tamper-rejection": results["tamper_rejections_rate"] >= 1.0,
        "expired-claim-rejection": results["expired_claim_rejections_rate"] >= 1.0,
        "reference-conflict-rejection": results[
            "reference_conflict_rejections_rate"
        ]
        >= 1.0,
        "terminal-digest-rejection": results["terminal_digest_rejections_rate"]
        >= 1.0,
        "partial-accounting-honesty": results[
            "partial_accounting_honesty_rate"
        ]
        >= 1.0,
        "read-budget-classification": results[
            "read_budget_classification_rate"
        ]
        >= 1.0,
        "acknowledged-input-replay-classification": results[
            "acknowledged_input_replay_classification_rate"
        ]
        >= 1.0,
        "metric-evidence-rejection": results["metric_evidence_rejections_rate"]
        >= 1.0,
        "canary-tamper-rejection": results["canary_tamper_rejections_rate"]
        >= 1.0,
        "effect-watermark-rejection": results[
            "effect_watermark_rejections_rate"
        ]
        >= 1.0,
        "capacity-rejection": results["capacity_rejected"] is True
        and results["capacity_cases_rejected"] == results["capacity_cases_total"],
        "false-healthy": results["false_healthy_claims"] == 0,
        "false-current": results["false_current_assertions"] == 0,
        "authority": results["authority_violations"] == 0,
        "provider": results["provider_invocations"] == 0,
        "external": results["external_services"] == 0,
        "operational-latency-p95": latency["p95"] < 50.0,
        "scale-latency-p95": scale_latency["p95"] < 500.0,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {"status": "passed" if not failed else "failed", "failed_gates": failed}


def benchmark_context_health_projection(
    *, root: Path, iterations: int = 1000, generated_at: str
) -> dict[str, Any]:
    """Measure deterministic M9-04 build, rebuild and tamper rejection."""
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ValueError("iterations must be an integer from 1 through 1000")
    root = Path(root).resolve()
    _timestamp(generated_at, "generated_at")

    # The benchmark fixture is repository-only acceptance material. Runtime
    # projection code has no dependency on the test package.
    from tests.test_m9_04_context_health_projection import M904Fixture

    counts = {field: 0 for field in _COUNT_FIELDS}
    zeros = {field: 0 for field in _ZERO_FIELDS}
    latency_samples: list[float] = []
    deterministic_projection_sha256: str | None = None
    effect_probe = _EffectProbe()
    with effect_probe, tempfile.TemporaryDirectory() as directory:
        store = LocalArtifactStore(Path(directory) / "artifacts")
        store.initialize()
        fixture = M904Fixture(root, store)
        baseline_arguments = _projection_arguments(fixture)
        scale_trace_events = 3500
        scale_arguments = {
            **baseline_arguments,
            "trace_events": _scaled_trace_events(
                fixture.trace_events, total_events=scale_trace_events
            ),
        }
        arguments = baseline_arguments
        reference_conflict_records = _reference_conflict_records(fixture)
        terminal_digest_runs = copy.deepcopy(fixture.harness_runs)
        terminal_digest_runs[0]["provider"] = "benchmark-tamper"
        _rehash(terminal_digest_runs[0], "run_sha256")
        partial_accounting, partial_trace = _accounting_scenario(
            fixture, measured_route_count=1, evidence_available=True
        )
        missing_metric_accounting, missing_metric_trace = _accounting_scenario(
            fixture,
            measured_route_count=len(fixture.accounting_records[0]["receipt"]["routes"]),
            evidence_available=False,
        )
        read_budget_records = [copy.deepcopy(fixture.recovery_records[0])]
        read_budget_records[0]["recovery_budget_bytes"] = 1
        acknowledged_input_records = [copy.deepcopy(fixture.recovery_records[0])]
        acknowledged_input_records[0]["response_input_id"] = (
            "input-m9-04-answered"
        )
        tampered_canary_records = copy.deepcopy(fixture.compaction_records)
        tampered_canary_records[0]["canary_receipt"]["canary_sha256"] = "0" * 64
        drifted_watermark_records = [copy.deepcopy(fixture.recovery_records[0])]
        drifted_watermark_records[0]["effect_high_watermark"] += 1
        for _ in range(iterations):
            started = time.perf_counter()
            projection = build_context_health_projection(**arguments)
            validate_context_health_projection(
                **_validate_arguments(
                    fixture, projection, source_arguments=arguments
                )
            )

            tampered = copy.deepcopy(projection)
            tampered["harness_health"]["failed_run_count"] = 0
            tampered["harness_health"]["status"] = "passed"
            body = {
                key: value
                for key, value in tampered.items()
                if key not in {"projection_sha256", "signature"}
            }
            tampered["projection_sha256"] = _digest(body)
            tampered["signature"] = fixture.signer.sign(tampered)
            try:
                validate_context_health_projection(
                    **_validate_arguments(
                        fixture, tampered, source_arguments=arguments
                    )
                )
            except ContextHealthProjectionError:
                counts["tamper_rejections"] += 1
            latency_samples.append((time.perf_counter() - started) * 1000)

            state_revision = fixture.snapshot["project"]["revision"]
            counts["same_revision_matches"] += (
                projection["state_revision"] == state_revision
                and projection["state_sha256"]
                == fixture.source_projection["state_sha256"]
            )
            bindings = projection["source_bindings"]
            counts["source_binding_matches"] += (
                bindings["trace_head_sha256"]
                == arguments["trace_events"][-1]["event_sha256"]
                and bindings["harness_event_head_sha256"]
                == fixture.harness_events[-1]["event_sha256"]
                and bindings["reference_validity_watermark"] == 7
                and bindings["checkpoint_sha256s"]
                == [fixture.checkpoint_ref.digest]
            )
            context = projection["context_health"]
            counts["context_binding_matches"] += (
                context["required_event_coverage_millionths"] == 1_000_000
                and context["revision_mismatch_count"] == 0
                and context["active_work_mismatch_count"] == 0
                and context["compaction_pair_count"] == 1
            )
            reference = projection["reference_health"]
            counts["stale_assertion_visibility_matches"] += (
                reference["stale_assertion_ids"]
                == ["assertion-m9-04-reference"]
                and reference["current_assertion_ids"] == []
                and reference["status"] == "failed"
            )
            harness = projection["harness_health"]
            counts["harness_failure_visibility_matches"] += (
                harness["failed_run_count"] == 1
                and harness["status"] == "failed"
                and harness["effect_health_status"]
                == "unavailable-v1-revision-semantics"
            )
            replay = projection["replay_health"]
            counts["replay_failure_visibility_matches"] += (
                replay["passed_recovery_count"] == 1
                and replay["failed_recovery_count"] == 1
                and replay["first_action_mismatch_count"] == 1
            )
            counts["unavailable_measurement_honesty_matches"] += (
                context["provider_token_status"] == "unavailable"
                and context["context_window_status"] == "unavailable"
                and context["input_tokens"] is None
                and context["output_tokens"] is None
            )
            zeros["false_healthy_claims"] += projection["overall_status"] != "failed"
            zeros["false_current_assertions"] += bool(
                set(reference["stale_assertion_ids"])
                & set(reference["current_assertion_ids"])
            )
            zeros["authority_violations"] += projection["authority"] != _AUTHORITY

            counts["expired_claim_rejections"] += _is_rejected(
                baseline_arguments,
                observed_at="2026-08-19T00:00:00+08:00",
            )
            counts["reference_conflict_rejections"] += _is_rejected(
                baseline_arguments,
                reference_records=reference_conflict_records,
            )
            counts["terminal_digest_rejections"] += _is_rejected(
                baseline_arguments,
                harness_runs=terminal_digest_runs,
            )
            partial_projection = build_context_health_projection(
                **{
                    **baseline_arguments,
                    "accounting_records": partial_accounting,
                    "trace_events": partial_trace,
                }
            )
            partial_context = partial_projection["context_health"]
            partial_honest = (
                partial_context["provider_token_status"] == "unavailable"
                and partial_context["input_tokens"] is None
                and partial_context["output_tokens"] is None
            )
            counts["partial_accounting_honesty_matches"] += partial_honest
            zeros["false_healthy_claims"] += not partial_honest

            read_budget_projection = build_context_health_projection(
                **{**baseline_arguments, "recovery_records": read_budget_records}
            )
            read_budget = read_budget_projection["replay_health"]
            read_budget_match = (
                read_budget["first_action_mismatch_count"] == 0
                and read_budget["recoveries"][0]["failure_kind"] == "read-budget"
                and read_budget["recoveries"][0]["first_action_match"] is None
            )
            counts["read_budget_classification_matches"] += read_budget_match
            zeros["false_healthy_claims"] += not read_budget_match

            input_replay_projection = build_context_health_projection(
                **{
                    **baseline_arguments,
                    "recovery_records": acknowledged_input_records,
                }
            )
            input_replay = input_replay_projection["replay_health"]
            input_replay_match = (
                input_replay["first_action_mismatch_count"] == 0
                and input_replay["recoveries"][0]["failure_kind"] == "input-replay"
                and input_replay["recoveries"][0]["first_action_match"] is None
            )
            counts["acknowledged_input_replay_classification_matches"] += (
                input_replay_match
            )
            zeros["false_healthy_claims"] += not input_replay_match
            counts["metric_evidence_rejections"] += _is_rejected(
                baseline_arguments,
                accounting_records=missing_metric_accounting,
                trace_events=missing_metric_trace,
            )
            counts["canary_tamper_rejections"] += _is_rejected(
                baseline_arguments,
                compaction_records=tampered_canary_records,
            )
            counts["effect_watermark_rejections"] += _is_rejected(
                baseline_arguments,
                recovery_records=drifted_watermark_records,
            )
            if deterministic_projection_sha256 is None:
                deterministic_projection_sha256 = projection["projection_sha256"]
            elif projection["projection_sha256"] != deterministic_projection_sha256:
                counts["source_binding_matches"] -= 1

        scale_iterations = min(iterations, 25)
        scale_latency_samples: list[float] = []
        for _ in range(scale_iterations):
            started = time.perf_counter()
            scale_projection = build_context_health_projection(**scale_arguments)
            validate_context_health_projection(
                **_validate_arguments(
                    fixture,
                    scale_projection,
                    source_arguments=scale_arguments,
                )
            )
            scale_latency_samples.append((time.perf_counter() - started) * 1000)

        capacity_cases_rejected, capacity_cases_total = _capacity_rejections(
            baseline_arguments
        )
        capacity_rejected = capacity_cases_rejected == capacity_cases_total

    zeros["provider_invocations"] = effect_probe.provider_invocations
    zeros["external_services"] = effect_probe.external_services

    results: dict[str, Any] = {**counts, **zeros}
    for count_field, rate_field in zip(_COUNT_FIELDS, _RATE_FIELDS, strict=True):
        results[rate_field] = counts[count_field] / iterations
    results["capacity_rejected"] = capacity_rejected
    results["capacity_cases_rejected"] = capacity_cases_rejected
    results["capacity_cases_total"] = capacity_cases_total
    latency = _latency(latency_samples)
    scale_latency = _latency(scale_latency_samples)
    gate = _gate(results, latency, scale_latency)
    receipt: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "generated_at": generated_at,
        "parameters": {
            "iterations": iterations,
            "fixture": "m9-04-veto-matrix-scale-v2",
            "latency_path": "scale-build+source-bound-rebuild+coordinated-tamper",
            "scale_trace_events": scale_trace_events,
            "scale_iterations": scale_iterations,
        },
        "thresholds": copy.deepcopy(_THRESHOLDS),
        "results": results,
        "latency_ms": latency,
        "latency_samples_ms": [round(value, 6) for value in latency_samples],
        "scale_latency_ms": scale_latency,
        "scale_latency_samples_ms": [
            round(value, 6) for value in scale_latency_samples
        ],
        "attestation": {
            "mode": "local-unattested",
            "measurement_authenticity": False,
            "zero_count_source": "isolated-effect-probe",
            "provider_probe_targets": list(effect_probe.provider_probe_targets),
            "external_probe_targets": list(effect_probe.external_probe_targets),
        },
        "gate": gate,
        "provenance": {
            "implementation_sha256": _file_digest(
                root / "context_control_plane/context_health_projection.py"
            ),
            "benchmark_sha256": _file_digest(
                root / "context_control_plane/context_health_benchmark.py"
            ),
            "fixture_sha256": _file_digest(
                root / "tests/test_m9_04_context_health_projection.py"
            ),
            "schema_sha256": _file_digest(
                root / "schemas/m9-04/context-health-projection.schema.json"
            ),
            "metric_evidence_implementation_sha256": _file_digest(
                root / "context_control_plane/metric_evidence.py"
            ),
            "metric_evidence_schema_sha256": _file_digest(
                root / "schemas/m9-04/metric-evidence.schema.json"
            ),
            "projection_sha256": deterministic_projection_sha256,
        },
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    return receipt


def validate_context_health_benchmark(receipt: Any, *, root: Path) -> dict[str, Any]:
    """Validate counts, samples, gate and source provenance in a receipt."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ContextHealthBenchmarkError("benchmark receipt fields are invalid")
    if (
        receipt["schema_version"] != BENCHMARK_SCHEMA_VERSION
        or receipt["benchmark_id"] != BENCHMARK_ID
    ):
        raise ContextHealthBenchmarkError("benchmark identity is invalid")
    _timestamp(receipt["generated_at"], "generated_at")
    parameters = receipt["parameters"]
    if not isinstance(parameters, dict) or set(parameters) != {
        "iterations",
        "fixture",
        "latency_path",
        "scale_trace_events",
        "scale_iterations",
    }:
        raise ContextHealthBenchmarkError("benchmark parameters are invalid")
    iterations = parameters["iterations"]
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ContextHealthBenchmarkError("benchmark iterations are invalid")
    if (
        parameters["fixture"] != "m9-04-veto-matrix-scale-v2"
        or parameters["latency_path"]
        != "scale-build+source-bound-rebuild+coordinated-tamper"
        or parameters["scale_trace_events"] != 3500
        or parameters["scale_iterations"] != min(iterations, 25)
    ):
        raise ContextHealthBenchmarkError("benchmark fixture is invalid")
    if receipt["thresholds"] != _THRESHOLDS:
        raise ContextHealthBenchmarkError("benchmark thresholds are invalid")

    results = receipt["results"]
    if not isinstance(results, dict) or set(results) != _RESULT_FIELDS:
        raise ContextHealthBenchmarkError("benchmark results are invalid")
    for field in _COUNT_FIELDS:
        value = results[field]
        if type(value) is not int or not 0 <= value <= iterations:
            raise ContextHealthBenchmarkError(f"{field} is invalid")
    for count_field, rate_field in zip(_COUNT_FIELDS, _RATE_FIELDS, strict=True):
        rate = results[rate_field]
        expected = results[count_field] / iterations
        if type(rate) is not float or not math.isfinite(rate) or rate != expected:
            raise ContextHealthBenchmarkError(f"{rate_field} is invalid")
    for field in _ZERO_FIELDS:
        if type(results[field]) is not int or results[field] != 0:
            raise ContextHealthBenchmarkError(f"{field} must remain zero")
    if results["capacity_rejected"] is not True:
        raise ContextHealthBenchmarkError("capacity rejection is required")
    if (
        results["capacity_cases_total"] != 11
        or results["capacity_cases_rejected"] != results["capacity_cases_total"]
    ):
        raise ContextHealthBenchmarkError("capacity matrix rejection is required")

    samples = receipt["latency_samples_ms"]
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
        raise ContextHealthBenchmarkError("latency samples are invalid")
    latency = receipt["latency_ms"]
    if not isinstance(latency, dict) or set(latency) != {"min", "p50", "p95", "max"}:
        raise ContextHealthBenchmarkError("latency summary is invalid")
    expected_latency = _latency([float(value) for value in samples])
    if latency != expected_latency:
        raise ContextHealthBenchmarkError("latency summary does not match samples")

    scale_samples = receipt["scale_latency_samples_ms"]
    if (
        not isinstance(scale_samples, list)
        or len(scale_samples) != parameters["scale_iterations"]
        or any(
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
            for value in scale_samples
        )
    ):
        raise ContextHealthBenchmarkError("scale latency samples are invalid")
    scale_latency = receipt["scale_latency_ms"]
    if not isinstance(scale_latency, dict) or set(scale_latency) != {
        "min",
        "p50",
        "p95",
        "max",
    }:
        raise ContextHealthBenchmarkError("scale latency summary is invalid")
    expected_scale_latency = _latency([float(value) for value in scale_samples])
    if scale_latency != expected_scale_latency:
        raise ContextHealthBenchmarkError(
            "scale latency summary does not match samples"
        )

    if receipt["attestation"] != {
        "mode": "local-unattested",
        "measurement_authenticity": False,
        "zero_count_source": "isolated-effect-probe",
        "provider_probe_targets": list(_EffectProbe.provider_probe_targets),
        "external_probe_targets": list(_EffectProbe.external_probe_targets),
    }:
        raise ContextHealthBenchmarkError("benchmark attestation is invalid")
    expected_gate = _gate(results, latency, scale_latency)
    if receipt["gate"] != expected_gate or expected_gate["status"] != "passed":
        raise ContextHealthBenchmarkError("benchmark gate did not pass")

    root = Path(root).resolve()
    provenance = receipt["provenance"]
    expected_provenance = {
        "implementation_sha256": _file_digest(
            root / "context_control_plane/context_health_projection.py"
        ),
        "benchmark_sha256": _file_digest(
            root / "context_control_plane/context_health_benchmark.py"
        ),
        "fixture_sha256": _file_digest(
            root / "tests/test_m9_04_context_health_projection.py"
        ),
        "schema_sha256": _file_digest(
            root / "schemas/m9-04/context-health-projection.schema.json"
        ),
        "metric_evidence_implementation_sha256": _file_digest(
            root / "context_control_plane/metric_evidence.py"
        ),
        "metric_evidence_schema_sha256": _file_digest(
            root / "schemas/m9-04/metric-evidence.schema.json"
        ),
        "projection_sha256": provenance.get("projection_sha256")
        if isinstance(provenance, dict)
        else None,
    }
    if provenance != expected_provenance:
        raise ContextHealthBenchmarkError("benchmark provenance is invalid")
    projection_sha256 = provenance["projection_sha256"]
    if (
        not isinstance(projection_sha256, str)
        or len(projection_sha256) != 64
        or any(char not in "0123456789abcdef" for char in projection_sha256)
    ):
        raise ContextHealthBenchmarkError("projection provenance is invalid")
    expected_receipt_sha256 = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if receipt["receipt_sha256"] != expected_receipt_sha256:
        raise ContextHealthBenchmarkError("benchmark receipt digest mismatch")
    return copy.deepcopy(receipt)


__all__ = [
    "BENCHMARK_ID",
    "BENCHMARK_SCHEMA_VERSION",
    "ContextHealthBenchmarkError",
    "benchmark_context_health_projection",
    "validate_context_health_benchmark",
]
