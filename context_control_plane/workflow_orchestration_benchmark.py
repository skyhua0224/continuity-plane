"""Offline M8-03 workflow replay, patch, and rollover benchmark."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .durable_workflow import (
    DurableWorkflowError,
    activate_workflow_patch,
    append_workflow_step,
    build_workflow_definition,
    continue_workflow_as_new,
    create_workflow_run,
    record_workflow_effect,
    record_workflow_input,
    replay_workflow_chain,
    validate_workflow_replay_receipt,
)

SCHEMA_VERSION = "context.workflow-orchestration-benchmark/v1alpha1"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {
    "schema_version",
    "benchmark_id",
    "generated_at",
    "samples",
    "generations_per_chain",
    "successful_chains",
    "run_count",
    "rollover_count",
    "history_event_count",
    "command_count",
    "fault_families",
    "fault_attempts",
    "fault_rejections",
    "metrics",
    "latency_ms",
    "provider_invocations",
    "external_services",
    "shared_authority_claim",
    "temporal_sdk_reference",
    "provenance",
    "thresholds",
    "verdict",
    "receipt_sha256",
}
_METRICS = {
    "replay_mismatches",
    "lost_inputs",
    "duplicate_effects",
    "authority_violations",
    "chain_link_failures",
    "nondeterminism_failures",
}
_PROVENANCE_PATHS = {
    "durable_workflow_sha256": "context_control_plane/durable_workflow.py",
    "orchestration_sha256": "context_control_plane/workflow_orchestration.py",
    "temporal_adapter_sha256": "context_control_plane/temporal_workflow_adapter.py",
    "benchmark_sha256": "context_control_plane/workflow_orchestration_benchmark.py",
}
_WORKFLOW_SCHEMA_IDS = {
    "context.workflow-run",
    "context.workflow-history-event",
    "context.workflow-definition",
    "context.workflow-replay-receipt",
    "context.workflow-rollover-receipt",
    "context.workflow-backend-capabilities",
    "context.workflow-runtime-selection",
    "context.workflow-adapter-manifest",
    "context.temporal-workflow-input",
    "context.workflow-run-receipt",
    "context.workflow-backend-binding",
    "context.workflow-orchestration-benchmark",
}
_FAULT_FAMILIES = (
    "event-hash-tamper",
    "chain-link-tamper",
    "unsupported-history-version",
    "unknown-patch-marker",
    "unsettled-effect-rollover",
    "unacknowledged-input-rollover",
    "continuation-payload-tamper",
)


class WorkflowOrchestrationBenchmarkError(RuntimeError):
    """Raised when workflow benchmark evidence is incomplete or forged."""


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
        raise WorkflowOrchestrationBenchmarkError(
            "benchmark data must be canonical JSON"
        ) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise WorkflowOrchestrationBenchmarkError(
            f"benchmark provenance is unavailable: {path}"
        ) from exc


def _workflow_registry_sha256(path: Path) -> str:
    try:
        registry = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise WorkflowOrchestrationBenchmarkError(
            f"workflow registry provenance is unavailable: {path}"
        ) from exc
    schemas = registry.get("schemas") if isinstance(registry, dict) else None
    if not isinstance(schemas, list):
        raise WorkflowOrchestrationBenchmarkError(
            "workflow registry provenance is invalid"
        )
    selected = [
        entry
        for entry in schemas
        if isinstance(entry, dict) and entry.get("schema_id") in _WORKFLOW_SCHEMA_IDS
    ]
    selected_ids = [entry["schema_id"] for entry in selected]
    if len(selected_ids) != len(set(selected_ids)) or set(selected_ids) != _WORKFLOW_SCHEMA_IDS:
        raise WorkflowOrchestrationBenchmarkError(
            "workflow registry provenance is incomplete"
        )
    return _digest(
        {
            "registry_version": registry.get("registry_version"),
            "schemas": sorted(selected, key=lambda entry: entry["schema_id"]),
        }
    )


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise WorkflowOrchestrationBenchmarkError("generated_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WorkflowOrchestrationBenchmarkError("generated_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkflowOrchestrationBenchmarkError("generated_at requires a timezone")
    return value


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index], 6)


def _result_sha256(step_id: str, input_sha256: str, patches: frozenset[str]) -> str:
    return _digest(
        {
            "step_id": step_id,
            "input_sha256": input_sha256,
            "patches": sorted(patches),
        }
    )


def _authority(revision: int) -> dict[str, Any]:
    return {
        "project_revision": revision,
        "event_head": {
            "sequence_no": revision,
            "event_sha256": _digest({"revision": revision}),
        },
    }


def _definition() -> dict[str, Any]:
    return build_workflow_definition(
        workflow_type="context.campaign",
        definition_version=2,
        implementation_sha256=_digest("workflow-definition-v2"),
        supported_history_versions=[1, 2],
        patches=[
            {
                "patch_id": "route-v2",
                "introduced_in_version": 2,
                "deprecated_in_version": None,
                "semantic_sha256": _digest("route-v2-semantics"),
            }
        ],
    )


def _build_chain(sample: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    version = 1 if sample % 2 == 0 else 2
    implementation_sha256 = _digest(f"workflow-definition-v{version}")
    run = create_workflow_run(
        project_id="project-m8-03",
        root_work_id=f"M8-03-{sample}",
        request_id=f"request-m8-03-{sample}",
        workflow_type="context.campaign",
        definition_version=version,
        implementation_sha256=implementation_sha256,
        authority=_authority(sample * 10 + 1),
        checkpoint_ref="artifact://sha256/" + _digest([sample, 1]),
        checkpoint_sha256=_digest([sample, 1]),
        continuation_sha256=_digest([sample, "continuation", 1]),
        started_at=f"2026-08-16T20:{sample % 60:02d}:00+08:00",
    )
    if version == 2:
        run = activate_workflow_patch(
            run,
            definition=_definition(),
            patch_id="route-v2",
            occurred_at=f"2026-08-16T20:{sample % 60:02d}:01+08:00",
        )
    runs: list[dict[str, Any]] = []
    rollovers: list[dict[str, Any]] = []
    for generation in range(1, 4):
        input_sha256 = _digest([sample, generation, "input"])
        input_id = f"input-{sample}-{generation}"
        run = record_workflow_input(
            run,
            input_id=input_id,
            input_sha256=input_sha256,
            status="received",
            occurred_at=f"2026-08-16T20:{sample % 60:02d}:{generation * 10:02d}+08:00",
        )
        run = record_workflow_input(
            run,
            input_id=input_id,
            input_sha256=input_sha256,
            status="acknowledged",
            occurred_at=f"2026-08-16T20:{sample % 60:02d}:{generation * 10 + 1:02d}+08:00",
        )
        run = append_workflow_step(
            run,
            step_id=f"step-{generation}",
            input_sha256=input_sha256,
            result_sha256=_result_sha256(
                f"step-{generation}",
                input_sha256,
                frozenset(run["active_patch_ids"]),
            ),
            occurred_at=f"2026-08-16T20:{sample % 60:02d}:{generation * 10 + 2:02d}+08:00",
        )
        effect_id = f"effect-{sample}-{generation}"
        effect_key = f"effect-key-{sample}-{generation}"
        request_sha256 = _digest([sample, generation, "effect"])
        run = record_workflow_effect(
            run,
            effect_id=effect_id,
            effect_key=effect_key,
            request_sha256=request_sha256,
            status="started",
            occurred_at=f"2026-08-16T20:{sample % 60:02d}:{generation * 10 + 3:02d}+08:00",
        )
        run = record_workflow_effect(
            run,
            effect_id=effect_id,
            effect_key=effect_key,
            request_sha256=request_sha256,
            status="settled",
            occurred_at=f"2026-08-16T20:{sample % 60:02d}:{generation * 10 + 4:02d}+08:00",
        )
        if generation == 3:
            runs.append(run)
            break
        checkpoint_sha256 = _digest([sample, generation + 1])
        rollover = continue_workflow_as_new(
            run,
            authority=_authority(sample * 10 + generation + 1),
            checkpoint_ref="artifact://sha256/" + checkpoint_sha256,
            checkpoint_sha256=checkpoint_sha256,
            continuation_sha256=_digest([sample, "continuation", generation + 1]),
            occurred_at=f"2026-08-16T20:{sample % 60:02d}:{generation * 10 + 5:02d}+08:00",
        )
        runs.append(rollover["closed_run"])
        rollovers.append(rollover["receipt"])
        run = rollover["next_run"]
    return runs, rollovers


def _expect_rejected(action: Any) -> bool:
    try:
        action()
    except DurableWorkflowError:
        return True
    return False


def _chain_link_failure_count(chain: list[dict[str, Any]]) -> int:
    failures = 0
    for index, run in enumerate(chain):
        if run["generation"] != index + 1:
            failures += 1
        if index == 0:
            failures += run["previous_run_id"] is not None
            failures += run["previous_run_event_sha256"] is not None
            continue
        predecessor = chain[index - 1]
        failures += run["previous_run_id"] != predecessor["run_id"]
        failures += (
            run["previous_run_event_sha256"]
            != predecessor["history"][-1]["event_sha256"]
        )
    return failures


def _nondeterminism_failure_count(first: dict[str, Any], second: dict[str, Any]) -> int:
    return int(first != second)


def _fault_rejections(
    chain: list[dict[str, Any]], *, definition: dict[str, Any], sample: int
) -> int:
    rejected = 0

    tampered = copy.deepcopy(chain)
    tampered[0]["history"][0]["event_sha256"] = "0" * 64
    rejected += _expect_rejected(
        lambda: replay_workflow_chain(
            tampered, definition=definition, step_resolver=_result_sha256
        )
    )

    broken = copy.deepcopy(chain)
    broken[1]["previous_run_id"] = "wfr_broken"
    rejected += _expect_rejected(
        lambda: replay_workflow_chain(
            broken, definition=definition, step_resolver=_result_sha256
        )
    )

    incompatible = build_workflow_definition(
        workflow_type="context.campaign",
        definition_version=3,
        implementation_sha256=_digest("workflow-definition-v3"),
        supported_history_versions=[3],
        patches=definition["patches"],
    )
    rejected += _expect_rejected(
        lambda: replay_workflow_chain(
            chain, definition=incompatible, step_resolver=_result_sha256
        )
    )

    foreign_definition = build_workflow_definition(
        workflow_type="context.campaign",
        definition_version=2,
        implementation_sha256=_digest("workflow-definition-v2"),
        supported_history_versions=[1, 2],
        patches=[
            *definition["patches"],
            {
                "patch_id": f"unknown-patch-{sample}",
                "introduced_in_version": 1,
                "deprecated_in_version": None,
                "semantic_sha256": _digest([sample, "unknown-patch"]),
            },
        ],
    )
    unknown = activate_workflow_patch(
        chain[-1],
        definition=foreign_definition,
        patch_id=f"unknown-patch-{sample}",
        occurred_at=f"2026-08-16T21:{sample % 60:02d}:00+08:00",
    )
    rejected += _expect_rejected(
        lambda: replay_workflow_chain(
            [unknown], definition=definition, step_resolver=_result_sha256
        )
    )

    unsafe = create_workflow_run(
        project_id="project-m8-03",
        root_work_id=f"unsafe-{sample}",
        request_id=f"unsafe-request-{sample}",
        workflow_type="context.campaign",
        definition_version=1,
        implementation_sha256=_digest("workflow-definition-v1"),
        authority=_authority(sample + 1),
        checkpoint_ref="artifact://sha256/" + _digest([sample, "unsafe"]),
        checkpoint_sha256=_digest([sample, "unsafe"]),
        continuation_sha256=_digest([sample, "unsafe-continuation"]),
        started_at=f"2026-08-16T21:{sample % 60:02d}:01+08:00",
    )
    unsafe = record_workflow_effect(
        unsafe,
        effect_id=f"unsafe-effect-{sample}",
        effect_key=f"unsafe-key-{sample}",
        request_sha256=_digest([sample, "unsafe-effect"]),
        status="started",
        occurred_at=f"2026-08-16T21:{sample % 60:02d}:02+08:00",
    )
    rejected += _expect_rejected(
        lambda: continue_workflow_as_new(
            unsafe,
            authority=_authority(sample + 2),
            checkpoint_ref="artifact://sha256/" + _digest([sample, "next"]),
            checkpoint_sha256=_digest([sample, "next"]),
            continuation_sha256=_digest([sample, "next-continuation"]),
            occurred_at=f"2026-08-16T21:{sample % 60:02d}:03+08:00",
        )
    )

    pending = create_workflow_run(
        project_id="project-m8-03",
        root_work_id=f"pending-{sample}",
        request_id=f"pending-request-{sample}",
        workflow_type="context.campaign",
        definition_version=1,
        implementation_sha256=_digest("workflow-definition-v1"),
        authority=_authority(sample + 1),
        checkpoint_ref="artifact://sha256/" + _digest([sample, "pending"]),
        checkpoint_sha256=_digest([sample, "pending"]),
        continuation_sha256=_digest([sample, "pending-continuation"]),
        started_at=f"2026-08-16T21:{sample % 60:02d}:04+08:00",
    )
    pending = record_workflow_input(
        pending,
        input_id=f"pending-input-{sample}",
        input_sha256=_digest([sample, "pending-input"]),
        status="received",
        occurred_at=f"2026-08-16T21:{sample % 60:02d}:05+08:00",
    )
    rejected += _expect_rejected(
        lambda: continue_workflow_as_new(
            pending,
            authority=_authority(sample + 2),
            checkpoint_ref="artifact://sha256/" + _digest([sample, "pending-next"]),
            checkpoint_sha256=_digest([sample, "pending-next"]),
            continuation_sha256=_digest([sample, "pending-next-continuation"]),
            occurred_at=f"2026-08-16T21:{sample % 60:02d}:06+08:00",
        )
    )

    continuation_tampered = copy.deepcopy(chain)
    closed = continuation_tampered[0]
    closed["history"][-1]["payload"]["checkpoint_sha256"] = "0" * 64
    event = closed["history"][-1]
    event_body = copy.deepcopy(event)
    event_body.pop("event_sha256")
    event["event_sha256"] = _digest(event_body)
    closed["run_sha256"] = ""
    run_body = copy.deepcopy(closed)
    run_body.pop("run_sha256")
    closed["run_sha256"] = _digest(run_body)
    rejected += _expect_rejected(
        lambda: replay_workflow_chain(
            continuation_tampered,
            definition=definition,
            step_resolver=_result_sha256,
        )
    )
    return rejected


def benchmark_workflow_orchestration(
    *, root: Path, samples: int = 1000, generated_at: str
) -> dict[str, Any]:
    """Measure three-generation replay and five fail-closed fault families."""
    if type(samples) is not int or samples <= 0 or samples > 10000:
        raise WorkflowOrchestrationBenchmarkError("samples is invalid")
    root = Path(root).resolve()
    _timestamp(generated_at)
    definition = _definition()
    latencies: list[float] = []
    successful_chains = 0
    run_count = 0
    rollover_count = 0
    history_event_count = 0
    command_count = 0
    fault_rejections = 0
    replay_mismatches = 0
    lost_inputs = 0
    duplicate_effects = 0
    authority_violations = 0
    chain_link_failures = 0
    nondeterminism_failures = 0

    for sample in range(samples):
        started = time.perf_counter_ns()
        chain, rollovers = _build_chain(sample)
        replay = replay_workflow_chain(
            chain, definition=definition, step_resolver=_result_sha256
        )
        second_replay = replay_workflow_chain(
            copy.deepcopy(chain),
            definition=copy.deepcopy(definition),
            step_resolver=_result_sha256,
        )
        validate_workflow_replay_receipt(
            replay,
            runs=chain,
            definition=definition,
            step_resolver=_result_sha256,
        )
        chain_link_failures += _chain_link_failure_count(chain)
        nondeterminism_failures += _nondeterminism_failure_count(replay, second_replay)
        fault_rejections += _fault_rejections(
            chain, definition=definition, sample=sample
        )
        latencies.append((time.perf_counter_ns() - started) / 1_000_000)
        successful_chains += replay["command_mismatches"] == 0
        run_count += replay["run_count"]
        rollover_count += len(rollovers)
        history_event_count += replay["history_event_count"]
        command_count += replay["command_count"]
        replay_mismatches += replay["command_mismatches"]
        lost_inputs += sum(item["lost_inputs"] for item in rollovers)
        duplicate_effects += sum(item["duplicate_effects"] for item in rollovers)
        authority_violations += sum(
            any(
                run[field] is not False
                for field in (
                    "state_write_authority",
                    "effect_dispatch_authority",
                    "provider_native_authority",
                )
            )
            for run in chain
        )
        authority_violations += any(
            replay[field] is not False
            for field in (
                "state_write_authority",
                "effect_dispatch_authority",
                "provider_native_authority",
            )
        )

    provenance = {
        field: _file_sha256(root / relative)
        for field, relative in _PROVENANCE_PATHS.items()
    }
    provenance["registry_sha256"] = _workflow_registry_sha256(
        root / "schemas/registry.yaml"
    )
    fault_attempts = samples * len(_FAULT_FAMILIES)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "benchmark_id": "benchmark-m8-03-"
        + _digest(
            {
                "samples": samples,
                "definition": definition["manifest_sha256"],
                "faults": list(_FAULT_FAMILIES),
            }
        )[:24],
        "generated_at": generated_at,
        "samples": samples,
        "generations_per_chain": 3,
        "successful_chains": successful_chains,
        "run_count": run_count,
        "rollover_count": rollover_count,
        "history_event_count": history_event_count,
        "command_count": command_count,
        "fault_families": list(_FAULT_FAMILIES),
        "fault_attempts": fault_attempts,
        "fault_rejections": fault_rejections,
        "metrics": {
            "replay_mismatches": replay_mismatches,
            "lost_inputs": lost_inputs,
            "duplicate_effects": duplicate_effects,
            "authority_violations": authority_violations,
            "chain_link_failures": chain_link_failures,
            "nondeterminism_failures": nondeterminism_failures,
        },
        "latency_ms": {
            "p50": _percentile(latencies, 0.50),
            "p95": _percentile(latencies, 0.95),
            "max": round(max(latencies), 6),
        },
        "provider_invocations": 0,
        "external_services": 0,
        "shared_authority_claim": False,
        "temporal_sdk_reference": {
            "release": "1.31.0",
            "source_revision": "84b519e0ff407b049da88ac7d1711f110494ff4d",
            "source_tree": "6ca7d581e9e0bea3f19a0e1bf5f3a5ef9fec6d21",
            "source_listing_sha256": (
                "44fcd507cce70c1fd4210edcb554c9b0275b849bfe8ac9867aa0af7975f16435"
            ),
            "runtime_tested": False,
            "worker_replayer": "unavailable",
        },
        "provenance": provenance,
        "thresholds": {
            "successful_chain_rate_min": 1.0,
            "fault_rejection_rate_min": 1.0,
            "zero_loss_required": True,
            "latency_p95_ms_max": 50.0,
        },
        "verdict": {
            "decision": "pass",
            "successful_chain_rate": successful_chains / samples,
            "fault_rejection_rate": fault_rejections / fault_attempts,
            "latency_p95_within_threshold": _percentile(latencies, 0.95) < 50,
            "failed_gates": [],
        },
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    validate_workflow_orchestration_benchmark(receipt, root=root)
    return receipt


def validate_workflow_orchestration_benchmark(
    receipt: Any, *, root: Path
) -> dict[str, Any]:
    """Validate current provenance and every veto metric."""
    if not isinstance(receipt, dict) or set(receipt) != _FIELDS:
        raise WorkflowOrchestrationBenchmarkError(
            "benchmark receipt fields are invalid"
        )
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise WorkflowOrchestrationBenchmarkError(
            "benchmark receipt version is invalid"
        )
    _timestamp(receipt["generated_at"])
    for field in (
        "samples",
        "generations_per_chain",
        "successful_chains",
        "run_count",
        "rollover_count",
        "history_event_count",
        "command_count",
        "fault_attempts",
        "fault_rejections",
    ):
        if type(receipt[field]) is not int or receipt[field] < 0:
            raise WorkflowOrchestrationBenchmarkError(f"{field} is invalid")
    samples = receipt["samples"]
    if samples <= 0 or receipt["generations_per_chain"] != 3:
        raise WorkflowOrchestrationBenchmarkError("benchmark sample shape is invalid")
    if receipt["successful_chains"] != samples:
        raise WorkflowOrchestrationBenchmarkError("workflow chains did not all replay")
    if receipt["run_count"] != samples * 3 or receipt["rollover_count"] != samples * 2:
        raise WorkflowOrchestrationBenchmarkError("workflow chain counts are invalid")
    if receipt["fault_families"] != list(_FAULT_FAMILIES):
        raise WorkflowOrchestrationBenchmarkError("fault families are invalid")
    expected_faults = samples * len(_FAULT_FAMILIES)
    if (
        receipt["fault_attempts"] != expected_faults
        or receipt["fault_rejections"] != expected_faults
    ):
        raise WorkflowOrchestrationBenchmarkError("fault rejection gate failed")
    if not isinstance(receipt["metrics"], dict) or set(receipt["metrics"]) != _METRICS:
        raise WorkflowOrchestrationBenchmarkError("benchmark metrics are invalid")
    if any(value != 0 for value in receipt["metrics"].values()):
        raise WorkflowOrchestrationBenchmarkError("benchmark veto metric is non-zero")
    latency = receipt["latency_ms"]
    if (
        not isinstance(latency, dict)
        or set(latency) != {"p50", "p95", "max"}
        or any(
            type(value) not in {int, float} or value <= 0 for value in latency.values()
        )
        or not (latency["p50"] <= latency["p95"] <= latency["max"])
    ):
        raise WorkflowOrchestrationBenchmarkError("benchmark latency is invalid")
    if receipt["provider_invocations"] != 0 or receipt["external_services"] != 0:
        raise WorkflowOrchestrationBenchmarkError(
            "offline benchmark used an external backend"
        )
    if receipt["shared_authority_claim"] is not False:
        raise WorkflowOrchestrationBenchmarkError("benchmark claimed shared authority")
    temporal = receipt["temporal_sdk_reference"]
    if temporal != {
        "release": "1.31.0",
        "source_revision": "84b519e0ff407b049da88ac7d1711f110494ff4d",
        "source_tree": "6ca7d581e9e0bea3f19a0e1bf5f3a5ef9fec6d21",
        "source_listing_sha256": "44fcd507cce70c1fd4210edcb554c9b0275b849bfe8ac9867aa0af7975f16435",
        "runtime_tested": False,
        "worker_replayer": "unavailable",
    }:
        raise WorkflowOrchestrationBenchmarkError("Temporal SDK reference is invalid")
    root = Path(root).resolve()
    expected_provenance = {
        field: _file_sha256(root / relative)
        for field, relative in _PROVENANCE_PATHS.items()
    }
    expected_provenance["registry_sha256"] = _workflow_registry_sha256(
        root / "schemas/registry.yaml"
    )
    if receipt["provenance"] != expected_provenance:
        raise WorkflowOrchestrationBenchmarkError("benchmark provenance mismatch")
    expected_thresholds = {
        "successful_chain_rate_min": 1.0,
        "fault_rejection_rate_min": 1.0,
        "zero_loss_required": True,
        "latency_p95_ms_max": 50.0,
    }
    if receipt["thresholds"] != expected_thresholds:
        raise WorkflowOrchestrationBenchmarkError("benchmark thresholds are invalid")
    expected_verdict = {
        "decision": "pass",
        "successful_chain_rate": 1.0,
        "fault_rejection_rate": 1.0,
        "latency_p95_within_threshold": receipt["latency_ms"]["p95"] < 50,
        "failed_gates": [],
    }
    if (
        receipt["verdict"] != expected_verdict
        or not expected_verdict["latency_p95_within_threshold"]
    ):
        raise WorkflowOrchestrationBenchmarkError("benchmark verdict is invalid")
    if (
        not isinstance(receipt["receipt_sha256"], str)
        or _SHA_RE.fullmatch(receipt["receipt_sha256"]) is None
    ):
        raise WorkflowOrchestrationBenchmarkError("benchmark receipt digest is invalid")
    expected_digest = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if receipt["receipt_sha256"] != expected_digest:
        raise WorkflowOrchestrationBenchmarkError("benchmark receipt digest mismatch")
    return copy.deepcopy(receipt)
