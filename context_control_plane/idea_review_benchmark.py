"""Local-embedded M3-07 Idea review, dedupe, and protection benchmark."""

from __future__ import annotations

import hashlib
import platform
import statistics
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .idea_continuity_benchmark import (
    _AllowAuthorizer,
    build_idea_snapshot,
    execution_authority_fingerprint,
)
from .idea_review import (
    IdeaReviewError,
    apply_idea_review,
    migrate_typed_state_v3_to_v4,
    packet_eligible_ideas,
    release_correction_protection,
)
from .sqlite_state_store import SQLiteStateStore
from .state_mcp import RequestContext, StateMCPService

_PROVENANCE_SOURCES = {
    "implementation": "context_control_plane/idea_review.py",
    "state_mcp": "context_control_plane/state_mcp.py",
    "state_events": "context_control_plane/state_events.py",
    "route_apply": "context_control_plane/route_apply.py",
    "effect_scope_gate": "context_control_plane/effect_scope_gate.py",
    "typed_state": "context_control_plane/typed_state.py",
    "sqlite_state_store": "context_control_plane/sqlite_state_store.py",
    "benchmark": "context_control_plane/idea_review_benchmark.py",
    "runner": "tools/run_idea_review_benchmark.py",
    "registry": "schemas/registry.yaml",
    "typed_state_schema": "schemas/m3-07/typed-state-v4alpha1.schema.json",
    "idea_event_schema": "schemas/m3-07/idea-event-v2alpha1.schema.json",
    "idea_capture_schema": "schemas/m3-07/idea-capture-v2alpha1.schema.json",
    "idea_review_schema": "schemas/m3-07/idea-review-request.schema.json",
    "protection_schema": "schemas/m3-07/idea-correction-protection-request.schema.json",
    "release_schema": "schemas/m3-07/idea-correction-release-request.schema.json",
    "benchmark_schema": "schemas/m3-07/idea-review-benchmark.schema.json",
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capture_request(sample: int, occurrence: int, revision: int) -> dict[str, Any]:
    return {
        "schema_version": "context.idea-capture-request/v2alpha1",
        "request_id": f"m307-{sample}-capture-{occurrence}",
        "project_id": f"project-m307-benchmark-{sample}",
        "expected_revision": revision,
        "idea_id": f"idea-m307-{sample}-{occurrence}",
        "parent_work_id": "work-active",
        "return_work_id": "work-active",
        "source_ref": (
            "rng_abcdefghijklmnopqrstuvwxyz"
            if occurrence == 1
            else "rng_bcdefghijklmnopqrstuvwxyza"
        ),
        "summary": "Deduplicate this bounded candidate before later review.",
        "scope_ref": "work://work-active",
        "urgency": "later",
        "review_at": None,
        "occurrence_id": f"occ-m307-{sample}-{occurrence}",
        "action": "capture-and-continue",
        "switch_target_work_id": None,
        "expiry": None,
        "causation_ref": "work:M3-07",
        "correlation_ref": "campaign:M3",
    }


def run_idea_review_benchmark(
    *, root: str | Path, samples: int, observed_at: str
) -> dict[str, Any]:
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    root = Path(root)
    durations: list[float] = []
    dedupe_converged = 0
    occurrences_retained = 0
    packet_excluded = 0
    unauthorized_write_count = 0
    terminal_revival_count = 0
    unverified_release_count = 0

    for sample in range(samples):
        snapshot = migrate_typed_state_v3_to_v4(
            build_idea_snapshot(), migrated_at=observed_at
        )
        snapshot["project"]["project_id"] = f"project-m307-benchmark-{sample}"
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        snapshot["claims"][0]["lease_expires_at"] = (
            observed + timedelta(hours=1)
        ).isoformat()
        authority_before = execution_authority_fingerprint(snapshot)
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
            context = RequestContext("actor-owner", "authorization-owner")
            first = service.call_tool(
                "context.idea.capture",
                _capture_request(sample, 1, 9),
                context=context,
            )
            second = service.call_tool(
                "context.idea.capture",
                _capture_request(sample, 2, 10),
                context=context,
            )
            canonical_idea_id = (
                second["result"]["canonical_idea_id"] if second["ok"] else "missing"
            )
            reviewed = service.call_tool(
                "context.idea.review",
                {
                    "schema_version": "context.idea-review-request/v1alpha1",
                    "request_id": f"m307-{sample}-review",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 11,
                    "idea_id": canonical_idea_id,
                    "review_id": f"review-m307-{sample}",
                    "decision": "park",
                    "urgency": "later",
                    "impact": "medium",
                    "review_at": None,
                    "evidence_ids": [],
                    "causation_ref": "work:M3-07",
                    "correlation_ref": "campaign:M3",
                },
                context=context,
            )
            protected = service.call_tool(
                "context.idea.correction.protect",
                {
                    "schema_version": "context.idea-correction-protection-request/v1alpha1",
                    "request_id": f"m307-{sample}-protect",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 12,
                    "idea_id": canonical_idea_id,
                    "protection_id": f"protection-m307-{sample}",
                    "affected_work_ids": ["work-active"],
                    "affected_scope_refs": ["capability:idea/active"],
                    "reason": "Benchmark protected write gate.",
                    "evidence_ids": [],
                    "causation_ref": "work:M3-07",
                    "correlation_ref": "campaign:M3",
                },
                context=context,
            )
            current = store.read_project(snapshot["project"]["project_id"])
            work = next(item for item in current["works"] if item["work_id"] == "work-active")
            denied_commit = service.call_tool(
                "context.state.commit",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": f"m307-{sample}-denied-commit",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 13,
                    "causation_ref": "work:M3-07",
                    "correlation_ref": "campaign:M3",
                    "supersedes_event_id": None,
                    "changes": [
                        {
                            "collection": "works",
                            "object_id": "work-active",
                            "value": {**work, "title": "Unauthorized mutation"},
                        }
                    ],
                },
                context=context,
            )
            denied_effect = service.call_tool(
                "context.state.effect.gate",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": f"m307-{sample}-denied-effect",
                    "project_id": snapshot["project"]["project_id"],
                    "expected_revision": 13,
                    "effect_id": f"effect-m307-{sample}",
                    "work_id": "work-active",
                    "claim_id": "claim-active",
                    "operation": "record-correction",
                    "scope_ref": {
                        "scope_kind": "capability",
                        "scope_ref": "idea/active",
                    },
                },
                context=context,
            )
            stored = store.read_project(snapshot["project"]["project_id"])
            events = store.read_events(snapshot["project"]["project_id"])

        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        if len(stored["ideas"]) == 1:
            dedupe_converged += 1
        if len(stored["idea_occurrences"]) == 2:
            occurrences_retained += 1
        if packet_eligible_ideas(stored, now=observed_at) == []:
            packet_excluded += 1
        if denied_commit["ok"]:
            unauthorized_write_count += 1
        if (
            not denied_effect["ok"]
            or denied_effect["result"]["verdict"]["decision"] != "deny"
        ):
            unauthorized_write_count += 1
        terminal = {**stored, "ideas": [dict(stored["ideas"][0], status="rejected")]}
        try:
            apply_idea_review(
                terminal,
                idea_id=canonical_idea_id,
                review_id=f"review-revive-{sample}",
                reviewer_ref="actor-owner",
                decision="approve",
                urgency="next",
                impact="high",
                review_at=None,
                evidence_ids=[],
                reviewed_at=observed_at,
            )
        except IdeaReviewError:
            pass
        else:
            terminal_revival_count += 1
        try:
            release_correction_protection(
                stored,
                protection_id=f"protection-m307-{sample}",
                idea_id=canonical_idea_id,
                released_by_ref="actor-verifier",
                release_reason="Unverified release must fail.",
                release_evidence_ids=[],
                released_at=observed_at,
            )
        except IdeaReviewError:
            pass
        else:
            unverified_release_count += 1
        if (
            not all(response["ok"] for response in (first, second, reviewed, protected))
            or len(events) != 4
            or execution_authority_fingerprint(stored) != authority_before
        ):
            raise ValueError("M3-07 benchmark run failed execution authority acceptance")

    ordered = sorted(durations)
    measurement = {
        "samples": samples,
        "successful_runs": samples,
        "p50_total_ms": round(statistics.median(durations), 6),
        "p95_total_ms": round(ordered[max(0, int(samples * 0.95) - 1)], 6),
        "max_total_ms": round(max(durations), 6),
        "events_per_run": 4,
        "dedupe_convergence_rate_millionths": dedupe_converged * 1_000_000 // samples,
        "occurrence_retention_rate_millionths": occurrences_retained * 1_000_000 // samples,
        "packet_exclusion_rate_millionths": packet_excluded * 1_000_000 // samples,
        "unauthorized_write_count": unauthorized_write_count,
        "terminal_revival_count": terminal_revival_count,
        "unverified_release_count": unverified_release_count,
    }
    return {
        "schema_version": "context.idea-review-benchmark/v1alpha1",
        "observed_at": observed_at,
        "provenance": {
            f"{name}_sha256": _sha256(root / relative)
            for name, relative in _PROVENANCE_SOURCES.items()
        },
        "environment": {
            "python_implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "platform": platform.system(),
            "machine": platform.machine(),
            "external_services": 0,
        },
        "measurement": measurement,
        "acceptance": {
            "all_runs_succeeded": True,
            "dedupe_deterministic": measurement["dedupe_convergence_rate_millionths"] == 1_000_000,
            "occurrences_retained": measurement["occurrence_retention_rate_millionths"] == 1_000_000,
            "protected_writes_denied": unauthorized_write_count == 0,
            "packet_exclusion_complete": measurement["packet_exclusion_rate_millionths"] == 1_000_000,
            "terminal_ideas_not_revived": terminal_revival_count == 0,
            "unverified_release_denied": unverified_release_count == 0,
            "zero_external_services": True,
        },
    }


