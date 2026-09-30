"""Independent M7-05 affected-test selection verification."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from statistics import median
from typing import Any

from .affected_test_selection import (
    build_affected_change_set,
    build_affected_graph,
    build_derivation_receipt,
    build_test_inventory,
    select_affected_tests,
    validate_affected_test_selection_receipt,
)
from .verification_profile import build_verification_profile

FIXTURE_SCHEMA_VERSION = "context.affected-test-selection-fixture/v1alpha1"
GOLDEN_SCHEMA_VERSION = "context.affected-test-selection-golden/v1alpha1"
BENCHMARK_SCHEMA_VERSION = "context.affected-test-selection-benchmark/v1alpha1"

_SHA256 = set("0123456789abcdef")
_FIXTURE_FIELDS = {
    "schema_version",
    "fixture_id",
    "golden_id",
    "golden_sha256",
    "project_revision",
    "repository_revision",
    "evaluated_at",
    "profile",
    "artifacts",
    "contexts",
    "inventory",
    "graphs",
    "change_sets",
    "micro_workload",
    "state_write_authority",
    "completion_authority",
    "fixture_sha256",
}
_GOLDEN_FIELDS = {
    "schema_version",
    "golden_id",
    "scenarios",
    "performance_scenario_id",
    "real_commands",
    "state_write_authority",
    "completion_authority",
    "golden_sha256",
}
_SCENARIO_FIELDS = {
    "scenario_id",
    "graph_key",
    "changed_paths",
    "graph_provenance_available",
    "expected_selection_mode",
    "expected_test_ids",
    "expected_fallback_reasons",
}
_COMMAND_FIELDS = {"test_id", "argv", "cwd", "command_kind"}
_COMMAND_RECEIPT_FIELDS = {
    "test_id",
    "argv",
    "cwd",
    "exit_codes",
    "wall_time_ns_samples",
    "stdout_sha256_samples",
    "stderr_sha256_samples",
}
_ENVIRONMENT_FIELDS = {
    "python_implementation",
    "python_version",
    "platform_system",
    "platform_machine",
    "repository_tree_sha256",
    "command_catalog_sha256",
    "timing_clock",
}
_BENCHMARK_FIELDS = {
    "schema_version",
    "iterations",
    "fixture_id",
    "fixture_sha256",
    "golden_id",
    "golden_sha256",
    "scenario_counts",
    "selection_mode_counts",
    "missed_test_count",
    "unsafe_partial_selection_count",
    "fallback_mismatch_count",
    "replay_mismatch_count",
    "micro_baseline_test_ids",
    "micro_selected_test_ids",
    "micro_wall_time_ns_samples",
    "micro_processed_bytes",
    "micro_workload_outputs",
    "micro_baseline_wall_time_ns",
    "micro_selected_wall_time_ns",
    "micro_wall_time_reduction_basis_points",
    "micro_bytes_reduction_basis_points",
    "micro_workload_outputs_sha256",
    "real_baseline_test_ids",
    "real_selected_test_ids",
    "real_command_receipts",
    "real_baseline_wall_time_ns",
    "real_selected_wall_time_ns",
    "real_wall_time_reduction_basis_points",
    "real_command_missed_count",
    "environment",
    "external_service_calls",
    "state_write_authority_count",
    "completion_authority_count",
    "benchmark_sha256",
}
_TREE_PATHS = (
    "context_control_plane/affected_test_selection.py",
    "context_control_plane/affected_test_selection_benchmark.py",
    "tests/test_m7_05_affected_test_selection.py",
    "tests/test_m7_05_affected_test_selection_benchmark.py",
    "tests/test_m7_05_contract_schemas.py",
    "tests/test_m7_05_trusted_selection.py",
    "tests/test_m7_05_trusted_benchmark.py",
)


class AffectedTestSelectionBenchmarkError(ValueError):
    """Raised when independent M7-05 verification fails closed."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise AffectedTestSelectionBenchmarkError(
            "benchmark data must be canonical JSON"
        ) from exc


def _digest(value: Mapping[str, Any], field: str) -> str:
    body = {key: item for key, item in value.items() if key != field}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _ref(payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    return f"artifact://sha256/{digest}", digest


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and not (set(value) - _SHA256)
    )


