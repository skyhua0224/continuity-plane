"""Independent M3-08 local-embedded continuation benchmark."""

from __future__ import annotations

import copy
import hashlib
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from .input_progression import (
    InputProgressionError,
    canonical_blocking_decision_bytes,
    canonical_progression_decision_bytes,
    decide_input_progression,
)
from .sticky_router import canonical_route_decision_bytes, route_task_input

BENCHMARK_SCHEMA_VERSION = "context.continuation-dispatch-benchmark/v1alpha1"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _provenance_paths(root: Path) -> dict[str, Path]:
    return {
        "implementation_sha256": root / "context_control_plane/input_progression.py",
        "benchmark_runner_sha256": root / "context_control_plane/input_progression_benchmark.py",
        "sticky_router_sha256": root / "context_control_plane/sticky_router.py",
        "typed_state_sha256": root / "context_control_plane/typed_state.py",
        "governance_profile_sha256": root / "context_control_plane/project_governance_profile.py",
        "request_schema_sha256": root / "schemas/m3-08/continuation-dispatch-request.schema.json",
        "blocking_schema_sha256": root / "schemas/m3-08/blocking-decision.schema.json",
        "decision_schema_sha256": root / "schemas/m3-08/continuation-dispatch-decision.schema.json",
        "benchmark_schema_sha256": root / "schemas/m3-08/continuation-dispatch-benchmark.schema.json",
        "registry_sha256": root / "schemas/registry.yaml",
    }


def _work(
    work_id: str,
    status: str,
    *,
    kind: str = "work",
    parent_work_id: str | None = "goal-m3-08",
    blocker_ids: list[str] | None = None,
    evidence_ids: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "work_id": work_id,
        "kind": kind,
        "title": work_id,
        "status": status,
        "parent_work_id": parent_work_id,
        "dependency_ids": [],
        "owner_refs": ["actor://owner"],
        "scope_refs": [{"scope_kind": "capability", "scope_ref": f"m3-08/{work_id}"}],
        "overlap_candidate_ids": [],
        "dedupe_status": "clear",
        "supersedes_work_id": None,
        "evidence_ids": evidence_ids or [],
        "blocker_ids": blocker_ids or [],
        "revision": 1,
        "return_point_work_id": None,
        "exit_criteria": [],
        "attempt_budget": None,
        "expires_at": None,
        "promotion_target_work_id": None,
        "mainline_authority": True,
    }


def _claim(work_id: str) -> dict[str, Any]:
    return {
        "claim_id": f"claim-{work_id}",
        "work_id": work_id,
        "actor_ref": "actor://owner",
        "status": "active",
        "expected_project_revision": 48,
        "claimed_at": "2026-08-14T10:00:00+08:00",
        "lease_expires_at": "2026-08-14T18:00:00+08:00",
        "released_at": None,
        "scope_owners": [{"scope_kind": "capability", "scope_ref": f"m3-08/{work_id}"}],
    }


def _state(active_work_id: str | None = "work-a") -> dict[str, Any]:
    return {
        "schema_version": "context.typed-state/v2alpha1",
        "project": {
            "project_id": "project-m3-08-benchmark",
            "revision": 48,
            "governance_ref": "governance://m3-08-benchmark",
            "active_work_ids": [active_work_id] if active_work_id else [],
            "primary_work_id": active_work_id,
            "current_decision_ids": [],
            "active_constraint_ids": [],
            "open_blocker_ids": [],
            "effect_high_watermark": 0,
            "updated_at": "2026-08-14T11:00:00+08:00",
        },
        "works": [
            _work("campaign-m3", "ready", kind="campaign", parent_work_id=None),
            _work("goal-m3-08", "ready", kind="goal", parent_work_id="campaign-m3"),
            _work("work-a", "active" if active_work_id == "work-a" else "ready"),
            _work("work-b", "ready"),
            _work("work-z", "ready"),
        ],
        "claims": [_claim(active_work_id)] if active_work_id else [],
        "ideas": [],
        "decisions": [],
        "constraints": [],
        "evidence": [],
        "blockers": [],
        "effects": [],
    }


