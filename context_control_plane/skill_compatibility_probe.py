"""Measured fault matrix for active-task Skill compatibility locks."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import platform
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from context_control_plane import compiled_skill_packet, skill_compatibility

SCHEMA_VERSION = "context.skill-compatibility-probe/v1alpha1"
_PROBE_RELATIVE_PATH = Path("context_control_plane/skill_compatibility_probe.py")
_IMPLEMENTATION_RELATIVE_PATH = Path("context_control_plane/skill_compatibility.py")
_RUNNER_RELATIVE_PATH = Path("tools/run_skill_compatibility_probe.py")
_SCHEMA_RELATIVE_PATH = Path("schemas/m4-06/skill-compatibility-probe.schema.json")
_LOCK_SCHEMA_RELATIVE_PATH = Path("schemas/m4-06/skill-compatibility-lock.schema.json")
_DECISION_SCHEMA_RELATIVE_PATH = Path("schemas/m4-06/skill-compatibility-decision.schema.json")
_MIGRATION_SCHEMA_RELATIVE_PATH = Path("schemas/m4-06/skill-compatibility-migration.schema.json")
_MANIFEST_FIXTURE_RELATIVE_PATH = Path(
    "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json"
)
_COMPILER_RELATIVE_PATH = Path("context_control_plane/compiled_skill_packet.py")
_MANIFEST_VALIDATOR_RELATIVE_PATH = Path("context_control_plane/skill_manifest_set.py")
_PROVIDER_ADAPTER_RELATIVE_PATH = Path("context_control_plane/provider_skill_adapter.py")
_COMPATIBILITY_TEST_RELATIVE_PATH = Path("tests/test_m4_06_skill_compatibility.py")
_PROBE_TEST_RELATIVE_PATH = Path("tests/test_m4_06_skill_compatibility_probe.py")
_REGISTRY_SCHEMA_IDS = (
    "context.skill-compatibility-lock",
    "context.skill-compatibility-decision",
    "context.skill-compatibility-migration",
    "context.skill-compatibility-probe",
)
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_ENVIRONMENT_FIELDS = {
    "python_version",
    "python_implementation",
    "platform",
    "machine",
    "external_services",
}
_LIMITATIONS = [
    "This probe measures the local compatibility gate, not a provider process.",
    "M5 and M8 must bind this gate to checkpoint restore and authoritative task state.",
]


class _RecordingAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.provider_id = "codex"
        self.adapter_surface_contract = (
            "context.adapter-surface/codex-app-server-skill-input/v1alpha1"
        )

    def compose(self, *_args: Any, **_kwargs: Any) -> dict[str, bool]:
        self.calls += 1
        return {"composed": True}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _document_digest(document: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _percentile(values: list[float], percentile: float) -> float:
    if not values or any(
        type(value) not in {int, float} or not math.isfinite(value) or value < 0
        for value in values
    ):
        raise ValueError("latency samples are invalid")
    ordered = sorted(values)
    rank = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[rank], 4)


def build_probe_case(root: Path) -> dict[str, Any]:
    fixture = json.loads(
        (root / "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json")
        .read_text(encoding="utf-8")
    )
    template = fixture["manifests"][0]
    manifests = []
    for skill_id, content in (
        ("core.bootstrap", b"bootstrap"),
        ("project.active", b"active"),
        ("unused.metadata", b"unused"),
    ):
        manifest = copy.deepcopy(template)
        manifest["skill_id"] = skill_id
        manifest["version"] = "1.0.0"
        manifest["content_sha256"] = hashlib.sha256(content).hexdigest()
        manifest["rule_ids"] = [f"{skill_id}.rule"]
        manifest["dependencies"] = []
        manifest["conflicts"] = []
        manifest["expires_at"] = None
        if skill_id != "unused.metadata":
            manifest["applicability"] = [
                {"kind": "provider", "ref": "provider://claude"},
                {"kind": "provider", "ref": "provider://codex"},
            ]
            manifest["compatibility"]["provider_contract_refs"] = [
                "provider://claude/v1",
                "provider://codex/v1",
            ]
        manifests.append(manifest)
    manifest_set = {"schema_version": fixture["schema_version"], "manifests": manifests}
    packet = compiled_skill_packet.compile_skill_packet(
        manifest_set,
        selected_skill_ids=["core.bootstrap", "project.active"],
    )
    provider_contract_refs = ["provider://claude/v1", "provider://codex/v1"]
    lock = skill_compatibility.create_skill_compatibility_lock(
        manifest_set,
        packet,
        task_id="task/m4-06-probe",
        operation_id="operation://code-change",
        provider_contract_refs=provider_contract_refs,
    )
    version_changed = copy.deepcopy(manifest_set)
    version_changed["manifests"][1]["version"] = "1.1.0"
    return {
        "manifest_set": manifest_set,
        "original_packet": packet,
        "lock": lock,
        "provider_contract_refs": provider_contract_refs,
        "version_changed_manifest_set": version_changed,
    }


def _scenario(
    case: dict[str, Any], index: int
) -> tuple[str, dict[str, Any], dict[str, Any], list[str]]:
    kind_index = index // 8
    manifest_set = copy.deepcopy(case["manifest_set"])
    packet = case["original_packet"]
    provider_refs = list(case["provider_contract_refs"])
    if kind_index == 0:
        change_kind = "unselected-metadata"
        manifest_set["manifests"][2]["provenance_refs"] = [
            "artifact://sha256/" + f"{index + 1:064x}"
        ]
    elif kind_index == 1:
        change_kind = "selected-version"
        manifest_set["manifests"][1]["version"] = f"1.{index - 7}.0"
        packet = compiled_skill_packet.compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.bootstrap", "project.active"],
        )
    elif kind_index == 2:
        change_kind = "selected-content"
        manifest_set["manifests"][1]["content_sha256"] = f"{index + 1:064x}"
        packet = compiled_skill_packet.compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.bootstrap", "project.active"],
        )
    elif kind_index == 3:
        change_kind = "selected-rules"
        manifest_set["manifests"][1]["rule_ids"] = [f"project.active.changed-{index}"]
        packet = compiled_skill_packet.compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.bootstrap", "project.active"],
        )
    else:
        change_kind = "provider-contract"
        provider_refs = ["provider://claude/v1", f"provider://codex/v{index - 30}"]
        for manifest in manifest_set["manifests"][:2]:
            manifest["compatibility"]["provider_contract_refs"] = list(
                provider_refs
            )
        packet = compiled_skill_packet.compile_skill_packet(
            manifest_set,
            selected_skill_ids=["core.bootstrap", "project.active"],
        )
    return change_kind, manifest_set, packet, provider_refs


def _probe_evidence_verifier(
    reference: str, *, purpose: str, migration: dict[str, Any]
) -> bool:
    expected = {
        "approval": "artifact://sha256/" + "a" * 64,
        "replay": "artifact://sha256/" + "b" * 64,
        "rollback": "artifact://sha256/" + "c" * 64,
    }
    return (
        reference == expected.get(purpose)
        and migration["task_id"] == "task/m4-06-probe"
    )


def _scenario_result(
    case: dict[str, Any], index: int, *, measure_latency: bool
) -> tuple[dict[str, Any], float | None, tuple[str, dict[str, Any], dict[str, Any], list[str]]]:
    change_kind, manifest_set, packet, provider_refs = _scenario(case, index)
    started = time.perf_counter_ns() if measure_latency else None
    decision = skill_compatibility.assess_skill_compatibility(
        case["lock"],
        manifest_set,
        packet,
        provider_contract_refs=provider_refs,
    )
    latency = (
        round((time.perf_counter_ns() - started) / 1_000_000, 4)
        if started is not None
        else None
    )
    adapter = _RecordingAdapter()
    blocked = False
    try:
        skill_compatibility.compose_provider_skills(
            adapter,
            packet,
            "loaded",
            candidate_manifest_set=manifest_set,
            compatibility_lock=case["lock"],
            provider_contract_refs=provider_refs,
            compatibility_decision=decision,
        )
    except skill_compatibility.SkillCompatibilityError:
        blocked = True
    result = {
        "scenario_id": f"m4-06-{index + 1:02d}",
        "change_kind": change_kind,
        "classification": decision["classification"],
        "delivery_allowed_without_migration": decision[
            "delivery_allowed_without_migration"
        ],
        "provider_compose_calls": adapter.calls,
        "blocked": blocked,
        "decision_sha256": hashlib.sha256(
            skill_compatibility.canonical_skill_compatibility_decision_bytes(decision)
        ).hexdigest(),
    }
    return result, latency, (change_kind, manifest_set, packet, provider_refs)


def _matrix_results(
    case: dict[str, Any], *, measure_latency: bool
) -> tuple[
    list[dict[str, Any]],
    list[float],
    dict[str, tuple[dict[str, Any], dict[str, Any], list[str]]],
]:
    results: list[dict[str, Any]] = []
    latencies: list[float] = []
    exemplar_by_kind: dict[str, tuple[dict[str, Any], dict[str, Any], list[str]]] = {}
    for index in range(40):
        result, latency, values = _scenario_result(
            case, index, measure_latency=measure_latency
        )
        results.append(result)
        if latency is not None:
            latencies.append(latency)
        change_kind, manifest_set, packet, provider_refs = values
        if change_kind != "unselected-metadata" and change_kind not in exemplar_by_kind:
            exemplar_by_kind[change_kind] = (manifest_set, packet, provider_refs)
    return results, latencies, exemplar_by_kind


def _migration_result(
    case: dict[str, Any],
    *,
    change_kind: str,
    manifest_set: dict[str, Any],
    packet: dict[str, Any],
    provider_refs: list[str],
) -> dict[str, Any]:
    new_lock = skill_compatibility.create_skill_compatibility_lock(
        manifest_set,
        packet,
        task_id=case["lock"]["task_id"],
        operation_id=case["lock"]["operation_id"],
        provider_contract_refs=provider_refs,
    )
    decision = skill_compatibility.assess_skill_compatibility(
        case["lock"], manifest_set, packet, provider_contract_refs=provider_refs
    )
    kwargs = {
        "old_lock": case["lock"],
        "new_lock": new_lock,
        "migration_id": f"migration/{change_kind}",
        "reason": f"probe {change_kind}",
        "approval_ref": "artifact://sha256/" + "a" * 64,
        "replay_proof_ref": "artifact://sha256/" + "b" * 64,
        "rollback_proof_ref": "artifact://sha256/" + "c" * 64,
    }
    migration = skill_compatibility.create_skill_compatibility_migration(**kwargs)
    replay = skill_compatibility.create_skill_compatibility_migration(**kwargs)
    missing_evidence = copy.deepcopy(migration)
    missing_evidence["replay_proof_ref"] = None
    wrong_binding = copy.deepcopy(migration)
    wrong_binding["task_id"] = "task/wrong"
    return {
        "change_kind": change_kind,
        "migration_allowed": skill_compatibility.authorize_skill_delivery(
            decision,
            migration=migration,
            evidence_verifier=_probe_evidence_verifier,
        ),
        "missing_evidence_allowed": skill_compatibility.authorize_skill_delivery(
            decision,
            migration=missing_evidence,
            evidence_verifier=_probe_evidence_verifier,
        ),
        "replay_match": (
            skill_compatibility.canonical_skill_compatibility_migration_bytes(migration)
            == skill_compatibility.canonical_skill_compatibility_migration_bytes(replay)
        ),
        "rollback_match": (
            skill_compatibility.rollback_skill_compatibility_migration(migration)
            == case["lock"]
        ),
        "wrong_binding_allowed": skill_compatibility.authorize_skill_delivery(
            decision,
            migration=wrong_binding,
            evidence_verifier=_probe_evidence_verifier,
        ),
        "migration_sha256": hashlib.sha256(
            skill_compatibility.canonical_skill_compatibility_migration_bytes(migration)
        ).hexdigest(),
    }


def _registry_entries(root: Path) -> dict[str, dict[str, Any]]:
    registry = yaml.safe_load((root / "schemas/registry.yaml").read_text(encoding="utf-8"))
    entries = {
        item["schema_id"]: item
        for item in registry["schemas"]
        if item["schema_id"] in _REGISTRY_SCHEMA_IDS
    }
    if set(entries) != set(_REGISTRY_SCHEMA_IDS):
        raise ValueError("M4-06 registry entries are incomplete")
    return entries


def _provenance(root: Path) -> dict[str, str]:
    entries = _registry_entries(root)
    return {
        "implementation_sha256": _sha256(root / _IMPLEMENTATION_RELATIVE_PATH),
        "probe_sha256": _sha256(root / _PROBE_RELATIVE_PATH),
        "runner_sha256": _sha256(root / _RUNNER_RELATIVE_PATH),
        "probe_schema_sha256": _sha256(root / _SCHEMA_RELATIVE_PATH),
        "lock_schema_sha256": _sha256(root / _LOCK_SCHEMA_RELATIVE_PATH),
        "decision_schema_sha256": _sha256(root / _DECISION_SCHEMA_RELATIVE_PATH),
        "migration_schema_sha256": _sha256(root / _MIGRATION_SCHEMA_RELATIVE_PATH),
        "manifest_fixture_sha256": _sha256(root / _MANIFEST_FIXTURE_RELATIVE_PATH),
        "compiled_packet_implementation_sha256": _sha256(root / _COMPILER_RELATIVE_PATH),
        "manifest_validator_implementation_sha256": _sha256(
            root / _MANIFEST_VALIDATOR_RELATIVE_PATH
        ),
        "provider_adapter_implementation_sha256": _sha256(
            root / _PROVIDER_ADAPTER_RELATIVE_PATH
        ),
        "compatibility_test_sha256": _sha256(root / _COMPATIBILITY_TEST_RELATIVE_PATH),
        "probe_test_sha256": _sha256(root / _PROBE_TEST_RELATIVE_PATH),
        "lock_registry_entry_sha256": _document_digest(
            entries["context.skill-compatibility-lock"]
        ),
        "decision_registry_entry_sha256": _document_digest(
            entries["context.skill-compatibility-decision"]
        ),
        "migration_registry_entry_sha256": _document_digest(
            entries["context.skill-compatibility-migration"]
        ),
        "probe_registry_entry_sha256": _document_digest(
            entries["context.skill-compatibility-probe"]
        ),
    }


def run_skill_compatibility_probe(
    root: Path,
    *,
    samples: int,
    observed_at: str,
    arguments: list[str],
) -> dict[str, Any]:
    if samples != 40:
        raise ValueError("M4-06 acceptance requires exactly 40 samples")
    case = build_probe_case(root)
    results, latencies, exemplar_by_kind = _matrix_results(
        case, measure_latency=True
    )

    migration_results = [
        _migration_result(
            case,
            change_kind=change_kind,
            manifest_set=values[0],
            packet=values[1],
            provider_refs=values[2],
        )
        for change_kind, values in sorted(exemplar_by_kind.items())
    ]
    compatible_samples = sum(item["classification"] == "compatible" for item in results)
    breaking_samples = sum(item["classification"] == "migration_required" for item in results)
    unauthorized_deliveries = sum(
        item["classification"] == "migration_required"
        and (item["delivery_allowed_without_migration"] or item["provider_compose_calls"] > 0)
        for item in results
    )
    measurement = {
        "samples": samples,
        "compatible_samples": compatible_samples,
        "breaking_samples": breaking_samples,
        "classification_counts": {
            classification: sum(item["classification"] == classification for item in results)
            for classification in ("unchanged", "compatible", "migration_required", "rejected")
        },
        "unauthorized_deliveries": unauthorized_deliveries,
        "blocked_provider_compose_calls": sum(
            item["provider_compose_calls"]
            for item in results
            if item["classification"] == "migration_required"
        ),
        "compatible_provider_compose_calls": sum(
            item["provider_compose_calls"]
            for item in results
            if item["classification"] == "compatible"
        ),
        "evidence_missing_migrations_allowed": sum(
            item["missing_evidence_allowed"] for item in migration_results
        ),
        "valid_migrations_allowed": sum(
            item["migration_allowed"] for item in migration_results
        ),
        "migration_replay_mismatches": sum(
            not item["replay_match"] for item in migration_results
        ),
        "rollback_mismatches": sum(
            not item["rollback_match"] for item in migration_results
        ),
        "wrong_binding_migrations_allowed": sum(
            item["wrong_binding_allowed"] for item in migration_results
        ),
        "latency_samples_ms": latencies,
        "assessment_p50_ms": _percentile(latencies, 0.50),
        "assessment_p95_ms": _percentile(latencies, 0.95),
        "assessment_max_ms": round(max(latencies), 4),
        "assessment_p95_limit_ms": 10.0,
        "results": results,
        "migration_results": migration_results,
    }
    acceptance = {
        "forty_sample_gate_passed": samples == 40,
        "classification_gate_passed": compatible_samples == 8 and breaking_samples == 32,
        "unauthorized_delivery_gate_passed": unauthorized_deliveries == 0,
        "pre_provider_gate_passed": measurement["blocked_provider_compose_calls"] == 0,
        "migration_evidence_gate_passed": measurement["evidence_missing_migrations_allowed"] == 0 and measurement["valid_migrations_allowed"] == 4,
        "migration_replay_gate_passed": measurement["migration_replay_mismatches"] == 0,
        "rollback_gate_passed": measurement["rollback_mismatches"] == 0,
        "task_binding_gate_passed": measurement["wrong_binding_migrations_allowed"] == 0,
        "assessment_p95_gate_passed": measurement["assessment_p95_ms"] < 10.0,
        "zero_external_services_gate_passed": True,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "provenance": _provenance(root),
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine() or "unknown",
            "external_services": 0,
        },
        "workload": {
            "fixture": "m4-06-active-task-skill-change-matrix",
            "samples": samples,
            "change_kinds": [
                "unselected-metadata", "selected-version", "selected-content",
                "selected-rules", "provider-contract",
            ],
            "samples_per_change_kind": 8,
        },
        "measurement": measurement,
        "acceptance": acceptance,
        "authority_boundary": {
            "local_compatibility_gate_measured": True,
            "synthetic_skill_sources": True,
            "provider_adapter_test_double": True,
            "synthetic_evidence_verifier": True,
            "provider_process_invoked": False,
            "provider_network_invoked": False,
            "state_commit_invoked": False,
            "state_write_authority": False,
        },
        "limitations": list(_LIMITATIONS),
        "generation": {
            "command": "tools/run_skill_compatibility_probe.py",
            "arguments": list(arguments),
            "writes_runtime_state_to_repository": False,
        },
    }


def validate_skill_compatibility_probe_receipt(
    receipt: dict[str, Any], *, root: Path
) -> None:
    expected_fields = {
        "schema_version", "observed_at", "provenance", "environment", "workload",
        "measurement", "acceptance", "authority_boundary", "limitations", "generation",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_fields:
        raise ValueError("probe receipt fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("probe receipt schema is unsupported")
    observed_at = receipt["observed_at"]
    if not isinstance(observed_at, str) or not _RFC3339_RE.fullmatch(observed_at):
        raise ValueError("probe observed_at is invalid")
    try:
        datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("probe observed_at is invalid") from exc
    environment = receipt["environment"]
    if not isinstance(environment, dict) or set(environment) != _ENVIRONMENT_FIELDS:
        raise ValueError("probe environment fields are invalid")
    if (
        any(
            not isinstance(environment[field], str)
            or not environment[field]
            or len(environment[field]) > 1024
            for field in _ENVIRONMENT_FIELDS - {"external_services"}
        )
        or type(environment["external_services"]) is not int
        or environment["external_services"] != 0
    ):
        raise ValueError("probe environment is invalid")
    if receipt["limitations"] != _LIMITATIONS:
        raise ValueError("probe limitations are invalid")
    generation = receipt["generation"]
    if not isinstance(generation, dict) or set(generation) != {
        "command",
        "arguments",
        "writes_runtime_state_to_repository",
    }:
        raise ValueError("probe generation fields are invalid")
    arguments = generation["arguments"]
    expected_arguments = ["--samples", "40", "--observed-at", observed_at]
    if (
        generation["command"] != "tools/run_skill_compatibility_probe.py"
        or generation["writes_runtime_state_to_repository"] is not False
        or not isinstance(arguments, list)
        or (
            arguments != expected_arguments
            and not (
                len(arguments) == 6
                and arguments[:4] == expected_arguments
                and arguments[4] == "--output"
                and isinstance(arguments[5], str)
                and bool(arguments[5])
            )
        )
    ):
        raise ValueError("probe generation is invalid")
    if receipt["workload"] != {
        "fixture": "m4-06-active-task-skill-change-matrix",
        "samples": 40,
        "change_kinds": [
            "unselected-metadata", "selected-version", "selected-content",
            "selected-rules", "provider-contract",
        ],
        "samples_per_change_kind": 8,
    }:
        raise ValueError("probe workload is invalid")
    measurement = receipt["measurement"]
    measurement_fields = {
        "samples",
        "compatible_samples",
        "breaking_samples",
        "classification_counts",
        "unauthorized_deliveries",
        "blocked_provider_compose_calls",
        "compatible_provider_compose_calls",
        "evidence_missing_migrations_allowed",
        "valid_migrations_allowed",
        "migration_replay_mismatches",
        "rollback_mismatches",
        "wrong_binding_migrations_allowed",
        "latency_samples_ms",
        "assessment_p50_ms",
        "assessment_p95_ms",
        "assessment_max_ms",
        "assessment_p95_limit_ms",
        "results",
        "migration_results",
    }
    if not isinstance(measurement, dict) or set(measurement) != measurement_fields:
        raise ValueError("probe measurement fields are invalid")
    results = measurement["results"]
    migrations = measurement["migration_results"]
    if not isinstance(results, list) or not isinstance(migrations, list):
        raise ValueError("probe result collections are invalid")  # noqa: TRY004
    result_fields = {
        "scenario_id",
        "change_kind",
        "classification",
        "delivery_allowed_without_migration",
        "provider_compose_calls",
        "blocked",
        "decision_sha256",
    }
    for item in results:
        if (
            not isinstance(item, dict)
            or set(item) != result_fields
            or type(item["delivery_allowed_without_migration"]) is not bool
            or type(item["provider_compose_calls"]) is not int
            or type(item["blocked"]) is not bool
        ):
            raise ValueError("probe result fields are invalid")
    migration_result_fields = {
        "change_kind",
        "migration_allowed",
        "missing_evidence_allowed",
        "replay_match",
        "rollback_match",
        "wrong_binding_allowed",
        "migration_sha256",
    }
    migration_boolean_fields = migration_result_fields - {
        "change_kind",
        "migration_sha256",
    }
    for item in migrations:
        if (
            not isinstance(item, dict)
            or set(item) != migration_result_fields
            or any(type(item[field]) is not bool for field in migration_boolean_fields)
        ):
            raise ValueError("probe migration result fields are invalid")
    count_fields = measurement_fields - {
        "classification_counts",
        "latency_samples_ms",
        "assessment_p50_ms",
        "assessment_p95_ms",
        "assessment_max_ms",
        "assessment_p95_limit_ms",
        "results",
        "migration_results",
    }
    if any(type(measurement[field]) is not int for field in count_fields):
        raise ValueError("probe measurement count fields are invalid")
    classification_counts = measurement["classification_counts"]
    if (
        not isinstance(classification_counts, dict)
        or set(classification_counts)
        != {"unchanged", "compatible", "migration_required", "rejected"}
        or any(type(value) is not int for value in classification_counts.values())
    ):
        raise ValueError("probe classification counts are invalid")
    if len(results) != 40 or len({item["scenario_id"] for item in results}) != 40:
        raise ValueError("probe results are invalid")
    if len(migrations) != 4 or len({item["change_kind"] for item in migrations}) != 4:
        raise ValueError("probe migration results are invalid")
    case = build_probe_case(root)
    expected_results, _, exemplar_by_kind = _matrix_results(
        case, measure_latency=False
    )
    expected_migrations = [
        _migration_result(
            case,
            change_kind=change_kind,
            manifest_set=values[0],
            packet=values[1],
            provider_refs=values[2],
        )
        for change_kind, values in sorted(exemplar_by_kind.items())
    ]
    if results != expected_results:
        raise ValueError("probe scenario replay is invalid")
    if migrations != expected_migrations:
        raise ValueError("probe migration replay is invalid")
    counts = {
        classification: sum(item["classification"] == classification for item in results)
        for classification in ("unchanged", "compatible", "migration_required", "rejected")
    }
    derived = {
        "samples": len(results),
        "compatible_samples": counts["compatible"],
        "breaking_samples": counts["migration_required"],
        "classification_counts": counts,
        "unauthorized_deliveries": sum(
            item["classification"] == "migration_required"
            and (item["delivery_allowed_without_migration"] or item["provider_compose_calls"] > 0)
            for item in results
        ),
        "blocked_provider_compose_calls": sum(item["provider_compose_calls"] for item in results if item["classification"] == "migration_required"),
        "compatible_provider_compose_calls": sum(item["provider_compose_calls"] for item in results if item["classification"] == "compatible"),
        "evidence_missing_migrations_allowed": sum(item["missing_evidence_allowed"] for item in migrations),
        "valid_migrations_allowed": sum(item["migration_allowed"] for item in migrations),
        "migration_replay_mismatches": sum(not item["replay_match"] for item in migrations),
        "rollback_mismatches": sum(not item["rollback_match"] for item in migrations),
        "wrong_binding_migrations_allowed": sum(item["wrong_binding_allowed"] for item in migrations),
    }
    for field, expected in derived.items():
        if measurement.get(field) != expected:
            raise ValueError(f"probe measurement {field} is invalid")
    latencies = measurement["latency_samples_ms"]
    if len(latencies) != 40:
        raise ValueError("probe latency sample count is invalid")
    if any(
        type(value) not in {int, float} or not math.isfinite(value) or value < 0
        for value in (
            measurement["assessment_p50_ms"],
            measurement["assessment_p95_ms"],
            measurement["assessment_max_ms"],
            measurement["assessment_p95_limit_ms"],
        )
    ):
        raise ValueError("probe latency summary is invalid")
    if measurement["assessment_p50_ms"] != _percentile(latencies, 0.50) or measurement["assessment_p95_ms"] != _percentile(latencies, 0.95) or measurement["assessment_max_ms"] != round(max(latencies), 4):
        raise ValueError("probe latency summary is invalid")
    if measurement["assessment_p95_limit_ms"] != 10.0:
        raise ValueError("probe latency limit is invalid")
    expected_acceptance = {
        "forty_sample_gate_passed": derived["samples"] == 40,
        "classification_gate_passed": derived["compatible_samples"] == 8 and derived["breaking_samples"] == 32,
        "unauthorized_delivery_gate_passed": derived["unauthorized_deliveries"] == 0,
        "pre_provider_gate_passed": derived["blocked_provider_compose_calls"] == 0,
        "migration_evidence_gate_passed": derived["evidence_missing_migrations_allowed"] == 0 and derived["valid_migrations_allowed"] == 4,
        "migration_replay_gate_passed": derived["migration_replay_mismatches"] == 0,
        "rollback_gate_passed": derived["rollback_mismatches"] == 0,
        "task_binding_gate_passed": derived["wrong_binding_migrations_allowed"] == 0,
        "assessment_p95_gate_passed": measurement["assessment_p95_ms"] < 10.0,
        "zero_external_services_gate_passed": receipt["environment"]["external_services"] == 0,
    }
    acceptance = receipt["acceptance"]
    if (
        not isinstance(acceptance, dict)
        or set(acceptance) != set(expected_acceptance)
        or any(type(value) is not bool for value in acceptance.values())
        or acceptance != expected_acceptance
        or not all(expected_acceptance.values())
    ):
        raise ValueError("probe acceptance gates are invalid")
    authority = receipt["authority_boundary"]
    expected_authority = {
        "local_compatibility_gate_measured": True,
        "synthetic_skill_sources": True,
        "provider_adapter_test_double": True,
        "synthetic_evidence_verifier": True,
        "provider_process_invoked": False,
        "provider_network_invoked": False,
        "state_commit_invoked": False,
        "state_write_authority": False,
    }
    if (
        not isinstance(authority, dict)
        or set(authority) != set(expected_authority)
        or any(type(value) is not bool for value in authority.values())
        or authority != expected_authority
    ):
        raise ValueError("probe authority boundary is invalid")
    if receipt["provenance"] != _provenance(root):
        raise ValueError("probe provenance is invalid")
