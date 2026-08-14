"""Offline quantitative benchmark for the M4-09 Skill resolver."""

from __future__ import annotations

import copy
import hashlib
import json
import random
import time
from pathlib import Path
from typing import Any

from .skill_catalog import canonical_skill_catalog_bytes
from .skill_resolver import (
    canonical_skill_resolution_decision_bytes,
    resolve_skills,
)

SCHEMA_VERSION = "context.skill-resolution-benchmark/v1alpha1"


def _range() -> dict[str, Any]:
    return {
        "minimum": "1.0.0",
        "minimum_inclusive": True,
        "maximum": "1.0.0",
        "maximum_inclusive": True,
    }


def _entry(
    skill_id: str,
    source_kind: str,
    applicability: list[dict[str, str]],
    *,
    status: str = "approved",
    dependencies: list[str] | None = None,
    provider_contracts: list[str] | None = None,
) -> dict[str, Any]:
    manifest_status = "active" if status == "active" else "approved"
    manifest = {
        "skill_id": skill_id,
        "version": "1.0.0",
        "content_sha256": hashlib.sha256(skill_id.encode()).hexdigest(),
        "source_kind": source_kind,
        "license_ref": "MIT",
        "applicability": applicability,
        "rule_ids": [f"rule.{skill_id}.main"],
        "dependencies": [
            {"skill_id": dependency, "version_range": _range()}
            for dependency in (dependencies or [])
        ],
        "conflicts": [],
        "expires_at": None,
        "compatibility": {
            "schema_refs": ["context.compiled-skill-packet/v1alpha1"],
            "provider_contract_refs": provider_contracts
            if provider_contracts is not None
            else ["provider://codex/v1"],
        },
        "provenance_refs": ["artifact://sha256/" + "a" * 64],
        "status": manifest_status,
    }
    source_url = (
        f"builtin://{skill_id}"
        if source_kind == "builtin"
        else f"workflow://{skill_id}"
        if source_kind == "workflow"
        else f"https://skills.example/{skill_id}"
    )
    source_revision = "1.0.0" if source_kind in {"builtin", "workflow"} else "b" * 40
    return {
        "catalog_entry_id": f"catalog/{skill_id}",
        "source_kind": source_kind,
        "manifest": manifest,
        "source_url": source_url,
        "source_revision": source_revision,
        "source_path": "SKILL.md",
        "publisher": "context-control-plane",
        "license_ref": "MIT",
        "provenance_refs": ["artifact://sha256/" + "a" * 64],
        "capabilities": ["read:repository"],
        "approval_refs": ["artifact://sha256/" + "c" * 64],
        "verification_refs": ["artifact://sha256/" + "d" * 64],
        "permissions": {
            "state_write": False,
            "task_switch": False,
            "claim": False,
            "effect": False,
            "promotion": False,
            "evidence_gate": False,
        },
        "status": status,
    }


def benchmark_catalog() -> dict[str, Any]:
    return {
        "schema_version": "context.skill-catalog/v1alpha1",
        "catalog_id": "skill-catalog/m4-09-benchmark",
        "catalog_revision": "2026-08-14T00:00:00+08:00",
        "entries": [
            _entry(
                "builtin.recovery",
                "builtin",
                [{"kind": "operation", "ref": "operation://code-change"}],
                status="active",
            ),
            _entry(
                "project.testing",
                "project",
                [
                    {"kind": "project", "ref": "project://demo"},
                    {"kind": "repo", "ref": "repo://main"},
                    {"kind": "operation", "ref": "operation://code-change"},
                ],
                dependencies=["builtin.recovery"],
            ),
            _entry(
                "workflow.executor",
                "workflow",
                [
                    {"kind": "role", "ref": "role://executor"},
                    {"kind": "operation", "ref": "operation://code-change"},
                    {"kind": "provider", "ref": "provider://codex"},
                ],
                status="active",
            ),
            _entry(
                "workflow.verifier",
                "workflow",
                [
                    {"kind": "role", "ref": "role://verifier"},
                    {"kind": "operation", "ref": "operation://test"},
                    {"kind": "provider", "ref": "provider://codex"},
                ],
                status="active",
            ),
            _entry(
                "user.style",
                "user",
                [
                    {"kind": "role", "ref": "role://executor"},
                    {"kind": "task", "ref": "task://code-change"},
                    {"kind": "provider", "ref": "provider://codex"},
                ],
            ),
            _entry(
                "external.claude",
                "external",
                [
                    {"kind": "operation", "ref": "operation://code-change"},
                    {"kind": "provider", "ref": "provider://claude-code"},
                ],
                provider_contracts=["provider://claude-code/v1"],
            ),
        ],
    }