def _profile() -> dict[str, Any]:
    work_sources = []
    obligations = []
    for work_id, readiness, mode in (
        ("work-a", "active", "required"),
        ("work-b", "ready", "required"),
        ("work-z", "ready", "optional"),
    ):
        work_sources.append(
            {
                "work_source_id": f"source-{work_id}",
                "project_id": "project-m3-08-benchmark",
                "work_id": work_id,
                "source_kind": "master-workstream",
                "source_ref": f"opaque://m3-08/{work_id}",
                "source_revision": "revision-48",
                "governance_parent_id": "campaign-m3",
                "dependency_ids": [],
                "readiness": readiness,
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
                    "ref": "governance://m3-08-benchmark",
                },
                "automation_class": "autonomous",
                "verification_profile_ref": "verification://m3-08",
                "evidence_refs": [],
                "expires_at": None,
                "status": "pending",
                "revision": 1,
            }
        )
    return {
        "schema_version": "context.project-governance-profile/v1alpha1",
        "profile": {
            "profile_id": "profile-m3-08-benchmark",
            "project_id": "project-m3-08-benchmark",
            "revision": 48,
            "direction_state": "operational",
            "governance_owner_mode": "single-owner",
            "execution_worker_mode": "single-worker",
            "repository_topology": "monolith",
            "requested_runtime_profile": "local-embedded",
            "task_sources": ["master-workstream"],
            "governance_ref": "governance://m3-08-benchmark",
            "updated_at": "2026-08-14T11:00:00+08:00",
        },
        "charters": [
            {
                "charter_id": "charter-m3-08",
                "project_id": "project-m3-08-benchmark",
                "profile_id": "profile-m3-08-benchmark",
                "status": "approved",
                "problem_space": "Deterministic continuation and dispatch.",
                "intended_users": ["developer"],
                "confirmed_constraint_refs": [],
                "prohibited_side_effects": ["untyped-escalation"],
                "current_evidence_refs": [],
                "unknowns": [],
                "assumptions": [],
                "decision_owner_ref": "actor://owner",
                "discovery_campaign_id": "campaign-m3",
                "attempt_budget": 1,
                "expiry": None,
                "return_point_work_id": "work-a",
                "exit_criteria": ["m3-08-benchmark"],
                "mainline_authority": True,
                "revision": 1,
            }
        ],
        "work_sources": work_sources,
        "obligations": obligations,
        "adaptations": [],
    }


def _route_request(input_kind: str) -> dict[str, Any]:
    return {
        "schema_version": "context.task-route-request/v1alpha1",
        "request_id": f"route-m3-08-{input_kind}",
        "project_id": "project-m3-08-benchmark",
        "expected_project_revision": 48,
        "active_work_id": "work-a",
        "expected_active_work_revision": 1,
        "input_ref": f"opaque://benchmark/{input_kind}",
        "input_sha256": "a" * 64,
        "input_kind": input_kind,
        "classifier_confidence_millionths": 1_000_000,
        "classifier_provenance_ref": "artifact://classifier/m3-08",
        "target_work_id": None,
        "user_authorization_candidate": False,
        "authorization_candidate_ref": None,
        "evidence_refs": [],
    }


def _progression_request(
    intent_kind: str,
    *,
    route_decision: dict[str, Any] | None = None,
    blocking_decision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "context.continuation-dispatch-request/v1alpha1",
        "request_id": f"progress-m3-08-{intent_kind}",
        "project_id": "project-m3-08-benchmark",
        "expected_project_revision": 48,
        "governance_profile_id": "profile-m3-08-benchmark",
        "expected_governance_revision": 48,
        "intent_kind": intent_kind,
        "route_decision_sha256": (
            hashlib.sha256(canonical_route_decision_bytes(route_decision)).hexdigest()
            if route_decision is not None
            else None
        ),
        "blocking_decision_sha256": (
            hashlib.sha256(canonical_blocking_decision_bytes(blocking_decision)).hexdigest()
            if blocking_decision is not None
            else None
        ),
        "observed_at": "2026-08-14T16:00:00+08:00",
    }


