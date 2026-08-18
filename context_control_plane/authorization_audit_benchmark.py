"""M8-05 authorization, isolation, and audit failure benchmark."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .authorization_audit import (
    AUTHORIZATION_POLICY_SCHEMA_VERSION,
    AuthorizationAuditError,
    InMemoryAuthorizationAuditStore,
    TenantProjectAuthorizer,
    authorization_policy_sha256,
    replay_authorization_audit_events,
)
from .state_mcp import RequestContext

SCHEMA_VERSION = "context.authorization-isolation-benchmark/v1alpha1"
CASE_IDS = (
    "unknown-credential",
    "subject-mismatch",
    "action-not-granted",
    "same-tenant-project",
    "cross-tenant-project",
    "unknown-project",
    "expired-grant",
    "revoked-grant",
    "disabled-project",
    "policy-not-active",
)
_METRIC_FIELDS = {
    "unauthorized_accepts",
    "valid_authorization_denials",
    "cross_tenant_accepts",
    "same_tenant_project_accepts",
    "unknown_project_accepts",
    "stale_grant_accepts",
    "disabled_project_accepts",
    "inactive_policy_accepts",
    "missing_audit_events",
    "audit_chain_failures",
    "tenant_chain_collisions",
    "audit_failure_accepts",
    "policy_digest_mismatches",
}
_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "samples",
    "case_ids",
    "allowed_attempts",
    "allowed_decisions",
    "unauthorized_attempts",
    "unauthorized_rejections",
    "audit_failure_attempts",
    "audit_failure_rejections",
    "audit_event_count",
    "metrics",
    "latency_ms",
    "thresholds",
    "provider_invocations",
    "external_services",
    "shared_authority_claim",
    "provenance",
    "verdict",
    "receipt_sha256",
}
_PROVENANCE_PATHS = {
    "authorization_core_sha256": "context_control_plane/authorization_audit.py",
    "implementation_sha256": (
        "context_control_plane/authorization_audit_benchmark.py"
    ),
    "policy_schema_sha256": "schemas/m8-05/authorization-policy.schema.json",
    "audit_event_schema_sha256": (
        "schemas/m8-05/authorization-audit-event.schema.json"
    ),
    "benchmark_schema_sha256": (
        "schemas/m8-05/authorization-isolation-benchmark.schema.json"
    ),
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AuthorizationAuditBenchmarkError(RuntimeError):
    """Raised when an M8-05 benchmark receipt cannot be trusted."""


class _FailingAuditStore:
    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        raise OSError("audit storage unavailable")


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
        raise AuthorizationAuditBenchmarkError(
            "benchmark data is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _timestamp(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise AuthorizationAuditBenchmarkError("generated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationAuditBenchmarkError("generated_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorizationAuditBenchmarkError("generated_at requires an offset")
    return value


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[index], 6)


def _policy() -> dict[str, Any]:
    return {
        "schema_version": AUTHORIZATION_POLICY_SCHEMA_VERSION,
        "policy_id": "policy-m8-05-benchmark",
        "policy_revision": 1,
        "issued_at": "2026-08-16T10:00:00+00:00",
        "expires_at": None,
        "projects": [
            {"tenant_id": "tenant-a", "project_id": "project-a", "status": "active"},
            {
                "tenant_id": "tenant-a",
                "project_id": "project-a-secondary",
                "status": "active",
            },
            {
                "tenant_id": "tenant-a",
                "project_id": "project-a-disabled",
                "status": "disabled",
            },
            {"tenant_id": "tenant-b", "project_id": "project-b", "status": "active"},
        ],
        "grants": [
            {
                "grant_id": "grant-valid",
                "authorization_ref": "authorization://tenant-a/valid",
                "subject_ref": "actor-valid",
                "tenant_id": "tenant-a",
                "project_ids": ["project-a"],
                "actions": ["state.read"],
                "not_before": "2026-08-16T10:00:00+00:00",
                "expires_at": None,
                "status": "active",
            },
            {
                "grant_id": "grant-expired",
                "authorization_ref": "authorization://tenant-a/expired",
                "subject_ref": "actor-expired",
                "tenant_id": "tenant-a",
                "project_ids": ["project-a"],
                "actions": ["state.read"],
                "not_before": "2026-08-16T10:00:00+00:00",
                "expires_at": "2026-08-16T11:00:00+00:00",
                "status": "active",
            },
            {
                "grant_id": "grant-revoked",
                "authorization_ref": "authorization://tenant-a/revoked",
                "subject_ref": "actor-revoked",
                "tenant_id": "tenant-a",
                "project_ids": ["project-a"],
                "actions": ["state.read"],
                "not_before": "2026-08-16T10:00:00+00:00",
                "expires_at": None,
                "status": "revoked",
            },
        ],
    }


def _cases() -> list[tuple[str, RequestContext, str, str]]:
    valid_ref = "authorization://tenant-a/valid"
    return [
        (
            "unknown-credential",
            RequestContext("actor-valid", "authorization://unknown/grant"),
            "state.read",
            "project-a",
        ),
        (
            "subject-mismatch",
            RequestContext("actor-other", valid_ref),
            "state.read",
            "project-a",
        ),
        (
            "action-not-granted",
            RequestContext("actor-valid", valid_ref),
            "state.effect.dispatch",
            "project-a",
        ),
        (
            "same-tenant-project",
            RequestContext("actor-valid", valid_ref),
            "state.read",
            "project-a-secondary",
        ),
        (
            "cross-tenant-project",
            RequestContext("actor-valid", valid_ref),
            "state.read",
            "project-b",
        ),
        (
            "unknown-project",
            RequestContext("actor-valid", valid_ref),
            "state.read",
            "project-unknown",
        ),
        (
            "expired-grant",
            RequestContext("actor-expired", "authorization://tenant-a/expired"),
            "state.read",
            "project-a",
        ),
        (
            "revoked-grant",
            RequestContext("actor-revoked", "authorization://tenant-a/revoked"),
            "state.read",
            "project-a",
        ),
        (
            "disabled-project",
            RequestContext("actor-valid", valid_ref),
            "state.read",
            "project-a-disabled",
        ),
    ]


def _registry_sha256(path: Path) -> str:
    registry = yaml.safe_load(path.read_text(encoding="utf-8"))
    selected = [
        item
        for item in registry["schemas"]
        if item["schema_id"]
        in {
            "context.authorization-policy",
            "context.authorization-audit-event",
            "context.authorization-isolation-benchmark",
        }
    ]
    return _digest(selected)


def _provenance(root: Path) -> dict[str, str]:
    result = {
        field: _file_sha256(root / relative)
        for field, relative in _PROVENANCE_PATHS.items()
    }
    result["registry_sha256"] = _registry_sha256(root / "schemas/registry.yaml")
    return result


def benchmark_authorization_isolation(
    *,
    root: Path,
    samples: int,
    generated_at: str,
) -> dict[str, Any]:
    """Run exact-grant and fail-closed audit cases without external services."""
    root = Path(root).resolve()
    if type(samples) is not int or samples <= 0 or samples > 100_000:
        raise AuthorizationAuditBenchmarkError("samples is invalid")
    _timestamp(generated_at)
    policy = _policy()
    policy_sha256 = authorization_policy_sha256(policy)
    metrics = {field: 0 for field in _METRIC_FIELDS}
    allowed_decisions = 0
    unauthorized_rejections = 0
    audit_failure_rejections = 0
    audit_event_count = 0
    latencies: list[float] = []
    case_category = {
        "same-tenant-project": "same_tenant_project_accepts",
        "cross-tenant-project": "cross_tenant_accepts",
        "unknown-project": "unknown_project_accepts",
        "expired-grant": "stale_grant_accepts",
        "revoked-grant": "stale_grant_accepts",
        "disabled-project": "disabled_project_accepts",
    }

    for _ in range(samples):
        started = time.perf_counter_ns()
        store = InMemoryAuthorizationAuditStore()
        authorizer = TenantProjectAuthorizer(
            policy,
            expected_policy_sha256=policy_sha256,
            audit_store=store,
            clock=lambda: "2026-08-16T12:00:00+00:00",
        )
        valid = authorizer.authorize(
            RequestContext("actor-valid", "authorization://tenant-a/valid"),
            "state.read",
            "project-a",
        )
        allowed_decisions += int(valid)
        metrics["valid_authorization_denials"] += int(not valid)
        for case_id, context, action, project_id in _cases():
            allowed = authorizer.authorize(context, action, project_id)
            metrics["unauthorized_accepts"] += int(allowed)
            unauthorized_rejections += int(not allowed)
            category = case_category.get(case_id)
            if category is not None:
                metrics[category] += int(allowed)

        early_store = InMemoryAuthorizationAuditStore()
        early_authorizer = TenantProjectAuthorizer(
            policy,
            expected_policy_sha256=policy_sha256,
            audit_store=early_store,
            clock=lambda: "2026-08-16T09:59:59+00:00",
        )
        early_allowed = early_authorizer.authorize(
            RequestContext("actor-valid", "authorization://tenant-a/valid"),
            "state.read",
            "project-a",
        )
        metrics["unauthorized_accepts"] += int(early_allowed)
        metrics["inactive_policy_accepts"] += int(early_allowed)
        unauthorized_rejections += int(not early_allowed)

        events = store.read_events()
        early_events = early_store.read_events()
        audit_event_count += len(events) + len(early_events)
        metrics["missing_audit_events"] += abs(
            len(CASE_IDS) + 1 - len(events) - len(early_events)
        )
        for event_set in (events, early_events):
            streams: dict[str, list[dict[str, Any]]] = {}
            for event in event_set:
                streams.setdefault(event["tenant_id"], []).append(event)
            for tenant_id, stream in streams.items():
                try:
                    replay = replay_authorization_audit_events(stream)
                except AuthorizationAuditError:
                    metrics["audit_chain_failures"] += 1
                else:
                    metrics["tenant_chain_collisions"] += int(
                        replay["tenant_id"] != tenant_id
                        or stream[0]["sequence_no"] != 1
                        or stream[0]["previous_event_sha256"] is not None
                    )

        failing = TenantProjectAuthorizer(
            policy,
            expected_policy_sha256=policy_sha256,
            audit_store=_FailingAuditStore(),
            clock=lambda: "2026-08-16T12:00:00+00:00",
        )
        audit_failure_allowed = failing.authorize(
            RequestContext("actor-valid", "authorization://tenant-a/valid"),
            "state.read",
            "project-a",
        )
        metrics["audit_failure_accepts"] += int(audit_failure_allowed)
        audit_failure_rejections += int(not audit_failure_allowed)
        try:
            TenantProjectAuthorizer(
                policy,
                expected_policy_sha256="0" * 64,
                audit_store=InMemoryAuthorizationAuditStore(),
                clock=lambda: "2026-08-16T12:00:00+00:00",
            )
        except AuthorizationAuditError:
            pass
        else:
            metrics["policy_digest_mismatches"] += 1
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)

    unauthorized_attempts = samples * len(CASE_IDS)
    thresholds = {
        "unauthorized_rejection_rate_min": 1.0,
        "allowed_decision_rate_min": 1.0,
        "audit_failure_rejection_rate_min": 1.0,
        "zero_veto_metrics_required": True,
        "latency_p95_ms_max": 25.0,
    }
    latency = {
        "p50": _percentile(latencies, 0.50),
        "p95": _percentile(latencies, 0.95),
        "max": round(max(latencies), 6),
    }
    failed_gates = [field for field, value in metrics.items() if value != 0]
    if latency["p95"] >= thresholds["latency_p95_ms_max"]:
        failed_gates.append("latency_p95_ms")
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": "benchmark-m8-05-"
        + _digest(
            {
                "samples": samples,
                "cases": list(CASE_IDS),
                "policy_sha256": policy_sha256,
            }
        )[:24],
        "generated_at": generated_at,
        "samples": samples,
        "case_ids": list(CASE_IDS),
        "allowed_attempts": samples,
        "allowed_decisions": allowed_decisions,
        "unauthorized_attempts": unauthorized_attempts,
        "unauthorized_rejections": unauthorized_rejections,
        "audit_failure_attempts": samples,
        "audit_failure_rejections": audit_failure_rejections,
        "audit_event_count": audit_event_count,
        "metrics": metrics,
        "latency_ms": latency,
        "thresholds": thresholds,
        "provider_invocations": 0,
        "external_services": 0,
        "shared_authority_claim": False,
        "provenance": _provenance(root),
        "verdict": {
            "decision": "pass" if not failed_gates else "fail",
            "allowed_decision_rate": allowed_decisions / samples,
            "unauthorized_rejection_rate": (
                unauthorized_rejections / unauthorized_attempts
            ),
            "audit_failure_rejection_rate": audit_failure_rejections / samples,
            "latency_p95_within_threshold": (
                latency["p95"] < thresholds["latency_p95_ms_max"]
            ),
            "failed_gates": failed_gates,
        },
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    validate_authorization_isolation_benchmark(receipt, root=root)
    return receipt


def validate_authorization_isolation_benchmark(
    receipt: Any,
    *,
    root: Path,
) -> dict[str, Any]:
    """Validate M8-05 provenance, counts, latency, and all veto metrics."""
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS:
        raise AuthorizationAuditBenchmarkError("benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise AuthorizationAuditBenchmarkError("benchmark receipt version is invalid")
    _timestamp(receipt["generated_at"])
    samples = receipt["samples"]
    if type(samples) is not int or samples <= 0:
        raise AuthorizationAuditBenchmarkError("benchmark samples are invalid")
    if receipt["case_ids"] != list(CASE_IDS):
        raise AuthorizationAuditBenchmarkError("benchmark case matrix is invalid")
    expected_counts = {
        "allowed_attempts": samples,
        "allowed_decisions": samples,
        "unauthorized_attempts": samples * len(CASE_IDS),
        "unauthorized_rejections": samples * len(CASE_IDS),
        "audit_failure_attempts": samples,
        "audit_failure_rejections": samples,
        "audit_event_count": samples * (len(CASE_IDS) + 1),
    }
    if any(receipt[field] != value for field, value in expected_counts.items()):
        raise AuthorizationAuditBenchmarkError("benchmark decision counts are invalid")
    metrics = receipt["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != _METRIC_FIELDS:
        raise AuthorizationAuditBenchmarkError("benchmark metrics are invalid")
    if any(type(value) is not int or value != 0 for value in metrics.values()):
        raise AuthorizationAuditBenchmarkError("benchmark veto metric is non-zero")
    latency = receipt["latency_ms"]
    if (
        not isinstance(latency, dict)
        or set(latency) != {"p50", "p95", "max"}
        or any(
            type(value) not in {int, float} or value <= 0
            for value in latency.values()
        )
        or not latency["p50"] <= latency["p95"] <= latency["max"]
    ):
        raise AuthorizationAuditBenchmarkError("benchmark latency is invalid")
    expected_thresholds = {
        "unauthorized_rejection_rate_min": 1.0,
        "allowed_decision_rate_min": 1.0,
        "audit_failure_rejection_rate_min": 1.0,
        "zero_veto_metrics_required": True,
        "latency_p95_ms_max": 25.0,
    }
    if receipt["thresholds"] != expected_thresholds:
        raise AuthorizationAuditBenchmarkError("benchmark thresholds are invalid")
    if latency["p95"] >= expected_thresholds["latency_p95_ms_max"]:
        raise AuthorizationAuditBenchmarkError("benchmark latency gate failed")
    if (
        receipt["provider_invocations"] != 0
        or receipt["external_services"] != 0
        or receipt["shared_authority_claim"] is not False
    ):
        raise AuthorizationAuditBenchmarkError("benchmark authority boundary is invalid")
    expected_provenance = _provenance(Path(root).resolve())
    if receipt["provenance"] != expected_provenance:
        raise AuthorizationAuditBenchmarkError("benchmark provenance mismatch")
    expected_verdict = {
        "decision": "pass",
        "allowed_decision_rate": 1.0,
        "unauthorized_rejection_rate": 1.0,
        "audit_failure_rejection_rate": 1.0,
        "latency_p95_within_threshold": True,
        "failed_gates": [],
    }
    if receipt["verdict"] != expected_verdict:
        raise AuthorizationAuditBenchmarkError("benchmark verdict is invalid")
    digest = receipt["receipt_sha256"]
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise AuthorizationAuditBenchmarkError("benchmark receipt digest is invalid")
    if digest != _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    ):
        raise AuthorizationAuditBenchmarkError("benchmark receipt digest mismatch")
    return copy.deepcopy(receipt)