def benchmark_request(catalog: dict[str, Any], *, provider_contract: str = "provider://codex/v1") -> dict[str, Any]:
    catalog_bytes = canonical_skill_catalog_bytes(
        catalog, observed_at=catalog["catalog_revision"]
    )
    return {
        "schema_version": "context.skill-resolution-request/v1alpha1",
        "request_id": "resolve-m4-09-benchmark",
        "catalog_sha256": hashlib.sha256(catalog_bytes).hexdigest(),
        "project_ref": "project://demo",
        "repo_ref": "repo://main",
        "path_refs": ["path://src/core", "path://tests"],
        "task_ref": "task://code-change",
        "role_ref": "role://executor",
        "operation_ref": "operation://code-change",
        "provider_contract_ref": provider_contract,
        "required_schema_refs": ["context.compiled-skill-packet/v1alpha1"],
        "required_skill_ids": [],
        "binding_provenance_ref": None,
        "observed_at": "2026-08-14T12:00:00+08:00",
        "state_write_authority": False,
    }


def benchmark_replay_fixture() -> dict[str, Any]:
    """Build the canonical catalog-to-packet replay fixture."""
    catalog = benchmark_catalog()
    request = benchmark_request(catalog)
    outcome = resolve_skills(catalog, request)
    return {
        "fixture_version": "context.skill-resolution-replay-fixture/v1alpha1",
        "catalog": catalog,
        "request": request,
        "decision": outcome.decision,
        "manifest_set": outcome.manifest_set,
        "packet": outcome.packet,
    }


def canonical_benchmark_replay_fixture_bytes() -> bytes:
    return json.dumps(
        benchmark_replay_fixture(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _percentile(samples: list[float], percentile: float) -> float:
    ordered = sorted(samples)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percentile + 0.999999) - 1))
    return ordered[index]