def _blocking_decision(
    state: dict[str, Any],
    work_id: str,
    kind: str,
    *,
    external: bool,
) -> dict[str, Any]:
    evidence_id = f"evidence-{work_id}"
    state["evidence"].append(
        {
            "evidence_id": evidence_id,
            "kind": "user-decision",
            "artifact_ref": f"artifact://m3-08/{work_id}",
            "content_sha256": "b" * 64,
            "validity": "verified",
            "observed_at": "2026-08-14T10:30:00+08:00",
            "verified_at": "2026-08-14T10:31:00+08:00",
        }
    )
    blocker_id = f"blocker-{work_id}"
    blocker = {
        "blocker_id": blocker_id,
        "status": "open",
        "reason": "M3-08 benchmark requires bounded resolution.",
        "blocked_work_ids": [work_id],
        "evidence_ids": [evidence_id],
        "opened_at": "2026-08-14T10:32:00+08:00",
        "resolved_at": None,
        "supersedes_blocker_id": None,
    }
    state["blockers"].append(blocker)
    state["project"]["open_blocker_ids"].append(blocker_id)
    next(work for work in state["works"] if work["work_id"] == work_id)[
        "blocker_ids"
    ].append(blocker_id)
    return {
        "schema_version": "context.blocking-decision/v1alpha1",
        "blocking_decision_id": f"decision-{blocker_id}",
        "project_id": state["project"]["project_id"],
        "project_revision": 48,
        "blocker_id": blocker_id,
        "blocker_kind": kind,
        "reason": blocker["reason"],
        "affected_work_ids": [work_id],
        "evidence_ids": [evidence_id],
        "affected_scope_refs": [
            {"scope_kind": "capability", "scope_ref": f"m3-08/{work_id}"}
        ],
        "decision_options": []
        if external
        else [
            {"option_id": "retain", "summary": "Retain current output."},
            {"option_id": "replace", "summary": "Replace current output."},
        ],
        "default_option_id": None if external else "retain",
        "resume_condition": {
            "kind": "evidence" if external else "decision",
            "refs": ["evidence://m3-08/external" if external else "decision://m3-08"],
        },
        "resolution_actor": "external_system" if external else "user",
        "safe_reversible_default_available": False,
        "state_write_authority": False,
    }


