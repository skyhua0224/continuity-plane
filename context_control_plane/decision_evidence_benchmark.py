"""Measured acceptance for the M9-03 Decision and Evidence projection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .assertion_provenance import compose_assertion_provenance
from .claim_evidence_gate import evaluate_claim_evidence_gate
from .decision_evidence_projection import (
    DecisionEvidenceProjectionError,
    build_decision_evidence_projection,
    validate_decision_evidence_projection,
)
from .external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
)
from .idea_continuity_benchmark import build_idea_snapshot
from .state_mcp import RequestContext
from .typed_state import validate_typed_state

BENCHMARK_SCHEMA_VERSION = "context.decision-evidence-benchmark/v1alpha1"
BENCHMARK_ID = "m9-03-decision-evidence"
_LONG_CHAIN_ITEMS = 128
_STRESS_MATRIX_SIDE = 64
_STRESS_MATRIX_CELLS = _STRESS_MATRIX_SIDE * _STRESS_MATRIX_SIDE
_MAX_EVIDENCE_REFERENCES = 50_000
_THRESHOLDS = {
    "same_revision_rate_min": 1.0,
    "decision_timeline_complete_rate_min": 1.0,
    "constraint_matrix_complete_rate_min": 1.0,
    "evidence_matrix_complete_rate_min": 1.0,
    "current_decision_rate_min": 1.0,
    "supersedes_chain_rate_min": 1.0,
    "metadata_only_honesty_rate_min": 1.0,
    "validated_provenance_rate_min": 1.0,
    "evidence_classification_rate_min": 1.0,
    "tamper_rejection_rate_min": 1.0,
    "object_capacity_rejection_required": True,
    "reference_capacity_rejection_required": True,
    "false_supports_max": 0,
    "old_decision_resurrections_max": 0,
    "authority_violations_max": 0,
    "provider_invocations_max": 0,
    "external_services_max": 0,
    "worst_path_latency_p95_ms_max": 50.0,
}
_MIN_PERFORMANCE_SAMPLES = 25
_COUNT_FIELDS = (
    "same_revision_matches",
    "decision_timeline_complete_matches",
    "constraint_matrix_complete_matches",
    "evidence_matrix_complete_matches",
    "current_decision_matches",
    "supersedes_chain_matches",
    "metadata_only_honesty_matches",
    "validated_provenance_matches",
    "evidence_classification_matches",
    "tamper_rejections",
)
_RATE_FIELDS = (
    "same_revision_rate",
    "decision_timeline_complete_rate",
    "constraint_matrix_complete_rate",
    "evidence_matrix_complete_rate",
    "current_decision_rate",
    "supersedes_chain_rate",
    "metadata_only_honesty_rate",
    "validated_provenance_rate",
    "evidence_classification_rate",
    "tamper_rejection_rate",
)
_ZERO_FIELDS = (
    "false_supports",
    "old_decision_resurrections",
    "authority_violations",
    "provider_invocations",
    "external_services",
)
_RESULT_FIELDS = set(_COUNT_FIELDS) | set(_RATE_FIELDS) | set(_ZERO_FIELDS) | {
    "object_capacity_rejected",
    "reference_capacity_rejected",
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "parameters",
    "thresholds",
    "results",
    "latency_ms",
    "latency_samples_ms",
    "attestation",
    "gate",
    "provenance",
    "receipt_sha256",
}
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:[0-9]{2})$"
)


class DecisionEvidenceBenchmarkError(ValueError):
    """Raised when an M9-03 benchmark receipt cannot be accepted."""


class _Source:
    def __init__(self, snapshot: dict[str, Any]) -> None:
        self.snapshot = copy.deepcopy(snapshot)

    def call_tool(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        return {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": arguments["request_id"],
            "tool": "context.state.read",
            "ok": True,
            "result": {
                "snapshot": copy.deepcopy(self.snapshot),
                "revision": self.snapshot["project"]["revision"],
                "event_head": None,
                "registry_digest": "a" * 64,
                "capabilities": {"adapter_id": "context.m9-03-benchmark"},
            },
            "error": None,
        }


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DecisionEvidenceBenchmarkError(
            "benchmark is not canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _latency(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        "min": ordered[0],
        "p50": ordered[max(0, (len(ordered) * 50 + 99) // 100 - 1)],
        "p95": ordered[max(0, (len(ordered) * 95 + 99) // 100 - 1)],
        "max": ordered[-1],
    }


def _evidence(
    evidence_id: str,
    digest: str,
    *,
    validity: str = "verified",
) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "kind": "test",
        "artifact_ref": f"artifact://sha256/{digest}",
        "content_sha256": digest,
        "validity": validity,
        "observed_at": "2026-08-14T06:00:00+08:00",
        "verified_at": (
            "2026-08-14T06:01:00+08:00" if validity == "verified" else None
        ),
    }


def _baseline_snapshot(state_digest: str) -> dict[str, Any]:
    snapshot = build_idea_snapshot()
    snapshot["evidence"] = [
        _evidence("evidence-old", "1" * 64),
        _evidence("evidence-current", state_digest),
        _evidence("evidence-stale", "3" * 64, validity="stale"),
        _evidence("evidence-rejected", "4" * 64, validity="rejected"),
        _evidence("evidence-unreferenced", "5" * 64),
    ]
    snapshot["decisions"] = [
        {
            "decision_id": "decision-old",
            "work_id": "work-active",
            "status": "superseded",
            "statement": "Use the old path",
            "decided_at": "2026-08-14T06:10:00+08:00",
            "supersedes_decision_id": None,
            "evidence_ids": ["evidence-old"],
        },
        {
            "decision_id": "decision-current",
            "work_id": "work-active",
            "status": "accepted",
            "statement": "Use the current path",
            "decided_at": "2026-08-14T06:20:00+08:00",
            "supersedes_decision_id": "decision-old",
            "evidence_ids": ["evidence-current"],
        },
        {
            "decision_id": "decision-degraded",
            "work_id": "work-target",
            "status": "proposed",
            "statement": "Review degraded evidence",
            "decided_at": "2026-08-14T06:30:00+08:00",
            "supersedes_decision_id": None,
            "evidence_ids": ["evidence-stale", "evidence-rejected"],
        },
    ]
    snapshot["project"]["current_decision_ids"] = ["decision-current"]
    snapshot["constraints"] = [
        {
            "constraint_id": "constraint-old",
            "status": "superseded",
            "statement": "Use old evidence",
            "scope_work_ids": ["work-active"],
            "expires_at": None,
            "supersedes_constraint_id": None,
            "evidence_ids": ["evidence-old"],
        },
        {
            "constraint_id": "constraint-current",
            "status": "active",
            "statement": "Use current evidence",
            "scope_work_ids": ["work-active"],
            "expires_at": None,
            "supersedes_constraint_id": "constraint-old",
            "evidence_ids": ["evidence-current"],
        },
    ]
    snapshot["project"]["active_constraint_ids"] = ["constraint-current"]
    validate_typed_state(snapshot)
    return snapshot


def _chain_snapshot() -> dict[str, Any]:
    snapshot = build_idea_snapshot()
    snapshot["evidence"] = [_evidence("evidence-chain", "6" * 64)]
    start = datetime(2026, 8, 14, 6, 0, tzinfo=timezone(timedelta(hours=8)))
    snapshot["decisions"] = []
    snapshot["constraints"] = []
    for index in range(_LONG_CHAIN_ITEMS):
        previous_decision = None if index == 0 else f"decision-chain-{index - 1:03d}"
        previous_constraint = (
            None if index == 0 else f"constraint-chain-{index - 1:03d}"
        )
        snapshot["decisions"].append(
            {
                "decision_id": f"decision-chain-{index:03d}",
                "work_id": "work-active",
                "status": (
                    "accepted" if index == _LONG_CHAIN_ITEMS - 1 else "superseded"
                ),
                "statement": f"Decision chain revision {index}",
                "decided_at": (start + timedelta(seconds=index)).isoformat(),
                "supersedes_decision_id": previous_decision,
                "evidence_ids": ["evidence-chain"],
            }
        )
        snapshot["constraints"].append(
            {
                "constraint_id": f"constraint-chain-{index:03d}",
                "status": (
                    "active" if index == _LONG_CHAIN_ITEMS - 1 else "superseded"
                ),
                "statement": f"Constraint chain revision {index}",
                "scope_work_ids": ["work-active"],
                "expires_at": None,
                "supersedes_constraint_id": previous_constraint,
                "evidence_ids": ["evidence-chain"],
            }
        )
    snapshot["project"]["current_decision_ids"] = [
        f"decision-chain-{_LONG_CHAIN_ITEMS - 1:03d}"
    ]
    snapshot["project"]["active_constraint_ids"] = [
        f"constraint-chain-{_LONG_CHAIN_ITEMS - 1:03d}"
    ]
    validate_typed_state(snapshot)
    return snapshot


def _stress_snapshot() -> dict[str, Any]:
    snapshot = build_idea_snapshot()
    snapshot["evidence"] = [
        _evidence(f"evidence-stress-{index:03d}", f"{index + 16:064x}")
        for index in range(_STRESS_MATRIX_SIDE)
    ]
    evidence_ids = [item["evidence_id"] for item in snapshot["evidence"]]
    start = datetime(2026, 8, 14, 6, 0, tzinfo=timezone(timedelta(hours=8)))
    snapshot["decisions"] = [
        {
            "decision_id": f"decision-stress-{index:03d}",
            "work_id": "work-active",
            "status": "accepted" if index == 0 else "proposed",
            "statement": f"Stress Decision {index}",
            "decided_at": (start + timedelta(seconds=index)).isoformat(),
            "supersedes_decision_id": None,
            "evidence_ids": list(evidence_ids),
        }
        for index in range(_STRESS_MATRIX_SIDE)
    ]
    snapshot["project"]["current_decision_ids"] = ["decision-stress-000"]
    validate_typed_state(snapshot)
    return snapshot


def _object_capacity_snapshot() -> dict[str, Any]:
    snapshot = build_idea_snapshot()
    snapshot["evidence"] = [
        _evidence(f"evidence-capacity-{index:05d}", f"{index:064x}", validity="candidate")
        for index in range(10_001)
    ]
    validate_typed_state(snapshot)
    return snapshot


def _reference_capacity_snapshot() -> dict[str, Any]:
    snapshot = build_idea_snapshot()
    snapshot["evidence"] = [
        _evidence(
            f"evidence-reference-{index:05d}",
            f"{index:064x}",
            validity="candidate",
        )
        for index in range(10_000)
    ]
    evidence_ids = [item["evidence_id"] for item in snapshot["evidence"]]
    for work in snapshot["works"]:
        work["evidence_ids"] = list(evidence_ids)
    snapshot["ideas"] = [
        {
            "idea_id": "idea-reference-capacity",
            "parent_work_id": "work-active",
            "source_ref": "opaque://benchmark/reference-capacity",
            "summary": "Exercise the aggregate evidence reference boundary.",
            "status": "candidate",
            "return_work_id": "work-active",
            "expiry": None,
            "attempt_budget": None,
            "promotion_target": None,
            "evidence_ids": list(evidence_ids),
        }
    ]
    snapshot["decisions"] = [
        {
            "decision_id": "decision-reference-capacity",
            "work_id": "work-active",
            "status": "accepted",
            "statement": "Reject aggregate evidence references above the contract.",
            "decided_at": "2026-08-14T07:00:00+08:00",
            "supersedes_decision_id": None,
            "evidence_ids": [evidence_ids[0]],
        }
    ]
    snapshot["project"]["current_decision_ids"] = ["decision-reference-capacity"]
    validate_typed_state(snapshot)
    return snapshot


def _external_projection(
    snapshot: dict[str, Any],
    *,
    signer: HMACExternalStateProjectionSigner,
    request_id: str,
) -> dict[str, Any]:
    response = ExternalStateProjectionProvider(
        _Source(snapshot),
        provider_id="provider-m9-03-benchmark",
        signer=signer,
    ).call_tool(
        EXTERNAL_READ_TOOL,
        {
            "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
            "request_id": request_id,
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
        },
        context=RequestContext("actor-benchmark", "authorization-benchmark"),
    )
    if not response["ok"]:
        raise DecisionEvidenceBenchmarkError("fixture source was rejected")
    return response["result"]


def _resign_projection(
    projection: dict[str, Any], signer: HMACExternalStateProjectionSigner
) -> None:
    body = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    projection["projection_sha256"] = _digest(body)
    projection["signature"] = signer.sign(
        {**body, "projection_sha256": projection["projection_sha256"]}
    )


def _provenance_bundle(
    *,
    root: Path,
    source: dict[str, Any],
    state_payload: bytes,
) -> tuple[dict[str, Any], Any, Any]:
    receipt_payload = (
        root / "experiments/retrieval/m6-01-retrieval-receipt.json"
    ).read_bytes()
    receipt_ref = "artifact://sha256/" + hashlib.sha256(receipt_payload).hexdigest()

    def evidence_resolver(source_ref: str, revision: str) -> bytes | None:
        if (
            source_ref == "state://project/project-idea-benchmark/revision/9"
            and revision == "state:revision:9"
        ):
            return state_payload
        return None

    def artifact_resolver(ref: str) -> bytes | None:
        return receipt_payload if ref == receipt_ref else None

    assertion = compose_assertion_provenance(
        assertion_id="assertion/m9-03/current-decision",
        assertion_text="The current Decision is supported by current State.",
        bearing=True,
        evidence=[
            {
                "evidence_id": "evidence-current",
                "authority_kind": "current_state",
                "source_ref": "state://project/project-idea-benchmark/revision/9",
                "revision": "state:revision:9",
                "sha256": hashlib.sha256(state_payload).hexdigest(),
                "valid_at": "2026-08-14T06:01:00+08:00",
                "retrieval_receipt_ref": receipt_ref,
            }
        ],
        asserted_at="2026-08-14T06:02:00+08:00",
        valid_until="2026-09-17T22:00:00+08:00",
        evidence_resolver=evidence_resolver,
        artifact_resolver=artifact_resolver,
    )
    claim = {
        "claim_id": "claim/m9-03/current-decision",
        "work_id": "work-active",
        "claim_kind": "decision",
        "statement": "Use the current path",
        "evidence_assertion_ids": [assertion["assertion_id"]],
        "scope_refs": [],
    }
    verdict = evaluate_claim_evidence_gate(
        claim,
        evidence_records=[assertion],
        current_time="2026-08-14T06:03:00+08:00",
        evidence_resolver=evidence_resolver,
        artifact_resolver=artifact_resolver,
    )
    bundle = {
        "schema_version": "context.decision-evidence-provenance-bundle/v1alpha1",
        "project_id": source["project_id"],
        "state_revision": source["state_revision"],
        "state_sha256": source["state_sha256"],
        "source_projection_sha256": source["projection_sha256"],
        "assertion_records": [assertion],
        "claims": [claim],
        "claim_verdicts": [verdict],
        "bindings": [
            {
                "object_kind": "decision",
                "object_id": "decision-current",
                "typed_evidence_id": "evidence-current",
                "assertion_id": assertion["assertion_id"],
                "assertion_evidence_id": "evidence-current",
                "assertion_record_sha256": assertion["record_sha256"],
                "claim_id": claim["claim_id"],
                "claim_sha256": verdict["claim_sha256"],
                "gate_id": verdict["gate_id"],
                "verdict_sha256": verdict["verdict_sha256"],
            }
        ],
        "bundle_sha256": "",
    }
    bundle["bundle_sha256"] = _digest(
        {key: value for key, value in bundle.items() if key != "bundle_sha256"}
    )
    return bundle, evidence_resolver, artifact_resolver


def _scenarios(root: Path) -> dict[str, Any]:
    signer = HMACExternalStateProjectionSigner(
        key_id="key-m9-03-benchmark",
        secret=b"m9-03-decision-evidence-benchmark-key",
    )
    state_payload = b"M9-03 benchmark current State evidence"
    baseline_snapshot = _baseline_snapshot(hashlib.sha256(state_payload).hexdigest())
    baseline = _external_projection(
        baseline_snapshot,
        signer=signer,
        request_id="request-m9-03-baseline",
    )
    bundle, evidence_resolver, artifact_resolver = _provenance_bundle(
        root=root,
        source=baseline,
        state_payload=state_payload,
    )
    chain_snapshot = _chain_snapshot()
    stress_snapshot = _stress_snapshot()
    object_capacity = _external_projection(
        _object_capacity_snapshot(),
        signer=signer,
        request_id="request-m9-03-object-capacity",
    )
    try:
        build_decision_evidence_projection(
            object_capacity,
            signer=signer,
            observed_at="2026-08-17T22:00:00+08:00",
        )
    except DecisionEvidenceProjectionError:
        object_capacity_rejected = True
    else:
        object_capacity_rejected = False
    reference_capacity = _external_projection(
        _reference_capacity_snapshot(),
        signer=signer,
        request_id="request-m9-03-reference-capacity",
    )
    try:
        build_decision_evidence_projection(
            reference_capacity,
            signer=signer,
            observed_at="2026-08-17T22:00:00+08:00",
        )
    except DecisionEvidenceProjectionError:
        reference_capacity_rejected = True
    else:
        reference_capacity_rejected = False
    return {
        "signer": signer,
        "baseline_snapshot": baseline_snapshot,
        "baseline": baseline,
        "bundle": bundle,
        "evidence_resolver": evidence_resolver,
        "artifact_resolver": artifact_resolver,
        "chain_snapshot": chain_snapshot,
        "chain": _external_projection(
            chain_snapshot,
            signer=signer,
            request_id="request-m9-03-chain",
        ),
        "stress_snapshot": stress_snapshot,
        "stress": _external_projection(
            stress_snapshot,
            signer=signer,
            request_id="request-m9-03-stress",
        ),
        "object_capacity_rejected": object_capacity_rejected,
        "reference_capacity_rejected": reference_capacity_rejected,
    }


def _provenance(root: Path) -> dict[str, str]:
    return {
        "implementation_sha256": _file_digest(
            root / "context_control_plane/decision_evidence_projection.py"
        ),
        "benchmark_sha256": _file_digest(
            root / "context_control_plane/decision_evidence_benchmark.py"
        ),
        "external_state_provider_sha256": _file_digest(
            root / "context_control_plane/external_state_provider.py"
        ),
        "typed_state_sha256": _file_digest(
            root / "context_control_plane/typed_state.py"
        ),
        "assertion_provenance_sha256": _file_digest(
            root / "context_control_plane/assertion_provenance.py"
        ),
        "claim_evidence_gate_sha256": _file_digest(
            root / "context_control_plane/claim_evidence_gate.py"
        ),
        "retrieval_receipt_sha256": _file_digest(
            root / "experiments/retrieval/m6-01-retrieval-receipt.json"
        ),
    }


def _failed_gates(
    results: dict[str, int | float | bool],
    latency: dict[str, float],
    *,
    evaluate_latency: bool,
) -> list[str]:
    checks = {
        "same-revision": results["same_revision_rate"]
        >= _THRESHOLDS["same_revision_rate_min"],
        "decision-timeline-complete": results["decision_timeline_complete_rate"]
        >= _THRESHOLDS["decision_timeline_complete_rate_min"],
        "constraint-matrix-complete": results["constraint_matrix_complete_rate"]
        >= _THRESHOLDS["constraint_matrix_complete_rate_min"],
        "evidence-matrix-complete": results["evidence_matrix_complete_rate"]
        >= _THRESHOLDS["evidence_matrix_complete_rate_min"],
        "current-decision": results["current_decision_rate"]
        >= _THRESHOLDS["current_decision_rate_min"],
        "supersedes-chain": results["supersedes_chain_rate"]
        >= _THRESHOLDS["supersedes_chain_rate_min"],
        "metadata-only-honesty": results["metadata_only_honesty_rate"]
        >= _THRESHOLDS["metadata_only_honesty_rate_min"],
        "validated-provenance": results["validated_provenance_rate"]
        >= _THRESHOLDS["validated_provenance_rate_min"],
        "evidence-classification": results["evidence_classification_rate"]
        >= _THRESHOLDS["evidence_classification_rate_min"],
        "tamper-rejection": results["tamper_rejection_rate"]
        >= _THRESHOLDS["tamper_rejection_rate_min"],
        "object-capacity-rejection": results["object_capacity_rejected"]
        is _THRESHOLDS["object_capacity_rejection_required"],
        "reference-capacity-rejection": results["reference_capacity_rejected"]
        is _THRESHOLDS["reference_capacity_rejection_required"],
        "false-support": results["false_supports"]
        <= _THRESHOLDS["false_supports_max"],
        "old-decision-resurrection": results["old_decision_resurrections"]
        <= _THRESHOLDS["old_decision_resurrections_max"],
        "authority": results["authority_violations"]
        <= _THRESHOLDS["authority_violations_max"],
        "provider": results["provider_invocations"]
        <= _THRESHOLDS["provider_invocations_max"],
        "external-service": results["external_services"]
        <= _THRESHOLDS["external_services_max"],
    }
    if evaluate_latency:
        checks["latency"] = (
            latency["p95"] < _THRESHOLDS["worst_path_latency_p95_ms_max"]
        )
    return [name for name, passed in checks.items() if not passed]


def benchmark_decision_evidence_projection(
    *,
    root: str | Path,
    iterations: int,
    generated_at: str,
) -> dict[str, Any]:
    """Measure Decision/Evidence integrity, scale, and zero-authority gates."""
    if type(iterations) is not int or not 0 < iterations <= 1000:
        raise ValueError("iterations must be an integer between 1 and 1000")
    if not isinstance(generated_at, str) or _TIMESTAMP_RE.fullmatch(generated_at) is None:
        raise ValueError("generated_at must be RFC3339")
    try:
        parsed_generated_at = datetime.fromisoformat(
            generated_at.replace("Z", "+00:00")
        )
    except ValueError as exc:
        raise ValueError("generated_at must be RFC3339") from exc
    if parsed_generated_at.tzinfo is None:
        raise ValueError("generated_at must include timezone")

    root = Path(root).resolve()
    scenarios = _scenarios(root)
    signer = scenarios["signer"]
    counts = {field: 0 for field in _COUNT_FIELDS}
    false_supports = 0
    old_decision_resurrections = 0
    authority_violations = 0
    latencies: list[float] = []
    for _ in range(iterations):
        run_latencies: list[float] = []

        started = time.perf_counter_ns()
        baseline = build_decision_evidence_projection(
            scenarios["baseline"],
            signer=signer,
            observed_at=generated_at,
            provenance_bundle=scenarios["bundle"],
            evidence_resolver=scenarios["evidence_resolver"],
            artifact_resolver=scenarios["artifact_resolver"],
        )
        validate_decision_evidence_projection(
            baseline,
            source_projection=scenarios["baseline"],
            signer=signer,
            provenance_bundle=scenarios["bundle"],
            evidence_resolver=scenarios["evidence_resolver"],
            artifact_resolver=scenarios["artifact_resolver"],
        )
        run_latencies.append((time.perf_counter_ns() - started) / 1_000_000)

        started = time.perf_counter_ns()
        chain = build_decision_evidence_projection(
            scenarios["chain"],
            signer=signer,
            observed_at=generated_at,
        )
        validate_decision_evidence_projection(
            chain,
            source_projection=scenarios["chain"],
            signer=signer,
        )
        run_latencies.append((time.perf_counter_ns() - started) / 1_000_000)

        started = time.perf_counter_ns()
        stress = build_decision_evidence_projection(
            scenarios["stress"],
            signer=signer,
            observed_at=generated_at,
        )
        validate_decision_evidence_projection(
            stress,
            source_projection=scenarios["stress"],
            signer=signer,
        )
        run_latencies.append((time.perf_counter_ns() - started) / 1_000_000)

        same_revision = all(
            projection["state_revision"] == scenarios[name]["state_revision"]
            for projection, name in (
                (baseline, "baseline"),
                (chain, "chain"),
                (stress, "stress"),
            )
        )
        counts["same_revision_matches"] += same_revision
        counts["decision_timeline_complete_matches"] += (
            len(baseline["decision_timeline"])
            == len(scenarios["baseline_snapshot"]["decisions"])
            and len(chain["decision_timeline"]) == _LONG_CHAIN_ITEMS
            and len(stress["decision_timeline"]) == _STRESS_MATRIX_SIDE
        )
        counts["constraint_matrix_complete_matches"] += (
            len(baseline["constraint_matrix"])
            == len(scenarios["baseline_snapshot"]["constraints"])
            and len(chain["constraint_matrix"]) == _LONG_CHAIN_ITEMS
        )
        stress_cells = sum(
            len(item["referencing_objects"]) for item in stress["evidence_matrix"]
        )
        counts["evidence_matrix_complete_matches"] += (
            len(baseline["evidence_matrix"])
            == len(scenarios["baseline_snapshot"]["evidence"])
            and stress_cells == _STRESS_MATRIX_CELLS
        )
        baseline_current = [
            item["decision_id"]
            for item in baseline["decision_timeline"]
            if item["is_current"]
        ]
        chain_current = [
            item["decision_id"]
            for item in chain["decision_timeline"]
            if item["is_current"]
        ]
        counts["current_decision_matches"] += (
            baseline_current == ["decision-current"]
            and chain_current == [f"decision-chain-{_LONG_CHAIN_ITEMS - 1:03d}"]
        )
        chain_by_id = {
            item["decision_id"]: item for item in chain["decision_timeline"]
        }
        counts["supersedes_chain_matches"] += all(
            chain_by_id[f"decision-chain-{index:03d}"]["supersedes_decision_id"]
            == (None if index == 0 else f"decision-chain-{index - 1:03d}")
            for index in range(_LONG_CHAIN_ITEMS)
        )
        metadata_honest = (
            chain["capabilities"]["provenance_mode"] == "metadata-only"
            and not chain["capabilities"]["assertion_provenance"]
            and not chain["capabilities"]["claim_evidence"]
            and all(
                item["support_status"] != "validated"
                for item in chain["decision_timeline"]
            )
        )
        counts["metadata_only_honesty_matches"] += metadata_honest
        false_supports += sum(
            item["support_status"] == "validated"
            for item in chain["decision_timeline"]
        )
        current = next(
            item
            for item in baseline["decision_timeline"]
            if item["decision_id"] == "decision-current"
        )
        current_evidence = next(
            item
            for item in baseline["evidence_matrix"]
            if item["evidence_id"] == "evidence-current"
        )
        counts["validated_provenance_matches"] += (
            baseline["capabilities"]["provenance_mode"] == "validated"
            and current["support_status"] == "validated"
            and len(current_evidence["provenance"]) == 1
        )
        display = {
            item["evidence_id"]: item["display_status"]
            for item in baseline["evidence_matrix"]
        }
        counts["evidence_classification_matches"] += (
            display["evidence-stale"] == "stale"
            and display["evidence-rejected"] == "rejected"
            and display["evidence-unreferenced"] == "unreferenced"
            and display["evidence-old"] == "superseded"
            and display["evidence-current"] == "current"
        )
        old_decision_resurrections += sum(
            item["is_current"]
            for item in chain["decision_timeline"][:-1]
        )
        authority_violations += sum(
            value is not False
            for value in (
                baseline["authority"]["state_write_authority"],
                baseline["authority"]["completion_authority"],
                baseline["authority"]["approval_authority"],
            )
        )
        authority_violations += baseline["authority"]["provider_authority"]
        authority_violations += baseline["authority"]["external_effect_authority"]

        forged = copy.deepcopy(baseline)
        forged["decision_timeline"][0]["is_current"] = True
        _resign_projection(forged, signer)
        started = time.perf_counter_ns()
        try:
            validate_decision_evidence_projection(
                forged,
                source_projection=scenarios["baseline"],
                signer=signer,
                provenance_bundle=scenarios["bundle"],
                evidence_resolver=scenarios["evidence_resolver"],
                artifact_resolver=scenarios["artifact_resolver"],
            )
        except DecisionEvidenceProjectionError:
            counts["tamper_rejections"] += 1
        run_latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        latencies.append(max(run_latencies))

    results: dict[str, int | float | bool] = dict(counts)
    for count_field, rate_field in zip(_COUNT_FIELDS, _RATE_FIELDS, strict=True):
        results[rate_field] = counts[count_field] / iterations
    results.update(
        {
            "object_capacity_rejected": scenarios["object_capacity_rejected"],
            "reference_capacity_rejected": scenarios[
                "reference_capacity_rejected"
            ],
            "false_supports": false_supports,
            "old_decision_resurrections": old_decision_resurrections,
            "authority_violations": authority_violations,
            "provider_invocations": 0,
            "external_services": 0,
        }
    )
    latency_samples = [round(value, 6) for value in latencies]
    latency = _latency(latency_samples)
    latency_gate_evaluated = iterations >= _MIN_PERFORMANCE_SAMPLES
    failed_gates = _failed_gates(
        results, latency, evaluate_latency=latency_gate_evaluated
    )
    receipt: dict[str, Any] = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": BENCHMARK_ID,
        "generated_at": generated_at,
        "parameters": {
            "iterations": iterations,
            "long_chain_items": _LONG_CHAIN_ITEMS,
            "stress_matrix_side": _STRESS_MATRIX_SIDE,
            "stress_matrix_cells": _STRESS_MATRIX_CELLS,
            "max_evidence_references": _MAX_EVIDENCE_REFERENCES,
            "latency_path": "max(baseline-validated,chain-128,matrix-4096,tamper-rebuild)",
            "latency_gate_evaluated": latency_gate_evaluated,
        },
        "thresholds": copy.deepcopy(_THRESHOLDS),
        "results": results,
        "latency_ms": latency,
        "latency_samples_ms": latency_samples,
        "attestation": {
            "mode": "local-unattested",
            "measurement_authenticity": False,
            "zero_count_source": "fixture-instrumentation",
        },
        "gate": {
            "status": "passed" if not failed_gates else "failed",
            "failed_gates": failed_gates,
        },
        "provenance": _provenance(root),
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    return receipt


def validate_decision_evidence_benchmark(
    receipt: dict[str, Any], *, root: str | Path
) -> None:
    """Validate a measured receipt and reject all failed-gate receipts."""
    if (
        not isinstance(receipt, dict)
        or set(receipt) != _TOP_LEVEL_FIELDS
        or receipt["schema_version"] != BENCHMARK_SCHEMA_VERSION
        or receipt["benchmark_id"] != BENCHMARK_ID
    ):
        raise DecisionEvidenceBenchmarkError("benchmark fields are invalid")
    generated_at = receipt["generated_at"]
    if not isinstance(generated_at, str) or _TIMESTAMP_RE.fullmatch(generated_at) is None:
        raise DecisionEvidenceBenchmarkError("generated_at is invalid")
    try:
        parsed_generated_at = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecisionEvidenceBenchmarkError("generated_at is invalid") from exc
    if parsed_generated_at.tzinfo is None:
        raise DecisionEvidenceBenchmarkError("generated_at requires timezone")
    parameters = receipt["parameters"]
    expected_parameter_fields = {
        "iterations",
        "long_chain_items",
        "stress_matrix_side",
        "stress_matrix_cells",
        "max_evidence_references",
        "latency_path",
        "latency_gate_evaluated",
    }
    if not isinstance(parameters, dict) or set(parameters) != expected_parameter_fields:
        raise DecisionEvidenceBenchmarkError("benchmark parameters are invalid")
    iterations = parameters["iterations"]
    if type(iterations) is not int or not 0 < iterations <= 1000:
        raise DecisionEvidenceBenchmarkError("benchmark iterations are invalid")
    if type(parameters["latency_gate_evaluated"]) is not bool:
        raise DecisionEvidenceBenchmarkError("latency gate evaluation flag is invalid")
    if parameters != {
        "iterations": iterations,
        "long_chain_items": _LONG_CHAIN_ITEMS,
        "stress_matrix_side": _STRESS_MATRIX_SIDE,
        "stress_matrix_cells": _STRESS_MATRIX_CELLS,
        "max_evidence_references": _MAX_EVIDENCE_REFERENCES,
        "latency_path": "max(baseline-validated,chain-128,matrix-4096,tamper-rebuild)",
        "latency_gate_evaluated": iterations >= _MIN_PERFORMANCE_SAMPLES,
    }:
        raise DecisionEvidenceBenchmarkError("benchmark workload is invalid")
    thresholds = receipt["thresholds"]
    if not isinstance(thresholds, dict) or set(thresholds) != set(_THRESHOLDS):
        raise DecisionEvidenceBenchmarkError("benchmark thresholds are invalid")
    for field in (
        "same_revision_rate_min",
        "decision_timeline_complete_rate_min",
        "constraint_matrix_complete_rate_min",
        "evidence_matrix_complete_rate_min",
        "current_decision_rate_min",
        "supersedes_chain_rate_min",
        "metadata_only_honesty_rate_min",
        "validated_provenance_rate_min",
        "evidence_classification_rate_min",
        "tamper_rejection_rate_min",
        "worst_path_latency_p95_ms_max",
    ):
        if type(thresholds[field]) is not float:
            raise DecisionEvidenceBenchmarkError("benchmark threshold type is invalid")
    if any(
        type(thresholds[field]) is not bool
        for field in (
            "object_capacity_rejection_required",
            "reference_capacity_rejection_required",
        )
    ):
        raise DecisionEvidenceBenchmarkError("benchmark threshold type is invalid")
    for field in (
        "false_supports_max",
        "old_decision_resurrections_max",
        "authority_violations_max",
        "provider_invocations_max",
        "external_services_max",
    ):
        if type(thresholds[field]) is not int:
            raise DecisionEvidenceBenchmarkError("benchmark threshold type is invalid")
    if thresholds != _THRESHOLDS:
        raise DecisionEvidenceBenchmarkError("benchmark thresholds are invalid")
    results = receipt["results"]
    if not isinstance(results, dict) or set(results) != _RESULT_FIELDS:
        raise DecisionEvidenceBenchmarkError("benchmark results are invalid")
    if any(
        type(results[field]) is not bool
        for field in ("object_capacity_rejected", "reference_capacity_rejected")
    ):
        raise DecisionEvidenceBenchmarkError("capacity result is invalid")
    for field in _COUNT_FIELDS:
        if type(results[field]) is not int or not 0 <= results[field] <= iterations:
            raise DecisionEvidenceBenchmarkError("result count is invalid")
    for field in _ZERO_FIELDS:
        if type(results[field]) is not int or results[field] < 0:
            raise DecisionEvidenceBenchmarkError("zero-tolerance result is invalid")
    for count_field, rate_field in zip(_COUNT_FIELDS, _RATE_FIELDS, strict=True):
        expected_rate = results[count_field] / iterations
        if type(results[rate_field]) is not float or results[rate_field] != expected_rate:
            raise DecisionEvidenceBenchmarkError("result rate is invalid")
    latency = receipt["latency_ms"]
    if not isinstance(latency, dict) or set(latency) != {"min", "p50", "p95", "max"}:
        raise DecisionEvidenceBenchmarkError("latency fields are invalid")
    latency_values = [latency[field] for field in ("min", "p50", "p95", "max")]
    if any(
        type(value) not in {int, float}
        or not math.isfinite(value)
        or value < 0
        for value in latency_values
    ) or latency_values != sorted(latency_values):
        raise DecisionEvidenceBenchmarkError("latency values are invalid")
    latency_samples = receipt["latency_samples_ms"]
    if (
        not isinstance(latency_samples, list)
        or len(latency_samples) != iterations
        or any(
            type(value) is not float or not math.isfinite(value) or value <= 0
            for value in latency_samples
        )
        or latency != _latency(latency_samples)
    ):
        raise DecisionEvidenceBenchmarkError("latency samples are invalid")
    if receipt["attestation"] != {
        "mode": "local-unattested",
        "measurement_authenticity": False,
        "zero_count_source": "fixture-instrumentation",
    }:
        raise DecisionEvidenceBenchmarkError("benchmark attestation is invalid")
    if receipt["provenance"] != _provenance(Path(root).resolve()):
        raise DecisionEvidenceBenchmarkError("benchmark provenance mismatch")
    failed_gates = _failed_gates(
        results,
        latency,
        evaluate_latency=parameters["latency_gate_evaluated"],
    )
    expected_gate = {
        "status": "passed" if not failed_gates else "failed",
        "failed_gates": failed_gates,
    }
    if receipt["gate"] != expected_gate:
        raise DecisionEvidenceBenchmarkError("gate derivation is invalid")
    if failed_gates:
        raise DecisionEvidenceBenchmarkError("benchmark failed acceptance gates")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt["receipt_sha256"] != _digest(body):
        raise DecisionEvidenceBenchmarkError("receipt digest mismatch")


__all__ = [
    "BENCHMARK_SCHEMA_VERSION",
    "DecisionEvidenceBenchmarkError",
    "benchmark_decision_evidence_projection",
    "validate_decision_evidence_benchmark",
]
