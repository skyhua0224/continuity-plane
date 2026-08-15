"""Unique-case paired no-memory versus reference-recall conformance benchmark."""

from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import json
import math
import string
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from .recall_provider import (
    InMemoryRecallProvider,
    RecallCoordinator,
    UnavailableRecallProvider,
    validate_recall_receipt,
)

SCHEMA_VERSION = "context.memory-ablation/v1alpha1"
PROVIDER_ADMISSION_STATUS = "reference_fixture_conformance_only"
_OBSERVED_AT = "2026-08-15T05:30:00Z"
_MAX_SAMPLES = 10_000
_RECEIPT_ARTIFACT_FIELDS = {
    "artifact_ref",
    "media_type",
    "encoding",
    "receipt_count",
    "uncompressed_bytes",
    "payload_base64",
}
_FIELDS = {
    "schema_version",
    "generated_at",
    "samples",
    "unique_case_count",
    "baseline_correct",
    "candidate_correct",
    "baseline_accuracy",
    "candidate_accuracy",
    "absolute_gain",
    "relative_gain_percent",
    "paired_improvements",
    "paired_regressions",
    "paired_p_value",
    "paired_outcomes",
    "outcomes_sha256",
    "recall_receipt_artifact",
    "provider_503_receipt_sha256",
    "stale_decision_revivals",
    "authority_violations",
    "provider_503_state_failures",
    "provider_admission_status",
    "external_services",
    "fixture_sha256",
    "benchmark_sha256",
    "subject_sha256",
}
_OUTCOME_FIELDS = {
    "case_id",
    "expected_answer_sha256",
    "baseline_answer_sha256",
    "baseline_correct",
    "candidate_id",
    "candidate_answer_sha256",
    "candidate_freshness",
    "candidate_correct",
    "recall_receipt_sha256",
    "recall_state_write_authority",
    "recall_provider_authority",
    "candidate_active_state_authority",
}