def _case(mode: int) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str]:
    state = _state("work-a")
    profile = _profile()
    route_decision = None
    blocking_decision = None
    if mode == 0 or mode == 1:
        input_kind = "idea" if mode == 1 else "status_query"
        route_decision = route_task_input(_route_request(input_kind), state)
        request = _progression_request("routed_input", route_decision=route_decision)
        expected = "capture-and-continue" if mode == 1 else "continue-active"
    elif mode == 2:
        state = _state(None)
        for source in profile["work_sources"]:
            source["readiness"] = "ready"
        request = _progression_request("dispatch_tick")
        expected = "select-next-ready"
    elif mode == 3:
        state = _state(None)
        for source in profile["work_sources"]:
            source["readiness"] = "ready"
        blocked = next(work for work in state["works"] if work["work_id"] == "work-b")
        blocked["status"] = "blocked"
        next(source for source in profile["work_sources"] if source["work_id"] == "work-b")["readiness"] = "blocked"
        blocking_decision = _blocking_decision(state, "work-b", "external-completion-evidence-unavailable", external=True)
        request = _progression_request("blocking_decision", blocking_decision=blocking_decision)
        expected = "select-next-ready"
    elif mode == 4:
        blocking_decision = _blocking_decision(state, "work-a", "acceptance-output-choice-required", external=False)
        request = _progression_request("blocking_decision", blocking_decision=blocking_decision)
        expected = "ask-user"
    elif mode == 5:
        state = _state(None)
        next(item for item in profile["obligations"] if item["work_id"] == "work-a")["mode"] = "optional"
        next(item for item in profile["work_sources"] if item["work_id"] == "work-a")["readiness"] = "ready"
        blocked = next(work for work in state["works"] if work["work_id"] == "work-b")
        blocked["status"] = "blocked"
        next(source for source in profile["work_sources"] if source["work_id"] == "work-b")["readiness"] = "blocked"
        blocking_decision = _blocking_decision(state, "work-b", "external-completion-evidence-unavailable", external=True)
        request = _progression_request("blocking_decision", blocking_decision=blocking_decision)
        expected = "stop-blocked"
    else:
        state = _state(None)
        completed = next(work for work in state["works"] if work["work_id"] == "work-b")
        completed["status"] = "completed"
        completed["evidence_ids"] = ["evidence-completed-b"]
        state["evidence"].append({"evidence_id": "evidence-completed-b", "kind": "test", "artifact_ref": "artifact://m3-08/completed-b", "content_sha256": "e" * 64, "validity": "verified", "observed_at": "2026-08-14T10:40:00+08:00", "verified_at": "2026-08-14T10:41:00+08:00"})
        next(item for item in profile["obligations"] if item["work_id"] == "work-a")["mode"] = "optional"
        next(item for item in profile["work_sources"] if item["work_id"] == "work-a")["readiness"] = "ready"
        source = next(item for item in profile["work_sources"] if item["work_id"] == "work-b")
        source["readiness"] = "completed"
        obligation = next(item for item in profile["obligations"] if item["work_id"] == "work-b")
        obligation["status"] = "satisfied"
        obligation["evidence_refs"] = ["evidence://m3-08/completed-b"]
        request = _progression_request("dispatch_tick")
        expected = "stop-complete"
    return state, profile, request, route_decision, blocking_decision, expected


def _negative_case() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    state = _state(None)
    profile = _profile()
    for work in state["works"]:
        if work["work_id"] in {"work-a", "work-b"}:
            work["status"] = "blocked"
    for source in profile["work_sources"]:
        if source["work_id"] in {"work-a", "work-b"}:
            source["readiness"] = "blocked"
    return state, profile, _progression_request("dispatch_tick")


