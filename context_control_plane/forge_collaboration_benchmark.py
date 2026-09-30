"""Deterministic M8-08 forge collaboration acceptance benchmark."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .forge_collaboration import (
    ForgeConflictError,
    build_ref_update_intent,
    project_forge_snapshot,
    project_unpublished_work,
    replay_forge_snapshot,
)

BENCHMARK_SCHEMA_VERSION = "context.forge-collaboration-benchmark/v1alpha1"
_PROVIDERS = ("github", "gitea")


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


def _snapshot(provider: str, index: int) -> dict[str, Any]:
    issue_number_field = "number" if provider == "github" else "index"
    issue_link_field = "issue_number" if provider == "github" else "issue_index"
    checks_field = "checks" if provider == "github" else "statuses"
    check = (
        {"name": "unit", "conclusion": "success"}
        if provider == "github"
        else {"context": "unit", "state": "success"}
    )
    branch = f"feat/forge-{index}"
    issue = {
        issue_number_field: index + 1,
        "title": f"Forge work {index}",
        "state": "open",
        "assignees": [{"login": "executor"}],
    }
    pull_request = {
        issue_number_field: index + 1001,
        issue_link_field: index + 1,
        "state": "open",
        "head": {"ref": branch, "sha": "a" * 40},
        "assignees": [{"login": "executor"}],
        "requested_reviewers": [{"login": "verifier"}],
        "reviews": [{"user": {"login": "verifier"}, "state": "approved"}],
        checks_field: [check],
    }
    return {
        "provider": provider,
        "instance_id": f"{provider}.example",
        "repository": {"owner": "example", "name": "relay"},
        "source_revision": f"{provider}-snapshot-{index}",
        "observed_at": "2026-08-17T00:00:00Z",
        "issues": [issue],
        "pull_requests": [pull_request],
        "refs": {f"refs/heads/{branch}": "a" * 40},
    }


def _complete_visible_mapping(projection: Mapping[str, Any]) -> bool:
    works = projection.get("works")
    if not isinstance(works, list) or len(works) != 1:
        return False
    work = works[0]
    if not isinstance(work, Mapping):
        return False
    evidence = work.get("evidence")
    evidence_kinds = {
        item.get("kind") for item in evidence if isinstance(item, Mapping)
    } if isinstance(evidence, list) else set()
    claim = work.get("candidate_claim")
    return bool(
        work.get("source_kind") == "issue-backed"
        and work.get("readiness") == "verifying"
        and isinstance(work.get("branch_ref"), str)
        and isinstance(claim, Mapping)
        and claim.get("visibility") == "published"
        and claim.get("actor_refs")
        == [
            f"actor://{projection.get('provider')}/"
            f"{projection.get('instance_id')}/executor"
        ]
        and claim.get("claim_uniqueness") == "not-guaranteed"
        and work.get("reviewer_refs")
        == [
            f"actor://{projection.get('provider')}/"
            f"{projection.get('instance_id')}/verifier"
        ]
        and evidence_kinds == {"review", "ci"}
    )


def _authority_escalated(document: Mapping[str, Any]) -> bool:
    authority = document.get("authority")
    if not isinstance(authority, Mapping):
        return True
    for key, value in authority.items():
        if key.endswith("authority") and value is not False:
            return True
        if key in {"provider_invocations", "external_services"} and value != 0:
            return True
    return False


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _provenance(root: Path) -> dict[str, str]:
    schema_dir = root / "schemas" / "m8-08"
    return {
        "implementation_sha256": _file_digest(
            root / "context_control_plane/forge_collaboration.py"
        ),
        "benchmark_sha256": _file_digest(
            root / "context_control_plane/forge_collaboration_benchmark.py"
        ),
        "projection_schema_sha256": _file_digest(
            schema_dir / "forge-work-projection.schema.json"
        ),
        "ref_intent_schema_sha256": _file_digest(
            schema_dir / "forge-ref-update-intent.schema.json"
        ),
        "unpublished_schema_sha256": _file_digest(
            schema_dir / "forge-unpublished-work.schema.json"
        ),
    }


def _failed_gates(
    rates: Mapping[str, float],
    thresholds: Mapping[str, float | int],
    *,
    authority_escalations: int,
    latency_p95_ms: float,
) -> list[str]:
    return [
        gate
        for gate, passed in {
            "visible-record-mapping": rates["visible_record_mapping_rate"]
            >= thresholds["visible_record_mapping_rate_min"],
            "deterministic-replay": rates["deterministic_replay_rate"]
            >= thresholds["deterministic_replay_rate_min"],
            "stale-ref-rejection": rates["stale_ref_rejection_rate"]
            >= thresholds["stale_ref_rejection_rate_min"],
            "unpublished-downgrade": rates["unpublished_downgrade_rate"]
            >= thresholds["unpublished_downgrade_rate_min"],
            "authority": authority_escalations
            <= thresholds["authority_escalations_max"],
            "latency": latency_p95_ms <= thresholds["latency_p95_ms_max"],
        }.items()
        if not passed
    ]


def benchmark_forge_collaboration(
    *,
    root: Path,
    samples: int = 1000,
    generated_at: str = "2026-08-17T00:00:00Z",
) -> dict[str, Any]:
    """Measure dual-adapter replay and conflict behavior without remote calls."""
    if type(samples) is not int or samples < 1:
        raise ValueError("samples must be positive")

    projection_attempts = complete_mappings = 0
    replay_attempts = deterministic_replays = 0
    ref_update_intents = 0
    stale_ref_attempts = stale_ref_rejections = 0
    unpublished_attempts = unpublished_downgrades = 0
    authority_escalations = 0
    latencies: list[float] = []

    for index in range(samples):
        started = time.perf_counter()
        for provider in _PROVIDERS:
            snapshot = _snapshot(provider, index)
            projection = project_forge_snapshot(snapshot)
            projection_attempts += 1
            complete_mappings += int(_complete_visible_mapping(projection))
            authority_escalations += int(_authority_escalated(projection))

            replay = replay_forge_snapshot(
                snapshot,
                expected_projection_sha256=projection["projection_sha256"],
            )
            replay_attempts += 1
            deterministic_replays += int(replay == projection)

            branch_ref = next(iter(projection["refs"]))
            intent = build_ref_update_intent(
                projection,
                branch_ref=branch_ref,
                expected_remote_oid="a" * 40,
                desired_oid="c" * 40,
            )
            ref_update_intents += 1
            authority_escalations += int(_authority_escalated(intent))

            stale_ref_attempts += 1
            try:
                build_ref_update_intent(
                    projection,
                    branch_ref=branch_ref,
                    expected_remote_oid="b" * 40,
                    desired_oid="c" * 40,
                )
            except ForgeConflictError:
                stale_ref_rejections += 1

        unpublished = project_unpublished_work(
            {
                "work_id": f"local-forge-{index}",
                "actor_ref": "actor://executor",
                "local_ref": f"worktree://executor/forge-{index}",
                "observed_at": generated_at,
            }
        )
        unpublished_attempts += 1
        unpublished_downgrades += int(
            unpublished["visibility"] == "local-only"
            and unpublished["claim_uniqueness"] == "not-guaranteed"
            and unpublished["required_resolution"] == "publish-or-state-mcp"
        )
        authority_escalations += int(_authority_escalated(unpublished))
        latencies.append((time.perf_counter() - started) * 1000.0)

    ordered = sorted(latencies)
    p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    latency = {
        "p50_ms": float(statistics.median(ordered)),
        "p95_ms": float(ordered[p95_index]),
        "max_ms": float(max(ordered)),
    }
    rates = {
        "visible_record_mapping_rate": _rate(complete_mappings, projection_attempts),
        "deterministic_replay_rate": _rate(deterministic_replays, replay_attempts),
        "stale_ref_rejection_rate": _rate(stale_ref_rejections, stale_ref_attempts),
        "unpublished_downgrade_rate": _rate(
            unpublished_downgrades, unpublished_attempts
        ),
    }
    thresholds = {
        "visible_record_mapping_rate_min": 1.0,
        "deterministic_replay_rate_min": 1.0,
        "stale_ref_rejection_rate_min": 1.0,
        "unpublished_downgrade_rate_min": 1.0,
        "authority_escalations_max": 0,
        "latency_p95_ms_max": 50.0,
    }
    failed_gates = _failed_gates(
        rates,
        thresholds,
        authority_escalations=authority_escalations,
        latency_p95_ms=latency["p95_ms"],
    )
    receipt = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": f"benchmark-m8-08-{_digest({'generated_at': generated_at, 'samples': samples})[:24]}",
        "generated_at": generated_at,
        "samples": samples,
        "providers": list(_PROVIDERS),
        "projection_attempts": projection_attempts,
        "complete_visible_record_mappings": complete_mappings,
        "replay_attempts": replay_attempts,
        "deterministic_replays": deterministic_replays,
        "ref_update_intents": ref_update_intents,
        "stale_ref_attempts": stale_ref_attempts,
        "stale_ref_rejections": stale_ref_rejections,
        "unpublished_work_attempts": unpublished_attempts,
        "explicit_unpublished_downgrades": unpublished_downgrades,
        "rates": rates,
        "latency_ms": latency,
        "thresholds": thresholds,
        "authority_escalations": authority_escalations,
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


def validate_forge_collaboration_benchmark(
    receipt: Mapping[str, Any], *, root: Path
) -> None:
    """Validate receipt integrity, derived metrics, and local provenance."""
    document = dict(receipt)
    receipt_sha256 = document.pop("receipt_sha256", None)
    if receipt_sha256 != _digest(document):
        raise ValueError("receipt_sha256 mismatch")
    if document.get("schema_version") != BENCHMARK_SCHEMA_VERSION:
        raise ValueError("benchmark schema mismatch")

    samples = document.get("samples")
    expected_counts = {
        "projection_attempts": samples * 2,
        "replay_attempts": samples * 2,
        "ref_update_intents": samples * 2,
        "stale_ref_attempts": samples * 2,
        "unpublished_work_attempts": samples,
    }
    if document.get("providers") != list(_PROVIDERS):
        raise ValueError("provider coverage mismatch")
    for field, expected in expected_counts.items():
        if document.get(field) != expected:
            raise ValueError(f"{field} coverage mismatch")

    relationships = (
        ("complete_visible_record_mappings", "projection_attempts"),
        ("deterministic_replays", "replay_attempts"),
        ("stale_ref_rejections", "stale_ref_attempts"),
        ("explicit_unpublished_downgrades", "unpublished_work_attempts"),
    )
    for numerator, denominator in relationships:
        if document.get(numerator) != document.get(denominator):
            raise ValueError(f"{numerator} mapping gate failed")
    if document.get("authority_escalations") != 0:
        raise ValueError("authority escalation detected")
    if document.get("provider_invocations") != 0 or document.get("external_services") != 0:
        raise ValueError("offline benchmark boundary violated")
    counts = (
        ("visible_record_mapping_rate", "complete_visible_record_mappings", "projection_attempts"),
        ("deterministic_replay_rate", "deterministic_replays", "replay_attempts"),
        ("stale_ref_rejection_rate", "stale_ref_rejections", "stale_ref_attempts"),
        ("unpublished_downgrade_rate", "explicit_unpublished_downgrades", "unpublished_work_attempts"),
    )
    rates = document.get("rates", {})
    for rate_field, numerator, denominator in counts:
        expected_rate = _rate(document[numerator], document[denominator])
        if rates.get(rate_field) != expected_rate:
            raise ValueError(f"{rate_field} derived rate mismatch")

    expected_thresholds = {
        "visible_record_mapping_rate_min": 1.0,
        "deterministic_replay_rate_min": 1.0,
        "stale_ref_rejection_rate_min": 1.0,
        "unpublished_downgrade_rate_min": 1.0,
        "authority_escalations_max": 0,
        "latency_p95_ms_max": 50.0,
    }
    if document.get("thresholds") != expected_thresholds:
        raise ValueError("benchmark thresholds mismatch")
    latency = document.get("latency_ms", {})
    if not (
        0 < latency.get("p50_ms", 0)
        <= latency.get("p95_ms", 0)
        <= latency.get("max_ms", 0)
    ):
        raise ValueError("latency ordering mismatch")
    failed_gates = _failed_gates(
        rates,
        expected_thresholds,
        authority_escalations=document["authority_escalations"],
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