def validate_skill_resolution_benchmark_receipt(receipt: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "generated_at",
        "samples",
        "negative_samples",
        "successful_samples",
        "negative_quarantine",
        "replay_mismatch",
        "role_leakage",
        "provider_leakage",
        "state_write_authority_true",
        "available_manifest_count",
        "selected_count",
        "selection_reduction_percent",
        "p50_ms",
        "p95_ms",
        "max_ms",
        "external_services",
        "implementation_sha256",
        "fixture_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError("Skill resolver benchmark receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Skill resolver benchmark schema_version is unsupported")
    if not isinstance(receipt["generated_at"], str) or not receipt["generated_at"].endswith("Z"):
        raise ValueError("generated_at is invalid")
    for field in (
        "samples",
        "negative_samples",
        "successful_samples",
        "negative_quarantine",
        "replay_mismatch",
        "role_leakage",
        "provider_leakage",
        "state_write_authority_true",
        "available_manifest_count",
        "selected_count",
    ):
        if isinstance(receipt[field], bool) or not isinstance(receipt[field], int) or receipt[field] < 0:
            raise ValueError(f"{field} is invalid")
    if receipt["samples"] < 1000 or receipt["negative_samples"] < 125:
        raise ValueError("benchmark sample gate is not met")
    if receipt["successful_samples"] != receipt["samples"]:
        raise ValueError("not every valid sample succeeded")
    if receipt["negative_quarantine"] != receipt["negative_samples"]:
        raise ValueError("not every negative sample quarantined")
    if any(receipt[field] != 0 for field in ("replay_mismatch", "role_leakage", "provider_leakage", "state_write_authority_true")):
        raise ValueError("Skill resolver benchmark has a correctness failure")
    for field in ("p50_ms", "p95_ms", "max_ms", "selection_reduction_percent"):
        if not isinstance(receipt[field], (int, float)) or receipt[field] < 0:
            raise ValueError(f"{field} is invalid")
    if receipt["p95_ms"] >= 100:
        raise ValueError("Skill resolver p95 exceeds local gate")
    if receipt["selection_reduction_percent"] < 30:
        raise ValueError("Skill selection reduction is below the local gate")
    if receipt["external_services"] != 0:
        raise ValueError("benchmark must remain offline")
    for field in ("implementation_sha256", "fixture_sha256"):
        if not isinstance(receipt[field], str) or len(receipt[field]) != 64:
            raise ValueError(f"{field} is invalid")


def benchmark_skill_resolver(*, samples: int = 1000, negative_samples: int = 125, generated_at: str = "2026-08-14T18:00:00Z") -> dict[str, Any]:
    if samples < 1000 or negative_samples < 125:
        raise ValueError("benchmark sample gate requires at least 1000/125 samples")
    catalog = benchmark_catalog()
    request = benchmark_request(catalog)
    baseline = resolve_skills(copy.deepcopy(catalog), copy.deepcopy(request))
    baseline_bytes = canonical_skill_resolution_decision_bytes(baseline.decision)
    latencies: list[float] = []
    successful = 0
    replay_mismatch = 0
    role_leakage = 0
    provider_leakage = 0
    state_authority_true = 0
    available_manifest_count = 0
    selected_count = 0
    for seed in range(samples):
        shuffled = copy.deepcopy(catalog)
        random.Random(seed).shuffle(shuffled["entries"])
        candidate_request = benchmark_request(shuffled)
        started = time.perf_counter_ns()
        outcome = resolve_skills(shuffled, candidate_request)
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        decision = outcome.decision
        if canonical_skill_resolution_decision_bytes(decision) != baseline_bytes:
            replay_mismatch += 1
        if any(item["skill_id"] in {"workflow.verifier", "external.claude"} for item in decision["selected_skills"]):
            role_leakage += 1
        if any(item["skill_id"] == "external.claude" for item in decision["selected_skills"]):
            provider_leakage += 1
        if decision["state_write_authority"]:
            state_authority_true += 1
        if decision["disposition"] == "resolved":
            successful += 1
        available_manifest_count += sum(
            entry["status"] in {"approved", "active"}
            for entry in shuffled["entries"]
        )
        selected_count += decision["selected_count"]

    quarantined = 0
    negative_catalog = copy.deepcopy(catalog)
    for index in range(negative_samples):
        negative_request = benchmark_request(
            negative_catalog,
            provider_contract="provider://codex/v2",
        )
        negative_request["request_id"] = f"negative-m4-09-{index}"
        outcome = resolve_skills(negative_catalog, negative_request)
        if outcome.decision["disposition"] == "quarantined":
            quarantined += 1

    reduction = (
        (available_manifest_count - selected_count)
        * 100
        / available_manifest_count
    )
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "samples": samples,
        "negative_samples": negative_samples,
        "successful_samples": successful,
        "negative_quarantine": quarantined,
        "replay_mismatch": replay_mismatch,
        "role_leakage": role_leakage,
        "provider_leakage": provider_leakage,
        "state_write_authority_true": state_authority_true,
        "available_manifest_count": available_manifest_count,
        "selected_count": selected_count,
        "selection_reduction_percent": round(reduction, 4),
        "p50_ms": round(_percentile(latencies, 0.50), 6),
        "p95_ms": round(_percentile(latencies, 0.95), 6),
        "max_ms": round(max(latencies), 6),
        "external_services": 0,
        "implementation_sha256": hashlib.sha256(
            (Path(__file__).with_name("skill_resolver.py")).read_bytes()
        ).hexdigest(),
        "fixture_sha256": hashlib.sha256(
            canonical_benchmark_replay_fixture_bytes()
        ).hexdigest(),
    }
    validate_skill_resolution_benchmark_receipt(receipt)
    return receipt