def _uint(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise AffectedTestSelectionBenchmarkError(f"{field} is invalid")
    return value


def _sorted_strings(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or len(value) > 4096
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
        or value != sorted(value)
    ):
        raise AffectedTestSelectionBenchmarkError(f"{field} is invalid")
    return value


def _reduction(baseline: int, selected: int) -> int:
    if baseline <= 0 or selected >= baseline:
        return 0
    return ((baseline - selected) * 10_000) // baseline


def _gate(
    gate_id: str, gate_kind: str, dependencies: list[str] | None = None
) -> dict[str, Any]:
    return {
        "gate_id": gate_id,
        "gate_kind": gate_kind,
        "mode": "required",
        "condition_ref": None,
        "capability_refs": ["capability/local-python"],
        "depends_on_gate_ids": dependencies or [],
        "evidence_requirements": ["artifact-digest"],
        "thresholds": [],
    }


def _test(
    test_id: str,
    gate_id: str,
    node_ids: list[str],
    *,
    always_run: bool = False,
    wall_time_ms: int = 10_000,
    input_bytes: int = 1_000_000,
) -> dict[str, Any]:
    return {
        "test_id": test_id,
        "gate_id": gate_id,
        "node_ids": node_ids,
        "always_run": always_run,
        "estimated_wall_time_ms": wall_time_ms,
        "estimated_input_bytes": input_bytes,
    }


def _node(
    node_id: str, path_prefixes: list[str], dependencies: list[str] | None = None
) -> dict[str, Any]:
    return {
        "node_id": node_id,
        "path_prefixes": path_prefixes,
        "depends_on_node_ids": dependencies or [],
    }


def build_affected_test_selection_golden_matrix() -> dict[str, Any]:
    """Build an oracle that contains no mutable graph or inventory."""
    full = [
        "m6-02-unit",
        "m7-01-unit",
        "m7-02-unit",
        "m7-05-contract",
        "python-compile",
        "ruff-selection",
    ]
    anchors = ["python-compile", "ruff-selection"]
    scenarios = [
        {
            "scenario_id": "selection-core-selected",
            "graph_key": "current",
            "changed_paths": ["context_control_plane/affected_test_selection.py"],
            "graph_provenance_available": True,
            "expected_selection_mode": "selected",
            "expected_test_ids": sorted(anchors + ["m7-05-contract"]),
            "expected_fallback_reasons": [],
        },
        {
            "scenario_id": "evidence-selected",
            "graph_key": "current",
            "changed_paths": ["context_control_plane/assertion_provenance.py"],
            "graph_provenance_available": True,
            "expected_selection_mode": "selected",
            "expected_test_ids": sorted(
                anchors + ["m7-01-unit", "m7-02-unit"]
            ),
            "expected_fallback_reasons": [],
        },
        {
            "scenario_id": "unmapped-full",
            "graph_key": "current",
            "changed_paths": ["generated/unknown.py"],
            "graph_provenance_available": True,
            "expected_selection_mode": "full-validation",
            "expected_test_ids": full,
            "expected_fallback_reasons": ["changed-path-unmapped"],
        },
        {
            "scenario_id": "mixed-unmapped-full",
            "graph_key": "current",
            "changed_paths": [
                "context_control_plane/affected_test_selection.py",
                "generated/unknown.py",
            ],
            "graph_provenance_available": True,
            "expected_selection_mode": "full-validation",
            "expected_test_ids": full,
            "expected_fallback_reasons": ["changed-path-unmapped"],
        },
        {
            "scenario_id": "incomplete-full",
            "graph_key": "incomplete",
            "changed_paths": ["context_control_plane/affected_test_selection.py"],
            "graph_provenance_available": True,
            "expected_selection_mode": "full-validation",
            "expected_test_ids": full,
            "expected_fallback_reasons": ["graph-incomplete"],
        },
        {
            "scenario_id": "dynamic-full",
            "graph_key": "dynamic",
            "changed_paths": ["context_control_plane/affected_test_selection.py"],
            "graph_provenance_available": True,
            "expected_selection_mode": "full-validation",
            "expected_test_ids": full,
            "expected_fallback_reasons": ["dynamic-edges-unknown"],
        },
        {
            "scenario_id": "stale-full",
            "graph_key": "stale",
            "changed_paths": ["context_control_plane/affected_test_selection.py"],
            "graph_provenance_available": True,
            "expected_selection_mode": "full-validation",
            "expected_test_ids": full,
            "expected_fallback_reasons": ["graph-stale"],
        },
        {
            "scenario_id": "missing-provenance-full",
            "graph_key": "current",
            "changed_paths": ["context_control_plane/affected_test_selection.py"],
            "graph_provenance_available": False,
            "expected_selection_mode": "full-validation",
            "expected_test_ids": full,
            "expected_fallback_reasons": ["graph-provenance-unavailable"],
        },
    ]
    python = sys.executable
    commands = [
        {
            "test_id": "m6-02-unit",
            "argv": [python, "-m", "unittest", "tests.test_m6_02_codegraph_verification", "-q"],
            "cwd": "repository-root",
            "command_kind": "unittest",
        },
        {
            "test_id": "m7-01-unit",
            "argv": [python, "-m", "unittest", "tests.test_m7_01_assertion_provenance", "-q"],
            "cwd": "repository-root",
            "command_kind": "unittest",
        },
        {
            "test_id": "m7-02-unit",
            "argv": [python, "-m", "unittest", "tests.test_m7_02_claim_evidence_gate", "-q"],
            "cwd": "repository-root",
            "command_kind": "unittest",
        },
        {
            "test_id": "m7-05-contract",
            "argv": [
                python,
                "-m",
                "unittest",
                "tests.test_m7_05_trusted_selection",
                "-q",
            ],
            "cwd": "repository-root",
            "command_kind": "unittest",
        },
        {
            "test_id": "python-compile",
            "argv": [
                python,
                "-c",
                (
                    "import ast,pathlib,sys;"
                    "[ast.parse(pathlib.Path(p).read_text(encoding='utf-8')) "
                    "for p in sys.argv[1:]]"
                ),
                "context_control_plane/affected_test_selection.py",
                "context_control_plane/affected_test_selection_benchmark.py",
            ],
            "cwd": "repository-root",
            "command_kind": "build",
        },
        {
            "test_id": "ruff-selection",
            "argv": [
                "ruff",
                "check",
                "context_control_plane/affected_test_selection.py",
                "context_control_plane/affected_test_selection_benchmark.py",
                "tests/test_m7_05_trusted_selection.py",
                "tests/test_m7_05_trusted_benchmark.py",
            ],
            "cwd": "repository-root",
            "command_kind": "static",
        },
    ]
    golden = {
        "schema_version": GOLDEN_SCHEMA_VERSION,
        "golden_id": "golden/m7-05/current-repository/v1",
        "scenarios": sorted(scenarios, key=lambda item: item["scenario_id"]),
        "performance_scenario_id": "selection-core-selected",
        "real_commands": sorted(commands, key=lambda item: item["test_id"]),
        "state_write_authority": False,
        "completion_authority": False,
        "golden_sha256": "0" * 64,
    }
    golden["golden_sha256"] = _digest(golden, "golden_sha256")
    validate_affected_test_selection_golden_matrix(golden)
    return copy.deepcopy(golden)


def validate_affected_test_selection_golden_matrix(golden: Any) -> None:
    if not isinstance(golden, Mapping) or set(golden) != _GOLDEN_FIELDS:
        raise AffectedTestSelectionBenchmarkError("golden matrix fields are invalid")
    if golden["schema_version"] != GOLDEN_SCHEMA_VERSION:
        raise AffectedTestSelectionBenchmarkError("golden matrix version is invalid")
    if not isinstance(golden["golden_id"], str) or not golden["golden_id"]:
        raise AffectedTestSelectionBenchmarkError("golden matrix ID is invalid")
    if not _is_sha256(golden["golden_sha256"]) or golden[
        "golden_sha256"
    ] != _digest(golden, "golden_sha256"):
        raise AffectedTestSelectionBenchmarkError("golden matrix digest mismatch")
    scenarios = golden["scenarios"]
    if not isinstance(scenarios, list) or len(scenarios) != 8:
        raise AffectedTestSelectionBenchmarkError("golden scenarios are invalid")
    scenario_ids: set[str] = set()
    for scenario in scenarios:
        if not isinstance(scenario, Mapping) or set(scenario) != _SCENARIO_FIELDS:
            raise AffectedTestSelectionBenchmarkError("golden scenario fields are invalid")
        scenario_id = scenario["scenario_id"]
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in scenario_ids:
            raise AffectedTestSelectionBenchmarkError("golden scenario ID is invalid")
        scenario_ids.add(scenario_id)
        if scenario["graph_key"] not in {"current", "dynamic", "incomplete", "stale"}:
            raise AffectedTestSelectionBenchmarkError("golden graph key is invalid")
        if type(scenario["graph_provenance_available"]) is not bool:
            raise AffectedTestSelectionBenchmarkError("golden provenance flag is invalid")
        if scenario["expected_selection_mode"] not in {"selected", "full-validation"}:
            raise AffectedTestSelectionBenchmarkError("golden selection mode is invalid")
        for field in ("changed_paths", "expected_test_ids", "expected_fallback_reasons"):
            _sorted_strings(
                scenario[field],
                f"scenario.{field}",
                allow_empty=(field == "expected_fallback_reasons"),
            )
    if golden["performance_scenario_id"] not in scenario_ids:
        raise AffectedTestSelectionBenchmarkError("performance scenario is invalid")
    commands = golden["real_commands"]
    if not isinstance(commands, list) or not commands:
        raise AffectedTestSelectionBenchmarkError("real command catalog is invalid")
    command_ids: set[str] = set()
    for command in commands:
        if not isinstance(command, Mapping) or set(command) != _COMMAND_FIELDS:
            raise AffectedTestSelectionBenchmarkError("real command fields are invalid")
        test_id = command["test_id"]
        if not isinstance(test_id, str) or not test_id or test_id in command_ids:
            raise AffectedTestSelectionBenchmarkError("real command ID is invalid")
        command_ids.add(test_id)
        argv = command["argv"]
        if (
            not isinstance(argv, list)
            or not argv
            or len(argv) > 64
            or any(not isinstance(arg, str) or not arg or len(arg) > 4096 for arg in argv)
        ):
            raise AffectedTestSelectionBenchmarkError("real command argv is invalid")
        if command["cwd"] != "repository-root" or command["command_kind"] not in {
            "unittest",
            "build",
            "static",
        }:
            raise AffectedTestSelectionBenchmarkError("real command policy is invalid")
    for scenario in scenarios:
        if not set(scenario["expected_test_ids"]).issubset(command_ids):
            raise AffectedTestSelectionBenchmarkError("golden scenario references unknown test")
    if (
        golden["state_write_authority"] is not False
        or golden["completion_authority"] is not False
    ):
        raise AffectedTestSelectionBenchmarkError("golden matrix has no authority")


def _fixture_derivation_resolver(
    receipt: Mapping[str, Any],
    source: bytes,
    config: bytes,
    dynamic: bytes | None,
) -> dict[str, Any]:
    del config
    try:
        payload = json.loads(source)
        if receipt["subject_kind"] == "affected-graph":
            dynamic_payload = json.loads(dynamic or b"{}")
            return {
                "nodes": payload["nodes"],
                "dynamic_edge_status": dynamic_payload["dynamic_edge_status"],
            }
        return {"tests": payload["tests"]}
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AffectedTestSelectionBenchmarkError(
            "fixture derivation payload is invalid"
        ) from exc


def _anchor(
    *,
    subject_kind: str,
    adapter_id: str,
    adapter_version: str,
    adapter: bytes,
    config: bytes,
    source: bytes,
    dynamic: bytes | None,
) -> dict[str, Any]:
    return {
        "subject_kind": subject_kind,
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "adapter_sha256": _ref(adapter)[1],
        "config_sha256": _ref(config)[1],
        "source_sha256": _ref(source)[1],
        "dynamic_edge_sha256": _ref(dynamic)[1] if dynamic is not None else None,
    }


def _context(
    *,
    project_id: str,
    repository_revision: str,
    profile: Mapping[str, Any],
    graph_anchor: Mapping[str, Any],
    inventory_anchor: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "project_id": project_id,
        "repository_revision": repository_revision,
        "profile_id": profile["profile_id"],
        "profile_sha256": profile["profile_sha256"],
        "derivation_anchors": {
            "affected-graph": copy.deepcopy(dict(graph_anchor)),
            "required-test-inventory": copy.deepcopy(dict(inventory_anchor)),
        },
    }


def _artifact_map(payloads: Sequence[bytes]) -> dict[str, str]:
    return {_ref(payload)[0]: payload.decode("utf-8") for payload in payloads}


def _resolver_for_context(context: Mapping[str, Any]) -> Callable[[str], Mapping[str, Any]]:
    def resolve(project_id: str) -> Mapping[str, Any]:
        if project_id != context["project_id"]:
            raise KeyError(project_id)
        return context

    return resolve


def build_affected_test_selection_fixture(
    *, golden_matrix: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Build source artifacts and derived contracts independently of the oracle."""
    golden = (
        build_affected_test_selection_golden_matrix()
        if golden_matrix is None
        else copy.deepcopy(dict(golden_matrix))
    )
    validate_affected_test_selection_golden_matrix(golden)
    project_id = "context-control-plane"
    repository_revision = "a" * 40
    profile = build_verification_profile(
        profile_id="verification/context-control-plane/m7-05",
        project_id=project_id,
        profile_version="1.0.0-alpha.1",
        revision=1,
        valid_from="2026-08-16T00:00:00Z",
        valid_until=None,
        gates=[
            _gate("static", "static"),
            _gate("tdd", "tdd", ["static"]),
            _gate("build", "build", ["tdd"]),
        ],
    )
    nodes = [
        _node("codegraph-core", ["context_control_plane/codegraph_verification.py"]),
        _node("evidence-core", ["context_control_plane/assertion_provenance.py"]),
        _node(
            "claim-evidence",
            ["context_control_plane/claim_evidence_gate.py"],
            ["evidence-core"],
        ),
        _node(
            "selection-core",
            ["context_control_plane/affected_test_selection.py"],
        ),
        _node(
            "selection-benchmark",
            ["context_control_plane/affected_test_selection_benchmark.py"],
            ["selection-core"],
        ),
        _node(
            "selection-tests",
            ["tests/test_m7_05"],
            ["selection-core", "selection-benchmark"],
        ),
    ]
    tests = [
        _test("m6-02-unit", "tdd", ["codegraph-core"], wall_time_ms=2700),
        _test("m7-01-unit", "tdd", ["evidence-core"], wall_time_ms=100),
        _test("m7-02-unit", "tdd", ["claim-evidence"], wall_time_ms=700),
        _test(
            "m7-05-contract",
            "tdd",
            ["selection-core", "selection-benchmark", "selection-tests"],
            wall_time_ms=200,
        ),
        _test("python-compile", "build", [], always_run=True, wall_time_ms=100),
        _test("ruff-selection", "static", [], always_run=True, wall_time_ms=100),
    ]
    graph_source = _canonical({"nodes": nodes})
    inventory_source = _canonical({"tests": tests})
    current_dynamic = _canonical(
        {
            "dynamic_edge_status": "none",
            "repository_revision": repository_revision,
            "unresolved_edge_count": 0,
        }
    )
    unknown_dynamic = _canonical(
        {
            "dynamic_edge_status": "unknown",
            "repository_revision": repository_revision,
            "unresolved_edge_count": 1,
        }
    )
    graph_adapter = b"context deterministic graph adapter v1"
    inventory_adapter = b"context deterministic inventory adapter v1"
    graph_config = b"context graph config v1"
    inventory_config = b"context inventory config v1"
    graph_anchor = _anchor(
        subject_kind="affected-graph",
        adapter_id="adapter/context/graph",
        adapter_version="1.0.0",
        adapter=graph_adapter,
        config=graph_config,
        source=graph_source,
        dynamic=current_dynamic,
    )
    dynamic_anchor = _anchor(
        subject_kind="affected-graph",
        adapter_id="adapter/context/graph",
        adapter_version="1.0.0",
        adapter=graph_adapter,
        config=graph_config,
        source=graph_source,
        dynamic=unknown_dynamic,
    )
    inventory_anchor = _anchor(
        subject_kind="required-test-inventory",
        adapter_id="adapter/context/inventory",
        adapter_version="1.0.0",
        adapter=inventory_adapter,
        config=inventory_config,
        source=inventory_source,
        dynamic=None,
    )
    current_context = _context(
        project_id=project_id,
        repository_revision=repository_revision,
        profile=profile,
        graph_anchor=graph_anchor,
        inventory_anchor=inventory_anchor,
    )
    dynamic_context = _context(
        project_id=project_id,
        repository_revision=repository_revision,
        profile=profile,
        graph_anchor=dynamic_anchor,
        inventory_anchor=inventory_anchor,
    )
    payloads: list[bytes] = [
        graph_source,
        inventory_source,
        current_dynamic,
        unknown_dynamic,
        graph_adapter,
        inventory_adapter,
        graph_config,
        inventory_config,
    ]
    scenario_diff: dict[str, bytes] = {}
    for scenario in golden["scenarios"]:
        base = hashlib.sha256(scenario["scenario_id"].encode()).hexdigest()[:40]
        payload = _canonical(
            {
                "base_repository_revision": base,
                "repository_revision": repository_revision,
                "changed_paths": scenario["changed_paths"],
            }
        )
        scenario_diff[scenario["scenario_id"]] = payload
        payloads.append(payload)
    artifacts = _artifact_map(payloads)

    def artifact_resolver(ref: str) -> bytes | None:
        content = artifacts.get(ref)
        return content.encode("utf-8") if content is not None else None

    current_context_resolver = _resolver_for_context(current_context)
    dynamic_context_resolver = _resolver_for_context(dynamic_context)

    def derivation(
        *, subject_kind: str, dynamic_payload: bytes | None = None
    ) -> dict[str, Any]:
        graph_kind = subject_kind == "affected-graph"
        adapter = graph_adapter if graph_kind else inventory_adapter
        config = graph_config if graph_kind else inventory_config
        source = graph_source if graph_kind else inventory_source
        context_resolver = (
            dynamic_context_resolver
            if dynamic_payload == unknown_dynamic
            else current_context_resolver
        )
        return build_derivation_receipt(
            derivation_id=(
                f"derivation/m7-05/{subject_kind}/"
                f"{'unknown' if dynamic_payload == unknown_dynamic else 'current'}"
            ),
            subject_kind=subject_kind,
            project_id=project_id,
            repository_revision=repository_revision,
            profile=profile,
            generated_at="2026-08-16T11:00:00Z",
            adapter_id=("adapter/context/graph" if graph_kind else "adapter/context/inventory"),
            adapter_version="1.0.0",
            adapter_artifact_ref=_ref(adapter)[0],
            adapter_artifact_sha256=_ref(adapter)[1],
            config_artifact_ref=_ref(config)[0],
            config_artifact_sha256=_ref(config)[1],
            source_artifact_ref=_ref(source)[0],
            source_artifact_sha256=_ref(source)[1],
            dynamic_edge_artifact_ref=(
                _ref(dynamic_payload or current_dynamic)[0] if graph_kind else None
            ),
            dynamic_edge_artifact_sha256=(
                _ref(dynamic_payload or current_dynamic)[1] if graph_kind else None
            ),
            artifact_resolver=artifact_resolver,
            derivation_resolver=_fixture_derivation_resolver,
            current_context_resolver=context_resolver,
        )

    current_derivation = derivation(subject_kind="affected-graph")
    dynamic_derivation = derivation(
        subject_kind="affected-graph", dynamic_payload=unknown_dynamic
    )
    inventory_derivation = derivation(subject_kind="required-test-inventory")
    inventory = build_test_inventory(
        inventory_id="test-inventory/m7-05/current",
        profile=profile,
        derivation=inventory_derivation,
        generated_at="2026-08-16T11:00:00Z",
        valid_until="2026-08-16T13:00:00Z",
        artifact_resolver=artifact_resolver,
        derivation_resolver=_fixture_derivation_resolver,
        current_context_resolver=current_context_resolver,
    )

    def graph(
        *,
        key: str,
        derivation_receipt: Mapping[str, Any],
        completeness: str,
        valid_until: str,
        context_resolver: Callable[[str], Mapping[str, Any]],
    ) -> dict[str, Any]:
        return build_affected_graph(
            graph_id=f"affected-graph/m7-05/{key}",
            profile=profile,
            derivation=derivation_receipt,
            generated_at="2026-08-16T11:00:00Z",
            valid_until=valid_until,
            completeness=completeness,
            artifact_resolver=artifact_resolver,
            derivation_resolver=_fixture_derivation_resolver,
            current_context_resolver=context_resolver,
        )

    graphs = {
        "current": graph(
            key="current",
            derivation_receipt=current_derivation,
            completeness="complete",
            valid_until="2026-08-16T13:00:00Z",
            context_resolver=current_context_resolver,
        ),
        "incomplete": graph(
            key="incomplete",
            derivation_receipt=current_derivation,
            completeness="incomplete",
            valid_until="2026-08-16T13:00:00Z",
            context_resolver=current_context_resolver,
        ),
        "stale": graph(
            key="stale",
            derivation_receipt=current_derivation,
            completeness="complete",
            valid_until="2026-08-16T11:30:00Z",
            context_resolver=current_context_resolver,
        ),
        "dynamic": graph(
            key="dynamic",
            derivation_receipt=dynamic_derivation,
            completeness="complete",
            valid_until="2026-08-16T13:00:00Z",
            context_resolver=dynamic_context_resolver,
        ),
    }
    change_sets: dict[str, dict[str, Any]] = {}
    for scenario in golden["scenarios"]:
        scenario_id = scenario["scenario_id"]
        payload = scenario_diff[scenario_id]
        base = json.loads(payload)["base_repository_revision"]

        def change_resolver(
            selected_project: str,
            selected_base: str,
            selected_head: str,
            *,
            expected=payload,
            expected_base=base,
        ) -> bytes:
            if (selected_project, selected_base, selected_head) != (
                project_id,
                expected_base,
                repository_revision,
            ):
                raise KeyError(selected_base)
            return expected

        change_sets[scenario_id] = build_affected_change_set(
            change_set_id=f"change-set/m7-05/{scenario_id}",
            project_id=project_id,
            base_repository_revision=base,
            repository_revision=repository_revision,
            generated_at="2026-08-16T11:30:00Z",
            diff_artifact_ref=_ref(payload)[0],
            diff_artifact_sha256=_ref(payload)[1],
            artifact_resolver=artifact_resolver,
            change_set_resolver=change_resolver,
            current_context_resolver=current_context_resolver,
        )
    fixture = {
        "schema_version": FIXTURE_SCHEMA_VERSION,
        "fixture_id": "fixture/m7-05/affected-test-selection/v2",
        "golden_id": golden["golden_id"],
        "golden_sha256": golden["golden_sha256"],
        "project_revision": 58,
        "repository_revision": repository_revision,
        "evaluated_at": "2026-08-16T12:00:00Z",
        "profile": profile,
        "artifacts": dict(sorted(artifacts.items())),
        "contexts": {"current": current_context, "dynamic": dynamic_context},
        "inventory": inventory,
        "graphs": graphs,
        "change_sets": dict(sorted(change_sets.items())),
        "micro_workload": {"payload_size_bytes": 65_536, "repetitions_per_test": 32},
        "state_write_authority": False,
        "completion_authority": False,
        "fixture_sha256": "0" * 64,
    }
    fixture["fixture_sha256"] = _digest(fixture, "fixture_sha256")
    validate_affected_test_selection_fixture(fixture, golden_matrix=golden)
    return copy.deepcopy(fixture)


def validate_affected_test_selection_fixture(
    fixture: Any, *, golden_matrix: Mapping[str, Any]
) -> None:
    validate_affected_test_selection_golden_matrix(golden_matrix)
    if not isinstance(fixture, Mapping) or set(fixture) != _FIXTURE_FIELDS:
        raise AffectedTestSelectionBenchmarkError("benchmark fixture fields are invalid")
    if fixture["schema_version"] != FIXTURE_SCHEMA_VERSION:
        raise AffectedTestSelectionBenchmarkError("benchmark fixture version is invalid")
    for field in ("fixture_id", "repository_revision", "evaluated_at"):
        if not isinstance(fixture[field], str) or not fixture[field]:
            raise AffectedTestSelectionBenchmarkError(f"fixture {field} is invalid")
    _uint(fixture["project_revision"], "project_revision")
    if (
        fixture["golden_id"] != golden_matrix["golden_id"]
        or fixture["golden_sha256"] != golden_matrix["golden_sha256"]
    ):
        raise AffectedTestSelectionBenchmarkError("fixture golden binding mismatch")
    if not _is_sha256(fixture["fixture_sha256"]) or fixture[
        "fixture_sha256"
    ] != _digest(fixture, "fixture_sha256"):
        raise AffectedTestSelectionBenchmarkError("fixture digest mismatch")
    artifacts = fixture["artifacts"]
    if not isinstance(artifacts, Mapping) or not artifacts or len(artifacts) > 1024:
        raise AffectedTestSelectionBenchmarkError("fixture artifacts are invalid")
    for ref, content in artifacts.items():
        if (
            not isinstance(ref, str)
            or not ref.startswith("artifact://sha256/")
            or not isinstance(content, str)
            or hashlib.sha256(content.encode("utf-8")).hexdigest() != ref.rsplit("/", 1)[-1]
        ):
            raise AffectedTestSelectionBenchmarkError("fixture artifact binding mismatch")
    if not isinstance(fixture["contexts"], Mapping) or set(fixture["contexts"]) != {
        "current",
        "dynamic",
    }:
        raise AffectedTestSelectionBenchmarkError("fixture contexts are invalid")
    if not isinstance(fixture["graphs"], Mapping) or set(fixture["graphs"]) != {
        "current",
        "dynamic",
        "incomplete",
        "stale",
    }:
        raise AffectedTestSelectionBenchmarkError("fixture graphs are invalid")
    scenario_ids = {item["scenario_id"] for item in golden_matrix["scenarios"]}
    if not isinstance(fixture["change_sets"], Mapping) or set(
        fixture["change_sets"]
    ) != scenario_ids:
        raise AffectedTestSelectionBenchmarkError("fixture change sets are invalid")
    workload = fixture["micro_workload"]
    if not isinstance(workload, Mapping) or set(workload) != {
        "payload_size_bytes",
        "repetitions_per_test",
    }:
        raise AffectedTestSelectionBenchmarkError("micro workload is invalid")
    payload_size = _uint(workload["payload_size_bytes"], "payload_size_bytes", positive=True)
    repetitions = _uint(
        workload["repetitions_per_test"], "repetitions_per_test", positive=True
    )
    if payload_size > 1_048_576 or repetitions > 4096:
        raise AffectedTestSelectionBenchmarkError("micro workload exceeds bounds")
    if (
        fixture["state_write_authority"] is not False
        or fixture["completion_authority"] is not False
    ):
        raise AffectedTestSelectionBenchmarkError("benchmark fixture has no authority")


def _fixture_artifact_resolver(
    fixture: Mapping[str, Any], *, graph_available: bool
) -> Callable[[str], bytes | None]:
    graph_refs = {
        graph["derivation"]["source_artifact_ref"]
        for graph in fixture["graphs"].values()
    }

    def resolve(ref: str) -> bytes | None:
        if not graph_available and ref in graph_refs:
            return None
        content = fixture["artifacts"].get(ref)
        return content.encode("utf-8") if content is not None else None

    return resolve


def _fixture_context_resolver(
    fixture: Mapping[str, Any], *, graph_key: str
) -> Callable[[str], Mapping[str, Any]]:
    context = fixture["contexts"]["dynamic" if graph_key == "dynamic" else "current"]
    return _resolver_for_context(context)


def _fixture_change_set_resolver(
    fixture: Mapping[str, Any], *, scenario_id: str
) -> Callable[[str, str, str], bytes]:
    change_set = fixture["change_sets"][scenario_id]
    expected = fixture["artifacts"][change_set["diff_artifact_ref"]].encode("utf-8")

    def resolve(project_id: str, base: str, head: str) -> bytes:
        if (project_id, base, head) != (
            change_set["project_id"],
            change_set["base_repository_revision"],
            change_set["repository_revision"],
        ):
            raise KeyError(base)
        return expected

    return resolve


def _select_scenario(
    fixture: Mapping[str, Any], scenario: Mapping[str, Any], *, suffix: str
) -> dict[str, Any]:
    graph_key = scenario["graph_key"]
    artifact_resolver = _fixture_artifact_resolver(
        fixture, graph_available=scenario["graph_provenance_available"]
    )
    context_resolver = _fixture_context_resolver(fixture, graph_key=graph_key)
    change_resolver = _fixture_change_set_resolver(
        fixture, scenario_id=scenario["scenario_id"]
    )
    return select_affected_tests(
        selection_id=f"selection/m7-05/{scenario['scenario_id']}/{suffix}",
        work_id="M7-05",
        project_revision=fixture["project_revision"],
        profile=fixture["profile"],
        inventory=fixture["inventory"],
        graph=fixture["graphs"][graph_key],
        change_set=fixture["change_sets"][scenario["scenario_id"]],
        evaluated_at=fixture["evaluated_at"],
        artifact_resolver=artifact_resolver,
        change_set_resolver=change_resolver,
        derivation_resolver=_fixture_derivation_resolver,
        current_context_resolver=context_resolver,
    )


def _measure_micro_test(
    test_id: str, *, payload_size_bytes: int, repetitions: int
) -> tuple[int, int, str]:
    seed = hashlib.sha256(test_id.encode("utf-8")).digest()
    payload = (seed * ((payload_size_bytes + len(seed) - 1) // len(seed)))[
        :payload_size_bytes
    ]
    accumulator = seed
    started = time.perf_counter_ns()
    for _ in range(repetitions):
        accumulator = hashlib.sha256(accumulator + payload).digest()
    elapsed = max(1, time.perf_counter_ns() - started)
    return elapsed, repetitions * (payload_size_bytes + len(seed)), accumulator.hex()


def _expected_micro_output(
    test_id: str, *, payload_size_bytes: int, repetitions: int
) -> tuple[int, str]:
    seed = hashlib.sha256(test_id.encode("utf-8")).digest()
    payload = (seed * ((payload_size_bytes + len(seed) - 1) // len(seed)))[
        :payload_size_bytes
    ]
    accumulator = seed
    for _ in range(repetitions):
        accumulator = hashlib.sha256(accumulator + payload).digest()
    return repetitions * (payload_size_bytes + len(seed)), accumulator.hex()


def _repository_tree_sha256(repository_root: Path) -> str:
    records: list[dict[str, str]] = []
    for relative in _TREE_PATHS:
        path = repository_root / relative
        if not path.is_file():
            raise AffectedTestSelectionBenchmarkError(
                f"repository tree input is missing: {relative}"
            )
        records.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    return hashlib.sha256(_canonical(records)).hexdigest()


def _command_catalog_sha256(commands: Sequence[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical(commands)).hexdigest()


def _environment(
    repository_root: Path, commands: Sequence[Mapping[str, Any]]
) -> dict[str, str]:
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform_system": platform.system() or "unknown",
        "platform_machine": platform.machine() or "unknown",
        "repository_tree_sha256": _repository_tree_sha256(repository_root),
        "command_catalog_sha256": _command_catalog_sha256(commands),
        "timing_clock": "perf_counter_ns",
    }


def _run_command(
    command: Mapping[str, Any], *, repository_root: Path, samples: int
) -> dict[str, Any]:
    exit_codes: list[int] = []
    wall_samples: list[int] = []
    stdout_hashes: list[str] = []
    stderr_hashes: list[str] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        try:
            completed = subprocess.run(
                command["argv"],
                cwd=repository_root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=120,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AffectedTestSelectionBenchmarkError(
                f"real command failed to execute: {command['test_id']}"
            ) from exc
        wall_samples.append(max(1, time.perf_counter_ns() - started))
        exit_codes.append(completed.returncode)
        stdout_hashes.append(hashlib.sha256(completed.stdout).hexdigest())
        stderr_hashes.append(hashlib.sha256(completed.stderr).hexdigest())
    if any(code != 0 for code in exit_codes):
        raise AffectedTestSelectionBenchmarkError(
            f"real command failed: {command['test_id']}"
        )
    return {
        "test_id": command["test_id"],
        "argv": list(command["argv"]),
        "cwd": command["cwd"],
        "exit_codes": exit_codes,
        "wall_time_ns_samples": wall_samples,
        "stdout_sha256_samples": stdout_hashes,
        "stderr_sha256_samples": stderr_hashes,
    }


def run_affected_test_selection_benchmark(
    *,
    fixture_path: Path,
    golden_path: Path,
    repository_root: Path,
    iterations: int = 1000,
    command_samples: int = 1,
) -> dict[str, Any]:
    if not all(isinstance(path, Path) for path in (fixture_path, golden_path, repository_root)):
        raise AffectedTestSelectionBenchmarkError("benchmark paths are invalid")
    if type(iterations) is not int or not 8 <= iterations <= 100_000:
        raise AffectedTestSelectionBenchmarkError("iterations is invalid")
    if type(command_samples) is not int or not 1 <= command_samples <= 5:
        raise AffectedTestSelectionBenchmarkError("command_samples is invalid")
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    validate_affected_test_selection_golden_matrix(golden)
    validate_affected_test_selection_fixture(fixture, golden_matrix=golden)
    scenario_counts = {item["scenario_id"]: 0 for item in golden["scenarios"]}
    mode_counts: dict[str, int] = {}
    missed = unsafe_partial = fallback_mismatch = replay_mismatch = 0
    state_authority = completion_authority = 0
    for index in range(iterations):
        scenario = golden["scenarios"][index % len(golden["scenarios"])]
        scenario_counts[scenario["scenario_id"]] += 1
        decision = _select_scenario(fixture, scenario, suffix=str(index))
        replay = _select_scenario(fixture, scenario, suffix=str(index))
        if decision != replay:
            replay_mismatch += 1
        expected = set(scenario["expected_test_ids"])
        actual = set(decision["selected_test_ids"])
        missed += len(expected - actual)
        mode = decision["selection_mode"]
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        if scenario["expected_selection_mode"] == "full-validation" and mode != "full-validation":
            unsafe_partial += 1
        if (
            mode != scenario["expected_selection_mode"]
            or decision["fallback_reasons"] != scenario["expected_fallback_reasons"]
            or actual != expected
        ):
            fallback_mismatch += 1
        state_authority += int(decision["state_write_authority"])
        completion_authority += int(decision["completion_authority"])
    performance_scenario = next(
        scenario
        for scenario in golden["scenarios"]
        if scenario["scenario_id"] == golden["performance_scenario_id"]
    )
    performance = _select_scenario(fixture, performance_scenario, suffix="performance")
    baseline_ids = performance["full_test_ids"]
    selected_ids = performance["selected_test_ids"]
    micro_wall: dict[str, list[int]] = {}
    micro_bytes: dict[str, int] = {}
    micro_outputs: dict[str, str] = {}
    workload = fixture["micro_workload"]
    for test_id in baseline_ids:
        elapsed, processed, output = _measure_micro_test(
            test_id,
            payload_size_bytes=workload["payload_size_bytes"],
            repetitions=workload["repetitions_per_test"],
        )
        micro_wall[test_id] = [elapsed]
        micro_bytes[test_id] = processed
        micro_outputs[test_id] = output
    micro_baseline_wall = sum(values[0] for values in micro_wall.values())
    micro_selected_wall = sum(micro_wall[test_id][0] for test_id in selected_ids)
    micro_baseline_bytes = sum(micro_bytes.values())
    micro_selected_bytes = sum(micro_bytes[test_id] for test_id in selected_ids)
    commands = golden["real_commands"]
    command_receipts = [
        _run_command(command, repository_root=repository_root, samples=command_samples)
        for command in commands
    ]
    receipt_by_id = {item["test_id"]: item for item in command_receipts}
    command_ids = sorted(receipt_by_id)
    real_selected_ids = selected_ids
    if command_ids != baseline_ids:
        raise AffectedTestSelectionBenchmarkError(
            "real command catalog differs from full test inventory"
        )
    real_baseline_wall = sum(
        int(median(item["wall_time_ns_samples"])) for item in command_receipts
    )
    real_selected_wall = sum(
        int(median(receipt_by_id[test_id]["wall_time_ns_samples"]))
        for test_id in real_selected_ids
    )
    real_missed = len(set(performance_scenario["expected_test_ids"]) - set(real_selected_ids))
    receipt = {
        "schema_version": BENCHMARK_SCHEMA_VERSION,
        "iterations": iterations,
        "fixture_id": fixture["fixture_id"],
        "fixture_sha256": fixture["fixture_sha256"],
        "golden_id": golden["golden_id"],
        "golden_sha256": golden["golden_sha256"],
        "scenario_counts": dict(sorted(scenario_counts.items())),
        "selection_mode_counts": dict(sorted(mode_counts.items())),
        "missed_test_count": missed,
        "unsafe_partial_selection_count": unsafe_partial,
        "fallback_mismatch_count": fallback_mismatch,
        "replay_mismatch_count": replay_mismatch,
        "micro_baseline_test_ids": baseline_ids,
        "micro_selected_test_ids": selected_ids,
        "micro_wall_time_ns_samples": dict(sorted(micro_wall.items())),
        "micro_processed_bytes": dict(sorted(micro_bytes.items())),
        "micro_workload_outputs": dict(sorted(micro_outputs.items())),
        "micro_baseline_wall_time_ns": micro_baseline_wall,
        "micro_selected_wall_time_ns": micro_selected_wall,
        "micro_wall_time_reduction_basis_points": _reduction(
            micro_baseline_wall, micro_selected_wall
        ),
        "micro_bytes_reduction_basis_points": _reduction(
            micro_baseline_bytes, micro_selected_bytes
        ),
        "micro_workload_outputs_sha256": hashlib.sha256(
            _canonical(micro_outputs)
        ).hexdigest(),
        "real_baseline_test_ids": baseline_ids,
        "real_selected_test_ids": real_selected_ids,
        "real_command_receipts": command_receipts,
        "real_baseline_wall_time_ns": real_baseline_wall,
        "real_selected_wall_time_ns": real_selected_wall,
        "real_wall_time_reduction_basis_points": _reduction(
            real_baseline_wall, real_selected_wall
        ),
        "real_command_missed_count": real_missed,
        "environment": _environment(repository_root, commands),
        "external_service_calls": 0,
        "state_write_authority_count": state_authority,
        "completion_authority_count": completion_authority,
        "benchmark_sha256": "0" * 64,
    }
    receipt["benchmark_sha256"] = _digest(receipt, "benchmark_sha256")
    _validate_benchmark_structure(receipt)
    return receipt


def _validate_command_receipt(receipt: Any) -> None:
    if not isinstance(receipt, Mapping) or set(receipt) != _COMMAND_RECEIPT_FIELDS:
        raise AffectedTestSelectionBenchmarkError("real command receipt fields are invalid")
    if not isinstance(receipt["test_id"], str) or not receipt["test_id"]:
        raise AffectedTestSelectionBenchmarkError("real command receipt ID is invalid")
    if receipt["cwd"] != "repository-root":
        raise AffectedTestSelectionBenchmarkError("real command cwd is invalid")
    argv = receipt["argv"]
    if not isinstance(argv, list) or not argv or any(
        not isinstance(arg, str) or not arg for arg in argv
    ):
        raise AffectedTestSelectionBenchmarkError("real command receipt argv is invalid")
    samples = receipt["wall_time_ns_samples"]
    if not isinstance(samples, list) or not 1 <= len(samples) <= 5 or any(
        type(value) is not int or value <= 0 for value in samples
    ):
        raise AffectedTestSelectionBenchmarkError("real timing samples are invalid")
    sample_count = len(samples)
    if (
        not isinstance(receipt["exit_codes"], list)
        or receipt["exit_codes"] != [0] * sample_count
    ):
        raise AffectedTestSelectionBenchmarkError("real command exit code failed")
    for field in ("stdout_sha256_samples", "stderr_sha256_samples"):
        values = receipt[field]
        if (
            not isinstance(values, list)
            or len(values) != sample_count
            or any(not _is_sha256(value) for value in values)
        ):
            raise AffectedTestSelectionBenchmarkError(
                f"real command {field} is invalid"
            )


def _validate_benchmark_structure(receipt: Any) -> None:
    if not isinstance(receipt, Mapping) or set(receipt) != _BENCHMARK_FIELDS:
        raise AffectedTestSelectionBenchmarkError("benchmark fields are invalid")
    if receipt["schema_version"] != BENCHMARK_SCHEMA_VERSION:
        raise AffectedTestSelectionBenchmarkError("benchmark version is invalid")
    iterations = _uint(receipt["iterations"], "iterations", positive=True)
    if iterations > 100_000:
        raise AffectedTestSelectionBenchmarkError("benchmark iterations exceed bounds")
    for field in ("fixture_id", "golden_id"):
        if not isinstance(receipt[field], str) or not receipt[field]:
            raise AffectedTestSelectionBenchmarkError(f"{field} is invalid")
    for field in (
        "fixture_sha256",
        "golden_sha256",
        "micro_workload_outputs_sha256",
        "benchmark_sha256",
    ):
        if not _is_sha256(receipt[field]):
            raise AffectedTestSelectionBenchmarkError(f"{field} is invalid")
    scenarios = receipt["scenario_counts"]
    if (
        not isinstance(scenarios, Mapping)
        or len(scenarios) != 8
        or any(
            not isinstance(key, str)
            or not key
            or type(value) is not int
            or value < 0
            for key, value in scenarios.items()
        )
        or sum(scenarios.values()) != iterations
    ):
        raise AffectedTestSelectionBenchmarkError("scenario counts are invalid")
    modes = receipt["selection_mode_counts"]
    if (
        not isinstance(modes, Mapping)
        or not set(modes).issubset({"selected", "full-validation"})
        or any(type(value) is not int or value < 0 for value in modes.values())
        or sum(modes.values()) != iterations
    ):
        raise AffectedTestSelectionBenchmarkError("selection mode counts are invalid")
    for field in (
        "missed_test_count",
        "unsafe_partial_selection_count",
        "fallback_mismatch_count",
        "replay_mismatch_count",
        "real_command_missed_count",
        "external_service_calls",
        "state_write_authority_count",
        "completion_authority_count",
    ):
        _uint(receipt[field], field)
        if receipt[field] != 0:
            raise AffectedTestSelectionBenchmarkError(f"{field} safety veto failed")
    baseline = _sorted_strings(receipt["micro_baseline_test_ids"], "micro baseline")
    selected = _sorted_strings(receipt["micro_selected_test_ids"], "micro selected")
    real_baseline = _sorted_strings(receipt["real_baseline_test_ids"], "real baseline")
    real_selected = _sorted_strings(receipt["real_selected_test_ids"], "real selected")
    if baseline != real_baseline or selected != real_selected or not set(selected).issubset(
        baseline
    ):
        raise AffectedTestSelectionBenchmarkError("micro and real selections differ")
    wall = receipt["micro_wall_time_ns_samples"]
    processed = receipt["micro_processed_bytes"]
    outputs = receipt["micro_workload_outputs"]
    for field, values in (
        ("micro_wall_time_ns_samples", wall),
        ("micro_processed_bytes", processed),
        ("micro_workload_outputs", outputs),
    ):
        if not isinstance(values, Mapping) or set(values) != set(baseline):
            raise AffectedTestSelectionBenchmarkError(f"{field} keys are invalid")
    if any(
        not isinstance(values, list)
        or len(values) != 1
        or type(values[0]) is not int
        or values[0] <= 0
        for values in wall.values()
    ):
        raise AffectedTestSelectionBenchmarkError("micro timing samples are invalid")
    if any(type(value) is not int or value <= 0 for value in processed.values()):
        raise AffectedTestSelectionBenchmarkError("micro byte counts are invalid")
    if any(not _is_sha256(value) for value in outputs.values()):
        raise AffectedTestSelectionBenchmarkError("micro outputs are invalid")
    for field in (
        "micro_baseline_wall_time_ns",
        "micro_selected_wall_time_ns",
        "micro_wall_time_reduction_basis_points",
        "micro_bytes_reduction_basis_points",
        "real_baseline_wall_time_ns",
        "real_selected_wall_time_ns",
        "real_wall_time_reduction_basis_points",
    ):
        _uint(receipt[field], field, positive=True)
    micro_baseline = sum(values[0] for values in wall.values())
    micro_selected = sum(wall[test_id][0] for test_id in selected)
    baseline_bytes = sum(processed.values())
    selected_bytes = sum(processed[test_id] for test_id in selected)
    if (
        receipt["micro_baseline_wall_time_ns"] != micro_baseline
        or receipt["micro_selected_wall_time_ns"] != micro_selected
        or receipt["micro_wall_time_reduction_basis_points"]
        != _reduction(micro_baseline, micro_selected)
        or receipt["micro_bytes_reduction_basis_points"]
        != _reduction(baseline_bytes, selected_bytes)
        or receipt["micro_workload_outputs_sha256"]
        != hashlib.sha256(_canonical(outputs)).hexdigest()
    ):
        raise AffectedTestSelectionBenchmarkError("micro benchmark accounting mismatch")
    command_receipts = receipt["real_command_receipts"]
    if not isinstance(command_receipts, list) or len(command_receipts) != len(baseline):
        raise AffectedTestSelectionBenchmarkError("real command receipts are invalid")
    for command_receipt in command_receipts:
        _validate_command_receipt(command_receipt)
    receipt_by_id = {item["test_id"]: item for item in command_receipts}
    if len(receipt_by_id) != len(command_receipts) or sorted(receipt_by_id) != baseline:
        raise AffectedTestSelectionBenchmarkError("real command receipt IDs are invalid")
    real_baseline_wall = sum(
        int(median(item["wall_time_ns_samples"])) for item in command_receipts
    )
    real_selected_wall = sum(
        int(median(receipt_by_id[test_id]["wall_time_ns_samples"]))
        for test_id in selected
    )
    if (
        receipt["real_baseline_wall_time_ns"] != real_baseline_wall
        or receipt["real_selected_wall_time_ns"] != real_selected_wall
        or receipt["real_wall_time_reduction_basis_points"]
        != _reduction(real_baseline_wall, real_selected_wall)
        or receipt["real_wall_time_reduction_basis_points"] < 3000
    ):
        raise AffectedTestSelectionBenchmarkError("real command wall-time gate failed")
    environment = receipt["environment"]
    if not isinstance(environment, Mapping) or set(environment) != _ENVIRONMENT_FIELDS:
        raise AffectedTestSelectionBenchmarkError("benchmark environment is invalid")
    if any(not isinstance(value, str) or not value for value in environment.values()):
        raise AffectedTestSelectionBenchmarkError("benchmark environment value is invalid")
    if not _is_sha256(environment["repository_tree_sha256"]) or not _is_sha256(
        environment["command_catalog_sha256"]
    ):
        raise AffectedTestSelectionBenchmarkError("benchmark environment digest is invalid")
    if environment["timing_clock"] != "perf_counter_ns":
        raise AffectedTestSelectionBenchmarkError("benchmark timing clock is invalid")
    if receipt["benchmark_sha256"] != _digest(receipt, "benchmark_sha256"):
        raise AffectedTestSelectionBenchmarkError("benchmark digest mismatch")


def validate_affected_test_selection_benchmark(
    receipt: Any,
    *,
    fixture_path: Path,
    golden_path: Path,
    repository_root: Path,
) -> None:
    """Validate structure and independently replay deterministic evidence."""
    verify_affected_test_selection_benchmark(
        receipt,
        fixture_path=fixture_path,
        golden_path=golden_path,
        repository_root=repository_root,
        rerun_real_commands=False,
    )


def verify_affected_test_selection_benchmark(
    receipt: Any,
    *,
    fixture_path: Path,
    golden_path: Path,
    repository_root: Path,
    rerun_real_commands: bool,
) -> None:
    _validate_benchmark_structure(receipt)
    golden = json.loads(golden_path.read_text(encoding="utf-8"))
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    validate_affected_test_selection_golden_matrix(golden)
    validate_affected_test_selection_fixture(fixture, golden_matrix=golden)
    if (
        receipt["fixture_id"] != fixture["fixture_id"]
        or receipt["fixture_sha256"] != fixture["fixture_sha256"]
        or receipt["golden_id"] != golden["golden_id"]
        or receipt["golden_sha256"] != golden["golden_sha256"]
    ):
        raise AffectedTestSelectionBenchmarkError("benchmark source binding mismatch")
    if receipt["environment"] != _environment(
        repository_root, golden["real_commands"]
    ):
        raise AffectedTestSelectionBenchmarkError("benchmark environment is not current")
    expected_counts = {item["scenario_id"]: 0 for item in golden["scenarios"]}
    expected_modes: dict[str, int] = {}
    missed = unsafe = fallback = replay_mismatch = 0
    for index in range(receipt["iterations"]):
        scenario = golden["scenarios"][index % len(golden["scenarios"])]
        expected_counts[scenario["scenario_id"]] += 1
        decision = _select_scenario(fixture, scenario, suffix=str(index))
        replay = _select_scenario(fixture, scenario, suffix=str(index))
        replay_mismatch += int(decision != replay)
        mode = decision["selection_mode"]
        expected_modes[mode] = expected_modes.get(mode, 0) + 1
        expected_tests = set(scenario["expected_test_ids"])
        actual_tests = set(decision["selected_test_ids"])
        missed += len(expected_tests - actual_tests)
        unsafe += int(
            scenario["expected_selection_mode"] == "full-validation"
            and mode != "full-validation"
        )
        fallback += int(
            mode != scenario["expected_selection_mode"]
            or decision["fallback_reasons"] != scenario["expected_fallback_reasons"]
            or actual_tests != expected_tests
        )
        validate_affected_test_selection_receipt(
            decision,
            profile=fixture["profile"],
            inventory=fixture["inventory"],
            graph=fixture["graphs"][scenario["graph_key"]],
            change_set=fixture["change_sets"][scenario["scenario_id"]],
            artifact_resolver=_fixture_artifact_resolver(
                fixture,
                graph_available=scenario["graph_provenance_available"],
            ),
            change_set_resolver=_fixture_change_set_resolver(
                fixture, scenario_id=scenario["scenario_id"]
            ),
            derivation_resolver=_fixture_derivation_resolver,
            current_context_resolver=_fixture_context_resolver(
                fixture, graph_key=scenario["graph_key"]
            ),
        )
    if (
        receipt["scenario_counts"] != dict(sorted(expected_counts.items()))
        or receipt["selection_mode_counts"] != dict(sorted(expected_modes.items()))
        or receipt["missed_test_count"] != missed
        or receipt["unsafe_partial_selection_count"] != unsafe
        or receipt["fallback_mismatch_count"] != fallback
        or receipt["replay_mismatch_count"] != replay_mismatch
    ):
        raise AffectedTestSelectionBenchmarkError(
            "benchmark counts differ from independent replay"
        )
    workload = fixture["micro_workload"]
    expected_bytes: dict[str, int] = {}
    expected_outputs: dict[str, str] = {}
    for test_id in receipt["micro_baseline_test_ids"]:
        processed, output = _expected_micro_output(
            test_id,
            payload_size_bytes=workload["payload_size_bytes"],
            repetitions=workload["repetitions_per_test"],
        )
        expected_bytes[test_id] = processed
        expected_outputs[test_id] = output
    if (
        receipt["micro_processed_bytes"] != dict(sorted(expected_bytes.items()))
        or receipt["micro_workload_outputs"] != dict(sorted(expected_outputs.items()))
        or receipt["micro_workload_outputs_sha256"]
        != hashlib.sha256(_canonical(expected_outputs)).hexdigest()
    ):
        raise AffectedTestSelectionBenchmarkError(
            "micro workload outputs differ from independent replay"
        )
    catalog = {item["test_id"]: item for item in golden["real_commands"]}
    for command_receipt in receipt["real_command_receipts"]:
        command = catalog.get(command_receipt["test_id"])
        if command is None or command_receipt["argv"] != command["argv"]:
            raise AffectedTestSelectionBenchmarkError("real command catalog binding mismatch")
    if rerun_real_commands:
        fresh = [
            _run_command(command, repository_root=repository_root, samples=1)
            for command in golden["real_commands"]
        ]
        fresh_by_id = {item["test_id"]: item for item in fresh}
        fresh_baseline = sum(item["wall_time_ns_samples"][0] for item in fresh)
        fresh_selected = sum(
            fresh_by_id[test_id]["wall_time_ns_samples"][0]
            for test_id in receipt["real_selected_test_ids"]
        )
        if _reduction(fresh_baseline, fresh_selected) < 3000:
            raise AffectedTestSelectionBenchmarkError(
                "fresh real command wall-time gate failed"
            )
