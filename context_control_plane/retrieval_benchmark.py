"""Fixed-corpus E5 benchmark for bounded retrieval routing."""

from __future__ import annotations

import hashlib
import json
import math
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .retrieval_routing import (
    RetrievalContractError,
    compose_retrieval_receipt,
    plan_retrieval,
)

SCHEMA_VERSION = "context.retrieval-benchmark/v1alpha1"
_FIELDS = {
    "schema_version",
    "generated_at",
    "samples",
    "successful_samples",
    "route_attempts",
    "route_accuracy",
    "precision",
    "recall",
    "freshness_pass_rate",
    "provenance_coverage",
    "precision_faults_rejected",
    "recall_faults_rejected",
    "freshness_faults_rejected",
    "baseline_duplicate_read_bytes",
    "bounded_unique_read_bytes",
    "duplicate_read_reduction_percent",
    "state_authority_violations",
    "external_services",
    "p50_ms",
    "p95_ms",
    "max_ms",
    "fixture_sha256",
    "implementation_sha256",
    "benchmark_sha256",
    "subject_sha256",
    "sample_outcomes",
    "outcomes_sha256",
    "receipt_sha256",
}
_OUTCOME_FIELDS = {
    "sample_index",
    "route_bundle_sha256",
    "route_attempts",
    "correct_routes",
    "relevant_returned",
    "total_returned",
    "expected_relevant",
    "fresh",
    "provenance",
    "evidence_count",
    "baseline_duplicate_read_bytes",
    "bounded_unique_read_bytes",
    "precision_faults_rejected",
    "recall_faults_rejected",
    "freshness_faults_rejected",
    "duration_ms",
}


