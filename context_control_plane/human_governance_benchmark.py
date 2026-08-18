"""Measured local acceptance for the M9-05 human governance facade."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from .authorization_audit import GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
from .human_governance import HUMAN_GOVERNANCE_ACTIONS, HumanGovernanceFacade
from .state_events import IDEA_EVENT_SCHEMA_VERSION_V2, build_state_event
from .state_mcp import RequestContext

BENCHMARK_SCHEMA_VERSION = "context.human-governance-benchmark/v1alpha1"
BENCHMARK_ID = "m9-05-human-governance"
_ACTIONS = tuple(sorted(HUMAN_GOVERNANCE_ACTIONS))
_THRESHOLDS = {
    "four_action_success_rate_min": 1.0,
    "generic_bypass_rejection_rate_min": 1.0,
    "session_denial_rate_min": 1.0,
    "duplicate_replay_rate_min": 1.0,
    "duplicate_drift_conflict_rate_min": 1.0,
    "event_tamper_rejection_rate_min": 1.0,
    "audit_binding_rejection_rate_min": 1.0,
    "project_scope_rate_min": 1.0,
    "invalid_input_rejection_rate_min": 1.0,
    "denied_state_calls_max": 0,
    "authority_violations_max": 0,
    "provider_invocations_max": 0,
    "external_services_max": 0,
    "latency_p95_ms_max": 50.0,
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "parameters",
    "thresholds",
    "results",
    "latency_ms",
    "latency_samples_ms",
    "attestation",
    "gate",
    "provenance",
    "receipt_sha256",
}
_RESULT_FIELDS = {
    "four_action_successes",
    "four_action_success_rate",
    "generic_bypass_rejections",
    "generic_bypass_rejection_rate",
    "session_denials",
    "session_denial_rate",
    "duplicate_replays",
    "duplicate_replay_rate",
    "duplicate_drift_conflicts",
    "duplicate_drift_conflict_rate",
    "event_tamper_rejections",
    "event_tamper_rejection_rate",
    "audit_binding_rejections",
    "audit_binding_rejection_rate",
    "project_scope_matches",
    "project_scope_rate",
    "invalid_input_rejections",
    "invalid_input_rejection_rate",
    "denied_state_calls",
    "authority_violations",
    "provider_invocations",
    "external_services",
}
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)
_AUTHORIZATION_ACTIONS = {
    action: f"state.{action.removeprefix('context.')}"
    for action in HUMAN_GOVERNANCE_ACTIONS
}


class HumanGovernanceBenchmarkError(ValueError):
    """Raised when an M9-05 benchmark receipt cannot be accepted."""


class _SessionResolver:
    def resolve(
        self, session_id: str, project_id: str, action: str
    ) -> RequestContext | None:
        if session_id != "session-valid":
            return None
        return RequestContext("actor-human", "authorization-human")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _state_request_sha256(
    tool: str, arguments: dict[str, Any], context: RequestContext
) -> str:
    return _digest(
        {
            "tool": tool,
            "arguments": arguments,
            "subject_ref": context.subject_ref,
            "authorization_ref": context.authorization_ref,
        }
    )


def _authorization_audit_event(
    *,
    tool: str,
    arguments: dict[str, Any],
    context: RequestContext,
    request_sha256: str,
) -> dict[str, Any]:
    event = {
        "schema_version": GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION,
        "event_type": "authorization_decision",
        "event_id": f"authorization-event-{arguments['request_id']}",
        "sequence_no": 1,
        "policy_id": "policy-benchmark",
        "policy_revision": 1,
        "policy_sha256": "a" * 64,
        "authorization_ref": context.authorization_ref,
        "subject_ref": context.subject_ref,
        "subject_tenant_id": "tenant-benchmark",
        "tenant_id": "tenant-benchmark",
        "project_id": arguments["project_id"],
        "action": _AUTHORIZATION_ACTIONS[tool],
        "decision": "allow",
        "reason_code": "grant_matched",
        "observed_at": "2026-08-17T09:00:00+08:00",
        "request_id": arguments["request_id"],
        "request_sha256": request_sha256,
        "previous_event_sha256": None,
    }
    event["event_sha256"] = _digest(event)
    return event


class _StateProbe:
    def __init__(
        self,
        *,
        wrong_binding: bool = False,
        wrong_audit_binding: bool = False,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any], RequestContext]] = []
        self.wrong_binding = wrong_binding
        self.wrong_audit_binding = wrong_audit_binding
        self._audit_events: dict[tuple[str, str, str], dict[str, Any]] = {}

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        self.calls.append((tool, copy.deepcopy(arguments), context))
        revision = arguments["expected_revision"] + 1
        request_sha256 = _state_request_sha256(tool, arguments, context)
        self._audit_events[(tool, arguments["request_id"], request_sha256)] = (
            _authorization_audit_event(
                tool=tool,
                arguments=arguments,
                context=context,
                request_sha256=request_sha256,
            )
        )
        if tool.startswith("context.experiment.promotion"):
            record_id = (
                arguments["proposal_id"]
                if tool.endswith("propose")
                else arguments["approval_id"]
            )
            if self.wrong_binding:
                record_id = "record-forged"
            event = build_state_event(
                event_id=f"event-{arguments['request_id']}",
                event_type="state-transition",
                project_id=arguments["project_id"],
                sequence_no=revision,
                revision_before=arguments["expected_revision"],
                occurred_at="2026-08-17T09:00:00+08:00",
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=None,
                supersedes_event_id=None,
                changes=[
                    {
                        "collection": "experiment_promotions",
                        "object_id": record_id,
                        "value": {"promotion_id": record_id},
                    }
                ],
                project_after={
                    "project_id": arguments["project_id"],
                    "revision": revision,
                },
                experiment_transition={
                    "operation": (
                        "promotion-proposed"
                        if tool.endswith("propose")
                        else "promotion-approved"
                    ),
                    "request_sha256": request_sha256,
                    "attempt_id": None,
                    "promotion_id": record_id,
                    "proposal_id": (
                        record_id
                        if tool.endswith("propose")
                        else arguments["proposal_id"]
                    ),
                },
            )
        else:
            record_id = arguments["protection_id"]
            if self.wrong_binding:
                record_id = "protection-forged"
            event = build_state_event(
                event_id=f"event-{arguments['request_id']}",
                event_type="state-transition",
                project_id=arguments["project_id"],
                sequence_no=revision,
                revision_before=arguments["expected_revision"],
                occurred_at="2026-08-17T09:00:00+08:00",
                actor_ref=context.subject_ref,
                causation_ref=arguments["causation_ref"],
                correlation_ref=arguments["correlation_ref"],
                previous_event_sha256=None,
                supersedes_event_id=None,
                changes=[
                    {
                        "collection": "correction_protections",
                        "object_id": record_id,
                        "value": {"protection_id": record_id},
                    }
                ],
                project_after={
                    "project_id": arguments["project_id"],
                    "revision": revision,
                },
                idea_transition={
                    "operation": (
                        "correction-guarded"
                        if tool.endswith("protect")
                        else "correction-released"
                    ),
                    "request_sha256": request_sha256,
                    "canonical_idea_id": arguments["idea_id"],
                    "submitted_idea_id": None,
                    "occurrence_id": None,
                    "review_id": None,
                    "protection_id": record_id,
                },
                schema_version=IDEA_EVENT_SCHEMA_VERSION_V2,
            )
        return {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": arguments["request_id"],
            "tool": tool,
            "ok": True,
            "result": {
                "revision": revision,
                "event_head": {
                    "sequence_no": event["sequence_no"],
                    "event_sha256": event["event_sha256"],
                },
                "event": event,
            },
            "error": None,
        }

    def authorization_receipt(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any] | None:
        request_sha256 = _state_request_sha256(tool, arguments, context)
        event = self._audit_events.get(
            (tool, arguments["request_id"], request_sha256)
        )
        if event is None:
            return None
        receipt = copy.deepcopy(event)
        if self.wrong_audit_binding:
            receipt["request_id"] = "request-forged"
            receipt["event_sha256"] = _digest(
                {
                    key: value
                    for key, value in receipt.items()
                    if key != "event_sha256"
                }
            )
        return receipt


def _payload(action: str, suffix: str) -> dict[str, Any]:
    common = {"causation_ref": "work:M9-05", "correlation_ref": "campaign:M9"}
    if action.endswith("propose"):
        return {
            **common,
            "work_id": "experiment-one",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "attempt_id": f"attempt-{suffix}",
            "proposal_id": f"proposal-{suffix}",
            "criterion_evidence": {"criterion-one": ["evidence-one"]},
        }
    if action.endswith("approve"):
        return {
            **common,
            "work_id": "experiment-one",
            "expected_work_revision": 3,
            "expected_target_work_revision": 2,
            "proposal_id": f"proposal-{suffix}",
            "approval_id": f"approval-{suffix}",
        }
    if action.endswith("protect"):
        return {
            **common,
            "idea_id": "idea-one",
            "protection_id": f"protection-{suffix}",
            "affected_work_ids": ["work-one"],
            "affected_scope_refs": ["file:src/main.py"],
            "reason": "Current evidence requires review.",
            "evidence_ids": ["evidence-one"],
        }
    return {
        **common,
        "idea_id": "idea-one",
        "protection_id": f"protection-{suffix}",
        "release_reason": "Verified evidence resolves the correction.",
        "release_evidence_ids": ["evidence-one"],
    }


def _request(
    action: str,
    suffix: str,
    *,
    request_id: str | None = None,
    project_id: str = "project-one",
) -> dict[str, Any]:
    return {
        "schema_version": "context.human-governance-request/v1alpha1",
        "request_id": request_id or f"request-{suffix}",
        "project_id": project_id,
        "expected_revision": 9,
        "action": action,
        "payload": _payload(action, suffix),
    }


def _latency(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    percentile = lambda value: ordered[
        max(0, math.ceil(len(ordered) * value) - 1)
    ]
    return {
        "min": ordered[0],
        "p50": percentile(0.50),
        "p95": percentile(0.95),
        "max": ordered[-1],
    }


def _provenance(root: Path) -> dict[str, str]:
    return {
        "implementation_sha256": _file_digest(
            root / "context_control_plane/human_governance.py"
        ),
        "benchmark_sha256": _file_digest(
            root / "context_control_plane/human_governance_benchmark.py"
        ),
        "request_schema_sha256": _file_digest(
            root / "schemas/m9-05/human-governance-request.schema.json"
        ),
        "response_schema_sha256": _file_digest(
            root / "schemas/m9-05/human-governance-response.schema.json"
        ),
        "benchmark_schema_sha256": _file_digest(
            root / "schemas/m9-05/human-governance-benchmark.schema.json"
        ),
        "state_mcp_sha256": _file_digest(root / "context_control_plane/state_mcp.py"),
        "state_event_sha256": _file_digest(
            root / "context_control_plane/state_events.py"
        ),
        "authorization_audit_sha256": _file_digest(
            root / "context_control_plane/authorization_audit.py"
        ),
        "authorization_audit_schema_sha256": _file_digest(
            root
            / "schemas/m9-05/governance-authorization-audit-event.schema.json"
        ),
    }


def _failed_gates(results: dict[str, int | float], latency: dict[str, float]) -> list[str]:
    checks = {
        "four-action": results["four_action_success_rate"]
        >= _THRESHOLDS["four_action_success_rate_min"],
        "generic-bypass": results["generic_bypass_rejection_rate"]
        >= _THRESHOLDS["generic_bypass_rejection_rate_min"],
        "session-denial": results["session_denial_rate"]
        >= _THRESHOLDS["session_denial_rate_min"],
        "duplicate-replay": results["duplicate_replay_rate"]
        >= _THRESHOLDS["duplicate_replay_rate_min"],
        "duplicate-drift": results["duplicate_drift_conflict_rate"]
        >= _THRESHOLDS["duplicate_drift_conflict_rate_min"],
        "event-tamper": results["event_tamper_rejection_rate"]
        >= _THRESHOLDS["event_tamper_rejection_rate_min"],
        "audit-binding": results["audit_binding_rejection_rate"]
        >= _THRESHOLDS["audit_binding_rejection_rate_min"],
        "project-scope": results["project_scope_rate"]
        >= _THRESHOLDS["project_scope_rate_min"],
        "invalid-input": results["invalid_input_rejection_rate"]
        >= _THRESHOLDS["invalid_input_rejection_rate_min"],
        "denied-state-call": results["denied_state_calls"]
        <= _THRESHOLDS["denied_state_calls_max"],
        "authority": results["authority_violations"]
        <= _THRESHOLDS["authority_violations_max"],
        "provider": results["provider_invocations"]
        <= _THRESHOLDS["provider_invocations_max"],
        "external": results["external_services"]
        <= _THRESHOLDS["external_services_max"],
        "latency": latency["p95"] < _THRESHOLDS["latency_p95_ms_max"],
    }
    return [name for name, passed in checks.items() if not passed]


def benchmark_human_governance(
    *,
    root: Path,
    iterations: int = 1000,
    generated_at: str = "2026-08-17T09:00:00+08:00",
) -> dict[str, Any]:
    """Measure bounded governance mapping and fail-closed behavior."""
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise ValueError("iterations must be an integer from 1 through 1000")
    if not isinstance(generated_at, str) or _TIMESTAMP_RE.fullmatch(generated_at) is None:
        raise ValueError("generated_at must be an offset timestamp")
    counters = {field: 0 for field in _RESULT_FIELDS if not field.endswith("_rate")}
    samples: list[float] = []
    external_calls = 0

    def blocked_external(*args: Any, **kwargs: Any) -> None:
        nonlocal external_calls
        external_calls += 1
        raise AssertionError("benchmark attempted an external service")

    with (
        patch.object(socket.socket, "connect", blocked_external),
        patch.object(socket, "create_connection", blocked_external),
        patch.object(subprocess, "Popen", blocked_external),
    ):
        for index in range(iterations):
            started = time.perf_counter_ns()
            suffix = f"{index:04d}"
            state = _StateProbe()
            facade = HumanGovernanceFacade(state, session_resolver=_SessionResolver())
            successful = [
                facade.submit(
                    _request(action, f"{suffix}-{action.rsplit('.', 1)[-1]}"),
                    session_id="session-valid",
                )
                for action in _ACTIONS
            ]
            counters["four_action_successes"] += sum(
                response["ok"] for response in successful
            )
            counters["authority_violations"] += sum(
                any(response["authority"].values()) for response in successful
            )

            generic = _request(_ACTIONS[0], f"{suffix}-generic")
            generic["action"] = "context.state.commit"
            generic["payload"] = {"changes": []}
            state_calls_before = len(state.calls)
            generic_response = facade.submit(generic, session_id="session-valid")
            counters["generic_bypass_rejections"] += (
                not generic_response["ok"]
                and generic_response["error"]["code"] == "invalid_request"
            )
            counters["denied_state_calls"] += len(state.calls) - state_calls_before

            denied_request = _request(_ACTIONS[0], f"{suffix}-denied")
            state_calls_before = len(state.calls)
            denied = facade.submit(denied_request, session_id="session-unknown")
            counters["session_denials"] += (
                not denied["ok"] and denied["error"]["code"] == "permission_denied"
            )
            counters["denied_state_calls"] += len(state.calls) - state_calls_before

            duplicate_request = _request(_ACTIONS[0], f"{suffix}-duplicate")
            first = facade.submit(duplicate_request, session_id="session-valid")
            calls_after_first = len(state.calls)
            replay = facade.submit(duplicate_request, session_id="session-valid")
            counters["duplicate_replays"] += (
                replay == first and len(state.calls) == calls_after_first
            )
            drifted = copy.deepcopy(duplicate_request)
            drifted["payload"]["proposal_id"] = f"proposal-{suffix}-drifted"
            drift = facade.submit(drifted, session_id="session-valid")
            counters["duplicate_drift_conflicts"] += (
                not drift["ok"] and drift["error"]["code"] == "conflict"
            )

            tamper = HumanGovernanceFacade(
                _StateProbe(wrong_binding=True), session_resolver=_SessionResolver()
            ).submit(
                _request(_ACTIONS[0], f"{suffix}-tamper"),
                session_id="session-valid",
            )
            counters["event_tamper_rejections"] += (
                not tamper["ok"] and tamper["error"]["code"] == "unavailable"
            )

            audit_tamper = HumanGovernanceFacade(
                _StateProbe(wrong_audit_binding=True),
                session_resolver=_SessionResolver(),
            ).submit(
                _request(_ACTIONS[0], f"{suffix}-audit-tamper"),
                session_id="session-valid",
            )
            counters["audit_binding_rejections"] += (
                not audit_tamper["ok"]
                and audit_tamper["error"]["code"] == "unavailable"
            )

            scoped_state = _StateProbe()
            scoped = HumanGovernanceFacade(
                scoped_state, session_resolver=_SessionResolver()
            )
            scoped_request = _request(
                _ACTIONS[0], f"{suffix}-scoped", request_id=f"scoped-{suffix}"
            )
            scoped_first = scoped.submit(scoped_request, session_id="session-valid")
            scoped_second_request = copy.deepcopy(scoped_request)
            scoped_second_request["project_id"] = "project-two"
            scoped_second = scoped.submit(
                scoped_second_request, session_id="session-valid"
            )
            counters["project_scope_matches"] += (
                scoped_first["ok"]
                and scoped_second["ok"]
                and len(scoped_state.calls) == 2
            )

            invalid = _request(_ACTIONS[-1], f"{suffix}-invalid")
            invalid["payload"]["release_evidence_ids"] = [[]]
            invalid_response = facade.submit(invalid, session_id="session-valid")
            counters["invalid_input_rejections"] += (
                not invalid_response["ok"]
                and invalid_response["error"]["code"] == "invalid_request"
            )
            samples.append((time.perf_counter_ns() - started) / 1_000_000)

    counters["external_services"] = external_calls
    counters["provider_invocations"] = 0
    rate_inputs = {
        "four_action_success_rate": (
            "four_action_successes",
            iterations * len(_ACTIONS),
        ),
        "generic_bypass_rejection_rate": (
            "generic_bypass_rejections",
            iterations,
        ),
        "session_denial_rate": ("session_denials", iterations),
        "duplicate_replay_rate": ("duplicate_replays", iterations),
        "duplicate_drift_conflict_rate": (
            "duplicate_drift_conflicts",
            iterations,
        ),
        "event_tamper_rejection_rate": (
            "event_tamper_rejections",
            iterations,
        ),
        "audit_binding_rejection_rate": (
            "audit_binding_rejections",
            iterations,
        ),
        "project_scope_rate": ("project_scope_matches", iterations),
        "invalid_input_rejection_rate": (
            "invalid_input_rejections",
            iterations,
        ),
    }
    results: dict[str, int | float] = dict(counters)
    for rate, (count, denominator) in rate_inputs.items():
        results[rate] = counters[count] / denominator
    latency = _latency(samples)
    failed = _failed_gates(results, latency)
    receipt = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "generated_at": generated_at,
        "parameters": {
            "iterations": iterations,
            "fixture": "m9-05-four-action-veto-matrix-v1",
            "latency_path": "four-actions+state/audit-tamper+replay+project-scope",
        },
        "thresholds": copy.deepcopy(_THRESHOLDS),
        "results": results,
        "latency_ms": latency,
        "latency_samples_ms": samples,
        "attestation": {
            "mode": "local-unattested",
            "measurement_authenticity": False,
            "zero_count_source": "isolated-effect-probe",
            "provider_probe_targets": [
                "provider-skill-adapter",
                "recall-provider",
                "reviewer-adapter",
            ],
            "external_probe_targets": [
                "socket.connect",
                "socket.create_connection",
                "subprocess.Popen",
            ],
        },
        "gate": {"status": "passed" if not failed else "failed", "failed_gates": failed},
        "provenance": _provenance(root),
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    validate_human_governance_benchmark(receipt, root=root)
    return receipt


def validate_human_governance_benchmark(
    receipt: Any, *, root: Path
) -> dict[str, Any]:
    """Validate receipt arithmetic, gates, provenance, and content digest."""
    if not isinstance(receipt, dict) or set(receipt) != _TOP_LEVEL_FIELDS:
        raise HumanGovernanceBenchmarkError("benchmark fields do not match")
    if (
        receipt["schema_version"] != BENCHMARK_SCHEMA_VERSION
        or receipt["benchmark_id"] != BENCHMARK_ID
        or not isinstance(receipt["generated_at"], str)
        or _TIMESTAMP_RE.fullmatch(receipt["generated_at"]) is None
        or receipt["thresholds"] != _THRESHOLDS
    ):
        raise HumanGovernanceBenchmarkError("benchmark identity is invalid")
    parameters = receipt["parameters"]
    if not isinstance(parameters, dict) or set(parameters) != {
        "iterations",
        "fixture",
        "latency_path",
    }:
        raise HumanGovernanceBenchmarkError("benchmark parameters are invalid")
    iterations = parameters["iterations"]
    if type(iterations) is not int or not 1 <= iterations <= 1000:
        raise HumanGovernanceBenchmarkError("benchmark iterations are invalid")
    results = receipt["results"]
    if not isinstance(results, dict) or set(results) != _RESULT_FIELDS:
        raise HumanGovernanceBenchmarkError("benchmark results are invalid")
    rate_inputs = {
        "four_action_success_rate": (
            "four_action_successes",
            iterations * len(_ACTIONS),
        ),
        "generic_bypass_rejection_rate": (
            "generic_bypass_rejections",
            iterations,
        ),
        "session_denial_rate": ("session_denials", iterations),
        "duplicate_replay_rate": ("duplicate_replays", iterations),
        "duplicate_drift_conflict_rate": (
            "duplicate_drift_conflicts",
            iterations,
        ),
        "event_tamper_rejection_rate": (
            "event_tamper_rejections",
            iterations,
        ),
        "audit_binding_rejection_rate": (
            "audit_binding_rejections",
            iterations,
        ),
        "project_scope_rate": ("project_scope_matches", iterations),
        "invalid_input_rejection_rate": (
            "invalid_input_rejections",
            iterations,
        ),
    }
    for rate, (count, denominator) in rate_inputs.items():
        if results[rate] != results[count] / denominator:
            raise HumanGovernanceBenchmarkError("benchmark rate arithmetic drifted")
    samples = receipt["latency_samples_ms"]
    if (
        not isinstance(samples, list)
        or len(samples) != iterations
        or any(type(sample) not in {int, float} or sample <= 0 for sample in samples)
        or receipt["latency_ms"] != _latency(samples)
    ):
        raise HumanGovernanceBenchmarkError("benchmark latency evidence is invalid")
    failed = _failed_gates(results, receipt["latency_ms"])
    if receipt["gate"] != {"status": "passed", "failed_gates": []} or failed:
        raise HumanGovernanceBenchmarkError("benchmark gates did not pass")
    if receipt["provenance"] != _provenance(root):
        raise HumanGovernanceBenchmarkError("benchmark provenance drifted")
    expected_digest = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if receipt["receipt_sha256"] != expected_digest:
        raise HumanGovernanceBenchmarkError("benchmark receipt digest drifted")
    return copy.deepcopy(receipt)


__all__ = [
    "BENCHMARK_ID",
    "BENCHMARK_SCHEMA_VERSION",
    "HumanGovernanceBenchmarkError",
    "benchmark_human_governance",
    "validate_human_governance_benchmark",
]
