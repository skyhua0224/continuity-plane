"""Deterministic offline benchmark for M7-06 reference freshness gates."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

from .assertion_provenance import compose_assertion_provenance
from .reference_watcher import (
    ReferenceWatcherError,
    assertion_is_completion_eligible,
    canonical_reference_watch_decision_bytes,
    canonical_reference_watch_observation_bytes,
    decide_reference_watch,
    observe_reference,
    trusted_time_registry_entry_sha256,
    validate_reference_watch_decision,
    validate_reference_watch_observation,
)
from .retrieval_routing import compose_retrieval_receipt, plan_retrieval

SCHEMA_VERSION = "context.reference-watcher-benchmark/v1alpha1"
ROOT = Path(__file__).resolve().parents[1]
_FIELDS = {
    "schema_version",
    "benchmark_id",
    "samples",
    "successful_samples",
    "outcome_counts",
    "changed_fixture_count",
    "changed_fixture_stale_or_quarantined_count",
    "unsafe_fixture_count",
    "unsafe_fixture_stale_or_quarantined_count",
    "unreviewed_completion_attempts",
    "unreviewed_completion_allows",
    "reverified_release_attempts",
    "reverified_release_allows",
    "adversarial_case_counts",
    "adversarial_allow_counts",
    "replay_mismatch_count",
    "authority_violation_count",
    "external_service_calls",
    "outcomes_sha256",
    "implementation_sha256",
    "receipt_sha256",
}
_OUTCOMES = {"unchanged", "changed", "unavailable", "expired"}
_ADVERSARIAL_CASES = {
    "detached-decision",
    "detached-observation",
    "forged-state-matrix",
    "unresolved-replacement",
    "unverified-trusted-time",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: dict[str, Any], field: str) -> str:
    body = copy.deepcopy(value)
    body.pop(field, None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def _resign(value: dict[str, Any], field: str) -> None:
    value[field] = _digest(value, field)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_FIXTURE_VERIFIER_ID = "verifier/m7-06-fixture-clock"
_FIXTURE_VERIFIER_VERSION = "2026-08-16"
_FIXTURE_KEY = b"context-control-plane/m7-06/fixed-fixture-key/v1"


def _fixture_time_evidence(kind: str, trusted_at: str) -> bytes:
    message = f"{kind}\n{trusted_at}".encode()
    return hmac.new(_FIXTURE_KEY, message, hashlib.sha256).hexdigest().encode()


def _fixture_time_verifier(kind: str, trusted_at: str, evidence: bytes) -> bool:
    return hmac.compare_digest(evidence, _fixture_time_evidence(kind, trusted_at))


def _fixture_time_verifier_entry() -> dict[str, Any]:
    entry: dict[str, Any] = {
        "verifier_id": _FIXTURE_VERIFIER_ID,
        "verifier_version": _FIXTURE_VERIFIER_VERSION,
        "trust_anchor_sha256": hashlib.sha256(_FIXTURE_KEY).hexdigest(),
        "supported_kinds": ["fixture-clock", "provider-signed"],
        "verify": _fixture_time_verifier,
    }
    entry["registry_entry_sha256"] = trusted_time_registry_entry_sha256(entry)
    return entry


_FIXTURE_TIME_VERIFIER_ENTRY = _fixture_time_verifier_entry()


def _fixture_time_verifier_resolver(
    verifier_id: str, verifier_version: str
) -> dict[str, Any] | None:
    if (verifier_id, verifier_version) != (
        _FIXTURE_VERIFIER_ID,
        _FIXTURE_VERIFIER_VERSION,
    ):
        return None
    return _FIXTURE_TIME_VERIFIER_ENTRY


def _retrieval_receipt(
    *, sample: int, source_ref: str, revision: str, content: bytes, asserted_at: str
) -> bytes:
    plan = plan_retrieval(
        {
            "question_id": f"question/m7-06/reverified-{sample}",
            "kind": "official_reference",
            "query": "current official reference",
            "repositories": ["external-reference"],
            "freshness_required": True,
        },
        available_tools={"rtfm"},
        max_queries=1,
        max_scanned_bytes=max(len(content), 1),
        max_returned_bytes=max(len(content), 1),
        max_index_age_seconds=3600,
    )
    receipt = compose_retrieval_receipt(
        plan=plan,
        evidence=[
            {
                "evidence_id": f"evidence/m7-06/reverified-{sample}",
                "source_kind": "official_reference",
                "source_ref": source_ref,
                "revision": revision,
                "sha256": hashlib.sha256(content).hexdigest(),
                "range": {"offset_bytes": 0, "length_bytes": len(content)},
                "retrieved_at": asserted_at,
                "valid_at": asserted_at,
            }
        ],
        step_results=[
            {
                "tool": "rtfm",
                "queries": 1,
                "scanned_bytes": len(content),
                "returned_bytes": len(content),
                "index_revision": revision,
                "index_sha256": hashlib.sha256(content).hexdigest(),
                "index_age_seconds": 0,
            }
        ],
        executed_at=asserted_at,
        cache_status="miss",
        prior_receipt_ref=None,
    )
    return _canonical(receipt)


def _replacement_assertion(
    *,
    sample: int,
    source_ref: str,
    revision: str,
    content: bytes,
    receipt_ref: str,
) -> dict[str, Any]:
    asserted_at = "2026-08-16T08:01:00Z"
    return compose_assertion_provenance(
        assertion_id=f"assertion/reference/reverified-{sample}",
        assertion_text="The current upstream reference was reverified after a detected change.",
        bearing=True,
        evidence=[
            {
                "evidence_id": f"evidence/reference/reverified-{sample}",
                "authority_kind": "industry_standard",
                "source_ref": source_ref,
                "revision": revision,
                "sha256": hashlib.sha256(content).hexdigest(),
                "valid_at": asserted_at,
                "retrieval_receipt_ref": receipt_ref,
            }
        ],
        asserted_at=asserted_at,
        valid_until="2026-10-01T00:00:00Z",
    )


def _sample(sample: int) -> dict[str, Any]:
    baseline = b"official specification revision one\n"
    changed = b"official specification revision two\n"
    assertion_id = f"assertion/reference/baseline-{sample}"
    source_ref = "https://standards.example.invalid/specification"
    watch = {
        "watch_id": f"watch/reference/specification-{sample}",
        "source_id": "reference/specification",
        "source_ref": source_ref,
        "authority_kind": "industry_standard",
        "watch_revision": 7,
        "baseline_revision": "revision-1",
        "baseline_sha256": hashlib.sha256(baseline).hexdigest(),
        "valid_until": "2026-09-01T00:00:00Z",
        "assertion_ids": [assertion_id],
    }
    trusted_at = "2026-08-16T08:00:00Z"
    artifacts: dict[str, bytes] = {}
    evidence_payloads: dict[tuple[str, str], bytes] = {}
    replacements: dict[tuple[str, str], dict[str, Any]] = {}
    values: dict[str, Any] = {
        "watch": watch,
        "mode": "fixture",
        "availability_status": "available",
        "observed_revision": "revision-1",
        "observed_content": baseline,
        "unavailability_evidence": None,
        "trusted_time": {
            "kind": "fixture-clock",
            "trusted_at": trusted_at,
            "evidence": _fixture_time_evidence("fixture-clock", trusted_at),
            "verifier_id": _FIXTURE_VERIFIER_ID,
            "verifier_version": _FIXTURE_VERIFIER_VERSION,
        },
        "trusted_time_verifier_resolver": _fixture_time_verifier_resolver,
    }
    case = sample % 5
    expected = "unchanged"
    current_content = baseline
    current_revision = "revision-1"
    if case == 1:
        expected = "changed"
        current_revision = "revision-2"
        values["observed_revision"] = current_revision
    elif case == 2:
        expected = "changed"
        current_content = changed
        values["observed_content"] = current_content
    elif case == 3:
        expected = "unavailable"
        values.update(
            {
                "availability_status": "unavailable",
                "observed_revision": None,
                "observed_content": None,
                "unavailability_evidence": b"offline fixture: upstream unavailable",
            }
        )
    elif case == 4:
        expected = "expired"
        expired_at = "2026-09-02T00:00:00Z"
        values["trusted_time"] = {
            "kind": "fixture-clock",
            "trusted_at": expired_at,
            "evidence": _fixture_time_evidence("fixture-clock", expired_at),
            "verifier_id": _FIXTURE_VERIFIER_ID,
            "verifier_version": _FIXTURE_VERIFIER_VERSION,
        }

    observation = observe_reference(**values)
    current_artifact = (
        values["observed_content"]
        if values["availability_status"] == "available"
        else values["unavailability_evidence"]
    )
    artifacts[observation["current_evidence_ref"]] = bytes(current_artifact)
    artifacts[observation["trusted_time_evidence_ref"]] = bytes(
        values["trusted_time"]["evidence"]
    )
    artifact_resolver = artifacts.get
    evidence_resolver = lambda source, revision: evidence_payloads.get(
        (source, revision)
    )
    replacement_resolver = lambda assertion, digest: replacements.get(
        (assertion, digest)
    )
    decision_args = {
        "expected_watch": watch,
        "artifact_resolver": artifact_resolver,
        "trusted_time_verifier_resolver": _fixture_time_verifier_resolver,
        "evidence_resolver": evidence_resolver,
    }
    decision = decide_reference_watch(observation, **decision_args)
    replacement_decision: dict[str, Any] | None = None
    replacement: dict[str, Any] | None = None
    if expected == "changed":
        asserted_at = "2026-08-16T08:01:00Z"
        receipt = _retrieval_receipt(
            sample=sample,
            source_ref=source_ref,
            revision=current_revision,
            content=current_content,
            asserted_at=asserted_at,
        )
        receipt_ref = f"artifact://sha256/{hashlib.sha256(receipt).hexdigest()}"
        artifacts[receipt_ref] = receipt
        evidence_payloads[(source_ref, current_revision)] = current_content
        replacement = _replacement_assertion(
            sample=sample,
            source_ref=source_ref,
            revision=current_revision,
            content=current_content,
            receipt_ref=receipt_ref,
        )
        replacements[(replacement["assertion_id"], replacement["record_sha256"])] = (
            replacement
        )
        replacement_decision = decide_reference_watch(
            observation,
            replacement_assertion=replacement,
            supersedes_assertion_ids=[assertion_id],
            **decision_args,
        )
    validation_args = {
        "expected_observation": observation,
        "expected_watch": watch,
        "artifact_resolver": artifact_resolver,
        "trusted_time_verifier_resolver": _fixture_time_verifier_resolver,
        "replacement_assertion_resolver": replacement_resolver,
        "evidence_resolver": evidence_resolver,
    }
    adversarial_cases = {case: 0 for case in sorted(_ADVERSARIAL_CASES)}
    adversarial_allows = {case: 0 for case in sorted(_ADVERSARIAL_CASES)}

    def adversarial(case: str, operation) -> None:
        adversarial_cases[case] += 1
        try:
            operation()
        except ReferenceWatcherError:
            return
        adversarial_allows[case] += 1

    invalid_live_time = {
        "kind": "provider-signed",
        "trusted_at": "2026-08-16T08:00:00Z",
        "evidence": b"not-a-signature-or-attestation",
        "verifier_id": _FIXTURE_VERIFIER_ID,
        "verifier_version": _FIXTURE_VERIFIER_VERSION,
    }
    adversarial(
        "unverified-trusted-time",
        lambda: observe_reference(
            watch=watch,
            mode="live",
            availability_status="available",
            observed_revision="revision-1",
            observed_content=baseline,
            unavailability_evidence=None,
            trusted_time=invalid_live_time,
            trusted_time_verifier_resolver=_fixture_time_verifier_resolver,
        ),
    )
    if expected == "changed":
        detached_observation = copy.deepcopy(observation)
        detached_observation["baseline_revision"] = detached_observation[
            "observed_revision"
        ]
        detached_observation["baseline_sha256"] = detached_observation[
            "observed_sha256"
        ]
        detached_observation["watch_outcome"] = "unchanged"
        _resign(detached_observation, "observation_sha256")
        adversarial(
            "detached-observation",
            lambda: validate_reference_watch_observation(
                detached_observation,
                expected_watch=watch,
                artifact_resolver=artifact_resolver,
                trusted_time_verifier_resolver=_fixture_time_verifier_resolver,
            ),
        )

        detached_decision = copy.deepcopy(decision)
        detached_decision.update(
            {
                "watch_outcome": "unchanged",
                "assertion_status": "current",
                "decision": "allow-current",
                "reason_code": "source-unchanged",
                "stale_assertion_ids": [],
                "quarantined_assertion_ids": [],
                "superseded_assertion_ids": [],
                "completion_eligible_assertion_ids": [assertion_id],
            }
        )
        _resign(detached_decision, "decision_sha256")
        adversarial(
            "detached-decision",
            lambda: assertion_is_completion_eligible(
                detached_decision, assertion_id, **validation_args
            ),
        )

        forged_matrix = copy.deepcopy(decision)
        forged_matrix["decision"] = "allow-current"
        forged_matrix["superseded_assertion_ids"] = [assertion_id]
        _resign(forged_matrix, "decision_sha256")
        adversarial(
            "forged-state-matrix",
            lambda: validate_reference_watch_decision(
                forged_matrix, **validation_args
            ),
        )
        adversarial(
            "unresolved-replacement",
            lambda: decide_reference_watch(
                observation,
                replacement_assertion=replacement,
                supersedes_assertion_ids=[assertion_id],
                expected_watch=watch,
                artifact_resolver=artifact_resolver,
                trusted_time_verifier_resolver=_fixture_time_verifier_resolver,
            ),
        )
    replay = {
        "observation": canonical_reference_watch_observation_bytes(
            observation,
            expected_watch=watch,
            artifact_resolver=artifact_resolver,
            trusted_time_verifier_resolver=_fixture_time_verifier_resolver,
        ).decode(),
        "decision": canonical_reference_watch_decision_bytes(
            decision, **validation_args
        ).decode(),
        "replacement_decision": (
            canonical_reference_watch_decision_bytes(
                replacement_decision, **validation_args
            ).decode()
            if replacement_decision is not None
            else None
        ),
    }
    authority_violations = sum(
        item[field] is not False
        for item in (
            observation,
            decision,
            *([replacement_decision] if replacement_decision else []),
        )
        for field in (
            "state_write_authority",
            "completion_authority",
            "provider_native_authority",
        )
    )
    unsafe = expected != "unchanged"
    unreviewed_allowed = assertion_is_completion_eligible(
        decision, assertion_id, **validation_args
    )
    reverified_allowed = (
        replacement_decision is not None
        and assertion_is_completion_eligible(
            replacement_decision,
            replacement_decision["replacement_assertion_id"],
            **validation_args,
        )
    )
    expected_status = {
        "unchanged": "current",
        "changed": "stale",
        "unavailable": "quarantined",
        "expired": "quarantined",
    }[expected]
    successful = (
        observation["watch_outcome"] == expected
        and decision["assertion_status"] == expected_status
        and (not unsafe or not unreviewed_allowed)
        and (expected != "changed" or reverified_allowed)
        and authority_violations == 0
    )
    return {
        "sample": sample,
        "expected": expected,
        "outcome": observation["watch_outcome"],
        "assertion_status": decision["assertion_status"],
        "unsafe": unsafe,
        "unreviewed_allowed": unreviewed_allowed,
        "reverified_allowed": bool(reverified_allowed),
        "authority_violations": authority_violations,
        "successful": successful,
        "replay": replay,
        "adversarial_case_counts": adversarial_cases,
        "adversarial_allow_counts": adversarial_allows,
    }


def benchmark_reference_watcher(
    *, samples: int = 1000, root: Path | None = None
) -> dict[str, Any]:
    """Run unchanged and fault fixtures without network or external services."""
    if type(samples) is not int or samples <= 0 or samples > 100_000:
        raise ValueError("samples must be between 1 and 100000")
    resolved_root = (root or ROOT).resolve()
    outcomes: list[dict[str, Any]] = []
    counts = {outcome: 0 for outcome in sorted(_OUTCOMES)}
    successful = 0
    changed_fixture_count = 0
    changed_covered = 0
    unsafe_fixture_count = 0
    unsafe_covered = 0
    unreviewed_attempts = 0
    unreviewed_allows = 0
    reverified_attempts = 0
    reverified_allows = 0
    adversarial_cases = {case: 0 for case in sorted(_ADVERSARIAL_CASES)}
    adversarial_allows = {case: 0 for case in sorted(_ADVERSARIAL_CASES)}
    replay_mismatches = 0
    authority_violations = 0

    for sample in range(samples):
        first = _sample(sample)
        second = _sample(sample)
        counts[first["outcome"]] += 1
        successful += first["successful"]
        changed = first["expected"] == "changed"
        unsafe = first["unsafe"]
        changed_fixture_count += changed
        changed_covered += changed and first["assertion_status"] in {
            "stale",
            "quarantined",
        }
        unsafe_fixture_count += unsafe
        unsafe_covered += unsafe and first["assertion_status"] in {
            "stale",
            "quarantined",
        }
        unreviewed_attempts += unsafe
        unreviewed_allows += unsafe and first["unreviewed_allowed"]
        reverified_attempts += changed
        reverified_allows += changed and first["reverified_allowed"]
        for case in sorted(_ADVERSARIAL_CASES):
            adversarial_cases[case] += first["adversarial_case_counts"][case]
            adversarial_allows[case] += first["adversarial_allow_counts"][case]
        replay_mismatches += first["replay"] != second["replay"]
        authority_violations += first["authority_violations"]
        outcomes.append(
            {
                key: first[key]
                for key in (
                    "sample",
                    "expected",
                    "outcome",
                    "assertion_status",
                    "unreviewed_allowed",
                    "reverified_allowed",
                    "successful",
                )
            }
        )

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": "benchmark/m7-06/reference-watcher",
        "samples": samples,
        "successful_samples": successful,
        "outcome_counts": counts,
        "changed_fixture_count": changed_fixture_count,
        "changed_fixture_stale_or_quarantined_count": changed_covered,
        "unsafe_fixture_count": unsafe_fixture_count,
        "unsafe_fixture_stale_or_quarantined_count": unsafe_covered,
        "unreviewed_completion_attempts": unreviewed_attempts,
        "unreviewed_completion_allows": unreviewed_allows,
        "reverified_release_attempts": reverified_attempts,
        "reverified_release_allows": reverified_allows,
        "adversarial_case_counts": adversarial_cases,
        "adversarial_allow_counts": adversarial_allows,
        "replay_mismatch_count": replay_mismatches,
        "authority_violation_count": authority_violations,
        "external_service_calls": 0,
        "outcomes_sha256": hashlib.sha256(_canonical(outcomes)).hexdigest(),
        "implementation_sha256": _sha256_file(
            resolved_root / "context_control_plane/reference_watcher.py"
        ),
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
    validate_reference_watcher_benchmark(receipt, root=resolved_root)
    return receipt


def validate_reference_watcher_benchmark(
    receipt: Any, *, root: Path | None = None
) -> None:
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS:
        raise ValueError("M7-06 benchmark fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("M7-06 benchmark version is invalid")
    if receipt["benchmark_id"] != "benchmark/m7-06/reference-watcher":
        raise ValueError("M7-06 benchmark ID is invalid")
    integer_fields = _FIELDS - {
        "schema_version",
        "benchmark_id",
        "outcome_counts",
        "adversarial_case_counts",
        "adversarial_allow_counts",
        "outcomes_sha256",
        "implementation_sha256",
        "receipt_sha256",
    }
    if any(
        type(receipt[field]) is not int or receipt[field] < 0
        for field in integer_fields
    ):
        raise ValueError("M7-06 benchmark counters are invalid")
    if receipt["samples"] <= 0 or receipt["successful_samples"] != receipt["samples"]:
        raise ValueError("M7-06 benchmark samples did not all succeed")
    counts = receipt["outcome_counts"]
    if not isinstance(counts, dict) or set(counts) != _OUTCOMES:
        raise ValueError("M7-06 outcome counts are invalid")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("M7-06 outcome count is invalid")
    if sum(counts.values()) != receipt["samples"]:
        raise ValueError("M7-06 outcome denominator is invalid")
    if receipt["changed_fixture_count"] != counts["changed"]:
        raise ValueError("M7-06 changed fixture denominator is invalid")
    if (
        receipt["changed_fixture_stale_or_quarantined_count"]
        != receipt["changed_fixture_count"]
    ):
        raise ValueError("M7-06 changed fixture coverage regressed")
    expected_unsafe = receipt["samples"] - counts["unchanged"]
    if receipt["unsafe_fixture_count"] != expected_unsafe:
        raise ValueError("M7-06 unsafe fixture denominator is invalid")
    if receipt["unsafe_fixture_stale_or_quarantined_count"] != expected_unsafe:
        raise ValueError("M7-06 unsafe fixture coverage regressed")
    if receipt["unreviewed_completion_attempts"] != expected_unsafe:
        raise ValueError("M7-06 unreviewed completion denominator is invalid")
    if receipt["unreviewed_completion_allows"] != 0:
        raise ValueError("M7-06 unreviewed assertion passed completion")
    if (
        receipt["reverified_release_attempts"] != receipt["changed_fixture_count"]
        or receipt["reverified_release_allows"] != receipt["changed_fixture_count"]
    ):
        raise ValueError("M7-06 current reverification coverage is incomplete")
    adversarial_cases = receipt["adversarial_case_counts"]
    adversarial_allows = receipt["adversarial_allow_counts"]
    if (
        not isinstance(adversarial_cases, dict)
        or set(adversarial_cases) != _ADVERSARIAL_CASES
        or not isinstance(adversarial_allows, dict)
        or set(adversarial_allows) != _ADVERSARIAL_CASES
    ):
        raise ValueError("M7-06 adversarial counters are invalid")
    if any(
        type(adversarial_cases[case]) is not int
        or adversarial_cases[case] <= 0
        or type(adversarial_allows[case]) is not int
        or adversarial_allows[case] != 0
        for case in _ADVERSARIAL_CASES
    ):
        raise ValueError("M7-06 adversarial binding attack passed validation")
    if adversarial_cases["unverified-trusted-time"] != receipt["samples"] or any(
        adversarial_cases[case] != receipt["changed_fixture_count"]
        for case in _ADVERSARIAL_CASES - {"unverified-trusted-time"}
    ):
        raise ValueError("M7-06 adversarial denominators are invalid")
    if any(
        receipt[field] != 0
        for field in (
            "replay_mismatch_count",
            "authority_violation_count",
            "external_service_calls",
        )
    ):
        raise ValueError("M7-06 safety or offline gate failed")
    for field in ("outcomes_sha256", "implementation_sha256", "receipt_sha256"):
        value = receipt[field]
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError(f"M7-06 {field} is invalid")
    if receipt["receipt_sha256"] != _digest(receipt, "receipt_sha256"):
        raise ValueError("M7-06 receipt digest mismatch")
    if root is not None:
        expected = _sha256_file(
            root.resolve() / "context_control_plane/reference_watcher.py"
        )
        if receipt["implementation_sha256"] != expected:
            raise ValueError("M7-06 implementation hash is stale")
