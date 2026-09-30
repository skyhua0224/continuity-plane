"""Deterministic builders for committed M6 acceptance receipts."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from .assertion_provenance import compose_assertion_provenance
from .codegraph_verification import probe_codegraph_relation
from .mcp_admission import decide_mcp_admission, mcp_registry_snapshot_sha256
from .memory_ablation import benchmark_memory_ablation
from .recall_provider import (
    InMemoryRecallProvider,
    RecallCoordinator,
    UnavailableRecallProvider,
)
from .retrieval_benchmark import benchmark_retrieval
from .retrieval_routing import (
    canonical_retrieval_receipt_bytes,
    compose_retrieval_receipt,
    plan_retrieval,
)
from .reviewer_adapter import (
    LocalReviewerAdapter,
    ReviewCoordinator,
    TimeoutReviewerAdapter,
)

ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_MEASUREMENT_FIELDS = {"p50_ms", "p95_ms", "max_ms"}


def _sha256_file(relative_path: str) -> str:
    return hashlib.sha256((ROOT / relative_path).read_bytes()).hexdigest()


def canonical_m6_semantic_bundle_bytes(
    fixtures: dict[str, dict[str, Any]],
) -> bytes:
    """Return replay-comparable bytes without host-dependent timing values."""
    normalized = copy.deepcopy(fixtures)
    retrieval_name = "m6-01-retrieval-results.json"
    assertion_name = "m6-07-assertion-provenance.json"
    if retrieval_name not in normalized or assertion_name not in normalized:
        raise ValueError("M6 semantic bundle requires retrieval and assertion receipts")
    retrieval = normalized[retrieval_name]
    for field in _RUNTIME_MEASUREMENT_FIELDS:
        retrieval.pop(field, None)
    retrieval.pop("outcomes_sha256", None)
    retrieval.pop("receipt_sha256", None)
    for outcome in retrieval.get("sample_outcomes", []):
        outcome.pop("duration_ms", None)
    semantic_retrieval_sha256 = hashlib.sha256(
        json.dumps(
            retrieval, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    assertion = normalized[assertion_name]
    for evidence in assertion.get("evidence", []):
        if evidence.get("source_ref") == (
            "repo://context-control-plane/"
            "experiments/retrieval/m6-01-retrieval-results.json"
        ):
            evidence["sha256"] = semantic_retrieval_sha256
            evidence["revision"] = f"worktree:sha256:{semantic_retrieval_sha256}"
    assertion.pop("record_sha256", None)
    return json.dumps(
        normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _codegraph_receipt() -> dict[str, Any]:
    return probe_codegraph_relation(
        root=ROOT,
        clue_id="clue/m6-02/retrieval-to-benchmark",
        source_repository="context-control-plane",
        target_repository="context-control-plane",
        source_symbol="context_control_plane.retrieval_benchmark._plan",
        target_symbol="context_control_plane.retrieval_routing.plan_retrieval",
        relation="references",
        source_path="context_control_plane/retrieval_benchmark.py",
        symbol_name="plan_retrieval",
        index_revision="worktree:m6-codegraph-probe",
        verified_at="2026-08-15T07:15:00Z",
    )


def _line_evidence(
    *, relative_path: str, needle: bytes, evidence_id: str, retrieved_at: str
) -> dict[str, Any]:
    payload = (ROOT / relative_path).read_bytes()
    offset = payload.index(needle)
    line_end = payload.find(b"\n", offset)
    if line_end < 0:
        line_end = len(payload)
    return {
        "evidence_id": evidence_id,
        "source_kind": "current_code",
        "source_ref": f"repo://context-control-plane/{relative_path}#bytes={offset}:{line_end - offset}",
        "revision": "worktree:m6-measured-retrieval",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "range": {"offset_bytes": offset, "length_bytes": line_end - offset},
        "retrieved_at": retrieved_at,
        "valid_at": retrieved_at,
    }


def _retrieval_receipt(
    codegraph_receipt: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    observed_at = "2026-08-15T07:15:00Z"
    evidence = [
        _line_evidence(
            relative_path="context_control_plane/retrieval_benchmark.py",
            needle=b"return plan_retrieval(",
            evidence_id="evidence/m6-01/benchmark-call",
            retrieved_at=observed_at,
        ),
        _line_evidence(
            relative_path="context_control_plane/retrieval_routing.py",
            needle=b"def plan_retrieval(",
            evidence_id="evidence/m6-01/router-definition",
            retrieved_at=observed_at,
        ),
    ]
    plan = plan_retrieval(
        {
            "question_id": "question/m6-01/acceptance",
            "kind": "symbol_definition",
            "query": "plan_retrieval",
            "repositories": ["context-control-plane"],
            "freshness_required": True,
        },
        available_tools={"rg", "lsp"},
        max_queries=2,
        max_scanned_bytes=262_144,
        max_returned_bytes=8_192,
        max_index_age_seconds=3_600,
    )
    source_size = (ROOT / "context_control_plane/retrieval_benchmark.py").stat().st_size
    target_size = (ROOT / "context_control_plane/retrieval_routing.py").stat().st_size
    receipt = compose_retrieval_receipt(
        plan=plan,
        evidence=evidence,
        step_results=[
            {
                "tool": "rg",
                "queries": 1,
                "scanned_bytes": source_size,
                "returned_bytes": evidence[0]["range"]["length_bytes"],
                "index_revision": None,
                "index_sha256": None,
                "index_age_seconds": 0,
            },
            {
                "tool": "lsp",
                "queries": 1,
                "scanned_bytes": source_size + target_size,
                "returned_bytes": len(
                    json.dumps(
                        next(
                            item["output"]
                            for item in codegraph_receipt["verifier_evidence"]
                            if item["verifier"] == "lsp"
                        ),
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ),
                "index_revision": codegraph_receipt["clues"][0]["index_revision"],
                "index_sha256": codegraph_receipt["receipt_sha256"],
                "index_age_seconds": 0,
            },
        ],
        executed_at="2026-08-15T07:15:01Z",
        cache_status="miss",
        prior_receipt_ref=None,
    )
    return plan, receipt


def _recall_receipts() -> tuple[dict[str, Any], dict[str, Any]]:
    content = "M6 bounded retrieval is the current candidate decision."
    provider = InMemoryRecallProvider(
        [
            {
                "record_id": "memory/m6-current-retrieval",
                "content": content,
                "tags": ["m6", "current", "retrieval"],
                "observed_at": "2026-08-15T07:00:00Z",
                "valid_until": "2026-08-16T07:00:00Z",
                "source_ref": "event://decision/m6-retrieval",
                "source_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
        ]
    )
    request = {
        "request_id": "recall/m6-03/acceptance",
        "task_id": "M6-03",
        "query": "m6 current retrieval",
        "max_candidates": 2,
        "max_returned_bytes": 512,
    }
    completed = RecallCoordinator(provider).retrieve(
        request, observed_at="2026-08-15T07:15:00Z"
    )
    failed = RecallCoordinator(UnavailableRecallProvider("503")).retrieve(
        {**request, "request_id": "recall/m6-03/503"},
        observed_at="2026-08-15T07:15:00Z",
    )
    return completed, failed


def _review_receipts() -> tuple[dict[str, Any], dict[str, Any]]:
    artifact = "artifact://sha256/" + _sha256_file(
        "context_control_plane/retrieval_routing.py"
    )
    request = {
        "request_id": "review/m6-05/acceptance",
        "task_id": "M6-05",
        "execution_packet_ref": "artifact://sha256/" + _sha256_file(
            "context_control_plane/execution_packet.py"
        ),
        "artifact_refs": [artifact],
        "max_output_bytes": 2048,
        "deadline_at": "2026-08-15T07:20:00Z",
    }
    finding = {
        "finding_id": "finding/m6-retrieval-budget",
        "severity": "medium",
        "summary": "Keep scan, return, freshness and provenance gates fail closed.",
        "evidence_refs": [artifact],
        "candidate_only": True,
    }
    local = ReviewCoordinator(LocalReviewerAdapter([finding])).review(
        request, observed_at="2026-08-15T07:15:00Z"
    )
    timed_out = ReviewCoordinator(TimeoutReviewerAdapter()).review(
        {**request, "request_id": "review/m6-05/timeout"},
        observed_at="2026-08-15T07:21:00Z",
    )
    return local, timed_out


def _mcp_fixtures() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    tools = [
        {"name": "reference.search", "scope": "read", "requires_state_mcp": False},
        {"name": "state.commit", "scope": "state_write", "requires_state_mcp": True},
        {"name": "issue.create", "scope": "external_effect", "requires_state_mcp": True},
    ]
    snapshot = {
        "schema_version": "context.mcp-registry-snapshot/v1alpha1",
        "snapshot_id": "mcp-registry/fixture/2026-08-15",
        "registry_kind": "fixture",
        "registry_url": "https://example.invalid/context-mcp-fixture",
        "registry_revision": "1111111111111111111111111111111111111111",
        "registry_sha256": "",
        "retrieved_at": "2026-08-15T07:15:00Z",
        "servers": [
            {
                "server_id": "fixture/reference-server",
                "publisher": "fixture-publisher",
                "publisher_verified": True,
                "license_ref": "Apache-2.0",
                "license_verified": True,
                "source_url": "https://example.invalid/reference-server",
                "source_revision": "2222222222222222222222222222222222222222",
                "source_sha256": hashlib.sha256(b"fixture/reference-server").hexdigest(),
                "auth_kind": "token",
                "tools": tools,
            }
        ],
    }
    snapshot["registry_sha256"] = mcp_registry_snapshot_sha256(snapshot)
    base_request = {
        "request_id": "mcp-admission/m6-06/read",
        "project_id": "context-control-plane",
        "operation_id": "operation/m6-06/research",
        "requested_server_ids": ["fixture/reference-server"],
        "granted_scopes": ["read"],
        "available_auth_kinds": ["token"],
        "allow_external_effects": False,
        "state_mcp_route": None,
        "expected_registry_revision": snapshot["registry_revision"],
        "expected_registry_sha256": snapshot["registry_sha256"],
    }
    read = decide_mcp_admission(snapshot, base_request)
    unauthorized = decide_mcp_admission(
        snapshot,
        {**base_request, "request_id": "mcp-admission/m6-06/unauthorized-write"},
    )
    return snapshot, read, unauthorized


def _assertion_record(
    retrieval_result: dict[str, Any], retrieval_receipt: dict[str, Any]
) -> dict[str, Any]:
    retrieval_bytes = (
        json.dumps(
            retrieval_result,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    retrieval_sha256 = hashlib.sha256(retrieval_bytes).hexdigest()
    receipt_bytes = canonical_retrieval_receipt_bytes(retrieval_receipt)
    receipt_ref = "artifact://sha256/" + hashlib.sha256(receipt_bytes).hexdigest()
    source_ref = (
        "repo://context-control-plane/"
        "experiments/retrieval/m6-01-retrieval-results.json"
    )

    def evidence_resolver(ref: str, revision: str) -> bytes | None:
        if ref == source_ref and revision == f"worktree:sha256:{retrieval_sha256}":
            return retrieval_bytes
        return None

    def artifact_resolver(ref: str) -> bytes | None:
        return receipt_bytes if ref == receipt_ref else None

    return compose_assertion_provenance(
        assertion_id="assertion/m6-07/e5-reduction",
        assertion_text=(
            "The fixed E5 fixture reduced duplicate read bytes by "
            f"{retrieval_result['duplicate_read_reduction_percent']} percent with full provenance."
        ),
        bearing=True,
        evidence=[
            {
                "evidence_id": "evidence/m6-07/retrieval-benchmark",
                "authority_kind": "current_code",
                "source_ref": source_ref,
                "revision": f"worktree:sha256:{retrieval_sha256}",
                "sha256": retrieval_sha256,
                "valid_at": "2026-08-15T07:15:00Z",
                "retrieval_receipt_ref": receipt_ref,
            }
        ],
        asserted_at="2026-08-15T07:15:01Z",
        valid_until="2026-09-15T07:15:01Z",
        evidence_resolver=evidence_resolver,
        artifact_resolver=artifact_resolver,
    )


def build_m6_acceptance_fixtures(*, samples: int = 1000) -> dict[str, dict[str, Any]]:
    """Build all independently validated M6 evidence documents."""
    retrieval = benchmark_retrieval(samples=samples)
    codegraph = _codegraph_receipt()
    retrieval_plan, retrieval_receipt = _retrieval_receipt(codegraph)
    recall_completed, recall_503 = _recall_receipts()
    review_local, review_timeout = _review_receipts()
    mcp_snapshot, mcp_read, mcp_unauthorized = _mcp_fixtures()
    return {
        "m6-01-retrieval-results.json": retrieval,
        "m6-01-retrieval-plan.json": retrieval_plan,
        "m6-01-retrieval-receipt.json": retrieval_receipt,
        "m6-02-codegraph-verification.json": codegraph,
        "m6-03-recall-completed.json": recall_completed,
        "m6-03-recall-503.json": recall_503,
        "m6-04-memory-ablation.json": benchmark_memory_ablation(samples=samples),
        "m6-05-review-local.json": review_local,
        "m6-05-review-timeout.json": review_timeout,
        "m6-06-registry-snapshot.json": mcp_snapshot,
        "m6-06-read-admission.json": mcp_read,
        "m6-06-unauthorized-write-admission.json": mcp_unauthorized,
        "m6-07-assertion-provenance.json": _assertion_record(
            retrieval, retrieval_receipt
        ),
    }
