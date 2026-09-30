"""Zero-service benchmark for the M3-05 Experiment lifecycle contract."""

from __future__ import annotations

import copy
import hashlib
import platform
import statistics
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .sqlite_state_store import SQLiteStateStore
from .state_mcp import RequestContext, StateMCPService
from .typed_state import validate_typed_state

_PROVENANCE_PATHS = {
    "fixture_sha256": "experiments/routing/m3-05-experiment-lifecycle.yaml",
    "implementation_sha256": "context_control_plane/experiment_lifecycle.py",
    "state_mcp_sha256": "context_control_plane/state_mcp.py",
    "state_events_sha256": "context_control_plane/state_events.py",
    "typed_state_sha256": "context_control_plane/typed_state.py",
    "benchmark_sha256": "context_control_plane/experiment_lifecycle_benchmark.py",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_lifecycle_fixture(root: str | Path) -> dict[str, Any]:
    root = Path(root)
    fixture = yaml.safe_load(
        (root / "experiments/routing/m3-05-experiment-lifecycle.yaml").read_text(
            encoding="utf-8"
        )
    )
    if not isinstance(fixture, dict) or fixture.get("schema_version") != (
        "context.experiment-lifecycle-benchmark-fixture/v1alpha1"
    ):
        raise ValueError("M3-05 benchmark fixture is invalid")
    return fixture


def build_lifecycle_snapshot(fixture: dict[str, Any]) -> dict[str, Any]:
    experiment_spec = fixture["experiment"]
    evidence = [
        {
            "evidence_id": item["evidence_id"],
            "kind": "test",
            "artifact_ref": f"artifact://verification/{item['evidence_id']}",
            "content_sha256": item["content_sha256"],
            "validity": "verified",
            "observed_at": "2026-08-14T08:31:00+08:00",
            "verified_at": "2026-08-14T08:32:00+08:00",
        }
        for item in fixture["evidence"]
    ]
    snapshot = {
        "schema_version": "context.typed-state/v3alpha1",
        "project": {
            "project_id": fixture["project_id"],
            "revision": 9,
            "governance_ref": "artifact://governance/m305-benchmark",
            "active_work_ids": [experiment_spec["work_id"]],
            "primary_work_id": experiment_spec["work_id"],
            "current_decision_ids": [],
            "active_constraint_ids": [],
            "open_blocker_ids": [],
            "effect_high_watermark": 0,
            "updated_at": "2026-08-14T08:00:00+08:00",
        },
        "works": [
            {
                "work_id": "campaign",
                "kind": "campaign",
                "title": "Campaign",
                "status": "ready",
                "parent_work_id": None,
                "dependency_ids": [],
                "owner_refs": ["actor-owner"],
                "scope_refs": [{"scope_kind": "capability", "scope_ref": "campaign"}],
                "overlap_candidate_ids": [], "dedupe_status": "clear",
                "supersedes_work_id": None, "evidence_ids": [], "blocker_ids": [],
                "revision": 1, "return_point_work_id": None, "exit_criteria": [],
                "attempt_budget": None, "expires_at": None,
                "promotion_target_work_id": None, "mainline_authority": True,
            },
            {
                "work_id": "mainline-target",
                "kind": "goal",
                "title": "Mainline target",
                "status": "ready",
                "parent_work_id": "campaign",
                "dependency_ids": [],
                "owner_refs": ["actor-owner"],
                "scope_refs": [{"scope_kind": "capability", "scope_ref": "mainline"}],
                "overlap_candidate_ids": [], "dedupe_status": "clear",
                "supersedes_work_id": None, "evidence_ids": [], "blocker_ids": [],
                "revision": 2, "return_point_work_id": None, "exit_criteria": [],
                "attempt_budget": None, "expires_at": None,
                "promotion_target_work_id": None, "mainline_authority": True,
            },
            {
                "work_id": experiment_spec["work_id"],
                "kind": "experiment",
                "title": experiment_spec["title"],
                "status": "active",
                "parent_work_id": "mainline-target",
                "dependency_ids": [],
                "owner_refs": ["actor-owner"],
                "scope_refs": [{"scope_kind": "capability", "scope_ref": "experiment"}],
                "overlap_candidate_ids": [], "dedupe_status": "clear",
                "supersedes_work_id": None, "evidence_ids": [], "blocker_ids": [],
                "revision": 3,
                "return_point_work_id": experiment_spec["return_point_work_id"],
                "exit_criteria": copy.deepcopy(experiment_spec["exit_criteria"]),
                "attempt_budget": experiment_spec["attempt_budget"],
                "expires_at": experiment_spec["expires_at"],
                "promotion_target_work_id": experiment_spec["promotion_target_work_id"],
                "mainline_authority": False,
            },
        ],
        "claims": [{
            "claim_id": "claim-experiment", "work_id": experiment_spec["work_id"],
            "actor_ref": "actor-owner", "status": "active",
            "expected_project_revision": 9,
            "claimed_at": "2026-08-14T07:30:00+08:00",
            "lease_expires_at": "2026-08-14T10:00:00+08:00",
            "released_at": None,
            "scope_owners": [{"scope_kind": "capability", "scope_ref": "experiment"}],
        }],
        "ideas": [], "decisions": [], "constraints": [], "evidence": evidence,
        "blockers": [], "effects": [], "experiment_attempts": [],
        "experiment_promotions": [],
    }
    validate_typed_state(snapshot)
    return snapshot


class _AllowAuthorizer:
    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return True


def _requests(snapshot: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    project_id = snapshot["project"]["project_id"]
    base = {"project_id": project_id, "causation_ref": "work:M3-05", "correlation_ref": "campaign:M3"}
    return (
        {"schema_version": "context.experiment-attempt-request/v1alpha1", "request_id": "benchmark-attempt", "expected_revision": 9, "attempt_id": "benchmark-attempt", "work_id": "experiment-throughput", "claim_id": "claim-experiment", **base},
        {"schema_version": "context.experiment-promotion-proposal-request/v1alpha1", "request_id": "benchmark-proposal", "expected_revision": 10, "work_id": "experiment-throughput", "expected_work_revision": 3, "expected_target_work_revision": 2, "attempt_id": "benchmark-attempt", "proposal_id": "benchmark-proposal", "criterion_evidence": {"throughput target": ["evidence-throughput"], "recovery target": ["evidence-recovery"]}, **base},
        {"schema_version": "context.experiment-promotion-approval-request/v1alpha1", "request_id": "benchmark-approval", "expected_revision": 11, "work_id": "experiment-throughput", "expected_work_revision": 3, "expected_target_work_revision": 2, "proposal_id": "benchmark-proposal", "approval_id": "benchmark-approval", **base},
    )


def run_experiment_lifecycle_benchmark(*, root: str | Path, samples: int, observed_at: str) -> dict[str, Any]:
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    root = Path(root)
    fixture = load_lifecycle_fixture(root)
    snapshot = build_lifecycle_snapshot(fixture)
    durations: list[float] = []
    successful_runs = 0
    for sample in range(samples):
        started = time.perf_counter_ns()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store, authorizer=_AllowAuthorizer(), registry_digest="a" * 64,
                clock=lambda: observed_at,
                event_id_factory=lambda request_id, sample=sample: f"event-{sample}-{request_id}",
            )
            contexts = (
                RequestContext("actor-owner", "authorization-executor"),
                RequestContext("actor-owner", "authorization-executor"),
                RequestContext("actor-verifier", "authorization-verifier"),
            )
            responses = [
                service.call_tool(tool, request, context=context)
                for tool, request, context in zip(
                    ("context.experiment.attempt", "context.experiment.promotion.propose", "context.experiment.promotion.approve"),
                    _requests(snapshot), contexts,
                )
            ]
            final = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])
            if not all(response["ok"] for response in responses) or final["project"]["revision"] != 12 or len(events) != 3 or len(final["experiment_attempts"]) != 1 or len(final["experiment_promotions"]) != 2:
                raise ValueError("M3-05 lifecycle benchmark run failed acceptance")
            successful_runs += 1
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(durations)
    p95 = ordered[max(0, int(samples * 0.95) - 1)]
    return {
        "schema_version": "context.experiment-lifecycle-benchmark/v1alpha1",
        "observed_at": observed_at,
        "provenance": {field: _sha256(root / relative) for field, relative in _PROVENANCE_PATHS.items()},
        "environment": {"python_implementation": platform.python_implementation(), "python_version": platform.python_version(), "platform": platform.system(), "machine": platform.machine(), "external_services": 0},
        "measurement": {"samples": samples, "successful_runs": successful_runs, "p50_total_ms": round(statistics.median(durations), 6), "p95_total_ms": round(p95, 6), "max_total_ms": round(max(durations), 6), "events_per_run": 3, "attempts_per_run": 1, "promotion_records_per_run": 2},
        "acceptance": {"all_runs_succeeded": successful_runs == samples, "zero_external_services": True, "event_count_exact": True, "lifecycle_projection_exact": True},
    }


def validate_experiment_lifecycle_benchmark_receipt(receipt: dict[str, Any], *, root: str | Path) -> None:
    expected = {"schema_version", "observed_at", "provenance", "environment", "measurement", "acceptance"}
    if not isinstance(receipt, dict) or set(receipt) != expected or receipt["schema_version"] != "context.experiment-lifecycle-benchmark/v1alpha1":
        raise ValueError("M3-05 benchmark receipt fields are invalid")
    expected_provenance = {field: _sha256(Path(root) / relative) for field, relative in _PROVENANCE_PATHS.items()}
    if receipt["provenance"] != expected_provenance:
        raise ValueError("M3-05 benchmark receipt provenance mismatch")
    measurement = receipt["measurement"]
    if measurement["samples"] <= 0 or measurement["successful_runs"] != measurement["samples"] or measurement["events_per_run"] != 3 or measurement["attempts_per_run"] != 1 or measurement["promotion_records_per_run"] != 2:
        raise ValueError("M3-05 benchmark measurement failed acceptance")
    if receipt["environment"].get("external_services") != 0 or not all(receipt["acceptance"].values()):
        raise ValueError("M3-05 benchmark acceptance failed")
