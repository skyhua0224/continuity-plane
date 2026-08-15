"""Deterministic M7-03 Verification Profile decision benchmark."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .verification_profile import (
    build_verification_adapter,
    build_verification_run_receipt,
    evaluate_verification_profile,
    validate_verification_profile,
)

SCHEMA_VERSION = "context.verification-profile-benchmark/v1alpha1"
_FIELDS = {
    "schema_version",
    "iterations",
    "fixture_profile_ids",
    "fixture_profile_sha256s",
    "scenario_counts",
    "overall_counts",
    "optional_failed_gate_count",
    "false_allow_count",
    "false_deny_count",
    "replay_mismatch_count",
    "external_service_calls",
    "state_write_authority_count",
    "completion_authority_count",
    "benchmark_sha256",
}
_SCENARIOS = {
    "required-passed",
    "required-capability-unavailable",
    "conditional-not-met",
    "optional-failed",
}


class VerificationProfileBenchmarkError(ValueError):
    """Raised when an M7-03 benchmark receipt is inconsistent."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        _canonical({key: item for key, item in value.items() if key != "benchmark_sha256"})
    ).hexdigest()


def _load_profiles() -> list[dict[str, Any]]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "profiles/verification/alkaidlab.example.json",
        root / "profiles/verification/portable-python-library.example.json",
    )
    profiles = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    for profile in profiles:
        validate_verification_profile(profile, observed_at="2026-08-16T00:03:00Z")
    return sorted(profiles, key=lambda item: item["profile_id"])