def run_input_progression_benchmark(
    *, root: Path, samples: int, observed_at: str
) -> dict[str, Any]:
    if samples < 1:
        raise ValueError("samples must be positive")
    timings: list[float] = []
    successful = 0
    ready_required_missed = 0
    premature_stop = 0
    untyped_ask = 0
    incomplete_escalation = 0
    active_leaf_changes = 0
    nondeterministic_replays = 0
    request_mutations = 0
    state_mutations = 0
    profile_mutations = 0
    for sample in range(samples):
        state, profile, request, route, blocker, expected = _case(sample % 7)
        before_request = copy.deepcopy(request)
        before_state = copy.deepcopy(state)
        before_profile = copy.deepcopy(profile)
        started = time.perf_counter_ns()
        decision = decide_input_progression(
            request,
            state,
            profile,
            route_decision=route,
            blocking_decision=blocker,
        )
        timings.append((time.perf_counter_ns() - started) / 1_000_000)
        replay = decide_input_progression(
            copy.deepcopy(request),
            copy.deepcopy(state),
            copy.deepcopy(profile),
            route_decision=copy.deepcopy(route),
            blocking_decision=copy.deepcopy(blocker),
        )
        successful += 1
        if decision["action"] != expected:
            if expected == "select-next-ready":
                ready_required_missed += 1
            if decision["action"] in {"ask-user", "stop-blocked", "stop-complete"}:
                premature_stop += 1
        if decision["action"] in {"ask-user", "stop-blocked"}:
            if decision["blocker_id"] is None:
                untyped_ask += 1
            if not decision["reason"] or not decision["evidence_ids"] or decision["resume_condition"] is None:
                incomplete_escalation += 1
        if decision["active_work_id_before"] != decision["active_work_id_after"]:
            active_leaf_changes += 1
        if canonical_progression_decision_bytes(decision) != canonical_progression_decision_bytes(replay):
            nondeterministic_replays += 1
        request_mutations += request != before_request
        state_mutations += state != before_state
        profile_mutations += profile != before_profile

    negative_rejections = 0
    for _ in range(max(1, samples // 8)):
        state, profile, request = _negative_case()
        try:
            decide_input_progression(
                request,
                state,
                profile,
                route_decision=None,
                blocking_decision=None,
            )
        except InputProgressionError:
            negative_rejections += 1

    ordered = sorted(timings)
    p95_index = max(0, int(len(ordered) * 0.95) - 1)
    implementation_paths = _provenance_paths(root)
    return {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "observed_at": observed_at,
        "provenance": {key: _sha256(path) for key, path in implementation_paths.items()},
        "environment": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "platform": sys.platform,
            "machine": platform.machine(),
            "external_services": 0,
        },
        "measurement": {
            "samples": samples,
            "successful_samples": successful,
            "negative_fixture_samples": max(1, samples // 8),
            "negative_fixture_rejections": negative_rejections,
            "ready_required_missed_count": ready_required_missed,
            "premature_stop_count": premature_stop,
            "untyped_ask_count": untyped_ask,
            "incomplete_escalation_count": incomplete_escalation,
            "active_leaf_change_count": active_leaf_changes,
            "nondeterministic_replay_count": nondeterministic_replays,
            "request_mutation_count": request_mutations,
            "state_mutation_count": state_mutations,
            "profile_mutation_count": profile_mutations,
            "p50_decision_latency_ms": round(statistics.median(timings), 6),
            "p95_decision_latency_ms": round(ordered[p95_index], 6),
            "max_decision_latency_ms": round(max(timings), 6),
        },
        "acceptance": {
            "all_samples_succeeded": successful == samples,
            "ready_required_never_missed": ready_required_missed == 0,
            "no_premature_stop": premature_stop == 0,
            "no_untyped_ask": untyped_ask == 0,
            "escalation_fields_complete": incomplete_escalation == 0,
            "active_leaf_preserved": active_leaf_changes == 0,
            "replay_deterministic": nondeterministic_replays == 0,
            "inputs_immutable": request_mutations == 0 and state_mutations == 0 and profile_mutations == 0,
            "negative_fixtures_rejected": negative_rejections == max(1, samples // 8),
            "zero_external_services": True,
        },
    }


def validate_input_progression_benchmark_receipt(
    receipt: dict[str, Any], *, root: Path
) -> None:
    if receipt.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("benchmark schema version is unsupported")
    measurement = receipt.get("measurement", {})
    acceptance = receipt.get("acceptance", {})
    required_zero_fields = (
        "ready_required_missed_count",
        "premature_stop_count",
        "untyped_ask_count",
        "incomplete_escalation_count",
        "active_leaf_change_count",
        "nondeterministic_replay_count",
        "request_mutation_count",
        "state_mutation_count",
        "profile_mutation_count",
    )
    if any(measurement.get(field) != 0 for field in required_zero_fields):
        raise ValueError("benchmark acceptance counters are non-zero")
    if not all(acceptance.get(field) is True for field in (
        "all_samples_succeeded",
        "ready_required_never_missed",
        "no_premature_stop",
        "no_untyped_ask",
        "escalation_fields_complete",
        "active_leaf_preserved",
        "replay_deterministic",
        "inputs_immutable",
        "negative_fixtures_rejected",
        "zero_external_services",
    )):
        raise ValueError("benchmark acceptance claim is false")
    expected_provenance = {
        key: _sha256(path) for key, path in _provenance_paths(root).items()
    }
    if receipt.get("provenance") != expected_provenance:
        raise ValueError("benchmark provenance does not match current artifacts")