def validate_idea_review_benchmark_receipt(
    receipt: dict[str, Any], *, root: str | Path
) -> None:
    if (
        not isinstance(receipt, dict)
        or set(receipt)
        != {"schema_version", "observed_at", "provenance", "environment", "measurement", "acceptance"}
        or receipt["schema_version"] != "context.idea-review-benchmark/v1alpha1"
    ):
        raise ValueError("M3-07 benchmark receipt fields are invalid")
    expected_provenance = {
        f"{name}_sha256": _sha256(Path(root) / relative)
        for name, relative in _PROVENANCE_SOURCES.items()
    }
    if receipt["provenance"] != expected_provenance:
        raise ValueError("M3-07 benchmark receipt provenance mismatch")
    measurement = receipt["measurement"]
    if (
        measurement.get("samples", 0) <= 0
        or measurement.get("successful_runs") != measurement["samples"]
        or measurement.get("events_per_run") != 4
        or measurement.get("dedupe_convergence_rate_millionths") != 1_000_000
        or measurement.get("occurrence_retention_rate_millionths") != 1_000_000
        or measurement.get("packet_exclusion_rate_millionths") != 1_000_000
        or measurement.get("unauthorized_write_count") != 0
        or measurement.get("terminal_revival_count") != 0
        or measurement.get("unverified_release_count") != 0
        or receipt["environment"].get("external_services") != 0
        or not all(receipt["acceptance"].values())
    ):
        raise ValueError("M3-07 benchmark acceptance failed")