def _build_adapters(profiles: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    adapters = {}
    for profile in profiles:
        bindings = [
            {
                "gate_id": gate["gate_id"],
                "runner_kind": "local-process",
                "executable": "fixture-runner",
                "arguments": [gate["gate_id"]],
                "working_directory_ref": f"repo://{profile['project_id']}",
                "environment_refs": [],
                "timeout_ms": 1000,
                "output_budget_bytes": 4096,
            }
            for gate in profile["gates"]
        ]
        adapter = build_verification_adapter(
            adapter_id=f"adapter/{profile['project_id']}/m7-03-benchmark",
            adapter_version="1.0.0-alpha.1",
            project_id=profile["project_id"],
            profile=profile,
            bindings=bindings,
        )
        adapters[profile["profile_id"]] = adapter
    return adapters


def _steps_for_gate(gate: Mapping[str, Any], suffix: str) -> list[dict[str, str]]:
    steps: list[dict[str, str]] = []
    if gate["gate_kind"] == "tdd":
        steps.extend(
            [
                {
                    "kind": "red-test-failed",
                    "observed_at": "2026-08-16T00:01:10Z",
                    "artifact_ref": "artifact://sha256/" + "a" * 64,
                    "artifact_sha256": "a" * 64,
                },
                {
                    "kind": "green-test-passed",
                    "observed_at": "2026-08-16T00:01:20Z",
                    "artifact_ref": "artifact://sha256/" + "b" * 64,
                    "artifact_sha256": "b" * 64,
                },
            ]
        )
    else:
        steps.append(
            {
                "kind": "command",
                "observed_at": "2026-08-16T00:01:10Z",
                "artifact_ref": "artifact://sha256/" + "a" * 64,
                "artifact_sha256": "a" * 64,
            }
        )
    steps.extend(
        [
            {
                "kind": "exit-status",
                "observed_at": "2026-08-16T00:01:30Z",
                "artifact_ref": "artifact://sha256/" + "b" * 64,
                "artifact_sha256": "b" * 64,
            },
            {
                "kind": "artifact-digest",
                "observed_at": "2026-08-16T00:01:40Z",
                "artifact_ref": "artifact://sha256/" + "c" * 64,
                "artifact_sha256": "c" * 64,
            },
        ]
    )
    required_kinds = set(gate["evidence_requirements"])
    for kind in sorted(required_kinds - {"command", "exit-status", "artifact-digest", "red", "green"}):
        steps.append(
            {
                "kind": kind,
                "observed_at": "2026-08-16T00:01:50Z",
                "artifact_ref": "artifact://sha256/" + "d" * 64,
                "artifact_sha256": "d" * 64,
            }
        )
    return steps


def _build_receipts(
    profile: Mapping[str, Any],
    adapter: Mapping[str, Any],
    *,
    failed_gate_ids: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    failed_gate_ids = failed_gate_ids or set()
    receipts = {}
    for gate in profile["gates"]:
        if gate["mode"] not in {"required", "optional"}:
            continue
        receipts[gate["gate_id"]] = build_verification_run_receipt(
            run_id=f"run/benchmark/{profile['project_id']}/{gate['gate_id']}",
            work_id=f"work/{profile['project_id']}",
            project_revision=1,
            repository_revision="a" * 40,
            profile=profile,
            adapter=adapter,
            gate_id=gate["gate_id"],
            status="failed" if gate["gate_id"] in failed_gate_ids else "passed",
            started_at="2026-08-16T00:01:00Z",
            completed_at="2026-08-16T00:02:00Z",
            evidence_steps=_steps_for_gate(gate, profile["project_id"]),
            measurements=[],
        )
    return receipts


def _capability(ref: str, status: str) -> dict[str, Any]:
    return {
        "capability_ref": ref,
        "status": status,
        "source_kind": "trusted-host-probe",
        "observed_at": "2026-08-16T00:02:00Z",
        "expires_at": "2026-08-16T00:10:00Z",
        "evidence_refs": [f"evidence://benchmark/{ref.rsplit('/', 1)[-1]}"],
    }


def _fixture_profile_sha256s() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    paths = (
        root / "profiles/verification/alkaidlab.example.json",
        root / "profiles/verification/portable-python-library.example.json",
    )
    profiles = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    for profile in profiles:
        validate_verification_profile(profile, observed_at="2026-08-16T00:03:00Z")
    return {
        profile["profile_id"]: profile["profile_sha256"]
        for profile in sorted(profiles, key=lambda item: item["profile_id"])
    }


def run_verification_profile_benchmark(*, iterations: int = 1000) -> dict[str, Any]:
    if type(iterations) is not int or iterations <= 0 or iterations > 100_000:
        raise VerificationProfileBenchmarkError("iterations is invalid")
    profiles = _load_profiles()
    adapters = _build_adapters(profiles)
    scenario_counts = {name: 0 for name in sorted(_SCENARIOS)}
    overall_counts: dict[str, int] = {}
    false_allow = false_deny = replay_mismatch = state_authority = completion_authority = 0
    optional_failed_gate_count = 0
    scenarios = (
        "required-passed",
        "required-capability-unavailable",
        "conditional-not-met",
        "optional-failed",
    )
    for index in range(iterations):
        scenario = scenarios[index % len(scenarios)]
        scenario_counts[scenario] += 1
        profile = profiles[index % len(profiles)]
        adapter = adapters[profile["profile_id"]]
        optional_gate_ids = {
            gate["gate_id"] for gate in profile["gates"] if gate["mode"] == "optional"
        }
        receipts = _build_receipts(
            profile,
            adapter,
            failed_gate_ids=optional_gate_ids if scenario == "optional-failed" else set(),
        )
        required_gate_ids = {
            gate["gate_id"] for gate in profile["gates"] if gate["mode"] == "required"
        }
        run_receipts = [
            receipt
            for gate_id, receipt in receipts.items()
            if gate_id in required_gate_ids
            or (scenario == "optional-failed" and gate_id not in required_gate_ids)
        ]
        optional_failed_gate_count += sum(
            receipt["status"] == "failed" and gate_id in optional_gate_ids
            for gate_id, receipt in receipts.items()
            if receipt in run_receipts
        )
        first_required = min(required_gate_ids)
        condition_gates = {
            gate["condition_ref"]: gate
            for gate in profile["gates"]
            if gate["condition_ref"] is not None
        }
        capability_gates: dict[str, list[Mapping[str, Any]]] = {}
        for gate in profile["gates"]:
            for capability_ref in gate["capability_refs"]:
                capability_gates.setdefault(capability_ref, []).append(gate)
        unavailable_refs = {
            capability_ref
            for gate in profile["gates"]
            if gate["gate_id"] == first_required
            for capability_ref in gate["capability_refs"]
        }
        inputs = {
            "decision_id": f"decision/benchmark/{profile['project_id']}/{index}",
            "work_id": f"work/{profile['project_id']}",
            "project_revision": 1,
            "profile": profile,
            "adapter": adapter,
            "condition_observations": [
                {
                    "condition_ref": gate["condition_ref"],
                    "status": "not-met",
                    "source_kind": "current-code",
                    "observed_at": "2026-08-16T00:02:00Z",
                    "expires_at": "2026-08-16T00:10:00Z",
                    "evidence_refs": [
                        f"evidence://benchmark/{profile['project_id']}/{gate['gate_id']}"
                    ],
                }
                for condition_ref, gate in sorted(condition_gates.items())
            ],
            "capability_observations": [
                _capability(
                    capability_ref,
                    (
                        "unavailable"
                        if scenario == "required-capability-unavailable"
                        and capability_ref in unavailable_refs
                        else "available"
                        if any(gate["mode"] == "required" for gate in gates)
                        or (
                            scenario == "optional-failed"
                            and any(gate["mode"] == "optional" for gate in gates)
                        )
                        else "unavailable"
                    ),
                )
                for capability_ref, gates in sorted(capability_gates.items())
            ],
            "run_receipts": run_receipts,
            "evaluated_at": "2026-08-16T00:03:00Z",
        }
        decision = evaluate_verification_profile(**inputs)
        replay = evaluate_verification_profile(**inputs)
        if decision != replay:
            replay_mismatch += 1
        overall_counts[decision["overall_status"]] = (
            overall_counts.get(decision["overall_status"], 0) + 1
        )
        expected = (
            "blocked" if scenario == "required-capability-unavailable" else "satisfied"
        )
        if decision["overall_status"] == "satisfied" and expected != "satisfied":
            false_allow += 1
        if decision["overall_status"] != "satisfied" and expected == "satisfied":
            false_deny += 1
        state_authority += int(decision["state_write_authority"])
        completion_authority += int(decision["completion_authority"])
    fixture_digests = _fixture_profile_sha256s()
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "iterations": iterations,
        "fixture_profile_ids": sorted(fixture_digests),
        "fixture_profile_sha256s": fixture_digests,
        "scenario_counts": scenario_counts,
        "overall_counts": dict(sorted(overall_counts.items())),
        "optional_failed_gate_count": optional_failed_gate_count,
        "false_allow_count": false_allow,
        "false_deny_count": false_deny,
        "replay_mismatch_count": replay_mismatch,
        "external_service_calls": 0,
        "state_write_authority_count": state_authority,
        "completion_authority_count": completion_authority,
        "benchmark_sha256": "",
    }
    receipt["benchmark_sha256"] = _digest(receipt)
    validate_verification_profile_benchmark(receipt)
    return receipt


def validate_verification_profile_benchmark(receipt: Any) -> None:
    if not isinstance(receipt, Mapping) or set(receipt) != _FIELDS:
        raise VerificationProfileBenchmarkError("benchmark fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise VerificationProfileBenchmarkError("benchmark version is invalid")
    iterations = receipt["iterations"]
    if type(iterations) is not int or iterations <= 0:
        raise VerificationProfileBenchmarkError("benchmark iterations are invalid")
    if receipt["fixture_profile_ids"] != sorted(receipt["fixture_profile_ids"]):
        raise VerificationProfileBenchmarkError("fixture profile IDs are invalid")
    fixture_digests = receipt["fixture_profile_sha256s"]
    if (
        not isinstance(fixture_digests, Mapping)
        or set(fixture_digests) != set(receipt["fixture_profile_ids"])
        or any(
            not isinstance(value, str) or len(value) != 64 or set(value) - set("0123456789abcdef")
            for value in fixture_digests.values()
        )
    ):
        raise VerificationProfileBenchmarkError("fixture profile digests are invalid")
    scenario_counts = receipt["scenario_counts"]
    if not isinstance(scenario_counts, Mapping) or set(scenario_counts) != _SCENARIOS:
        raise VerificationProfileBenchmarkError("benchmark scenarios are invalid")
    if any(type(value) is not int or value < 0 for value in scenario_counts.values()):
        raise VerificationProfileBenchmarkError("scenario counts are invalid")
    if sum(scenario_counts.values()) != iterations:
        raise VerificationProfileBenchmarkError("scenario counts do not match iterations")
    overall = receipt["overall_counts"]
    if not isinstance(overall, Mapping) or not set(overall).issubset(
        {"satisfied", "failed", "blocked"}
    ):
        raise VerificationProfileBenchmarkError("overall counts are invalid")
    if any(type(value) is not int or value < 0 for value in overall.values()):
        raise VerificationProfileBenchmarkError("overall counts are invalid")
    if sum(overall.values()) != iterations:
        raise VerificationProfileBenchmarkError("overall counts do not match iterations")
    for field in (
        "optional_failed_gate_count",
        "false_allow_count",
        "false_deny_count",
        "replay_mismatch_count",
        "external_service_calls",
        "state_write_authority_count",
        "completion_authority_count",
    ):
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise VerificationProfileBenchmarkError(f"{field} is invalid")
    if any(
        receipt[field] != 0
        for field in (
            "false_allow_count",
            "false_deny_count",
            "replay_mismatch_count",
            "state_write_authority_count",
            "completion_authority_count",
        )
    ):
        raise VerificationProfileBenchmarkError("verification safety veto failed")
    if receipt["external_service_calls"] != 0:
        raise VerificationProfileBenchmarkError("benchmark must remain local")
    digest = receipt["benchmark_sha256"]
    if not isinstance(digest, str) or digest != _digest(receipt):
        raise VerificationProfileBenchmarkError("benchmark digest mismatch")