class MemoryAblationError(ValueError):
    """Raised when paired outcomes or reference-conformance gates cannot be reproduced."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in string.hexdigits for character in value)
    )


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MemoryAblationError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryAblationError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MemoryAblationError(f"{field} requires a timezone")
    return value


def _build_recall_receipt_artifact(
    receipts: list[dict[str, Any]],
) -> dict[str, Any]:
    for receipt in receipts:
        validate_recall_receipt(receipt)
    payload = _canonical(receipts)
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    digest = _sha256_bytes(payload)
    return {
        "artifact_ref": f"artifact://sha256/{digest}",
        "media_type": "application/vnd.context.recall-receipts+json",
        "encoding": "gzip+base64",
        "receipt_count": len(receipts),
        "uncompressed_bytes": len(payload),
        "payload_base64": base64.b64encode(compressed).decode("ascii"),
    }


def _decode_recall_receipt_artifact(
    artifact: Any, *, samples: int
) -> list[dict[str, Any]]:
    if not isinstance(artifact, dict) or set(artifact) != _RECEIPT_ARTIFACT_FIELDS:
        raise MemoryAblationError("recall receipt artifact fields are invalid")
    if artifact["media_type"] != "application/vnd.context.recall-receipts+json":
        raise MemoryAblationError("recall receipt artifact media_type is invalid")
    if artifact["encoding"] != "gzip+base64":
        raise MemoryAblationError("recall receipt artifact encoding is invalid")
    expected_count = samples + 1
    if artifact["receipt_count"] != expected_count:
        raise MemoryAblationError("recall receipt artifact count is invalid")
    maximum_bytes = expected_count * 4096
    uncompressed_bytes = artifact["uncompressed_bytes"]
    if (
        type(uncompressed_bytes) is not int
        or uncompressed_bytes <= 0
        or uncompressed_bytes > maximum_bytes
    ):
        raise MemoryAblationError("recall receipt artifact size is invalid")
    encoded = artifact["payload_base64"]
    if (
        not isinstance(encoded, str)
        or not encoded
        or len(encoded) > maximum_bytes * 2
    ):
        raise MemoryAblationError("recall receipt artifact payload is invalid")
    try:
        compressed = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MemoryAblationError("recall receipt artifact payload is invalid") from exc
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        payload = decompressor.decompress(compressed, maximum_bytes + 1)
    except zlib.error as exc:
        raise MemoryAblationError("recall receipt artifact gzip is invalid") from exc
    if (
        len(payload) > maximum_bytes
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or len(payload) != uncompressed_bytes
    ):
        raise MemoryAblationError("recall receipt artifact size is invalid")
    artifact_ref = artifact["artifact_ref"]
    expected_ref = f"artifact://sha256/{_sha256_bytes(payload)}"
    if artifact_ref != expected_ref:
        raise MemoryAblationError("recall receipt artifact digest mismatch")
    try:
        receipts = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MemoryAblationError("recall receipt artifact JSON is invalid") from exc
    if (
        not isinstance(receipts, list)
        or len(receipts) != expected_count
        or _canonical(receipts) != payload
    ):
        raise MemoryAblationError("recall receipt artifact is not canonical")
    receipt_digests: list[str] = []
    for receipt in receipts:
        try:
            validate_recall_receipt(receipt)
        except (TypeError, ValueError) as exc:
            raise MemoryAblationError("recall receipt artifact contains an invalid receipt") from exc
        receipt_digests.append(receipt["receipt_sha256"])
    if len(set(receipt_digests)) != len(receipt_digests):
        raise MemoryAblationError("recall receipt artifact contains duplicate receipts")
    return receipts


def _paired_exact_p_value(improvements: int, regressions: int) -> float:
    discordant = improvements + regressions
    if discordant == 0:
        return 1.0
    tail = min(improvements, regressions)
    probability = sum(math.comb(discordant, index) for index in range(tail + 1))
    return min(1.0, 2 * probability / (2**discordant))


def _fixture_cases(samples: int) -> list[dict[str, Any]]:
    baseline_cutoff = samples * 6 // 10
    current_cutoff = samples * 9 // 10
    cases: list[dict[str, Any]] = []
    for index in range(samples):
        case_token = f"case-{index:04d}"
        case_id = f"case/m6-04/{index:04d}"
        expected_answer = f"Reference fixture answer for unique {case_token}."
        baseline_answer = (
            expected_answer
            if index < baseline_cutoff
            else f"No-memory fixture answer unavailable for unique {case_token}."
        )
        valid_until = (
            "2026-08-16T05:00:00Z"
            if index < current_cutoff
            else "2026-08-14T05:00:00Z"
        )
        record = {
            "record_id": f"memory/m6-04/{index:04d}",
            "content": expected_answer,
            "tags": [case_token, "reference-fixture"],
            "observed_at": "2026-08-10T05:00:00Z",
            "valid_until": valid_until,
            "source_ref": f"event://fixture/m6-04/{index:04d}",
            "source_sha256": _sha256_bytes(expected_answer.encode("utf-8")),
        }
        cases.append(
            {
                "case_id": case_id,
                "query": case_token,
                "expected_answer_sha256": _sha256_bytes(expected_answer.encode("utf-8")),
                "baseline_answer_sha256": _sha256_bytes(baseline_answer.encode("utf-8")),
                "record": record,
            }
        )
    return cases


def _fixture_bytes(samples: int) -> bytes:
    return _canonical(_fixture_cases(samples))


def _root(root: Path | None) -> Path:
    resolved = Path(__file__).parents[1] if root is None else Path(root)
    return resolved.resolve()


def _file_sha256(root: Path, relative: str, field: str) -> str:
    path = root / relative
    if not path.is_file():
        raise MemoryAblationError(f"{field} source is missing")
    return _sha256_bytes(path.read_bytes())


def benchmark_memory_ablation(
    *,
    samples: int = 1000,
    generated_at: str = "2026-08-15T05:30:00Z",
    root: Path | None = None,
) -> dict[str, Any]:
    """Run unique paired cases through the reference RecallCoordinator."""
    if (
        type(samples) is not int
        or samples <= 0
        or samples > _MAX_SAMPLES
        or samples % 10 != 0
    ):
        raise ValueError("samples must be a positive multiple of 10 within the limit")
    _timestamp(generated_at, "generated_at")
    repository_root = _root(root)
    cases = _fixture_cases(samples)
    coordinator = RecallCoordinator(
        InMemoryRecallProvider([case["record"] for case in cases])
    )
    outcomes: list[dict[str, Any]] = []
    recall_receipts: list[dict[str, Any]] = []
    for case in cases:
        receipt = coordinator.retrieve(
            {
                "request_id": f"recall/{case['case_id']}",
                "task_id": "M6-04",
                "query": case["query"],
                "max_candidates": 1,
                "max_returned_bytes": 512,
            },
            observed_at=_OBSERVED_AT,
        )
        recall_receipts.append(receipt)
        candidate = receipt["candidates"][0] if receipt["candidates"] else None
        candidate_answer_sha256 = (
            candidate["content_sha256"] if candidate is not None else None
        )
        candidate_freshness = (
            candidate["freshness"] if candidate is not None else "missing"
        )
        candidate_correct = bool(
            candidate is not None
            and candidate_freshness == "current"
            and candidate_answer_sha256 == case["expected_answer_sha256"]
        )
        outcomes.append(
            {
                "case_id": case["case_id"],
                "expected_answer_sha256": case["expected_answer_sha256"],
                "baseline_answer_sha256": case["baseline_answer_sha256"],
                "baseline_correct": (
                    case["baseline_answer_sha256"] == case["expected_answer_sha256"]
                ),
                "candidate_id": candidate["candidate_id"] if candidate is not None else None,
                "candidate_answer_sha256": candidate_answer_sha256,
                "candidate_freshness": candidate_freshness,
                "candidate_correct": candidate_correct,
                "recall_receipt_sha256": receipt["receipt_sha256"],
                "recall_state_write_authority": receipt["state_write_authority"],
                "recall_provider_authority": receipt["provider_authority"],
                "candidate_active_state_authority": (
                    candidate["active_state_authority"] if candidate is not None else False
                ),
            }
        )

    baseline_correct = sum(item["baseline_correct"] for item in outcomes)
    candidate_correct = sum(item["candidate_correct"] for item in outcomes)
    improvements = sum(
        item["candidate_correct"] and not item["baseline_correct"] for item in outcomes
    )
    regressions = sum(
        item["baseline_correct"] and not item["candidate_correct"] for item in outcomes
    )
    stale_revivals = sum(
        item["candidate_freshness"] == "stale" and item["candidate_correct"]
        for item in outcomes
    )
    authority_violations = sum(
        item["recall_state_write_authority"]
        or item["recall_provider_authority"]
        or item["candidate_active_state_authority"]
        for item in outcomes
    )
    unavailable = RecallCoordinator(UnavailableRecallProvider("503")).retrieve(
        {
            "request_id": "recall/m6-04/503",
            "task_id": "M6-04",
            "query": "provider failure",
            "max_candidates": 1,
            "max_returned_bytes": 128,
        },
        observed_at=_OBSERVED_AT,
    )
    recall_receipts.append(unavailable)
    provider_503_state_failures = int(
        unavailable["status"] != "degraded"
        or unavailable["state_write_authority"]
        or unavailable["provider_authority"]
        or bool(unavailable["candidates"])
    )
    baseline_accuracy = baseline_correct / samples
    candidate_accuracy = candidate_correct / samples
    result = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "unique_case_count": len({item["case_id"] for item in outcomes}),
        "baseline_correct": baseline_correct,
        "candidate_correct": candidate_correct,
        "baseline_accuracy": baseline_accuracy,
        "candidate_accuracy": candidate_accuracy,
        "absolute_gain": round(candidate_accuracy - baseline_accuracy, 4),
        "relative_gain_percent": round(
            (candidate_accuracy - baseline_accuracy) * 100 / baseline_accuracy, 4
        ),
        "paired_improvements": improvements,
        "paired_regressions": regressions,
        "paired_p_value": _paired_exact_p_value(improvements, regressions),
        "paired_outcomes": outcomes,
        "outcomes_sha256": _sha256_bytes(_canonical(outcomes)),
        "recall_receipt_artifact": _build_recall_receipt_artifact(recall_receipts),
        "provider_503_receipt_sha256": unavailable["receipt_sha256"],
        "stale_decision_revivals": stale_revivals,
        "authority_violations": authority_violations,
        "provider_503_state_failures": provider_503_state_failures,
        "provider_admission_status": PROVIDER_ADMISSION_STATUS,
        "external_services": 0,
        "fixture_sha256": _sha256_bytes(_fixture_bytes(samples)),
        "benchmark_sha256": _file_sha256(
            repository_root,
            "context_control_plane/memory_ablation.py",
            "benchmark_sha256",
        ),
        "subject_sha256": _file_sha256(
            repository_root,
            "context_control_plane/recall_provider.py",
            "subject_sha256",
        ),
    }
    validate_memory_ablation(result, root=repository_root)
    return result


def _validate_outcome(
    outcome: Any,
    *,
    expected_case: dict[str, Any],
    recall_receipt: dict[str, Any],
    index: int,
) -> tuple[bool, bool, bool, bool]:
    if not isinstance(outcome, dict) or set(outcome) != _OUTCOME_FIELDS:
        raise MemoryAblationError(f"paired outcome {index} fields are invalid")
    for field in (
        "expected_answer_sha256",
        "baseline_answer_sha256",
        "recall_receipt_sha256",
    ):
        if not _is_sha256(outcome[field]):
            raise MemoryAblationError(f"paired outcome {index} {field} is invalid")
    if outcome["candidate_answer_sha256"] is not None and not _is_sha256(
        outcome["candidate_answer_sha256"]
    ):
        raise MemoryAblationError("candidate answer digest is invalid")
    if outcome["candidate_freshness"] not in {"current", "stale", "missing"}:
        raise MemoryAblationError("candidate freshness is invalid")
    for field in (
        "baseline_correct",
        "candidate_correct",
        "recall_state_write_authority",
        "recall_provider_authority",
        "candidate_active_state_authority",
    ):
        if type(outcome[field]) is not bool:
            raise MemoryAblationError(f"paired outcome {index} {field} is invalid")
    expected_candidate_id = expected_case["record"]["record_id"]
    expected_freshness = (
        "current"
        if expected_case["record"]["valid_until"] >= _OBSERVED_AT
        else "stale"
    )
    if (
        outcome["case_id"] != expected_case["case_id"]
        or outcome["expected_answer_sha256"]
        != expected_case["expected_answer_sha256"]
        or outcome["baseline_answer_sha256"]
        != expected_case["baseline_answer_sha256"]
        or outcome["candidate_id"] != expected_candidate_id
        or outcome["candidate_answer_sha256"]
        != expected_case["expected_answer_sha256"]
        or outcome["candidate_freshness"] != expected_freshness
    ):
        raise MemoryAblationError("paired outcome does not match the fixed unique fixture")
    if outcome["recall_receipt_sha256"] != recall_receipt["receipt_sha256"]:
        raise MemoryAblationError("paired outcome recall receipt digest mismatch")
    if (
        recall_receipt["request_id"] != f"recall/{expected_case['case_id']}"
        or recall_receipt["task_id"] != "M6-04"
        or recall_receipt["provider_id"] != "fixture-memory/v1"
        or recall_receipt["status"] != "completed"
        or recall_receipt["provider_error_code"] is not None
        or recall_receipt["observed_at"] != _OBSERVED_AT
        or len(recall_receipt["candidates"]) != 1
    ):
        raise MemoryAblationError("paired outcome recall receipt identity mismatch")
    candidate = recall_receipt["candidates"][0]
    record = expected_case["record"]
    if (
        candidate["candidate_id"] != outcome["candidate_id"]
        or candidate["content_sha256"] != outcome["candidate_answer_sha256"]
        or candidate["freshness"] != outcome["candidate_freshness"]
        or candidate["observed_at"] != record["observed_at"]
        or candidate["valid_until"] != record["valid_until"]
        or candidate["source_ref"] != record["source_ref"]
        or candidate["source_sha256"] != record["source_sha256"]
        or recall_receipt["state_write_authority"]
        is not outcome["recall_state_write_authority"]
        or recall_receipt["provider_authority"]
        is not outcome["recall_provider_authority"]
        or candidate["active_state_authority"]
        is not outcome["candidate_active_state_authority"]
    ):
        raise MemoryAblationError("paired outcome does not match its recall receipt")
    baseline_correct = (
        outcome["baseline_answer_sha256"] == outcome["expected_answer_sha256"]
    )
    candidate_correct = (
        outcome["candidate_freshness"] == "current"
        and outcome["candidate_answer_sha256"] == outcome["expected_answer_sha256"]
    )
    if (
        outcome["baseline_correct"] is not baseline_correct
        or outcome["candidate_correct"] is not candidate_correct
    ):
        raise MemoryAblationError("paired outcome correctness is inaccurate")
    authority_violation = (
        outcome["recall_state_write_authority"]
        or outcome["recall_provider_authority"]
        or outcome["candidate_active_state_authority"]
    )
    stale_revival = outcome["candidate_freshness"] == "stale" and candidate_correct
    return baseline_correct, candidate_correct, stale_revival, authority_violation


def validate_memory_ablation(
    result: Any, *, root: Path | None = None
) -> None:
    """Recompute unique paired outcomes, statistics, fixture and source hashes."""
    if not isinstance(result, dict) or set(result) != _FIELDS:
        raise MemoryAblationError("memory ablation fields are invalid")
    if result["schema_version"] != SCHEMA_VERSION:
        raise MemoryAblationError("memory ablation schema_version is invalid")
    _timestamp(result["generated_at"], "generated_at")
    samples = result["samples"]
    if (
        type(samples) is not int
        or samples <= 0
        or samples > _MAX_SAMPLES
        or samples % 10 != 0
    ):
        raise MemoryAblationError(
            "samples must be a positive multiple of 10 within the limit"
        )
    outcomes = result["paired_outcomes"]
    if not isinstance(outcomes, list) or len(outcomes) != samples:
        raise MemoryAblationError("paired outcomes do not cover every sample")
    case_ids = [item.get("case_id") if isinstance(item, dict) else None for item in outcomes]
    if any(not isinstance(case_id, str) or not case_id for case_id in case_ids):
        raise MemoryAblationError("paired outcome case_id is invalid")
    if len(set(case_ids)) != samples or result["unique_case_count"] != samples:
        raise MemoryAblationError("paired cases must be unique")
    if result["outcomes_sha256"] != _sha256_bytes(_canonical(outcomes)):
        raise MemoryAblationError("paired outcomes digest mismatch")
    receipts = _decode_recall_receipt_artifact(
        result["recall_receipt_artifact"], samples=samples
    )
    receipts_by_request = {receipt["request_id"]: receipt for receipt in receipts}
    if len(receipts_by_request) != len(receipts):
        raise MemoryAblationError("recall receipt request IDs must be unique")
    expected_cases = _fixture_cases(samples)
    baseline_values: list[bool] = []
    candidate_values: list[bool] = []
    stale_revivals = 0
    authority_violations = 0
    for index, (outcome, expected_case) in enumerate(zip(outcomes, expected_cases)):
        request_id = f"recall/{expected_case['case_id']}"
        recall_receipt = receipts_by_request.get(request_id)
        if recall_receipt is None:
            raise MemoryAblationError("paired outcome recall receipt is missing")
        baseline, candidate, stale, authority = _validate_outcome(
            outcome,
            expected_case=expected_case,
            recall_receipt=recall_receipt,
            index=index,
        )
        baseline_values.append(baseline)
        candidate_values.append(candidate)
        stale_revivals += int(stale)
        authority_violations += int(authority)
    unavailable = receipts_by_request.get("recall/m6-04/503")
    if unavailable is None:
        raise MemoryAblationError("503 recall receipt evidence is missing")
    if result["provider_503_receipt_sha256"] != unavailable["receipt_sha256"]:
        raise MemoryAblationError("503 recall receipt digest mismatch")
    provider_503_state_failures = int(
        unavailable["task_id"] != "M6-04"
        or unavailable["provider_id"] != "unavailable/v1"
        or unavailable["status"] != "degraded"
        or unavailable["provider_error_code"] != "503"
        or unavailable["observed_at"] != _OBSERVED_AT
        or unavailable["state_write_authority"]
        or unavailable["provider_authority"]
        or bool(unavailable["candidates"])
    )
    baseline_correct = sum(baseline_values)
    candidate_correct = sum(candidate_values)
    improvements = sum(
        candidate and not baseline
        for baseline, candidate in zip(baseline_values, candidate_values)
    )
    regressions = sum(
        baseline and not candidate
        for baseline, candidate in zip(baseline_values, candidate_values)
    )
    expected_counts = {
        "baseline_correct": baseline_correct,
        "candidate_correct": candidate_correct,
        "paired_improvements": improvements,
        "paired_regressions": regressions,
        "stale_decision_revivals": stale_revivals,
        "authority_violations": authority_violations,
        "provider_503_state_failures": provider_503_state_failures,
    }
    for field, expected in expected_counts.items():
        if result[field] != expected:
            raise MemoryAblationError(f"{field} does not match paired outcomes")
    baseline_accuracy = baseline_correct / samples
    candidate_accuracy = candidate_correct / samples
    if (
        result["baseline_accuracy"] != baseline_accuracy
        or result["candidate_accuracy"] != candidate_accuracy
    ):
        raise MemoryAblationError("accuracy accounting is invalid")
    if (
        result["absolute_gain"] != round(candidate_accuracy - baseline_accuracy, 4)
        or result["absolute_gain"] <= 0
    ):
        raise MemoryAblationError("reference candidate recall has no measured gain")
    expected_relative = round(
        (candidate_accuracy - baseline_accuracy) * 100 / baseline_accuracy, 4
    )
    if result["relative_gain_percent"] != expected_relative:
        raise MemoryAblationError("relative gain is inaccurate")
    if improvements <= regressions:
        raise MemoryAblationError("paired cases do not improve")
    expected_p_value = _paired_exact_p_value(improvements, regressions)
    p_value = result["paired_p_value"]
    if (
        isinstance(p_value, bool)
        or not isinstance(p_value, (int, float))
        or not math.isfinite(p_value)
        or not math.isclose(p_value, expected_p_value, rel_tol=1e-12, abs_tol=0.0)
        or not 0 <= p_value < 0.05
    ):
        raise MemoryAblationError("paired gain is not significant")
    if result["provider_503_state_failures"] != 0 or result["external_services"] != 0:
        raise MemoryAblationError("provider failure or external service violates the gate")
    if result["provider_admission_status"] != PROVIDER_ADMISSION_STATUS:
        raise MemoryAblationError("provider admission exceeds reference fixture conformance")
    repository_root = _root(root)
    expected_hashes = {
        "fixture_sha256": _sha256_bytes(_fixture_bytes(samples)),
        "benchmark_sha256": _file_sha256(
            repository_root,
            "context_control_plane/memory_ablation.py",
            "benchmark_sha256",
        ),
        "subject_sha256": _file_sha256(
            repository_root,
            "context_control_plane/recall_provider.py",
            "subject_sha256",
        ),
    }
    for field, expected in expected_hashes.items():
        if result[field] != expected:
            raise MemoryAblationError(f"{field} does not match current evidence")
