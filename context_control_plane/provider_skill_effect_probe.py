"""Versioned local replay probe for bounded provider Skill delivery plans."""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import math
import platform
import re
import shlex
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .compiled_skill_packet import compile_skill_packet
from .layered_skill_loader import load_layered_skills
from .layered_skill_plan import (
    compile_layered_skill_load_plan,
    layered_skill_load_plan_digest,
)
from .provider_skill_adapter import (
    canonical_provider_skill_effect_bytes,
    get_provider_skill_adapter,
    provider_effect_digest,
    provider_neutral_effect_digest,
    validate_provider_skill_effect,
)
from .skill_drift_quarantine import assess_skill_drift


SCHEMA_VERSION = "context.provider-skill-effect-probe/v1alpha1"
PROVIDERS = ("codex", "claude")
DEFAULT_SAMPLES = 40
WARMUP_SAMPLES = 1
ADAPTER_P95_LIMIT_MS = 10.0
_EXPECTED_LAYER_BYTES = {"S0": 1024, "S1": 4096, "S2": 4096, "S3": 16384}
SCENARIO_IDS = (
    "checkpoint-recovery",
    "continuation-deduplication",
    "decision-supersession",
    "constraint-carry-forward",
    "active-leaf-resume",
    "return-point-boundary",
    "idea-capture-routing",
    "interrupt-classification",
    "skill-digest-binding",
    "skill-expiry-quarantine",
    "skill-conflict-resolution",
    "rule-identifier-binding",
    "provider-path-materialization",
    "provider-name-materialization",
    "provider-surface-isolation",
    "receipt-asset-integrity",
    "packet-receipt-identity",
    "bounded-composition-bytes",
    "section-boundary-preservation",
    "opaque-reference-preservation",
    "evidence-reference-carry-forward",
    "verification-profile-binding",
    "claim-scope-carry-forward",
    "lease-expiry-visibility",
    "external-effect-provenance",
    "artifact-range-reference",
    "stale-memory-rejection",
    "reverted-work-protection",
    "correction-priority",
    "workflow-role-isolation",
    "multi-agent-handoff",
    "task-path-ownership",
    "state-revision-boundary",
    "cas-conflict-visibility",
    "local-embedded-portability",
    "forge-coordinated-boundary",
    "cross-platform-path-stability",
    "reference-freshness-gate",
    "governance-plan-binding",
    "replay-fixture-provenance",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVENANCE_PATHS = {
    "adapter_sha256": "context_control_plane/provider_skill_adapter.py",
    "probe_sha256": "context_control_plane/provider_skill_effect_probe.py",
    "runner_sha256": "tools/run_provider_skill_effect_probe.py",
    "adapter_schema_sha256": "schemas/m4-05/provider-skill-adapter-effect.schema.json",
    "probe_schema_sha256": "schemas/m4-05/provider-skill-effect-probe.schema.json",
    "adapter_test_sha256": "tests/test_m4_05_provider_skill_adapter.py",
    "probe_test_sha256": "tests/test_m4_05_provider_skill_effect_probe.py",
    "compiler_sha256": "context_control_plane/compiled_skill_packet.py",
    "loader_sha256": "context_control_plane/layered_skill_loader.py",
    "plan_sha256": "context_control_plane/layered_skill_plan.py",
    "drift_validator_sha256": "context_control_plane/skill_drift_quarantine.py",
    "manifest_fixture_sha256": "experiments/skills/m4-01-skill-manifest-set-v1alpha1.json",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "observed_at",
    "provenance",
    "environment",
    "workload",
    "measurement",
    "acceptance",
    "authority_boundary",
    "limitations",
    "generation",
}
_ENVIRONMENT_FIELDS = {
    "python_version",
    "python_implementation",
    "platform",
    "machine",
    "external_services",
}
_MEASUREMENT_FIELDS = {
    "samples_per_provider",
    "warmup_samples_per_scenario_provider",
    "total_effects",
    "total_compose_calls",
    "validated_effects",
    "measured_operation",
    "compose_call_order",
    "latency_samples_ms",
    "adapter_p50_ms",
    "adapter_p95_ms",
    "adapter_max_ms",
    "adapter_p95_limit_ms",
    "effects_by_provider",
    "effect_digests_by_provider",
    "neutral_effect_digests_by_provider",
    "unique_effect_digests_by_provider",
    "unique_neutral_effect_digests",
    "cross_provider_neutral_mismatches",
    "provider_replay_mismatches",
    "effects_declaring_state_write",
    "composition_body_bytes_per_effect",
    "composition_body_bytes_total",
    "provider_payload_bytes_by_provider",
    "effect_bytes_by_provider",
}
_ACCEPTANCE_KEYS = {
    "two_provider_gate_passed",
    "distinct_composition_gate_passed",
    "effect_validation_gate_passed",
    "neutral_replay_gate_passed",
    "provider_replay_gate_passed",
    "state_write_declaration_gate_passed",
    "composition_integrity_gate_passed",
    "adapter_p95_gate_passed",
    "zero_external_services_gate_passed",
}
_AUTHORITY_BOUNDARY = {
    "local_adapter_delivery_plan_measured": True,
    "m4_04_verified_composition_measured": True,
    "provider_process_invoked": False,
    "provider_network_invoked": False,
    "provider_input_tokens_measured": False,
    "provider_prompt_cache_measured": False,
    "provider_rule_following_measured": False,
    "provider_compaction_measured": False,
    "state_port_calls_measured": False,
    "synthetic_plan_authorizer": True,
    "frozen_clock": True,
    "state_revision_authorization_measured": False,
    "state_write_authority": False,
    "timing_cryptographically_attested": False,
}
_LIMITATIONS = [
    "The receipt measures local Context Control Plane delivery-plan construction and replay, not provider process execution.",
    "Codex and Claude surface contracts are Context Control Plane adapter contracts, not provider-owned wire protocols.",
    "Provider input tokens, billable tokens, cache behavior, rule following, context-window use and compaction recovery are unmeasured.",
    "State-port calls are outside this adapter probe; effects only declare zero runtime-state write authority.",
    "Scenario labels identify byte-distinct synthetic compositions and do not execute the named typed workflow behaviors.",
    "The probe uses a synthetic allow-list plan authorizer and frozen clock; actor and state-revision authorization are unmeasured.",
    "Latency is local Python timing on one host and is not cryptographically attested.",
]
_GENERATION_FIELDS = {
    "command",
    "arguments",
    "writes_runtime_state_to_repository",
}


class _ProbePlanAuthorizer:
    def __init__(self, plan_sha256: str):
        self.plan_sha256 = plan_sha256

    def authorize(self, *, plan_sha256, packet_sha256, manifest_set_sha256):
        if plan_sha256 == self.plan_sha256:
            return "authorization.m4-05.probe"
        return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _registry_entry_sha256(path: Path, schema_id: str) -> str:
    registry = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = [
        entry
        for entry in registry.get("schemas", [])
        if entry.get("schema_id") == schema_id
    ]
    if len(entries) != 1:
        raise ValueError(f"{schema_id} registry entry is missing")
    return hashlib.sha256(
        json.dumps(
            entries[0], ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
    ).hexdigest()


def _provenance(root: Path) -> dict[str, str]:
    result = {
        field: _file_sha256(root / relative_path)
        for field, relative_path in _PROVENANCE_PATHS.items()
    }
    registry_path = root / "schemas" / "registry.yaml"
    result["adapter_effect_registry_entry_sha256"] = _registry_entry_sha256(
        registry_path,
        "context.provider-skill-adapter-effect",
    )
    result["probe_receipt_registry_entry_sha256"] = _registry_entry_sha256(
        registry_path,
        "context.provider-skill-effect-probe",
    )
    return result


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 4)


def _scenario_content(scenario_id: str, skill_id: str, size: int) -> bytes:
    seed = (
        f"skill_id={skill_id}\nscenario_id={scenario_id}\n"
        "scope=provider-delivery-replay\n"
    ).encode("ascii")
    repeated = hashlib.sha256(seed).hexdigest().encode("ascii")
    return (seed + repeated * ((size - len(seed) + len(repeated) - 1) // len(repeated)))[
        :size
    ]


def _workload(scenarios: tuple[str, ...]) -> dict[str, Any]:
    return {
        "fixture": "m4-05-two-provider-distinct-synthetic-compositions",
        "providers": list(PROVIDERS),
        "distinct_synthetic_compositions": len(scenarios),
        "coverage_scope": "byte-distinct-synthetic-compositions",
        "scenario_ids": list(scenarios),
        "requested_layers": ["S0", "S2"],
        "layer_content_bytes": dict(_EXPECTED_LAYER_BYTES),
    }


def _probe_case(root: Path, observed_at: str, scenario_id: str):
    fixture = json.loads(
        (root / _PROVENANCE_PATHS["manifest_fixture_sha256"]).read_text(
            encoding="utf-8"
        )
    )
    specs = tuple(
        (
            skill_id,
            layer,
            _scenario_content(scenario_id, skill_id, _EXPECTED_LAYER_BYTES[layer]),
        )
        for skill_id, layer in (
            ("core.bootstrap", "S0"),
            ("core.manifest", "S1"),
            ("project.active", "S2"),
            ("external.reference", "S3"),
        )
    )
    template = fixture["manifests"][0]
    manifests = []
    assets: dict[str, bytes] = {}
    bindings: dict[str, str] = {}
    for skill_id, layer, content in specs:
        manifest = copy.deepcopy(template)
        manifest["skill_id"] = skill_id
        manifest["version"] = "1.0.0"
        manifest["content_sha256"] = hashlib.sha256(content).hexdigest()
        manifest["rule_ids"] = [f"{skill_id}.rule"]
        manifest["dependencies"] = []
        manifest["conflicts"] = []
        manifest["expires_at"] = None
        manifests.append(manifest)
        assets[skill_id] = content
        bindings[skill_id] = layer
    manifest_set = {
        "schema_version": fixture["schema_version"],
        "manifests": manifests,
    }
    packet = compile_skill_packet(
        manifest_set,
        selected_skill_ids=[item[0] for item in specs],
        observed_at=observed_at,
    )
    assessment = assess_skill_drift(
        packet,
        manifest_set,
        asset_resolver=lambda skill_id: assets[skill_id],
        observed_at=observed_at,
    )
    if assessment["gate"] != "allow":
        raise RuntimeError("probe drift assessment did not allow the corpus")
    plan = compile_layered_skill_load_plan(
        packet,
        layer_bindings=bindings,
        requested_layers=["S0", "S2"],
        observed_at=observed_at,
    )
    clock = lambda: datetime.datetime.fromisoformat(
        observed_at.replace("Z", "+00:00")
    )
    authorizer = _ProbePlanAuthorizer(layered_skill_load_plan_digest(plan))
    loaded = load_layered_skills(
        packet,
        assessment,
        assets,
        layer_plan=plan,
        plan_authorizer=authorizer,
        trusted_clock=clock,
    )
    return packet, loaded, plan, authorizer, assessment


def _scenario_order(samples_per_provider: int) -> tuple[str, ...]:
    if type(samples_per_provider) is not int or not 1 <= samples_per_provider <= len(
        SCENARIO_IDS
    ):
        raise ValueError("samples_per_provider must select one to forty scenarios")
    return SCENARIO_IDS[:samples_per_provider]


def _compose_call_order(scenarios: tuple[str, ...]) -> list[dict[str, str]]:
    order: list[dict[str, str]] = []
    for index, scenario_id in enumerate(scenarios):
        providers = PROVIDERS if index % 2 == 0 else tuple(reversed(PROVIDERS))
        for provider in providers:
            order.extend(
                {
                    "scenario_id": scenario_id,
                    "provider_id": provider,
                    "phase": phase,
                }
                for phase in ("warmup", "measured", "replay")
            )
    return order


def _provider_maps(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(PROVIDERS):
        raise ValueError(f"{field} provider keys are invalid")
    return value


def run_provider_skill_effect_probe(
    root: str | Path,
    *,
    samples_per_provider: int = DEFAULT_SAMPLES,
    observed_at: str,
    arguments: list[str],
) -> dict[str, Any]:
    """Measure two local delivery-plan adapters across distinct compositions."""
    scenarios = _scenario_order(samples_per_provider)
    if not isinstance(arguments, list) or any(
        not isinstance(argument, str) for argument in arguments
    ):
        raise ValueError("effect probe arguments must be strings")
    root = Path(root)
    provenance = _provenance(root)
    latency_samples: dict[str, list[float]] = {provider: [] for provider in PROVIDERS}
    effects: dict[str, list[dict[str, Any]]] = {provider: [] for provider in PROVIDERS}
    effect_digests: dict[str, list[str]] = {provider: [] for provider in PROVIDERS}
    neutral_digests: dict[str, list[str]] = {provider: [] for provider in PROVIDERS}
    payload_bytes: dict[str, list[int]] = {provider: [] for provider in PROVIDERS}
    effect_bytes: dict[str, list[int]] = {provider: [] for provider in PROVIDERS}
    replay_mismatches = 0
    validated_effects = 0
    state_write_declarations = 0
    compose_call_order: list[dict[str, str]] = []
    for index, scenario_id in enumerate(scenarios):
        packet, loaded, layer_plan, plan_authorizer, drift_assessment = _probe_case(
            root, observed_at, scenario_id
        )
        provider_order = PROVIDERS if index % 2 == 0 else tuple(reversed(PROVIDERS))
        for provider in provider_order:
            adapter = get_provider_skill_adapter(provider)
            for _ in range(WARMUP_SAMPLES):
                compose_call_order.append(
                    {
                        "scenario_id": scenario_id,
                        "provider_id": provider,
                        "phase": "warmup",
                    }
                )
                adapter.compose(
                    packet,
                    loaded,
                    layer_plan=layer_plan,
                    plan_authorizer=plan_authorizer,
                    drift_assessment=drift_assessment,
                )
            started = time.perf_counter_ns()
            compose_call_order.append(
                {
                    "scenario_id": scenario_id,
                    "provider_id": provider,
                    "phase": "measured",
                }
            )
            effect = adapter.compose(
                packet,
                loaded,
                layer_plan=layer_plan,
                plan_authorizer=plan_authorizer,
                drift_assessment=drift_assessment,
            )
            validate_provider_skill_effect(effect)
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
            compose_call_order.append(
                {
                    "scenario_id": scenario_id,
                    "provider_id": provider,
                    "phase": "replay",
                }
            )
            replay = adapter.compose(
                packet,
                loaded,
                layer_plan=layer_plan,
                plan_authorizer=plan_authorizer,
                drift_assessment=drift_assessment,
            )
            replay_mismatches += provider_effect_digest(effect) != provider_effect_digest(
                replay
            )
            validated_effects += 1
            state_write_declarations += (
                effect["writes_runtime_state"] is not False
                or effect["state_revision"] is not None
            )
            effects[provider].append(effect)
            effect_digests[provider].append(provider_effect_digest(effect))
            neutral_digests[provider].append(provider_neutral_effect_digest(effect))
            latency_samples[provider].append(round(elapsed_ms, 6))
            payload_bytes[provider].append(effect["provider_payload_bytes"])
            effect_bytes[provider].append(len(canonical_provider_skill_effect_bytes(effect)))
    neutral_mismatches = sum(
        neutral_digests["codex"][index] != neutral_digests["claude"][index]
        for index in range(samples_per_provider)
    )
    body_sizes = {
        effect["composition_body_bytes"]
        for provider in PROVIDERS
        for effect in effects[provider]
    }
    if body_sizes != {5120}:
        raise RuntimeError("provider adapters produced inconsistent composition bytes")
    measurement = {
        "samples_per_provider": samples_per_provider,
        "warmup_samples_per_scenario_provider": WARMUP_SAMPLES,
        "total_effects": samples_per_provider * len(PROVIDERS),
        "total_compose_calls": len(compose_call_order),
        "validated_effects": validated_effects,
        "measured_operation": "local_provider_delivery_plan_compose_and_validate",
        "compose_call_order": compose_call_order,
        "latency_samples_ms": latency_samples,
        "adapter_p50_ms": {
            provider: _percentile(latency_samples[provider], 0.50)
            for provider in PROVIDERS
        },
        "adapter_p95_ms": {
            provider: _percentile(latency_samples[provider], 0.95)
            for provider in PROVIDERS
        },
        "adapter_max_ms": {
            provider: round(max(latency_samples[provider]), 4)
            for provider in PROVIDERS
        },
        "adapter_p95_limit_ms": ADAPTER_P95_LIMIT_MS,
        "effects_by_provider": effects,
        "effect_digests_by_provider": effect_digests,
        "neutral_effect_digests_by_provider": neutral_digests,
        "unique_effect_digests_by_provider": {
            provider: len(set(effect_digests[provider])) for provider in PROVIDERS
        },
        "unique_neutral_effect_digests": len(
            set(neutral_digests["codex"] + neutral_digests["claude"])
        ),
        "cross_provider_neutral_mismatches": neutral_mismatches,
        "provider_replay_mismatches": replay_mismatches,
        "effects_declaring_state_write": state_write_declarations,
        "composition_body_bytes_per_effect": 5120,
        "composition_body_bytes_total": 5120 * samples_per_provider * len(PROVIDERS),
        "provider_payload_bytes_by_provider": payload_bytes,
        "effect_bytes_by_provider": effect_bytes,
    }
    acceptance = {
        "two_provider_gate_passed": len(PROVIDERS) == 2,
        "distinct_composition_gate_passed": len(set(scenarios)) == DEFAULT_SAMPLES,
        "effect_validation_gate_passed": validated_effects
        == samples_per_provider * len(PROVIDERS),
        "neutral_replay_gate_passed": neutral_mismatches == 0,
        "provider_replay_gate_passed": replay_mismatches == 0,
        "state_write_declaration_gate_passed": state_write_declarations == 0,
        "composition_integrity_gate_passed": body_sizes == {5120},
        "adapter_p95_gate_passed": all(
            value < ADAPTER_P95_LIMIT_MS
            for value in measurement["adapter_p95_ms"].values()
        ),
        "zero_external_services_gate_passed": True,
    }
    if _provenance(root) != provenance:
        raise RuntimeError("effect probe source changed during measurement")
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at,
        "provenance": provenance,
        "environment": {
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": sys.platform,
            "machine": platform.machine(),
            "external_services": 0,
        },
        "workload": _workload(scenarios),
        "measurement": measurement,
        "acceptance": acceptance,
        "authority_boundary": dict(_AUTHORITY_BOUNDARY),
        "limitations": list(_LIMITATIONS),
        "generation": {
            "command": " ".join(
                [".venv/bin/python", "tools/run_provider_skill_effect_probe.py"]
                + [shlex.quote(argument) for argument in arguments]
            ),
            "arguments": list(arguments),
            "writes_runtime_state_to_repository": False,
        },
    }


def _argument_values(arguments: Any) -> dict[str, str]:
    if (
        not isinstance(arguments, list)
        or len(arguments) % 2 != 0
        or any(not isinstance(argument, str) for argument in arguments)
    ):
        raise ValueError("effect probe generation arguments are invalid")
    allowed = {"--samples-per-provider", "--observed-at", "--output"}
    values: dict[str, str] = {}
    for index in range(0, len(arguments), 2):
        option = arguments[index]
        value = arguments[index + 1]
        if option not in allowed or option in values or not value:
            raise ValueError("effect probe generation arguments are invalid")
        values[option] = value
    if set(values) != allowed:
        raise ValueError("effect probe generation arguments are incomplete")
    return values


def _timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("provider Skill effect probe observed_at is invalid")
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("provider Skill effect probe observed_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("provider Skill effect probe observed_at is invalid")
    return value


def validate_provider_skill_effect_probe_receipt(
    receipt: dict[str, Any],
    *,
    root: str | Path,
) -> None:
    """Reject stale, inconsistent, failed, or overstated M4-05 evidence."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ValueError("provider Skill effect probe fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ValueError("provider Skill effect probe schema is unsupported")
    observed_at = _timestamp(receipt["observed_at"])
    root = Path(root)
    expected_provenance = _provenance(root)
    provenance = receipt["provenance"]
    if (
        not isinstance(provenance, dict)
        or provenance != expected_provenance
        or any(
            not isinstance(value, str) or not _SHA256_RE.fullmatch(value)
            for value in provenance.values()
        )
    ):
        raise ValueError("provider Skill effect probe provenance is stale")
    environment = receipt["environment"]
    workload = receipt["workload"]
    measurement = receipt["measurement"]
    acceptance = receipt["acceptance"]
    boundary = receipt["authority_boundary"]
    generation = receipt["generation"]
    if (
        not isinstance(environment, dict)
        or set(environment) != _ENVIRONMENT_FIELDS
        or not isinstance(measurement, dict)
        or set(measurement) != _MEASUREMENT_FIELDS
        or not isinstance(acceptance, dict)
        or set(acceptance) != _ACCEPTANCE_KEYS
        or not isinstance(boundary, dict)
        or set(boundary) != set(_AUTHORITY_BOUNDARY)
        or not isinstance(generation, dict)
        or set(generation) != _GENERATION_FIELDS
    ):
        raise ValueError("provider Skill effect probe nested fields are invalid")
    if (
        type(environment["external_services"]) is not int
        or environment["external_services"] != 0
        or any(
            not isinstance(environment[field], str)
            or not environment[field]
            or len(environment[field]) > 512
            for field in (
                "python_version",
                "python_implementation",
                "platform",
                "machine",
            )
        )
    ):
        raise ValueError("provider Skill effect probe environment is invalid")
    samples = measurement["samples_per_provider"]
    if (
        type(samples) is not int
        or samples != DEFAULT_SAMPLES
        or measurement["warmup_samples_per_scenario_provider"] != WARMUP_SAMPLES
        or measurement["total_effects"] != samples * len(PROVIDERS)
        or measurement["total_compose_calls"]
        != samples * len(PROVIDERS) * (WARMUP_SAMPLES + 2)
        or measurement["validated_effects"] != samples * len(PROVIDERS)
        or measurement["measured_operation"]
        != "local_provider_delivery_plan_compose_and_validate"
        or workload != _workload(_scenario_order(samples))
    ):
        raise ValueError("provider Skill effect probe samples are invalid")
    scenarios = _scenario_order(samples)
    if measurement["compose_call_order"] != _compose_call_order(scenarios):
        raise ValueError("provider Skill effect probe invocation order is invalid")

    latencies = _provider_maps(measurement["latency_samples_ms"], "latencies")
    p50 = _provider_maps(measurement["adapter_p50_ms"], "p50")
    p95 = _provider_maps(measurement["adapter_p95_ms"], "p95")
    maximum = _provider_maps(measurement["adapter_max_ms"], "maximum")
    for provider in PROVIDERS:
        values = latencies[provider]
        if (
            not isinstance(values, list)
            or len(values) != samples
            or any(
                type(value) not in {int, float}
                or not math.isfinite(value)
                or value <= 0
                for value in values
            )
            or p50[provider] != _percentile(values, 0.50)
            or p95[provider] != _percentile(values, 0.95)
            or maximum[provider] != round(max(values), 4)
            or p95[provider] >= ADAPTER_P95_LIMIT_MS
        ):
            raise ValueError("provider Skill effect probe latency is invalid")
    if measurement["adapter_p95_limit_ms"] != ADAPTER_P95_LIMIT_MS:
        raise ValueError("provider Skill effect probe latency limit is invalid")

    effects = _provider_maps(measurement["effects_by_provider"], "effects")
    effect_digests = _provider_maps(
        measurement["effect_digests_by_provider"], "effect digests"
    )
    neutral_digests = _provider_maps(
        measurement["neutral_effect_digests_by_provider"], "neutral digests"
    )
    unique_effects = _provider_maps(
        measurement["unique_effect_digests_by_provider"], "unique effects"
    )
    payload_bytes = _provider_maps(
        measurement["provider_payload_bytes_by_provider"], "payload bytes"
    )
    serialized_effect_bytes = _provider_maps(
        measurement["effect_bytes_by_provider"], "effect bytes"
    )
    expected_effects: dict[str, list[dict[str, Any]]] = {provider: [] for provider in PROVIDERS}
    for scenario_id in scenarios:
        packet, loaded, layer_plan, plan_authorizer, drift_assessment = _probe_case(
            root, observed_at, scenario_id
        )
        for provider in PROVIDERS:
            expected_effects[provider].append(
                get_provider_skill_adapter(provider).compose(
                    packet,
                    loaded,
                    layer_plan=layer_plan,
                    plan_authorizer=plan_authorizer,
                    drift_assessment=drift_assessment,
                )
            )
    for provider in PROVIDERS:
        provider_effects = effects[provider]
        if not isinstance(provider_effects, list) or len(provider_effects) != samples:
            raise ValueError("provider Skill effect probe effects are invalid")
        for effect in provider_effects:
            try:
                validate_provider_skill_effect(effect)
            except ValueError as exc:
                raise ValueError("provider Skill effect probe effect is invalid") from exc
        expected_digests = [provider_effect_digest(effect) for effect in provider_effects]
        expected_neutral = [
            provider_neutral_effect_digest(effect) for effect in provider_effects
        ]
        expected_payload_bytes = [
            effect["provider_payload_bytes"] for effect in provider_effects
        ]
        expected_effect_bytes = [
            len(canonical_provider_skill_effect_bytes(effect))
            for effect in provider_effects
        ]
        if (
            provider_effects != expected_effects[provider]
            or effect_digests[provider] != expected_digests
            or neutral_digests[provider] != expected_neutral
            or payload_bytes[provider] != expected_payload_bytes
            or serialized_effect_bytes[provider] != expected_effect_bytes
            or unique_effects[provider] != len(set(expected_digests))
        ):
            raise ValueError("provider Skill effect probe output is stale")
    neutral_mismatches = sum(
        neutral_digests["codex"][index] != neutral_digests["claude"][index]
        for index in range(samples)
    )
    state_write_declarations = sum(
        effect["writes_runtime_state"] is not False
        or effect["state_revision"] is not None
        for provider in PROVIDERS
        for effect in effects[provider]
    )
    expected_metrics = {
        "unique_neutral_effect_digests": len(
            set(neutral_digests["codex"] + neutral_digests["claude"])
        ),
        "cross_provider_neutral_mismatches": neutral_mismatches,
        "provider_replay_mismatches": 0,
        "effects_declaring_state_write": state_write_declarations,
        "composition_body_bytes_per_effect": 5120,
        "composition_body_bytes_total": 5120 * samples * len(PROVIDERS),
    }
    if any(
        type(measurement[field]) is not int or measurement[field] != value
        for field, value in expected_metrics.items()
    ):
        raise ValueError("provider Skill effect probe metrics are inconsistent")
    if (
        neutral_mismatches != 0
        or state_write_declarations != 0
        or any(unique_effects[provider] != samples for provider in PROVIDERS)
        or expected_metrics["unique_neutral_effect_digests"] != samples
    ):
        raise ValueError("provider Skill effect probe replay gate failed")
    if not all(value is True for value in acceptance.values()):
        raise ValueError("provider Skill effect probe acceptance failed")
    if boundary != _AUTHORITY_BOUNDARY or any(
        type(value) is not bool for value in boundary.values()
    ):
        raise ValueError("provider Skill effect probe authority boundary changed")
    if receipt["limitations"] != _LIMITATIONS:
        raise ValueError("provider Skill effect probe limitations changed")
    values = _argument_values(generation["arguments"])
    if (
        values["--samples-per-provider"] != str(samples)
        or values["--observed-at"] != observed_at
        or generation["writes_runtime_state_to_repository"] is not False
    ):
        raise ValueError("provider Skill effect probe generation metadata is invalid")
    expected_command = " ".join(
        [".venv/bin/python", "tools/run_provider_skill_effect_probe.py"]
        + [shlex.quote(argument) for argument in generation["arguments"]]
    )
    if generation["command"] != expected_command:
        raise ValueError("provider Skill effect probe command is inconsistent")
