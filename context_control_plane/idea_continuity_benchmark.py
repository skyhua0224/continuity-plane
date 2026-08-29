"""Zero-service benchmark for M3-06 candidate Idea capture latency."""

from __future__ import annotations

import hashlib
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path
from typing import Any

from .sqlite_state_store import SQLiteStateStore
from .state_mcp import RequestContext, StateMCPService
from .typed_state import validate_typed_state

_PROVENANCE_SOURCES = {
    "implementation": "context_control_plane/idea_continuity.py",
    "state_mcp": "context_control_plane/state_mcp.py",
    "state_events": "context_control_plane/state_events.py",
    "typed_state": "context_control_plane/typed_state.py",
    "sqlite_state_store": "context_control_plane/sqlite_state_store.py",
    "benchmark": "context_control_plane/idea_continuity_benchmark.py",
    "runner": "tools/run_idea_continuity_benchmark.py",
    "registry": "schemas/registry.yaml",
    "idea_capture_schema": "schemas/m3-06/idea-capture.schema.json",
    "idea_event_schema": "schemas/m3-06/idea-event-v1alpha1.schema.json",
    "benchmark_schema": "schemas/m3-06/idea-continuity-benchmark.schema.json",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_idea_snapshot() -> dict[str, Any]:
    def work(work_id: str, kind: str, parent: str | None, status: str) -> dict[str, Any]:
        return {
            "work_id": work_id,
            "kind": kind,
            "title": work_id,
            "status": status,
            "parent_work_id": parent,
            "dependency_ids": [],
            "owner_refs": ["actor-owner"],
            "scope_refs": [
                {
                    "scope_kind": "capability",
                    "scope_ref": "idea/active" if work_id == "work-active" else work_id,
                }
            ],
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [],
            "blocker_ids": [],
            "revision": 1,
            "return_point_work_id": None,
            "exit_criteria": [],
            "attempt_budget": None,
            "expires_at": None,
            "promotion_target_work_id": None,
            "mainline_authority": True,
        }

    snapshot = {
        "schema_version": "context.typed-state/v3alpha1",
        "project": {
            "project_id": "project-idea-benchmark",
            "revision": 9,
            "governance_ref": "artifact://governance/m306-benchmark",
            "active_work_ids": ["work-active"],
            "primary_work_id": "work-active",
            "current_decision_ids": [],
            "active_constraint_ids": [],
            "open_blocker_ids": [],
            "effect_high_watermark": 0,
            "updated_at": "2026-08-14T08:00:00+08:00",
        },
        "works": [
            work("campaign", "campaign", None, "ready"),
            work("goal", "goal", "campaign", "ready"),
            work("work-active", "work", "goal", "active"),
            work("work-target", "work", "goal", "ready"),
        ],
        "claims": [
            {
                "claim_id": "claim-active",
                "work_id": "work-active",
                "actor_ref": "actor-owner",
                "status": "active",
                "expected_project_revision": 9,
                "claimed_at": "2026-08-14T07:30:00+08:00",
                    "lease_expires_at": "2026-08-14T12:00:00+08:00",
                "released_at": None,
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "idea/active"}
                ],
            }
        ],
        "ideas": [],
        "decisions": [],
        "constraints": [],
        "evidence": [],
        "blockers": [],
        "effects": [],
        "experiment_attempts": [],
        "experiment_promotions": [],
    }
    validate_typed_state(snapshot)
    return snapshot


class _AllowAuthorizer:
    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return True


