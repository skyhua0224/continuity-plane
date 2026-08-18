"""Deterministic M8-10 notification and long-campaign scale benchmark."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .collaboration_notifications import (
    CollaborationNotificationService,
    HMACNotificationSigner,
    SQLiteCollaborationNotificationStore,
    build_agent_inbox_items,
    render_sse_batch,
    validate_collaboration_notification_event,
)
from .state_mcp import RequestContext
from .unattended_receipts import validate_unattended_dispatch_step

BENCHMARK_SCHEMA_VERSION = "context.collaboration-notification-benchmark/v1alpha1"
_EVENT_KINDS = (
    "work_claimed",
    "review_requested",
    "conflict_detected",
    "approval_requested",
    "deploy_intent",
)
_PROVIDERS = ("codex", "claude")
_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "event_count",
    "campaign_steps",
    "providers",
    "publish_attempts",
    "published_events",
    "dual_session_delivery_attempts",
    "consistent_dual_session_events",
    "offline_catch_up_attempts",
    "offline_catch_up_deliveries",
    "duplicate_attempts",
    "duplicate_suppressions",
    "signed_event_validation_attempts",
    "signed_event_validations",
    "same_revision_delivery_attempts",
    "same_revision_deliveries",
    "deterministic_sse_matches",
    "rates",
    "latency_ms",
    "thresholds",
    "authority_violations",
    "provider_invocations",
    "external_services",
    "campaign_scale",
    "provenance",
    "verdict",
    "receipt_sha256",
}
_RATE_FIELDS = {
    "publish_rate",
    "dual_session_match_rate",
    "offline_catch_up_rate",
    "duplicate_suppression_rate",
    "signed_event_validation_rate",
    "same_revision_delivery_rate",
}
_THRESHOLDS: dict[str, float | int] = {
    "publish_rate_min": 1.0,
    "dual_session_match_rate_min": 1.0,
    "offline_catch_up_rate_min": 1.0,
    "duplicate_suppression_rate_min": 1.0,
    "signed_event_validation_rate_min": 1.0,
    "same_revision_delivery_rate_min": 1.0,
    "authority_violations_max": 0,
    "publish_latency_p95_ms_max": 50.0,
    "catch_up_latency_ms_max": 2000.0,
}


class _AllowAuthorizer:
    def authorize_scope(
        self,
        context: RequestContext,
        action: str,
        tenant_id: str,
        project_id: str,
    ) -> bool:
        return (
            tenant_id == "tenant-m8-10-benchmark"
            and project_id == "project-m8-10-benchmark"
        )

    def verify_notification_source(
        self,
        context: RequestContext,
        request: dict[str, Any],
    ) -> bool:
        return (
            request["tenant_id"] == "tenant-m8-10-benchmark"
            and request["project_id"] == "project-m8-10-benchmark"
            and request["state_revision"] == 67
            and request["evidence_refs"]
            == [
                "artifact://m8-10/"
                + request["request_id"].removeprefix("publish-m8-10-")
            ]
        )


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
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "0" * 64


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _latency(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return {
        "p50_ms": float(statistics.median(ordered)),
        "p95_ms": float(ordered[p95_index]),
        "max_ms": float(max(ordered)),
    }


def _publish_request(index: int, *, state_revision: int) -> dict[str, Any]:
    kind = _EVENT_KINDS[index % len(_EVENT_KINDS)]
    return {
        "schema_version": "context.collaboration-notification-publish/v1alpha1",
        "request_id": f"publish-m8-10-{index:06d}",
        "tenant_id": "tenant-m8-10-benchmark",
        "project_id": "project-m8-10-benchmark",
        "state_revision": state_revision,
        "event_kind": kind,
        "work_id": f"work-{index:06d}",
        "summary": f"Collaboration notification {index}.",
        "target_refs": ["actor://reviewer"],
        "evidence_refs": [f"artifact://m8-10/{index:06d}"],
        "requires_approval": kind in {"approval_requested", "deploy_intent"},
        "causation_ref": f"work:work-{index:06d}",
        "correlation_ref": "campaign:M8",
    }


def _subscription_request(provider: str) -> dict[str, Any]:
    return {
        "schema_version": "context.collaboration-subscription-request/v1alpha1",
        "request_id": f"subscribe-m8-10-{provider}",
        "subscription_id": f"subscription-m8-10-{provider}",
        "tenant_id": "tenant-m8-10-benchmark",
        "project_id": "project-m8-10-benchmark",
        "provider": provider,
        "transport": "sse",
        "event_kinds": sorted(_EVENT_KINDS),
    }


def _step_receipt() -> dict[str, Any]:
    body = {
        "schema_version": "context.unattended-dispatch-step/v1alpha1",
        "status": "completed",
        "project_id": "project-m8-10-benchmark",
        "project_revision_before": 1,
        "project_revision_after": 2,
        "profile_id": "profile-m8-10-benchmark",
        "governance_revision": 1,
        "obligation_id": "obligation-m8-10-benchmark",
        "work_id": "work-m8-10-benchmark",
        "work_revision_before": 1,
        "work_revision_after": 2,
        "claim_id": "claim-m8-10-benchmark",
        "lease_epoch": 1,
        "fence": 1,
        "packet_sha256": "1" * 64,
        "execution_sha256": "2" * 64,
        "verification_decision_sha256": "3" * 64,
        "claim_evidence_verdict_sha256": "4" * 64,
        "completion_receipt_sha256": "5" * 64,
        "evidence_ids": ["evidence-m8-10-benchmark"],
        "condition_decision_sha256": None,
        "condition_evidence_ids": [],
        "state_write_authority": False,
        "completion_authority": False,
        "provider_authority": 0,
        "external_effect_authority": 0,
    }
    return {**body, "step_sha256": _digest(body)}


def _campaign_scale(campaign_steps: int) -> dict[str, Any]:
    step = _step_receipt()
    latencies: list[float] = []
    validation_attempts = 0
    for committed_steps in range(1, campaign_steps + 1):
        started = time.perf_counter()
        for _ in range(committed_steps):
            validate_unattended_dispatch_step(step)
            validation_attempts += 1
        latencies.append((time.perf_counter() - started) * 1000.0)
    expected_attempts = campaign_steps * (campaign_steps + 1) // 2
    return {
        "steps": campaign_steps,
        "validation_attempts": validation_attempts,
        "expected_quadratic_attempts": expected_attempts,
        "incremental_index_attempts": campaign_steps,
        "validation_amplification": validation_attempts / campaign_steps,
        "projected_1000_step_validation_attempts": 1000 * 1001 // 2,
        "observed_complexity": "quadratic",
        "adoption_decision": "adopt-append-only-step-index",
        "target_stage": "M10-production-pilot",
        "latency_ms": _latency(latencies),
    }


def _provenance(root: Path) -> dict[str, str]:
    schema_dir = root / "schemas" / "m8-10"
    return {
        "implementation_sha256": _file_digest(
            root / "context_control_plane/collaboration_notifications.py"
        ),
        "benchmark_sha256": _file_digest(
            root / "context_control_plane/collaboration_notification_benchmark.py"
        ),
        "unattended_cursor_store_sha256": _file_digest(
            root / "context_control_plane/unattended_cursor_store.py"
        ),
        "notification_schema_sha256": _file_digest(
            schema_dir / "collaboration-notification.schema.json"
        ),
        "benchmark_schema_sha256": _file_digest(
            schema_dir / "collaboration-notification-benchmark.schema.json"
        ),
    }


def _failed_gates(
    rates: Mapping[str, float],
    latency: Mapping[str, Any],
    *,
    authority_violations: int,
    campaign_scale: Mapping[str, Any],
) -> list[str]:
    checks = {
        "publish": rates["publish_rate"] >= _THRESHOLDS["publish_rate_min"],
        "dual-session": rates["dual_session_match_rate"]
        >= _THRESHOLDS["dual_session_match_rate_min"],
        "offline-catch-up": rates["offline_catch_up_rate"]
        >= _THRESHOLDS["offline_catch_up_rate_min"],
        "duplicate-suppression": rates["duplicate_suppression_rate"]
        >= _THRESHOLDS["duplicate_suppression_rate_min"],
        "signed-event": rates["signed_event_validation_rate"]
        >= _THRESHOLDS["signed_event_validation_rate_min"],
        "same-revision": rates["same_revision_delivery_rate"]
        >= _THRESHOLDS["same_revision_delivery_rate_min"],
        "authority": authority_violations
        <= _THRESHOLDS["authority_violations_max"],
        "publish-latency": latency["publish"]["p95_ms"]
        <= _THRESHOLDS["publish_latency_p95_ms_max"],
        "catch-up-latency": latency["catch_up_total_ms"]
        <= _THRESHOLDS["catch_up_latency_ms_max"],
        "campaign-scale": campaign_scale["validation_attempts"]
        == campaign_scale["expected_quadratic_attempts"]
        and campaign_scale["adoption_decision"] == "adopt-append-only-step-index",
    }
    return [gate for gate, passed in checks.items() if not passed]


def benchmark_collaboration_notifications(
    *,
    root: Path,
    event_count: int = 1000,
    campaign_steps: int = 256,
    generated_at: str = "2026-08-17T13:30:00+08:00",
) -> dict[str, Any]:
    """Measure signed dual-session catch-up and current campaign validation growth."""
    if type(event_count) is not int or not 1 <= event_count <= 1000:
        raise ValueError("event_count must be between 1 and 1000")
    if type(campaign_steps) is not int or not 1 <= campaign_steps <= 1000:
        raise ValueError("campaign_steps must be between 1 and 1000")
    root = root.resolve()
    signer = HMACNotificationSigner(
        key_id="benchmark-m8-10",
        secret=b"m8-10-benchmark-signing-material",
    )
    authorizer = _AllowAuthorizer()
    publisher = RequestContext("actor-executor", "authorization-executor")
    subscribers = {
        provider: RequestContext(
            f"actor-{provider}-session",
            f"authorization-{provider}-session",
        )
        for provider in _PROVIDERS
    }
    state_revision = 67
    publish_latencies: list[float] = []
    requests: list[dict[str, Any]] = []
    published: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "notifications.sqlite3"
        store = SQLiteCollaborationNotificationStore(path, signer=signer)
        service = CollaborationNotificationService(
            store,
            authorizer=authorizer,
            clock=lambda: generated_at,
        )
        for index in range(event_count):
            request = _publish_request(index, state_revision=state_revision)
            started = time.perf_counter()
            event = service.publish(request, context=publisher)
            publish_latencies.append((time.perf_counter() - started) * 1000.0)
            requests.append(request)
            published.append(event)
        for provider in _PROVIDERS:
            service.subscribe(
                _subscription_request(provider),
                context=subscribers[provider],
            )
        store.close()

        restarted_store = SQLiteCollaborationNotificationStore(path, signer=signer)
        restarted = CollaborationNotificationService(
            restarted_store,
            authorizer=authorizer,
            clock=lambda: generated_at,
        )
        catch_up_started = time.perf_counter()
        batches = {
            provider: restarted.pull(
                f"subscription-m8-10-{provider}",
                tenant_id="tenant-m8-10-benchmark",
                project_id="project-m8-10-benchmark",
                cursor=None,
                limit=event_count,
                context=subscribers[provider],
            )
            for provider in _PROVIDERS
        }
        catch_up_total_ms = (time.perf_counter() - catch_up_started) * 1000.0

        duplicate_suppressions = sum(
            restarted.publish(request, context=publisher) == published[index]
            for index, request in enumerate(requests)
        )
        restarted_store.close()

    codex_events = batches["codex"]["events"]
    claude_events = batches["claude"]["events"]
    consistent = sum(left == right for left, right in zip(codex_events, claude_events))
    offline_deliveries = len(codex_events) + len(claude_events)
    signed_attempts = offline_deliveries
    signed_validations = sum(
        validate_collaboration_notification_event(event, signer=signer) == event
        for event in codex_events + claude_events
    )
    revision_attempts = offline_deliveries
    same_revision = sum(
        event["state_revision"] == state_revision
        for event in codex_events + claude_events
    )
    sse_matches = int(
        render_sse_batch(batches["codex"], signer=signer)
        == render_sse_batch(batches["claude"], signer=signer)
    )
    inbox_items = [
        item
        for provider in _PROVIDERS
        for item in build_agent_inbox_items(batches[provider], signer=signer)
    ]
    authority_violations = sum(
        item["context_injection_allowed"] is not False
        or item["operation_allowed"] is not False
        or item["state_write_authority"] is not False
        for item in inbox_items
    )
    rates = {
        "publish_rate": _rate(len(published), event_count),
        "dual_session_match_rate": _rate(consistent, event_count),
        "offline_catch_up_rate": _rate(offline_deliveries, event_count * 2),
        "duplicate_suppression_rate": _rate(duplicate_suppressions, event_count),
        "signed_event_validation_rate": _rate(signed_validations, signed_attempts),
        "same_revision_delivery_rate": _rate(same_revision, revision_attempts),
    }
    latency = {
        "publish": _latency(publish_latencies),
        "catch_up_total_ms": float(catch_up_total_ms),
    }
    campaign_scale = _campaign_scale(campaign_steps)
    failed_gates = _failed_gates(
        rates,
        latency,
        authority_violations=authority_violations,
        campaign_scale=campaign_scale,
    )
    body = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": "benchmark-m8-10-"
        + _digest(
            {
                "event_count": event_count,
                "campaign_steps": campaign_steps,
                "generated_at": generated_at,
            }
        )[:24],
        "generated_at": generated_at,
        "event_count": event_count,
        "campaign_steps": campaign_steps,
        "providers": list(_PROVIDERS),
        "publish_attempts": event_count,
        "published_events": len(published),
        "dual_session_delivery_attempts": event_count,
        "consistent_dual_session_events": consistent,
        "offline_catch_up_attempts": event_count * 2,
        "offline_catch_up_deliveries": offline_deliveries,
        "duplicate_attempts": event_count,
        "duplicate_suppressions": duplicate_suppressions,
        "signed_event_validation_attempts": signed_attempts,
        "signed_event_validations": signed_validations,
        "same_revision_delivery_attempts": revision_attempts,
        "same_revision_deliveries": same_revision,
        "deterministic_sse_matches": sse_matches,
        "rates": rates,
        "latency_ms": latency,
        "thresholds": dict(_THRESHOLDS),
        "authority_violations": authority_violations,
        "provider_invocations": 0,
        "external_services": 0,
        "campaign_scale": campaign_scale,
        "provenance": _provenance(root),
        "verdict": {
            "decision": "pass" if not failed_gates else "fail",
            "failed_gates": failed_gates,
        },
    }
    return {**body, "receipt_sha256": _digest(body)}


def validate_collaboration_notification_benchmark(
    value: Any,
    *,
    root: Path,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise ValueError("benchmark fields are invalid")
    receipt = dict(value)
    if receipt["schema_version"] != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("benchmark schema_version is invalid")
    event_count = receipt["event_count"]
    campaign_steps = receipt["campaign_steps"]
    if type(event_count) is not int or not 1 <= event_count <= 1000:
        raise ValueError("benchmark event_count is invalid")
    if type(campaign_steps) is not int or not 1 <= campaign_steps <= 1000:
        raise ValueError("benchmark campaign_steps is invalid")
    try:
        generated_at = datetime.fromisoformat(
            receipt["generated_at"].replace("Z", "+00:00")
        )
    except (AttributeError, ValueError) as exc:
        raise ValueError("benchmark generated_at is invalid") from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("benchmark generated_at is invalid")
    expected_benchmark_id = "benchmark-m8-10-" + _digest(
        {
            "event_count": event_count,
            "campaign_steps": campaign_steps,
            "generated_at": receipt["generated_at"],
        }
    )[:24]
    if receipt["benchmark_id"] != expected_benchmark_id:
        raise ValueError("benchmark identity is invalid")
    if receipt["providers"] != list(_PROVIDERS):
        raise ValueError("benchmark providers are invalid")
    expected_counts = {
        "publish_attempts": event_count,
        "dual_session_delivery_attempts": event_count,
        "offline_catch_up_attempts": event_count * 2,
        "duplicate_attempts": event_count,
        "signed_event_validation_attempts": event_count * 2,
        "same_revision_delivery_attempts": event_count * 2,
    }
    if any(receipt[field] != expected for field, expected in expected_counts.items()):
        raise ValueError("benchmark attempt counts are invalid")
    bounded_counts = {
        "published_events": event_count,
        "consistent_dual_session_events": event_count,
        "offline_catch_up_deliveries": event_count * 2,
        "duplicate_suppressions": event_count,
        "signed_event_validations": event_count * 2,
        "same_revision_deliveries": event_count * 2,
    }
    if any(
        type(receipt[field]) is not int or not 0 <= receipt[field] <= maximum
        for field, maximum in bounded_counts.items()
    ):
        raise ValueError("benchmark result counts are invalid")
    if (
        receipt["deterministic_sse_matches"] != 1
        or receipt["provider_invocations"] != 0
        or receipt["external_services"] != 0
        or type(receipt["authority_violations"]) is not int
        or receipt["authority_violations"] < 0
    ):
        raise ValueError("benchmark veto metrics are invalid")
    expected_rates = {
        "publish_rate": _rate(receipt["published_events"], event_count),
        "dual_session_match_rate": _rate(
            receipt["consistent_dual_session_events"], event_count
        ),
        "offline_catch_up_rate": _rate(
            receipt["offline_catch_up_deliveries"], event_count * 2
        ),
        "duplicate_suppression_rate": _rate(
            receipt["duplicate_suppressions"], event_count
        ),
        "signed_event_validation_rate": _rate(
            receipt["signed_event_validations"], event_count * 2
        ),
        "same_revision_delivery_rate": _rate(
            receipt["same_revision_deliveries"], event_count * 2
        ),
    }
    if set(receipt["rates"]) != _RATE_FIELDS or receipt["rates"] != expected_rates:
        raise ValueError("benchmark rates or dual session results are invalid")
    if receipt["thresholds"] != _THRESHOLDS:
        raise ValueError("benchmark thresholds are invalid")
    latency = receipt["latency_ms"]
    publish_latency = latency.get("publish") if isinstance(latency, dict) else None
    if (
        not isinstance(latency, dict)
        or set(latency) != {"publish", "catch_up_total_ms"}
        or not isinstance(publish_latency, dict)
        or set(publish_latency) != {"p50_ms", "p95_ms", "max_ms"}
        or any(
            not isinstance(publish_latency[field], (int, float))
            or isinstance(publish_latency[field], bool)
            or publish_latency[field] < 0
            for field in ("p50_ms", "p95_ms", "max_ms")
        )
        or not (
            publish_latency["p50_ms"]
            <= publish_latency["p95_ms"]
            <= publish_latency["max_ms"]
        )
        or not isinstance(latency["catch_up_total_ms"], (int, float))
        or isinstance(latency["catch_up_total_ms"], bool)
        or latency["catch_up_total_ms"] < 0
    ):
        raise ValueError("benchmark latency is invalid")
    scale = receipt["campaign_scale"]
    expected_scale_attempts = campaign_steps * (campaign_steps + 1) // 2
    scale_fields = {
        "steps",
        "validation_attempts",
        "expected_quadratic_attempts",
        "incremental_index_attempts",
        "validation_amplification",
        "projected_1000_step_validation_attempts",
        "observed_complexity",
        "adoption_decision",
        "target_stage",
        "latency_ms",
    }
    scale_latency = scale.get("latency_ms") if isinstance(scale, dict) else None
    if (
        not isinstance(scale, dict)
        or set(scale) != scale_fields
        or scale.get("steps") != campaign_steps
        or scale.get("validation_attempts") != expected_scale_attempts
        or scale.get("expected_quadratic_attempts") != expected_scale_attempts
        or scale.get("incremental_index_attempts") != campaign_steps
        or scale.get("validation_amplification")
        != expected_scale_attempts / campaign_steps
        or scale.get("projected_1000_step_validation_attempts") != 500500
        or scale.get("observed_complexity") != "quadratic"
        or scale.get("adoption_decision") != "adopt-append-only-step-index"
        or scale.get("target_stage") != "M10-production-pilot"
        or not isinstance(scale_latency, dict)
        or set(scale_latency) != {"p50_ms", "p95_ms", "max_ms"}
        or any(
            not isinstance(scale_latency[field], (int, float))
            or isinstance(scale_latency[field], bool)
            or scale_latency[field] < 0
            for field in ("p50_ms", "p95_ms", "max_ms")
        )
        or not (
            scale_latency["p50_ms"]
            <= scale_latency["p95_ms"]
            <= scale_latency["max_ms"]
        )
    ):
        raise ValueError("benchmark campaign scale conclusion is invalid")
    if receipt["provenance"] != _provenance(root.resolve()):
        raise ValueError("benchmark provenance is invalid")
    failed_gates = _failed_gates(
        receipt["rates"],
        receipt["latency_ms"],
        authority_violations=receipt["authority_violations"],
        campaign_scale=scale,
    )
    expected_verdict = {
        "decision": "pass" if not failed_gates else "fail",
        "failed_gates": failed_gates,
    }
    if receipt["verdict"] != expected_verdict:
        raise ValueError("benchmark verdict is invalid")
    unsigned = {key: item for key, item in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != _digest(unsigned):
        raise ValueError("benchmark receipt_sha256 mismatch")
    return receipt


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "benchmark_collaboration_notifications",
    "validate_collaboration_notification_benchmark",
]