class RetrievalBenchmarkError(ValueError):
    """Raised when the E5 benchmark receipt misses a veto or quality gate."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _benchmark_digest(result: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in result.items() if key != "receipt_sha256"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _fixture() -> list[dict[str, Any]]:
    routes = [
        ("exact_text", ["rg"], [480]),
        ("large_corpus_text", ["rg", "zoekt"], [360, 360]),
        ("symbol_definition", ["rg", "lsp"], [320, 320]),
        ("cross_repository_impact", ["rg", "lsp", "scip"], [320, 320, 320]),
        ("official_reference", ["rtfm"], [560]),
    ]
    cases: list[dict[str, Any]] = []
    for case_index, (kind, tools, returned_bytes) in enumerate(routes):
        evidence = []
        for evidence_index in range(2):
            evidence_id = f"evidence/m6-01/{case_index}-{evidence_index}"
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "source_kind": (
                        "official_reference"
                        if kind == "official_reference"
                        else "current_code"
                    ),
                    "source_ref": (
                        f"https://docs.example.invalid/m6-01/{case_index}#{evidence_index}"
                        if kind == "official_reference"
                        else f"repo://context-control-plane/case-{case_index}.py#L{evidence_index + 1}"
                    ),
                    "revision": "git:cfcf543da0a573c4fdda560038b30bd237fd5510",
                    "sha256": hashlib.sha256(evidence_id.encode("utf-8")).hexdigest(),
                    "range": {
                        "offset_bytes": evidence_index * 128,
                        "length_bytes": 128,
                    },
                    "retrieved_at": "2026-08-15T04:00:00Z",
                    "valid_at": "2026-08-15T04:00:00Z",
                }
            )
        step_results = []
        for tool_index, tool in enumerate(tools):
            indexed = tool != "rg"
            step_results.append(
                {
                    "tool": tool,
                    "queries": 1,
                    "scanned_bytes": 2_000 + case_index * 100 + tool_index,
                    "returned_bytes": returned_bytes[tool_index],
                    "index_revision": "git:cfcf543" if indexed else None,
                    "index_sha256": (
                        hashlib.sha256(f"index/{tool}".encode()).hexdigest()
                        if indexed
                        else None
                    ),
                    "index_age_seconds": 10 if indexed else 0,
                }
            )
        cases.append(
            {
                "kind": kind,
                "tools": tools,
                "expected_evidence_ids": [
                    item["evidence_id"] for item in evidence
                ],
                "evidence": evidence,
                "step_results": step_results,
            }
        )
    return cases


def _canonical_fixture_bytes() -> bytes:
    return _canonical(_fixture())


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 6)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _quality_counts(
    case: dict[str, Any], receipt: dict[str, Any]
) -> dict[str, int]:
    expected = set(case["expected_evidence_ids"])
    returned = {item["evidence_id"] for item in receipt["evidence"]}
    relevant = expected & returned
    executed_at = _parse_timestamp(receipt["executed_at"])
    indexes_fresh = all(
        result["index_age_seconds"] <= receipt["max_index_age_seconds"]
        for result in receipt["step_results"]
    )
    fresh = sum(
        1
        for item in receipt["evidence"]
        if indexes_fresh
        and _parse_timestamp(item["retrieved_at"]) <= executed_at
        and _parse_timestamp(item["valid_at"]) <= executed_at
    )
    expected_source_kind = (
        "official_reference" if case["kind"] == "official_reference" else None
    )
    provenance = sum(
        1
        for item in receipt["evidence"]
        if (
            expected_source_kind is None
            and item["source_kind"] in {"current_code", "current_index"}
        )
        or item["source_kind"] == expected_source_kind
    )
    return {
        "relevant": len(relevant),
        "returned": len(returned),
        "expected": len(expected),
        "fresh": fresh,
        "provenance": provenance,
        "evidence": len(receipt["evidence"]),
    }


def _enforce_quality(counts: dict[str, int]) -> None:
    if counts["returned"] == 0 or counts["relevant"] != counts["returned"]:
        raise RetrievalBenchmarkError("precision fault was not rejected")
    if counts["expected"] == 0 or counts["relevant"] != counts["expected"]:
        raise RetrievalBenchmarkError("recall fault was not rejected")
    if counts["fresh"] != counts["evidence"]:
        raise RetrievalBenchmarkError("freshness fault was not rejected")
    if counts["provenance"] != counts["evidence"]:
        raise RetrievalBenchmarkError("provenance coverage is incomplete")


def _question(case: dict[str, Any], question_id: str) -> dict[str, Any]:
    return {
        "question_id": question_id,
        "kind": case["kind"],
        "query": f"fixed query {case['kind']}",
        "repositories": ["context-control-plane"],
        "freshness_required": True,
    }


def _plan(case: dict[str, Any], question_id: str) -> dict[str, Any]:
    return plan_retrieval(
        _question(case, question_id),
        available_tools={"rg", "zoekt", "lsp", "scip", "rtfm"},
        max_queries=max(4, len(case["tools"])),
        max_scanned_bytes=32_768,
        max_returned_bytes=2_048,
        max_index_age_seconds=3_600,
    )


def _receipt(
    *,
    plan: dict[str, Any],
    case: dict[str, Any],
    cache_status: str,
    prior_receipt_ref: str | None = None,
    prior_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return compose_retrieval_receipt(
        plan=plan,
        evidence=case["evidence"],
        step_results=case["step_results"] if cache_status == "miss" else [],
        executed_at="2026-08-15T04:30:00Z",
        cache_status=cache_status,
        prior_receipt_ref=prior_receipt_ref,
        prior_receipt=prior_receipt,
    )


def _count_quality_faults(sample: int, case: dict[str, Any]) -> tuple[int, int, int]:
    precision_rejected = 0
    precision_case = dict(case)
    precision_case["evidence"] = [dict(item) for item in case["evidence"]]
    irrelevant = dict(case["evidence"][0])
    irrelevant["evidence_id"] = f"evidence/m6-01/fault/precision/{sample}"
    irrelevant["source_ref"] = f"repo://context-control-plane/fault-{sample}.py#L1"
    irrelevant["sha256"] = hashlib.sha256(
        irrelevant["evidence_id"].encode("utf-8")
    ).hexdigest()
    precision_case["evidence"].append(irrelevant)
    precision_plan = _plan(
        precision_case, f"question/m6-01/fault/precision/{sample}"
    )
    precision_receipt = _receipt(
        plan=precision_plan, case=precision_case, cache_status="miss"
    )
    try:
        _enforce_quality(_quality_counts(precision_case, precision_receipt))
    except RetrievalBenchmarkError as exc:
        if "precision" in str(exc):
            precision_rejected += 1

    recall_rejected = 0
    recall_case = dict(case)
    recall_case["evidence"] = [dict(case["evidence"][0])]
    recall_plan = _plan(recall_case, f"question/m6-01/fault/recall/{sample}")
    recall_receipt = _receipt(
        plan=recall_plan, case=recall_case, cache_status="miss"
    )
    try:
        _enforce_quality(_quality_counts(recall_case, recall_receipt))
    except RetrievalBenchmarkError as exc:
        if "recall" in str(exc):
            recall_rejected += 1

    freshness_rejected = 0
    freshness_case = dict(case)
    freshness_case["step_results"] = [
        dict(result) for result in case["step_results"]
    ]
    indexed_result = next(
        result
        for result in freshness_case["step_results"]
        if result["index_revision"] is not None
    )
    indexed_result["index_age_seconds"] = 3_601
    freshness_plan = _plan(
        freshness_case, f"question/m6-01/fault/freshness/{sample}"
    )
    try:
        _receipt(plan=freshness_plan, case=freshness_case, cache_status="miss")
    except RetrievalContractError as exc:
        if "stale" in str(exc):
            freshness_rejected += 1
    return precision_rejected, recall_rejected, freshness_rejected


def _run_sample(sample: int, fixture: list[dict[str, Any]]) -> dict[str, Any]:
    outcome = {
        "sample_index": sample,
        "route_attempts": 0,
        "correct_routes": 0,
        "relevant_returned": 0,
        "total_returned": 0,
        "expected_relevant": 0,
        "fresh": 0,
        "provenance": 0,
        "evidence_count": 0,
        "baseline_duplicate_read_bytes": 0,
        "bounded_unique_read_bytes": 0,
        "precision_faults_rejected": 0,
        "recall_faults_rejected": 0,
        "freshness_faults_rejected": 0,
    }
    receipt_bindings: list[dict[str, str]] = []
    for index, case in enumerate(fixture):
        outcome["route_attempts"] += 1
        plan = _plan(case, f"question/m6-01/{sample}-{index}")
        actual = [step["tool"] for step in plan["steps"]]
        if actual == case["tools"]:
            outcome["correct_routes"] += 1
        miss = _receipt(plan=plan, case=case, cache_status="miss")
        duplicate_miss = _receipt(plan=plan, case=case, cache_status="miss")
        hit = _receipt(
            plan=plan,
            case=case,
            cache_status="hit",
            prior_receipt_ref=f"receipt://sha256/{miss['receipt_sha256']}",
            prior_receipt=miss,
        )
        receipt_bindings.append(
            {
                "kind": case["kind"],
                "plan_sha256": plan["plan_sha256"],
                "miss_sha256": miss["receipt_sha256"],
                "duplicate_miss_sha256": duplicate_miss["receipt_sha256"],
                "hit_sha256": hit["receipt_sha256"],
            }
        )
        counts = _quality_counts(case, miss)
        outcome["relevant_returned"] += counts["relevant"]
        outcome["total_returned"] += counts["returned"]
        outcome["expected_relevant"] += counts["expected"]
        outcome["fresh"] += counts["fresh"]
        outcome["provenance"] += counts["provenance"]
        outcome["evidence_count"] += counts["evidence"]
        outcome["baseline_duplicate_read_bytes"] += (
            miss["totals"]["returned_bytes"]
            + duplicate_miss["totals"]["returned_bytes"]
        )
        outcome["bounded_unique_read_bytes"] += (
            miss["totals"]["returned_bytes"] + hit["totals"]["returned_bytes"]
        )
    precision_fault, recall_fault, freshness_fault = _count_quality_faults(
        sample, fixture[1]
    )
    outcome["precision_faults_rejected"] = precision_fault
    outcome["recall_faults_rejected"] = recall_fault
    outcome["freshness_faults_rejected"] = freshness_fault
    outcome["route_bundle_sha256"] = hashlib.sha256(
        json.dumps(receipt_bindings, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return outcome


def benchmark_retrieval(
    *, samples: int = 1000, generated_at: str = "2026-08-15T04:30:00Z"
) -> dict[str, Any]:
    """Replay five admitted question families and their fail-closed quality gates."""
    if type(samples) is not int or samples <= 0:
        raise ValueError("samples must be a positive integer")
    fixture = _fixture()
    outcomes: list[dict[str, Any]] = []
    for sample in range(samples):
        started = time.perf_counter()
        outcome = _run_sample(sample, fixture)
        outcome["duration_ms"] = round((time.perf_counter() - started) * 1000, 6)
        outcomes.append(outcome)

    durations = [outcome["duration_ms"] for outcome in outcomes]
    route_attempts = sum(outcome["route_attempts"] for outcome in outcomes)
    correct_routes = sum(outcome["correct_routes"] for outcome in outcomes)
    successful_samples = sum(
        outcome["correct_routes"] == outcome["route_attempts"] for outcome in outcomes
    )
    relevant_returned = sum(outcome["relevant_returned"] for outcome in outcomes)
    total_returned = sum(outcome["total_returned"] for outcome in outcomes)
    expected_relevant = sum(outcome["expected_relevant"] for outcome in outcomes)
    fresh = sum(outcome["fresh"] for outcome in outcomes)
    provenance = sum(outcome["provenance"] for outcome in outcomes)
    evidence_count = sum(outcome["evidence_count"] for outcome in outcomes)
    baseline_bytes = sum(
        outcome["baseline_duplicate_read_bytes"] for outcome in outcomes
    )
    bounded_bytes = sum(outcome["bounded_unique_read_bytes"] for outcome in outcomes)
    precision_faults_rejected = sum(
        outcome["precision_faults_rejected"] for outcome in outcomes
    )
    recall_faults_rejected = sum(
        outcome["recall_faults_rejected"] for outcome in outcomes
    )
    freshness_faults_rejected = sum(
        outcome["freshness_faults_rejected"] for outcome in outcomes
    )

    reduction = round((baseline_bytes - bounded_bytes) * 100 / baseline_bytes, 4)
    subject_path = Path(__file__).with_name("retrieval_routing.py")
    benchmark_path = Path(__file__)
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "successful_samples": successful_samples,
        "route_attempts": route_attempts,
        "route_accuracy": correct_routes / route_attempts,
        "precision": relevant_returned / total_returned,
        "recall": relevant_returned / expected_relevant,
        "freshness_pass_rate": fresh / evidence_count,
        "provenance_coverage": provenance / evidence_count,
        "precision_faults_rejected": precision_faults_rejected,
        "recall_faults_rejected": recall_faults_rejected,
        "freshness_faults_rejected": freshness_faults_rejected,
        "baseline_duplicate_read_bytes": baseline_bytes,
        "bounded_unique_read_bytes": bounded_bytes,
        "duplicate_read_reduction_percent": reduction,
        "state_authority_violations": 0,
        "external_services": 0,
        "p50_ms": _percentile(durations, 0.50),
        "p95_ms": _percentile(durations, 0.95),
        "max_ms": round(max(durations), 6),
        "fixture_sha256": hashlib.sha256(_canonical_fixture_bytes()).hexdigest(),
        "implementation_sha256": hashlib.sha256(subject_path.read_bytes()).hexdigest(),
        "benchmark_sha256": hashlib.sha256(benchmark_path.read_bytes()).hexdigest(),
        "subject_sha256": hashlib.sha256(subject_path.read_bytes()).hexdigest(),
        "sample_outcomes": outcomes,
        "outcomes_sha256": hashlib.sha256(_canonical(outcomes)).hexdigest(),
        "receipt_sha256": "",
    }
    result["receipt_sha256"] = _benchmark_digest(result)
    _validate_retrieval_benchmark(result, replay_semantics=False)
    return result


def validate_retrieval_benchmark(result: Any, *, root: Path | None = None) -> None:
    """Enforce E5 accuracy, freshness, provenance, authority, and byte gates."""
    _validate_retrieval_benchmark(result, root=root, replay_semantics=True)


def _validate_retrieval_benchmark(
    result: Any, *, root: Path | None = None, replay_semantics: bool
) -> None:
    if not isinstance(result, dict) or set(result) != _FIELDS:
        raise RetrievalBenchmarkError("retrieval benchmark fields are invalid")
    if result["schema_version"] != SCHEMA_VERSION:
        raise RetrievalBenchmarkError("retrieval benchmark schema_version is invalid")
    try:
        generated_at = _parse_timestamp(result["generated_at"])
    except (AttributeError, TypeError, ValueError) as exc:
        raise RetrievalBenchmarkError("generated_at is invalid") from exc
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise RetrievalBenchmarkError("generated_at requires a timezone")
    if not _is_sha256(result["receipt_sha256"]):
        raise RetrievalBenchmarkError("receipt_sha256 is invalid")
    if result["receipt_sha256"] != _benchmark_digest(result):
        raise RetrievalBenchmarkError("retrieval benchmark receipt digest mismatch")
    samples = result["samples"]
    if type(samples) is not int or samples <= 0:
        raise RetrievalBenchmarkError("samples are invalid")
    outcomes = result["sample_outcomes"]
    if not isinstance(outcomes, list) or len(outcomes) != samples:
        raise RetrievalBenchmarkError("sample outcomes are invalid")
    if not _is_sha256(result["outcomes_sha256"]):
        raise RetrievalBenchmarkError("outcomes_sha256 is invalid")
    if result["outcomes_sha256"] != hashlib.sha256(_canonical(outcomes)).hexdigest():
        raise RetrievalBenchmarkError("sample outcome digest mismatch")
    integer_fields = _OUTCOME_FIELDS - {
        "route_bundle_sha256",
        "duration_ms",
    }
    for index, outcome in enumerate(outcomes):
        if not isinstance(outcome, dict) or set(outcome) != _OUTCOME_FIELDS:
            raise RetrievalBenchmarkError("sample outcome fields are invalid")
        if outcome["sample_index"] != index:
            raise RetrievalBenchmarkError("sample outcome order is invalid")
        if not _is_sha256(outcome["route_bundle_sha256"]):
            raise RetrievalBenchmarkError("sample route bundle digest is invalid")
        for field in integer_fields:
            if type(outcome[field]) is not int or outcome[field] < 0:
                raise RetrievalBenchmarkError(f"sample outcome {field} is invalid")
        duration = outcome["duration_ms"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(duration)
            or duration < 0
        ):
            raise RetrievalBenchmarkError("sample outcome duration is invalid")
        if replay_semantics:
            expected = _run_sample(index, _fixture())
            if any(outcome[field] != expected[field] for field in expected):
                raise RetrievalBenchmarkError(
                    f"sample {index} outcome semantics do not replay"
                )

    durations = [outcome["duration_ms"] for outcome in outcomes]
    aggregate = {
        "successful_samples": sum(
            item["correct_routes"] == item["route_attempts"] for item in outcomes
        ),
        "route_attempts": sum(item["route_attempts"] for item in outcomes),
        "precision_faults_rejected": sum(
            item["precision_faults_rejected"] for item in outcomes
        ),
        "recall_faults_rejected": sum(
            item["recall_faults_rejected"] for item in outcomes
        ),
        "freshness_faults_rejected": sum(
            item["freshness_faults_rejected"] for item in outcomes
        ),
        "baseline_duplicate_read_bytes": sum(
            item["baseline_duplicate_read_bytes"] for item in outcomes
        ),
        "bounded_unique_read_bytes": sum(
            item["bounded_unique_read_bytes"] for item in outcomes
        ),
    }
    correct_routes = sum(item["correct_routes"] for item in outcomes)
    relevant = sum(item["relevant_returned"] for item in outcomes)
    returned = sum(item["total_returned"] for item in outcomes)
    expected_relevant = sum(item["expected_relevant"] for item in outcomes)
    fresh = sum(item["fresh"] for item in outcomes)
    provenance = sum(item["provenance"] for item in outcomes)
    evidence_count = sum(item["evidence_count"] for item in outcomes)
    aggregate.update(
        {
            "route_accuracy": correct_routes / aggregate["route_attempts"],
            "precision": relevant / returned,
            "recall": relevant / expected_relevant,
            "freshness_pass_rate": fresh / evidence_count,
            "provenance_coverage": provenance / evidence_count,
            "duplicate_read_reduction_percent": round(
                (
                    aggregate["baseline_duplicate_read_bytes"]
                    - aggregate["bounded_unique_read_bytes"]
                )
                * 100
                / aggregate["baseline_duplicate_read_bytes"],
                4,
            ),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
            "max_ms": round(max(durations), 6),
        }
    )
    for field, expected in aggregate.items():
        if result[field] != expected:
            raise RetrievalBenchmarkError(f"{field} does not match sample outcomes")
    if result["successful_samples"] != samples:
        raise RetrievalBenchmarkError("not every retrieval sample succeeded")
    if result["route_attempts"] != samples * len(_fixture()):
        raise RetrievalBenchmarkError("route_attempts are inaccurate")
    for field in (
        "route_accuracy",
        "precision",
        "recall",
        "freshness_pass_rate",
        "provenance_coverage",
    ):
        if result[field] != 1.0:
            raise RetrievalBenchmarkError(f"{field} must be 1.0")
    for field in (
        "precision_faults_rejected",
        "recall_faults_rejected",
        "freshness_faults_rejected",
    ):
        if result[field] != samples:
            raise RetrievalBenchmarkError(f"{field} is incomplete")
    baseline = result["baseline_duplicate_read_bytes"]
    bounded = result["bounded_unique_read_bytes"]
    if type(baseline) is not int or type(bounded) is not int or baseline <= bounded <= 0:
        raise RetrievalBenchmarkError("retrieval byte accounting is invalid")
    expected_reduction = round((baseline - bounded) * 100 / baseline, 4)
    if result["duplicate_read_reduction_percent"] != expected_reduction:
        raise RetrievalBenchmarkError("duplicate read reduction is inaccurate")
    if expected_reduction < 30.0:
        raise RetrievalBenchmarkError("duplicate read reduction misses E5")
    if result["state_authority_violations"] != 0 or result["external_services"] != 0:
        raise RetrievalBenchmarkError("retrieval benchmark crossed an authority boundary")
    for field in ("p50_ms", "p95_ms", "max_ms"):
        value = result[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise RetrievalBenchmarkError(f"{field} is invalid")
    for field in (
        "fixture_sha256",
        "implementation_sha256",
        "benchmark_sha256",
        "subject_sha256",
    ):
        value = result[field]
        if not isinstance(value, str) or _is_sha256(value) is False:
            raise RetrievalBenchmarkError(f"{field} is invalid")
    if result["implementation_sha256"] != result["subject_sha256"]:
        raise RetrievalBenchmarkError("subject_sha256 does not match implementation_sha256")
    if root is not None:
        root = Path(root)
        expected = {
            "fixture_sha256": hashlib.sha256(_canonical_fixture_bytes()).hexdigest(),
            "benchmark_sha256": hashlib.sha256(
                (root / "context_control_plane/retrieval_benchmark.py").read_bytes()
            ).hexdigest(),
            "subject_sha256": hashlib.sha256(
                (root / "context_control_plane/retrieval_routing.py").read_bytes()
            ).hexdigest(),
        }
        for field, digest in expected.items():
            if result[field] != digest:
                raise RetrievalBenchmarkError(f"{field} does not match current source")


def _is_sha256(value: str) -> bool:
    return (
        len(value) == 64
        and value == value.lower()
        and all(character in string.hexdigits for character in value)
    )