def execution_authority_fingerprint(snapshot: dict[str, Any]) -> str:
    """Hash all state that Idea capture is not authorized to change."""
    authority = {
        "schema_version": snapshot["schema_version"],
        "project": {
            key: value
            for key, value in snapshot["project"].items()
            if key not in {"revision", "updated_at"}
        },
        "works": snapshot["works"],
        "claims": [
            {
                key: value
                for key, value in claim.items()
                if key != "expected_project_revision"
            }
            for claim in snapshot["claims"]
        ],
        "decisions": snapshot["decisions"],
        "constraints": snapshot["constraints"],
        "evidence": snapshot["evidence"],
        "blockers": snapshot["blockers"],
        "effects": snapshot["effects"],
        "experiment_attempts": snapshot["experiment_attempts"],
        "experiment_promotions": snapshot["experiment_promotions"],
    }
    payload = json.dumps(
        authority,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def run_idea_continuity_benchmark(
    *, root: str | Path, samples: int, observed_at: str
) -> dict[str, Any]:
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    root = Path(root)
    durations: list[float] = []
    for sample in range(samples):
        snapshot = build_idea_snapshot()
        authority_before = execution_authority_fingerprint(snapshot)
        request = {
            "schema_version": "context.idea-capture-request/v1alpha1",
            "request_id": f"idea-benchmark-{sample}",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": 9,
            "idea_id": f"idea-benchmark-{sample}",
            "parent_work_id": "work-active",
            "return_work_id": "work-active",
            "source_ref": "rng_abcdefghijklmnopqrstuvwxyz",
            "summary": "Review the captured candidate later.",
            "action": "capture-and-continue",
            "switch_target_work_id": None,
            "expiry": None,
            "causation_ref": "work:M3-06",
            "correlation_ref": "campaign:M3",
        }
        started = time.perf_counter_ns()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(snapshot)
            service = StateMCPService(
                store,
                authorizer=_AllowAuthorizer(),
                registry_digest="a" * 64,
                clock=lambda: observed_at,
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            response = service.call_tool(
                "context.idea.capture",
                request,
                context=RequestContext("actor-owner", "authorization-owner"),
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])
        authority_after = execution_authority_fingerprint(stored)
        if (
            not response["ok"]
            or authority_after != authority_before
            or stored["project"]["primary_work_id"] != "work-active"
            or stored["project"]["active_work_ids"] != ["work-active"]
            or len(stored["ideas"]) != 1
            or stored["ideas"][0]["status"] != "candidate"
            or len(events) != 1
        ):
            raise ValueError(
                "M3-06 Idea benchmark run failed execution authority acceptance"
            )
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
    ordered = sorted(durations)
    return {
        "schema_version": "context.idea-continuity-benchmark/v1alpha1",
        "observed_at": observed_at,
        "provenance": {
            f"{source}_sha256": _sha256(root / relative)
            for source, relative in _PROVENANCE_SOURCES.items()
        },
        "environment": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "machine": platform.machine(),
            "external_services": 0,
        },
        "measurement": {
            "samples": samples,
            "successful_runs": samples,
            "p50_total_ms": round(statistics.median(durations), 6),
            "p95_total_ms": round(ordered[max(0, int(samples * 0.95) - 1)], 6),
            "max_total_ms": round(max(durations), 6),
            "events_per_run": 1,
            "ideas_per_run": 1,
        },
        "acceptance": {
            "all_runs_succeeded": True,
            "active_execution_authority_preserved": True,
            "candidate_only": True,
            "zero_external_services": True,
        },
    }


def validate_idea_continuity_benchmark_receipt(
    receipt: dict[str, Any], *, root: str | Path
) -> None:
    expected = {
        "schema_version",
        "observed_at",
        "provenance",
        "environment",
        "measurement",
        "acceptance",
    }
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected
        or receipt["schema_version"] != "context.idea-continuity-benchmark/v1alpha1"
    ):
        raise ValueError("M3-06 Idea benchmark receipt fields are invalid")
    expected_provenance = {
        f"{source}_sha256": _sha256(Path(root) / relative)
        for source, relative in _PROVENANCE_SOURCES.items()
    }
    if receipt["provenance"] != expected_provenance:
        raise ValueError("M3-06 Idea benchmark receipt provenance mismatch")
    measurement = receipt["measurement"]
    if (
        measurement["samples"] <= 0
        or measurement["successful_runs"] != measurement["samples"]
        or measurement["events_per_run"] != 1
        or measurement["ideas_per_run"] != 1
    ):
        raise ValueError("M3-06 Idea benchmark measurement failed acceptance")
    if receipt["environment"].get("external_services") != 0 or not all(
        receipt["acceptance"].values()
    ):
        raise ValueError("M3-06 Idea benchmark acceptance failed")
