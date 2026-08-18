"""Deterministic offline benchmark for the M8-09 unattended dispatcher."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import statistics
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .claim_evidence_gate import validate_claim_evidence_verdict
from .durable_state_migration import migrate_typed_state_v4_to_v5
from .idea_continuity_benchmark import build_idea_snapshot
from .idea_review import migrate_typed_state_v3_to_v4
from .shared_state_mcp import (
    CLAIM_LIFECYCLE_TOOL,
    REQUEST_SCHEMA_VERSION,
    WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
    WORK_COMPLETION_TOOL,
    SharedStateMCPService,
)
from .shared_state_migration import migrate_typed_state_v5_to_v6
from .shared_work_ledger import WorkLedger
from .state_mcp import RequestContext
from .unattended_cursor_store import InMemoryUnattendedCursorStore
from .unattended_dispatcher import UnattendedDispatcher
from .verification_profile import (
    build_verification_adapter,
    build_verification_profile,
    validate_verification_decision,
)

BENCHMARK_SCHEMA_VERSION = "context.unattended-dispatcher-benchmark/v1alpha1"
_ACTOR_REF = "actor-executor"
_REQUIRED_WORK_IDS = ("work-a", "work-b", "work-c")
_OPTIONAL_WORK_IDS = ("work-optional",)
_THRESHOLDS: dict[str, float | int] = {
    "required_completion_rate_min": 1.0,
    "campaign_closure_rate_min": 1.0,
    "replay_match_rate_min": 1.0,
    "duplicate_suppression_rate_min": 1.0,
    "authority_violations_max": 0,
    "latency_p95_ms_max": 50.0,
}
_BENCHMARK_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "samples",
    "required_work_per_sample",
    "optional_work_per_sample",
    "required_completion_attempts",
    "required_completions",
    "campaign_closure_attempts",
    "campaign_closures",
    "replay_attempts",
    "replay_matches",
    "duplicate_attempts",
    "duplicate_claim_execute_complete_rejections",
    "rates",
    "latency_ms",
    "thresholds",
    "authority_violations",
    "provider_invocations",
    "external_services",
    "provenance",
    "verdict",
    "receipt_sha256",
}


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


def _fixture(*, project_id: str, observed_at: str) -> tuple[dict[str, Any], dict[str, Any]]:
    state = migrate_typed_state_v5_to_v6(
        migrate_typed_state_v4_to_v5(
            migrate_typed_state_v3_to_v4(
                build_idea_snapshot(),
                migrated_at=observed_at,
            )
        )
    )
    state["project"].update(
        {
            "project_id": project_id,
            "updated_at": observed_at,
            "active_work_ids": [],
            "primary_work_id": None,
            "effect_high_watermark": 0,
        }
    )
    template = next(item for item in state["works"] if item["work_id"] == "work-active")
    definitions = [
        ("work-a", "required", "autonomous"),
        ("work-b", "required", "autonomous"),
        ("work-c", "required", "autonomous"),
        ("work-optional", "optional", "manual"),
    ]
    leaves: list[dict[str, Any]] = []
    for index, (work_id, _mode, _automation) in enumerate(definitions):
        work = copy.deepcopy(template)
        work.update(
            {
                "work_id": work_id,
                "title": work_id,
                "status": "ready",
                "parent_work_id": "goal",
                "dependency_ids": (
                    [definitions[index - 1][0]]
                    if 0 < index < len(_REQUIRED_WORK_IDS)
                    else []
                ),
                "owner_refs": [_ACTOR_REF],
                "scope_refs": [
                    {
                        "scope_kind": "capability",
                        "scope_ref": f"campaign/{work_id}",
                    }
                ],
                "evidence_ids": [],
                "blocker_ids": [],
                "revision": 1,
                "work_identity_sha256": None,
                "work_source_ref": None,
                "source_revision": 0,
                "dedupe_receipt_sha256": None,
            }
        )
        leaves.append(work)
    state["works"] = [
        copy.deepcopy(item)
        for item in state["works"]
        if item["work_id"] in {"campaign", "goal"}
    ] + leaves
    state["claims"] = []
    state["effects"] = []
    state["evidence"] = [
        {
            "evidence_id": f"evidence-{work_id}",
            "kind": "test",
            "artifact_ref": f"artifact://m8-09/{work_id}",
            "content_sha256": hashlib.sha256(work_id.encode()).hexdigest(),
            "validity": "verified",
            "observed_at": observed_at,
            "verified_at": observed_at,
        }
        for work_id, _mode, _automation in definitions
    ]

    work_sources = []
    obligations = []
    for work_id, mode, automation in definitions:
        work = next(item for item in leaves if item["work_id"] == work_id)
        work_sources.append(
            {
                "work_source_id": f"source-{work_id}",
                "project_id": project_id,
                "work_id": work_id,
                "source_kind": "state-native",
                "source_ref": f"opaque://state/{work_id}",
                "source_revision": "revision-1",
                "governance_parent_id": "campaign",
                "dependency_ids": list(work["dependency_ids"]),
                "readiness": "ready",
            }
        )
        obligations.append(
            {
                "obligation_id": f"obligation-{work_id}",
                "work_id": work_id,
                "mode": mode,
                "condition_ref": None,
                "authority": {
                    "kind": "project-governance",
                    "ref": state["project"]["governance_ref"],
                },
                "automation_class": automation,
                "verification_profile_ref": "verification://m8-09/default",
                "evidence_refs": [],
                "expires_at": None,
                "status": "pending",
                "revision": 1,
            }
        )
    governance = {
        "schema_version": "context.project-governance-profile/v1alpha1",
        "profile": {
            "profile_id": "profile-m8-09-benchmark",
            "project_id": project_id,
            "revision": 1,
            "direction_state": "operational",
            "governance_owner_mode": "single-owner",
            "execution_worker_mode": "single-worker",
            "repository_topology": "monolith",
            "requested_runtime_profile": "local-embedded",
            "task_sources": ["state-native"],
            "governance_ref": state["project"]["governance_ref"],
            "updated_at": observed_at,
        },
        "charters": [
            {
                "charter_id": "charter-m8-09-benchmark",
                "project_id": project_id,
                "profile_id": "profile-m8-09-benchmark",
                "status": "approved",
                "problem_space": "Unattended required Work closure.",
                "intended_users": ["developer"],
                "confirmed_constraint_refs": [],
                "prohibited_side_effects": ["untyped-escalation"],
                "current_evidence_refs": [],
                "unknowns": [],
                "assumptions": [],
                "decision_owner_ref": "actor://owner",
                "discovery_campaign_id": "campaign",
                "attempt_budget": 10,
                "expiry": None,
                "return_point_work_id": "work-a",
                "exit_criteria": ["required-work-closed"],
                "mainline_authority": True,
                "revision": 1,
            }
        ],
        "work_sources": work_sources,
        "obligations": obligations,
        "adaptations": [],
    }
    return state, governance


class _BenchmarkRuntime:
    def __init__(
        self,
        state: dict[str, Any],
        governance: dict[str, Any],
        *,
        observed_at: str,
        campaign_run_id: str,
    ) -> None:
        self.state = copy.deepcopy(state)
        self.governance = copy.deepcopy(governance)
        self.observed_at = observed_at
        self.campaign_run_id = campaign_run_id
        self.cursor_store = InMemoryUnattendedCursorStore()
        self.ledger = WorkLedger(
            project_id=state["project"]["project_id"],
            project_revision=state["project"]["revision"],
            works=state["works"],
            max_ttl_ms=60_000,
        )
        self.context = RequestContext(_ACTOR_REF, "authorization-m8-09-benchmark")
        self.service = SharedStateMCPService(
            self.ledger,
            authorizer=self,
            clock=lambda: self.observed_at,
            completion_evidence_resolver=self,
        )
        self.profile = build_verification_profile(
            profile_id="verification/m8-09/benchmark",
            project_id=state["project"]["project_id"],
            profile_version="1.0.0-alpha.1",
            revision=1,
            valid_from=observed_at,
            valid_until=None,
            gates=[
                {
                    "gate_id": "focused",
                    "gate_kind": "tdd",
                    "mode": "required",
                    "condition_ref": None,
                    "capability_refs": ["capability/python"],
                    "depends_on_gate_ids": [],
                    "evidence_requirements": ["red", "green", "artifact-digest"],
                    "thresholds": [],
                }
            ],
        )
        self.adapter = build_verification_adapter(
            adapter_id="adapter/m8-09/benchmark",
            adapter_version="1.0.0-alpha.1",
            project_id=state["project"]["project_id"],
            profile=self.profile,
            bindings=[
                {
                    "gate_id": "focused",
                    "runner_kind": "local-process",
                    "executable": "python3",
                    "arguments": ["-m", "unittest"],
                    "working_directory_ref": "repo://context-control-plane",
                    "environment_refs": [],
                    "timeout_ms": 120_000,
                    "output_budget_bytes": 1_048_576,
                }
            ],
        )
        self.composed: dict[str, dict[str, Any]] = {}
        self.executed: dict[str, dict[str, Any]] = {}
        self.verified: dict[str, dict[str, Any]] = {}
        self.verification_records: dict[str, dict[str, Any]] = {}
        self.verdict_records: dict[str, dict[str, Any]] = {}
        self.execute_side_effects = 0
        self.port_invocations = {name: 0 for name in ("claim", "compose", "execute", "verify", "complete")}
        self.duplicate_stage_passes: dict[str, set[str]] = {}
        self.durable_receipts: dict[tuple[str, str], dict[str, Any]] = {}

    def trusted_now(self) -> str:
        return self.observed_at

    def resolve_receipt(
        self, action: str, request_id: str
    ) -> dict[str, Any] | None:
        receipt = self.durable_receipts.get((action, request_id))
        return copy.deepcopy(receipt) if receipt is not None else None

    def _record_receipt(
        self, action: str, request_id: str, receipt: dict[str, Any]
    ) -> dict[str, Any]:
        self.durable_receipts[(action, request_id)] = copy.deepcopy(receipt)
        return copy.deepcopy(receipt)

    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return (
            context.subject_ref == _ACTOR_REF
            and project_id == self.state["project"]["project_id"]
            and action in {"state.claim.acquire", "state.work.complete"}
        )

    def read(self) -> dict[str, Any]:
        ledger = self.ledger.snapshot()
        state = copy.deepcopy(self.state)
        state["works"] = [
            {key: value for key, value in item.items() if key != "identity_key"}
            for item in ledger["works"]
        ]
        state["claims"] = copy.deepcopy(ledger["claims"])
        state["effects"] = copy.deepcopy(ledger["effects"])
        state["project"]["revision"] = ledger["project_revision"]
        active_ids = [
            item["work_id"] for item in state["works"] if item["status"] == "active"
        ]
        state["project"]["active_work_ids"] = active_ids
        state["project"]["primary_work_id"] = active_ids[0] if active_ids else None
        governance = copy.deepcopy(self.governance)
        status_by_work = {item["work_id"]: item["status"] for item in state["works"]}
        for source in governance["work_sources"]:
            source["readiness"] = status_by_work[source["work_id"]]
        return {
            "state": state,
            "governance_profile": governance,
            "condition_decisions": [],
            "blocking_decisions": [],
        }

    def _record_duplicate(self, work_id: str, stage: str, passed: bool) -> None:
        if passed:
            self.duplicate_stage_passes.setdefault(work_id, set()).add(stage)

    def claim(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.port_invocations["claim"] += 1
        request = {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "request_id": intent["request_id"],
            "project_id": intent["project_id"],
            "action": "acquire",
            "expected_project_revision": intent["expected_project_revision"],
            "work_id": intent["work_id"],
            "claim_id": intent["claim_id"],
            "requested_ttl_ms": 30_000,
            "scope_owners": intent["scope_owners"],
        }
        first = self.service.call_tool(CLAIM_LIFECYCLE_TOOL, request, context=self.context)
        after_first = self.ledger.snapshot()
        replay = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL, copy.deepcopy(request), context=self.context
        )
        self._record_duplicate(
            intent["work_id"],
            "claim",
            first == replay and self.ledger.snapshot() == after_first,
        )
        if not first["ok"]:
            raise RuntimeError(first["error"])
        return self._record_receipt("claim", intent["request_id"], first["result"])

    def compose(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.port_invocations["compose"] += 1
        request_id = intent["request_id"]
        if request_id not in self.composed:
            body = {
                "status": "accepted",
                "project_id": intent["project_id"],
                "project_revision": intent["project_revision"],
                "work_id": intent["work_id"],
                "work_revision": intent["work_revision"],
                "claim_id": intent["claim_id"],
                "lease_epoch": intent["lease_epoch"],
                "fence": intent["fence"],
            }
            self.composed[request_id] = {**body, "packet_sha256": _digest(body)}
        return self._record_receipt("compose", request_id, self.composed[request_id])

    def _execute_once(self, intent: dict[str, Any]) -> dict[str, Any]:
        request_id = intent["request_id"]
        if request_id not in self.executed:
            self.execute_side_effects += 1
            body = {
                "status": "succeeded",
                "work_id": intent["work_id"],
                "packet_sha256": intent["packet_sha256"],
                "claim_id": intent["claim_id"],
                "lease_epoch": intent["lease_epoch"],
                "fence": intent["fence"],
            }
            self.executed[request_id] = {**body, "execution_sha256": _digest(body)}
        return copy.deepcopy(self.executed[request_id])

    def execute(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.port_invocations["execute"] += 1
        first = self._execute_once(intent)
        side_effects = self.execute_side_effects
        replay = self._execute_once(copy.deepcopy(intent))
        self._record_duplicate(
            intent["work_id"],
            "execute",
            first == replay and self.execute_side_effects == side_effects,
        )
        return self._record_receipt("execute", intent["request_id"], first)

    def verify(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.port_invocations["verify"] += 1
        request_id = intent["request_id"]
        if request_id not in self.verified:
            evidence_ids = [f"evidence-{intent['work_id']}"]
            decision = self._verification_decision(intent)
            claim, verdict = self._claim_evidence(intent, evidence_ids)
            self.verification_records[decision["decision_sha256"]] = {
                "decision": decision,
                "profile": self.profile,
                "adapter": self.adapter,
            }
            self.verdict_records[verdict["verdict_sha256"]] = {
                "verdict": verdict,
                "claim": claim,
            }
            body = {
                "decision": "satisfied",
                "work_id": intent["work_id"],
                "execution_sha256": intent["execution_sha256"],
                "evidence_ids": evidence_ids,
                "claim_evidence_verdict_sha256": verdict["verdict_sha256"],
                "verifier_ref": "actor-independent-verifier",
                "completion_authority": False,
            }
            self.verified[request_id] = {
                **body,
                "verification_decision_sha256": decision["decision_sha256"],
            }
        return self._record_receipt("verify", request_id, self.verified[request_id])

    def _verification_decision(self, intent: dict[str, Any]) -> dict[str, Any]:
        decision = {
            "schema_version": "context.verification-decision/v1alpha1",
            "decision_id": f"verification/{intent['work_id']}",
            "work_id": intent["work_id"],
            "project_id": intent["project_id"],
            "project_revision": intent["project_revision"],
            "profile_id": self.profile["profile_id"],
            "profile_sha256": self.profile["profile_sha256"],
            "adapter_id": self.adapter["adapter_id"],
            "adapter_sha256": self.adapter["adapter_sha256"],
            "gate_outcomes": [
                {
                    "gate_id": "focused",
                    "gate_kind": "tdd",
                    "mode": "required",
                    "status": "satisfied",
                    "reason": "gate-satisfied",
                    "non_blocking": False,
                    "condition_ref": None,
                    "capability_refs": ["capability/python"],
                    "run_receipt_sha256": "e" * 64,
                    "evidence_refs": [f"artifact://m8-09/{intent['work_id']}"],
                }
            ],
            "overall_status": "satisfied",
            "evaluated_at": self.observed_at,
            "state_write_authority": False,
            "completion_authority": False,
            "decision_sha256": "",
        }
        decision["decision_sha256"] = _digest(
            {key: value for key, value in decision.items() if key != "decision_sha256"}
        )
        validate_verification_decision(decision, profile=self.profile, adapter=self.adapter)
        return decision

    def _claim_evidence(
        self, intent: dict[str, Any], evidence_ids: list[str]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        claim = {
            "claim_id": intent["claim_id"],
            "work_id": intent["work_id"],
            "claim_kind": "completion",
            "statement": f"{intent['work_id']} has current completion evidence.",
            "evidence_assertion_ids": sorted(evidence_ids),
            "scope_refs": [
                {
                    "scope_kind": "capability",
                    "scope_ref": f"campaign/{intent['work_id']}",
                }
            ],
        }
        verdict = {
            "schema_version": "context.claim-evidence-gate/v1alpha1",
            "gate_id": f"claim-gate/{intent['work_id']}",
            "claim_id": intent["claim_id"],
            "claim_kind": "completion",
            "claim_sha256": _digest(claim),
            "decision": "allow",
            "reason": "evidence_satisfied",
            "required_authority_groups": [["current_code", "current_state"]],
            "matched_authority_kinds": ["current_state"],
            "evidence_ids": sorted(evidence_ids),
            "evaluated_at": self.observed_at,
            "state_write_authority": False,
            "completion_authority": False,
            "verdict_sha256": "",
        }
        verdict["verdict_sha256"] = _digest(
            {key: value for key, value in verdict.items() if key != "verdict_sha256"}
        )
        validate_claim_evidence_verdict(verdict)
        return claim, verdict

    def resolve_verification_decision(self, digest: str) -> dict[str, Any] | None:
        value = self.verification_records.get(digest)
        return copy.deepcopy(value) if value is not None else None

    def resolve_claim_evidence_verdict(self, digest: str) -> dict[str, Any] | None:
        value = self.verdict_records.get(digest)
        return copy.deepcopy(value) if value is not None else None

    def complete(self, intent: dict[str, Any]) -> dict[str, Any]:
        self.port_invocations["complete"] += 1
        request = {
            "schema_version": WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
            "request_id": intent["request_id"],
            "project_id": intent["project_id"],
            "work_id": intent["work_id"],
            "claim_id": intent["claim_id"],
            "expected_project_revision": intent["expected_project_revision"],
            "expected_work_revision": intent["expected_work_revision"],
            "expected_claim_revision": intent["expected_claim_revision"],
            "lease_epoch": intent["lease_epoch"],
            "fence": intent["fence"],
            "evidence_ids": intent["evidence_ids"],
            "verification_decision_sha256": intent["verification_decision_sha256"],
            "claim_evidence_verdict_sha256": intent["claim_evidence_verdict_sha256"],
        }
        first = self.service.call_tool(WORK_COMPLETION_TOOL, request, context=self.context)
        after_first = self.ledger.snapshot()
        replay = self.service.call_tool(
            WORK_COMPLETION_TOOL, copy.deepcopy(request), context=self.context
        )
        self._record_duplicate(
            intent["work_id"],
            "complete",
            first == replay and self.ledger.snapshot() == after_first,
        )
        if not first["ok"]:
            raise RuntimeError(first["error"])
        return self._record_receipt(
            "complete", intent["request_id"], first["result"]
        )

    def block_and_release(self, intent: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError(f"unexpected block: {intent}")

    def duplicate_work_passes(self) -> int:
        return sum(
            self.duplicate_stage_passes.get(work_id) == {"claim", "execute", "complete"}
            for work_id in _REQUIRED_WORK_IDS
        )


def _authority_violations(receipt: Mapping[str, Any]) -> int:
    documents = [receipt, *receipt.get("steps", [])]
    return sum(
        any(
            document.get(field) != expected
            for field, expected in (
                ("state_write_authority", False),
                ("completion_authority", False),
                ("provider_authority", 0),
                ("external_effect_authority", 0),
            )
        )
        for document in documents
        if isinstance(document, Mapping)
    )


def _provenance(root: Path) -> dict[str, str]:
    return {
        "implementation_sha256": _file_digest(root / "context_control_plane/unattended_dispatcher.py"),
        "benchmark_sha256": _file_digest(root / "context_control_plane/unattended_dispatcher_benchmark.py"),
        "cursor_store_sha256": _file_digest(root / "context_control_plane/unattended_cursor_store.py"),
        "receipt_validator_sha256": _file_digest(
            root / "context_control_plane/unattended_receipts.py"
        ),
        "state_mcp_sha256": _file_digest(root / "context_control_plane/shared_state_mcp.py"),
        "work_ledger_sha256": _file_digest(root / "context_control_plane/shared_work_ledger.py"),
    }


def _failed_gates(
    rates: Mapping[str, float],
    *,
    authority_violations: int,
    latency_p95_ms: float,
) -> list[str]:
    return [
        gate
        for gate, passed in {
            "required-completion": rates["required_completion_rate"]
            >= _THRESHOLDS["required_completion_rate_min"],
            "campaign-closure": rates["campaign_closure_rate"]
            >= _THRESHOLDS["campaign_closure_rate_min"],
            "replay": rates["replay_match_rate"]
            >= _THRESHOLDS["replay_match_rate_min"],
            "duplicate-suppression": rates["duplicate_suppression_rate"]
            >= _THRESHOLDS["duplicate_suppression_rate_min"],
            "authority": authority_violations
            <= _THRESHOLDS["authority_violations_max"],
            "latency": latency_p95_ms <= _THRESHOLDS["latency_p95_ms_max"],
        }.items()
        if not passed
    ]


def benchmark_unattended_dispatcher(
    *,
    root: Path,
    samples: int = 1_000,
    generated_at: str = "2026-08-17T12:00:00+00:00",
) -> dict[str, Any]:
    """Run required closure, replay, and duplicate suppression offline."""
    if type(samples) is not int or samples < 1:
        raise ValueError("samples must be positive")

    required_completion_attempts = required_completions = 0
    campaign_closure_attempts = campaign_closures = 0
    replay_attempts = replay_matches = 0
    duplicate_attempts = duplicate_passes = 0
    authority_violations = 0
    latencies: list[float] = []

    for index in range(samples):
        started = time.perf_counter()
        project_id = f"project-m8-09-benchmark-{index}"
        state, governance = _fixture(project_id=project_id, observed_at=generated_at)
        runtime = _BenchmarkRuntime(
            state,
            governance,
            observed_at=generated_at,
            campaign_run_id=f"campaign-run-m8-09-{index}",
        )
        dispatcher = UnattendedDispatcher(runtime, actor_ref=_ACTOR_REF)
        receipt = dispatcher.run(max_steps=len(_REQUIRED_WORK_IDS))
        required_completion_attempts += len(_REQUIRED_WORK_IDS)
        completed_required = len(set(receipt["completed_work_ids"]) & set(_REQUIRED_WORK_IDS))
        required_completions += completed_required
        campaign_closure_attempts += 1
        campaign_closures += int(
            receipt["status"] == "completed"
            and set(receipt["completed_work_ids"]) == set(_REQUIRED_WORK_IDS)
            and receipt["remaining_optional_work_ids"] == list(_OPTIONAL_WORK_IDS)
            and len(receipt["steps"]) == len(_REQUIRED_WORK_IDS)
        )
        duplicate_attempts += len(_REQUIRED_WORK_IDS)
        duplicate_passes += runtime.duplicate_work_passes()
        authority_violations += _authority_violations(receipt)

        before_replay = copy.deepcopy(runtime.port_invocations)
        replay = dispatcher.run(max_steps=len(_REQUIRED_WORK_IDS))
        replay_attempts += 1
        replay_matches += int(
            replay == receipt and runtime.port_invocations == before_replay
        )
        latencies.append((time.perf_counter() - started) * 1_000.0)

    ordered = sorted(latencies)
    p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    latency = {
        "p50_ms": float(statistics.median(ordered)),
        "p95_ms": float(ordered[p95_index]),
        "max_ms": float(max(ordered)),
    }
    rates = {
        "required_completion_rate": _rate(required_completions, required_completion_attempts),
        "campaign_closure_rate": _rate(campaign_closures, campaign_closure_attempts),
        "replay_match_rate": _rate(replay_matches, replay_attempts),
        "duplicate_suppression_rate": _rate(duplicate_passes, duplicate_attempts),
    }
    failed_gates = _failed_gates(
        rates,
        authority_violations=authority_violations,
        latency_p95_ms=latency["p95_ms"],
    )
    receipt = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": f"benchmark-m8-09-{_digest({'generated_at': generated_at, 'samples': samples})[:24]}",
        "generated_at": generated_at,
        "samples": samples,
        "required_work_per_sample": len(_REQUIRED_WORK_IDS),
        "optional_work_per_sample": len(_OPTIONAL_WORK_IDS),
        "required_completion_attempts": required_completion_attempts,
        "required_completions": required_completions,
        "campaign_closure_attempts": campaign_closure_attempts,
        "campaign_closures": campaign_closures,
        "replay_attempts": replay_attempts,
        "replay_matches": replay_matches,
        "duplicate_attempts": duplicate_attempts,
        "duplicate_claim_execute_complete_rejections": duplicate_passes,
        "rates": rates,
        "latency_ms": latency,
        "thresholds": copy.deepcopy(_THRESHOLDS),
        "authority_violations": authority_violations,
        "provider_invocations": 0,
        "external_services": 0,
        "provenance": _provenance(root),
        "verdict": {
            "decision": "pass" if not failed_gates else "fail",
            "failed_gates": failed_gates,
        },
    }
    receipt["receipt_sha256"] = _digest(receipt)
    return receipt


def validate_unattended_dispatcher_benchmark(
    receipt: Mapping[str, Any], *, root: Path
) -> None:
    """Validate receipt integrity, derived gates, and local provenance."""
    document = dict(receipt)
    if set(document) != _BENCHMARK_FIELDS:
        raise ValueError("benchmark receipt fields are invalid")
    receipt_sha256 = document.pop("receipt_sha256", None)
    if receipt_sha256 != _digest(document):
        raise ValueError("receipt_sha256 mismatch")
    if document.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("benchmark schema mismatch")
    samples = document.get("samples")
    if type(samples) is not int or samples < 1:
        raise ValueError("sample coverage is invalid")
    expected_counts = {
        "required_work_per_sample": len(_REQUIRED_WORK_IDS),
        "optional_work_per_sample": len(_OPTIONAL_WORK_IDS),
        "required_completion_attempts": samples * len(_REQUIRED_WORK_IDS),
        "campaign_closure_attempts": samples,
        "replay_attempts": samples,
        "duplicate_attempts": samples * len(_REQUIRED_WORK_IDS),
    }
    for field, expected in expected_counts.items():
        if document.get(field) != expected:
            raise ValueError(f"{field} coverage mismatch")
    relationships = (
        ("required_completions", "required_completion_attempts"),
        ("campaign_closures", "campaign_closure_attempts"),
        ("replay_matches", "replay_attempts"),
        ("duplicate_claim_execute_complete_rejections", "duplicate_attempts"),
    )
    for numerator, denominator in relationships:
        if document.get(numerator) != document.get(denominator):
            raise ValueError(f"{numerator} completion gate failed")
    if document.get("authority_violations") != 0:
        raise ValueError("authority violation detected")
    if document.get("provider_invocations") != 0 or document.get("external_services") != 0:
        raise ValueError("offline benchmark boundary violated")
    expected_rates = {
        "required_completion_rate": _rate(
            document["required_completions"], document["required_completion_attempts"]
        ),
        "campaign_closure_rate": _rate(
            document["campaign_closures"], document["campaign_closure_attempts"]
        ),
        "replay_match_rate": _rate(document["replay_matches"], document["replay_attempts"]),
        "duplicate_suppression_rate": _rate(
            document["duplicate_claim_execute_complete_rejections"],
            document["duplicate_attempts"],
        ),
    }
    if document.get("rates") != expected_rates:
        raise ValueError("derived rates mismatch")
    if document.get("thresholds") != _THRESHOLDS:
        raise ValueError("benchmark thresholds mismatch")
    latency = document.get("latency_ms")
    if not isinstance(latency, Mapping) or not (
        0 < latency.get("p50_ms", 0)
        <= latency.get("p95_ms", 0)
        <= latency.get("max_ms", 0)
    ):
        raise ValueError("latency ordering mismatch")
    failed_gates = _failed_gates(
        expected_rates,
        authority_violations=document["authority_violations"],
        latency_p95_ms=latency["p95_ms"],
    )
    expected_verdict = {
        "decision": "pass" if not failed_gates else "fail",
        "failed_gates": failed_gates,
    }
    if document.get("verdict") != expected_verdict or failed_gates:
        raise ValueError("latency or benchmark verdict mismatch")
    if document.get("provenance") != _provenance(root):
        raise ValueError("benchmark provenance mismatch")


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "benchmark_unattended_dispatcher",
    "validate_unattended_dispatcher_benchmark",
]
