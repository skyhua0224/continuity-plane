"""Deterministic M7-02 claim/evidence negative-gate benchmark."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .assertion_provenance import compose_assertion_provenance
from .claim_evidence_gate import evaluate_claim_evidence_gate

SCHEMA_VERSION = "context.claim-evidence-benchmark/v1alpha1"
ROOT = Path(__file__).resolve().parents[1]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _receipt(root: Path) -> tuple[bytes, str]:
    payload = (root / "experiments/retrieval/m6-01-retrieval-receipt.json").read_bytes()
    return payload, "artifact://sha256/" + hashlib.sha256(payload).hexdigest()


def _claim(kind: str, assertion_ids: list[str]) -> dict[str, Any]:
    return {
        "claim_id": "claim/m7-02/benchmark",
        "work_id": "M7-02",
        "claim_kind": kind,
        "statement": "The benchmark claim is supported by the selected evidence.",
        "evidence_assertion_ids": sorted(assertion_ids),
        "scope_refs": [
            {
                "scope_kind": "file",
                "scope_ref": "repo://context-control-plane/context_control_plane/claim_evidence_gate.py",
            }
        ],
    }


def _assertion(
    *, root: Path, receipt_ref: str, authority_kind: str, bearing: bool
) -> dict[str, Any]:
    path = "context_control_plane/claim_evidence_gate.py"
    payload = (root / path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if authority_kind == "current_code":
        source_ref = f"repo://context-control-plane/{path}#L1"
        revision = f"worktree:sha256:{digest}"
    else:
        source_ref = "https://example.invalid/m7-02/authority"
        revision = "retrieval:m7-02-fixture"
    return compose_assertion_provenance(
        assertion_id=f"assertion/m7-02/benchmark/{authority_kind}/{int(bearing)}",
        assertion_text="The M7-02 gate has a deterministic evidence policy.",
        bearing=bearing,
        evidence=[
            {
                "evidence_id": f"evidence/m7-02/benchmark/{authority_kind}/{int(bearing)}",
                "authority_kind": authority_kind,
                "source_ref": source_ref,
                "revision": revision,
                "sha256": digest,
                "valid_at": "2026-08-16T07:00:00Z",
                "retrieval_receipt_ref": receipt_ref,
            }
        ],
        asserted_at="2026-08-16T07:00:01Z",
        valid_until="2026-09-16T07:00:01Z",
        root=root,
        artifact_resolver=lambda ref: (
            (root / "experiments/retrieval/m6-01-retrieval-receipt.json").read_bytes()
            if ref == receipt_ref
            else None
        ),
    )


def benchmark_claim_evidence(*, samples: int = 1000, root: Path | None = None) -> dict[str, Any]:
    """Run a replayable mix of E6 negative and positive claim cases."""
    if type(samples) is not int or samples <= 0 or samples > 100_000:
        raise ValueError("samples must be between 1 and 100000")
    resolved_root = (root or ROOT).resolve()
    receipt_payload, receipt_ref = _receipt(resolved_root)
    valid = _assertion(
        root=resolved_root, receipt_ref=receipt_ref, authority_kind="current_code", bearing=True
    )
    non_bearing = _assertion(
        root=resolved_root, receipt_ref=receipt_ref, authority_kind="current_code", bearing=False
    )
    mismatched = _assertion(
        root=resolved_root,
        receipt_ref=receipt_ref,
        authority_kind="industry_standard",
        bearing=True,
    )
    outcomes: list[dict[str, Any]] = []
    false_allows = 0
    false_denies = 0
    allowed = 0
    denied = 0
    successful = 0
    for sample in range(samples):
        case = sample % 4
        if case == 0:
            claim = _claim("completion", [valid["assertion_id"]])
            records: list[dict[str, Any]] = []
            expected = "deny"
            expected_reason = "missing_evidence"
        elif case == 1:
            claim = _claim("completion", [non_bearing["assertion_id"]])
            records = [copy.deepcopy(non_bearing)]
            expected = "deny"
            expected_reason = "non_bearing_evidence"
        elif case == 2:
            claim = _claim("path", [mismatched["assertion_id"]])
            records = [copy.deepcopy(mismatched)]
            expected = "deny"
            expected_reason = "required_authority_missing"
        else:
            claim = _claim("completion", [valid["assertion_id"]])
            records = [copy.deepcopy(valid)]
            expected = "allow"
            expected_reason = "evidence_satisfied"
        verdict = evaluate_claim_evidence_gate(
            claim,
            evidence_records=records,
            current_time="2026-08-16T07:01:00Z",
            root=resolved_root,
            artifact_resolver=lambda ref: receipt_payload if ref == receipt_ref else None,
        )
        actual = verdict["decision"]
        allowed += actual == "allow"
        denied += actual == "deny"
        false_allows += expected == "deny" and actual == "allow"
        false_denies += expected == "allow" and actual != "allow"
        successful += actual == expected and verdict["reason"] == expected_reason
        outcomes.append(
            {
                "sample": sample,
                "expected": expected,
                "expected_reason": expected_reason,
                "decision": actual,
                "reason": verdict["reason"],
            }
        )
    outcomes_sha256 = hashlib.sha256(_canonical(outcomes)).hexdigest()
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": "benchmark/m7-02/claim-evidence",
        "samples": samples,
        "successful_samples": successful,
        "allowed_count": allowed,
        "denied_count": denied,
        "false_allow_count": false_allows,
        "false_deny_count": false_denies,
        "outcomes_sha256": outcomes_sha256,
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "",
    }
    unsigned = copy.deepcopy(receipt)
    unsigned.pop("receipt_sha256")
    receipt["receipt_sha256"] = hashlib.sha256(_canonical(unsigned)).hexdigest()
    return receipt
