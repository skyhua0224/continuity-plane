"""Provider-neutral project Verification Profile contracts and evaluation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .schema_governance import SchemaGovernanceError, SemanticVersion

PROFILE_SCHEMA_VERSION = "context.verification-profile/v1alpha1"
ADAPTER_SCHEMA_VERSION = "context.verification-adapter/v1alpha1"
RUN_SCHEMA_VERSION = "context.verification-run-receipt/v1alpha1"
DECISION_SCHEMA_VERSION = "context.verification-decision/v1alpha1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
_GATE_KINDS = {
    "static",
    "tdd",
    "build",
    "contract",
    "golden",
    "mutation",
    "loopback",
    "weak-network",
    "live",
    "performance",
    "fault-recovery",
}
_MODES = {"required", "conditional", "optional"}
_THRESHOLD_OPERATORS = {"gte", "lte", "eq"}
_THRESHOLD_UNITS = {
    "basis-points",
    "milliseconds",
    "bytes-per-second",
    "count",
}
_PROFILE_FIELDS = {
    "schema_version",
    "profile_id",
    "project_id",
    "profile_version",
    "revision",
    "valid_from",
    "valid_until",
    "gates",
    "state_write_authority",
    "completion_authority",
    "profile_sha256",
}
_GATE_FIELDS = {
    "gate_id",
    "gate_kind",
    "mode",
    "condition_ref",
    "capability_refs",
    "depends_on_gate_ids",
    "evidence_requirements",
    "thresholds",
}
_THRESHOLD_FIELDS = {"metric", "operator", "value", "unit"}
_ADAPTER_FIELDS = {
    "schema_version",
    "adapter_id",
    "adapter_version",
    "project_id",
    "profile_id",
    "profile_sha256",
    "bindings",
    "state_write_authority",
    "completion_authority",
    "adapter_sha256",
}
_BINDING_INPUT_FIELDS = {
    "gate_id",
    "runner_kind",
    "executable",
    "arguments",
    "working_directory_ref",
    "environment_refs",
    "timeout_ms",
    "output_budget_bytes",
}
_BINDING_FIELDS = _BINDING_INPUT_FIELDS | {"invocation_sha256"}
_RUN_FIELDS = {
    "schema_version",
    "run_id",
    "work_id",
    "project_id",
    "project_revision",
    "repository_revision",
    "profile_id",
    "profile_sha256",
    "adapter_id",
    "adapter_sha256",
    "gate_id",
    "gate_kind",
    "invocation_sha256",
    "status",
    "started_at",
    "completed_at",
    "evidence_steps",
    "measurements",
    "state_write_authority",
    "completion_authority",
    "receipt_sha256",
}
_STEP_FIELDS = {"kind", "observed_at", "artifact_ref", "artifact_sha256"}
_MEASUREMENT_FIELDS = {"metric", "value", "unit"}
_DECISION_FIELDS = {
    "schema_version",
    "decision_id",
    "work_id",
    "project_id",
    "project_revision",
    "profile_id",
    "profile_sha256",
    "adapter_id",
    "adapter_sha256",
    "gate_outcomes",
    "overall_status",
    "evaluated_at",
    "state_write_authority",
    "completion_authority",
    "decision_sha256",
}
_OUTCOME_FIELDS = {
    "gate_id",
    "gate_kind",
    "mode",
    "status",
    "reason",
    "non_blocking",
    "condition_ref",
    "capability_refs",
    "run_receipt_sha256",
    "evidence_refs",
}
_OUTCOME_REASONS = {
    "gate-satisfied",
    "run-failed",
    "threshold-not-met",
    "condition-not-met",
    "condition-unknown",
    "capability-unavailable",
    "missing-run",
    "optional-not-run",
    "dependency-unsatisfied",
    "missing-evidence",
}
_EVIDENCE_REQUIREMENTS = {
    "command": "command",
    "exit-status": "exit-status",
    "artifact-digest": "artifact-digest",
    "red": "red-test-failed",
    "green": "green-test-passed",
    "mutation-summary": "mutation-summary",
    "environment-class": "environment-class",
}


class VerificationProfileError(ValueError):
    """Raised when a verification contract or decision is unsafe."""


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
        raise VerificationProfileError("verification data must be canonical JSON") from exc


def _body(value: Mapping[str, Any], digest_field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != digest_field}


def _digest(value: Mapping[str, Any], digest_field: str) -> str:
    return hashlib.sha256(_canonical(_body(value, digest_field))).hexdigest()


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise VerificationProfileError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, *, maximum: int = 512) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or "\x00" in value
    ):
        raise VerificationProfileError(f"{field} is invalid")
    return value


def _uint(value: Any, field: str, *, maximum: int | None = None) -> int:
    if type(value) is not int or value < 0 or (maximum is not None and value > maximum):
        raise VerificationProfileError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise VerificationProfileError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerificationProfileError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise VerificationProfileError(f"{field} requires a timezone")
    return parsed


def _strings(
    value: Any,
    field: str,
    *,
    allow_empty: bool = True,
    identifiers: bool = False,
) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value) or len(value) > 256:
        raise VerificationProfileError(f"{field} is invalid")
    for item in value:
        (_id if identifiers else _text)(item, field)
    if len(value) != len(set(value)):
        raise VerificationProfileError(f"{field} must be unique")
    return value


def _semantic_version(value: Any, field: str) -> str:
    try:
        SemanticVersion.parse(value)
    except SchemaGovernanceError as exc:
        raise VerificationProfileError(f"{field} is invalid") from exc
    return value


def _normalize_gate(gate: Any) -> dict[str, Any]:
    if not isinstance(gate, Mapping) or set(gate) != _GATE_FIELDS:
        raise VerificationProfileError("verification gate fields are invalid")
    normalized = copy.deepcopy(dict(gate))
    _id(normalized["gate_id"], "gate_id")
    if normalized["gate_kind"] not in _GATE_KINDS:
        raise VerificationProfileError("gate_kind is invalid")
    mode = normalized["mode"]
    if mode not in _MODES:
        raise VerificationProfileError("gate mode is invalid")
    condition_ref = normalized["condition_ref"]
    if mode == "conditional":
        _id(condition_ref, "condition_ref")
    elif condition_ref is not None:
        raise VerificationProfileError("only conditional gates may have condition_ref")
    for field in ("capability_refs", "depends_on_gate_ids", "evidence_requirements"):
        _strings(
            normalized[field],
            field,
            allow_empty=(field == "depends_on_gate_ids"),
            identifiers=True,
        )
        normalized[field] = sorted(normalized[field])
    thresholds = normalized["thresholds"]
    if not isinstance(thresholds, list) or len(thresholds) > 64:
        raise VerificationProfileError("thresholds are invalid")
    metrics: set[str] = set()
    for threshold in thresholds:
        if not isinstance(threshold, Mapping) or set(threshold) != _THRESHOLD_FIELDS:
            raise VerificationProfileError("threshold fields are invalid")
        metric = _id(threshold["metric"], "threshold.metric")
        if metric in metrics:
            raise VerificationProfileError("threshold metrics must be unique")
        metrics.add(metric)
        if threshold["operator"] not in _THRESHOLD_OPERATORS:
            raise VerificationProfileError("threshold operator is invalid")
        _uint(threshold["value"], "threshold.value")
        if threshold["unit"] not in _THRESHOLD_UNITS:
            raise VerificationProfileError("threshold unit is invalid")
    normalized["thresholds"] = sorted(
        (dict(item) for item in thresholds), key=lambda item: item["metric"]
    )
    return normalized


def _normalize_profile(profile: Any, *, observed_at: str | None = None) -> dict[str, Any]:
    if not isinstance(profile, Mapping) or set(profile) != _PROFILE_FIELDS:
        raise VerificationProfileError("verification profile fields are invalid")
    normalized = copy.deepcopy(dict(profile))
    if normalized["schema_version"] != PROFILE_SCHEMA_VERSION:
        raise VerificationProfileError("verification profile version is invalid")
    _id(normalized["profile_id"], "profile_id")
    _id(normalized["project_id"], "project_id")
    _semantic_version(normalized["profile_version"], "profile_version")
    _uint(normalized["revision"], "revision")
    valid_from = _timestamp(normalized["valid_from"], "valid_from")
    valid_until = normalized["valid_until"]
    if valid_until is not None:
        expires = _timestamp(valid_until, "valid_until")
        if expires <= valid_from:
            raise VerificationProfileError("profile validity interval is invalid")
    else:
        expires = None
    if observed_at is not None:
        observed = _timestamp(observed_at, "observed_at")
        if observed < valid_from or (expires is not None and observed >= expires):
            raise VerificationProfileError("verification profile is not current")
    gates = normalized["gates"]
    if not isinstance(gates, list) or not gates or len(gates) > 256:
        raise VerificationProfileError("verification profile gates are invalid")
    normalized_gates = [_normalize_gate(gate) for gate in gates]
    by_id = {gate["gate_id"]: gate for gate in normalized_gates}
    if len(by_id) != len(normalized_gates):
        raise VerificationProfileError("gate_id must be unique")
    if not any(gate["mode"] == "required" for gate in normalized_gates):
        raise VerificationProfileError("verification profile requires a required gate")
    for gate in normalized_gates:
        if gate["gate_id"] in gate["depends_on_gate_ids"] or any(
            dependency not in by_id for dependency in gate["depends_on_gate_ids"]
        ):
            raise VerificationProfileError("gate dependency is invalid")
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(gate_id: str) -> None:
        if gate_id in visiting:
            raise VerificationProfileError("verification gate dependency cycle")
        if gate_id in visited:
            return
        visiting.add(gate_id)
        for dependency in by_id[gate_id]["depends_on_gate_ids"]:
            visit(dependency)
        visiting.remove(gate_id)
        visited.add(gate_id)

    for gate_id in by_id:
        visit(gate_id)
    normalized["gates"] = sorted(normalized_gates, key=lambda gate: gate["gate_id"])
    if normalized["state_write_authority"] is not False or normalized["completion_authority"] is not False:
        raise VerificationProfileError("verification profile has no state or completion authority")
    digest = normalized["profile_sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise VerificationProfileError("profile_sha256 is invalid")
    if digest != _digest(normalized, "profile_sha256"):
        raise VerificationProfileError("verification profile digest mismatch")
    return normalized


def validate_verification_profile(profile: Any, *, observed_at: str | None = None) -> None:
    _normalize_profile(profile, observed_at=observed_at)


def canonical_verification_profile_bytes(profile: Mapping[str, Any]) -> bytes:
    return _canonical(_normalize_profile(profile))


def build_verification_profile(
    *,
    profile_id: str,
    project_id: str,
    profile_version: str,
    revision: int,
    valid_from: str,
    valid_until: str | None,
    gates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "profile_id": profile_id,
        "project_id": project_id,
        "profile_version": profile_version,
        "revision": revision,
        "valid_from": valid_from,
        "valid_until": valid_until,
        "gates": [_normalize_gate(gate) for gate in gates],
        "state_write_authority": False,
        "completion_authority": False,
        "profile_sha256": "0" * 64,
    }
    profile["gates"] = sorted(profile["gates"], key=lambda gate: gate["gate_id"])
    profile["profile_sha256"] = _digest(profile, "profile_sha256")
    validate_verification_profile(profile)
    return copy.deepcopy(profile)


def _binding_digest(binding: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(_body(binding, "invocation_sha256"))).hexdigest()


def _normalize_binding(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, Mapping) or set(binding) != _BINDING_FIELDS:
        raise VerificationProfileError("adapter binding fields are invalid")
    normalized = copy.deepcopy(dict(binding))
    _id(normalized["gate_id"], "binding.gate_id")
    if normalized["runner_kind"] not in {"local-process", "project-tool", "host-probe"}:
        raise VerificationProfileError("runner_kind is invalid")
    executable = _text(normalized["executable"], "executable")
    if executable.rsplit("/", 1)[-1].lower() in {
        "sh",
        "bash",
        "zsh",
        "cmd",
        "cmd.exe",
        "powershell",
        "pwsh",
    }:
        raise VerificationProfileError("shell executables are not typed adapter invocations")
    arguments = _strings(normalized["arguments"], "arguments")
    if any(argument in {"-c", "/c", "-command"} for argument in arguments) and any(
        re.search(r"[|;&`$<>]", argument) for argument in arguments
    ):
        raise VerificationProfileError("free shell command strings are forbidden")
    working_directory = _text(
        normalized["working_directory_ref"], "working_directory_ref"
    )
    if not working_directory.startswith("repo://"):
        raise VerificationProfileError("working_directory_ref must be repository-relative")
    environment_refs = _strings(normalized["environment_refs"], "environment_refs")
    if any(not ref.startswith(("env://", "secret://")) for ref in environment_refs):
        raise VerificationProfileError("environment values must use typed references")
    _uint(normalized["timeout_ms"], "timeout_ms", maximum=86_400_000)
    _uint(
        normalized["output_budget_bytes"],
        "output_budget_bytes",
        maximum=1_073_741_824,
    )
    digest = normalized["invocation_sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise VerificationProfileError("invocation_sha256 is invalid")
    if digest != _binding_digest(normalized):
        raise VerificationProfileError("invocation digest mismatch")
    return normalized


def _normalize_adapter(adapter: Any) -> dict[str, Any]:
    if not isinstance(adapter, Mapping) or set(adapter) != _ADAPTER_FIELDS:
        raise VerificationProfileError("verification adapter fields are invalid")
    normalized = copy.deepcopy(dict(adapter))
    if normalized["schema_version"] != ADAPTER_SCHEMA_VERSION:
        raise VerificationProfileError("verification adapter version is invalid")
    for field in ("adapter_id", "project_id", "profile_id"):
        _id(normalized[field], field)
    _semantic_version(normalized["adapter_version"], "adapter_version")
    if not _SHA256_RE.fullmatch(normalized["profile_sha256"] or ""):
        raise VerificationProfileError("adapter profile digest is invalid")
    bindings = normalized["bindings"]
    if not isinstance(bindings, list) or not bindings or len(bindings) > 256:
        raise VerificationProfileError("adapter bindings are invalid")
    normalized_bindings = [_normalize_binding(binding) for binding in bindings]
    if len({binding["gate_id"] for binding in normalized_bindings}) != len(normalized_bindings):
        raise VerificationProfileError("adapter gate bindings must be unique")
    normalized["bindings"] = sorted(normalized_bindings, key=lambda item: item["gate_id"])
    if normalized["state_write_authority"] is not False or normalized["completion_authority"] is not False:
        raise VerificationProfileError("verification adapter has no state or completion authority")
    digest = normalized["adapter_sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise VerificationProfileError("adapter_sha256 is invalid")
    if digest != _digest(normalized, "adapter_sha256"):
        raise VerificationProfileError("verification adapter digest mismatch")
    return normalized


def canonical_verification_adapter_bytes(adapter: Mapping[str, Any]) -> bytes:
    return _canonical(_normalize_adapter(adapter))


def build_verification_adapter(
    *,
    adapter_id: str,
    adapter_version: str,
    project_id: str,
    profile: Mapping[str, Any],
    bindings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    normalized_bindings: list[dict[str, Any]] = []
    for source in bindings:
        if not isinstance(source, Mapping) or set(source) != _BINDING_INPUT_FIELDS:
            raise VerificationProfileError("adapter binding input fields are invalid")
        binding = copy.deepcopy(dict(source))
        binding["invocation_sha256"] = "0" * 64
        binding["invocation_sha256"] = _binding_digest(binding)
        normalized_bindings.append(_normalize_binding(binding))
    expected_gate_ids = {gate["gate_id"] for gate in normalized_profile["gates"]}
    if {binding["gate_id"] for binding in normalized_bindings} != expected_gate_ids:
        raise VerificationProfileError("adapter must bind every profile gate exactly once")
    adapter = {
        "schema_version": ADAPTER_SCHEMA_VERSION,
        "adapter_id": adapter_id,
        "adapter_version": adapter_version,
        "project_id": project_id,
        "profile_id": normalized_profile["profile_id"],
        "profile_sha256": normalized_profile["profile_sha256"],
        "bindings": sorted(normalized_bindings, key=lambda item: item["gate_id"]),
        "state_write_authority": False,
        "completion_authority": False,
        "adapter_sha256": "0" * 64,
    }
    if project_id != normalized_profile["project_id"]:
        raise VerificationProfileError("adapter project does not match profile")
    adapter["adapter_sha256"] = _digest(adapter, "adapter_sha256")
    _normalize_adapter(adapter)
    return copy.deepcopy(adapter)


def _normalize_run_receipt(
    receipt: Any,
    *,
    profile: Mapping[str, Any],
    adapter: Mapping[str, Any],
    evaluation_time: datetime | None = None,
) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    normalized_adapter = _normalize_adapter(adapter)
    if not isinstance(receipt, Mapping) or set(receipt) != _RUN_FIELDS:
        raise VerificationProfileError("verification run receipt fields are invalid")
    normalized = copy.deepcopy(dict(receipt))
    if normalized["schema_version"] != RUN_SCHEMA_VERSION:
        raise VerificationProfileError("verification run version is invalid")
    for field in ("run_id", "work_id", "project_id", "profile_id", "adapter_id", "gate_id"):
        _id(normalized[field], field)
    _uint(normalized["project_revision"], "project_revision")
    if not _GIT_REVISION_RE.fullmatch(normalized["repository_revision"] or ""):
        raise VerificationProfileError("repository_revision is invalid")
    if (
        normalized["project_id"] != normalized_profile["project_id"]
        or normalized["profile_id"] != normalized_profile["profile_id"]
        or normalized["profile_sha256"] != normalized_profile["profile_sha256"]
        or normalized["adapter_id"] != normalized_adapter["adapter_id"]
        or normalized["adapter_sha256"] != normalized_adapter["adapter_sha256"]
    ):
        raise VerificationProfileError("run receipt profile or adapter binding mismatch")
    gate_by_id = {gate["gate_id"]: gate for gate in normalized_profile["gates"]}
    binding_by_id = {item["gate_id"]: item for item in normalized_adapter["bindings"]}
    gate = gate_by_id.get(normalized["gate_id"])
    if gate is None or normalized["gate_kind"] != gate["gate_kind"]:
        raise VerificationProfileError("run receipt gate binding mismatch")
    if normalized["invocation_sha256"] != binding_by_id[gate["gate_id"]]["invocation_sha256"]:
        raise VerificationProfileError("run receipt invocation mismatch")
    if normalized["status"] not in {"passed", "failed"}:
        raise VerificationProfileError("run receipt status is invalid")
    started = _timestamp(normalized["started_at"], "started_at")
    completed = _timestamp(normalized["completed_at"], "completed_at")
    if completed < started:
        raise VerificationProfileError("run receipt time interval is invalid")
    if evaluation_time is not None and completed > evaluation_time:
        raise VerificationProfileError("run receipt is from the future")
    steps = normalized["evidence_steps"]
    if not isinstance(steps, list) or not steps or len(steps) > 256:
        raise VerificationProfileError("evidence_steps are invalid")
    normalized_steps = []
    for step in steps:
        if not isinstance(step, Mapping) or set(step) != _STEP_FIELDS:
            raise VerificationProfileError("evidence step fields are invalid")
        item = copy.deepcopy(dict(step))
        _id(item["kind"], "evidence step kind")
        observed = _timestamp(item["observed_at"], "evidence step observed_at")
        if observed < started or observed > completed:
            raise VerificationProfileError("evidence step is outside the run interval")
        _text(item["artifact_ref"], "artifact_ref")
        if not _SHA256_RE.fullmatch(item["artifact_sha256"] or ""):
            raise VerificationProfileError("artifact_sha256 is invalid")
        if not re.fullmatch(r"artifact://sha256/[0-9a-f]{64}", item["artifact_ref"]):
            raise VerificationProfileError("artifact_ref must be content-addressed")
        if item["artifact_ref"].rsplit("/", 1)[-1] != item["artifact_sha256"]:
            raise VerificationProfileError("artifact_ref and artifact_sha256 mismatch")
        normalized_steps.append(item)
    normalized["evidence_steps"] = sorted(
        normalized_steps, key=lambda item: (item["observed_at"], item["kind"])
    )
    if gate["gate_kind"] == "tdd" and normalized["status"] == "passed":
        kinds = [item["kind"] for item in normalized["evidence_steps"]]
        if "red-test-failed" not in kinds or "green-test-passed" not in kinds:
            raise VerificationProfileError("passed TDD requires red and green evidence")
        if kinds.index("red-test-failed") >= kinds.index("green-test-passed"):
            raise VerificationProfileError("TDD red evidence must precede green evidence")
    measurements = normalized["measurements"]
    if not isinstance(measurements, list) or len(measurements) > 128:
        raise VerificationProfileError("measurements are invalid")
    normalized_measurements = []
    for measurement in measurements:
        if not isinstance(measurement, Mapping) or set(measurement) != _MEASUREMENT_FIELDS:
            raise VerificationProfileError("measurement fields are invalid")
        item = dict(measurement)
        _id(item["metric"], "measurement.metric")
        _uint(item["value"], "measurement.value")
        if item["unit"] not in _THRESHOLD_UNITS:
            raise VerificationProfileError("measurement unit is invalid")
        normalized_measurements.append(item)
    if len({item["metric"] for item in normalized_measurements}) != len(normalized_measurements):
        raise VerificationProfileError("measurement metrics must be unique")
    normalized["measurements"] = sorted(normalized_measurements, key=lambda item: item["metric"])
    gate_requirements = next(
        gate["evidence_requirements"]
        for gate in normalized_profile["gates"]
        if gate["gate_id"] == normalized["gate_id"]
    )
    step_kinds = {item["kind"] for item in normalized["evidence_steps"]}
    if any(
        _EVIDENCE_REQUIREMENTS.get(requirement, requirement) not in step_kinds
        for requirement in gate_requirements
    ):
        raise VerificationProfileError("run receipt is missing declared evidence")
    if normalized["state_write_authority"] is not False or normalized["completion_authority"] is not False:
        raise VerificationProfileError("run receipts have no state or completion authority")
    digest = normalized["receipt_sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise VerificationProfileError("receipt_sha256 is invalid")
    if digest != _digest(normalized, "receipt_sha256"):
        raise VerificationProfileError("run receipt digest mismatch")
    return normalized


def build_verification_run_receipt(
    *,
    run_id: str,
    work_id: str,
    project_revision: int,
    repository_revision: str,
    profile: Mapping[str, Any],
    adapter: Mapping[str, Any],
    gate_id: str,
    status: str,
    started_at: str,
    completed_at: str,
    evidence_steps: Sequence[Mapping[str, Any]],
    measurements: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile)
    normalized_adapter = _normalize_adapter(adapter)
    gate_by_id = {gate["gate_id"]: gate for gate in normalized_profile["gates"]}
    binding_by_id = {item["gate_id"]: item for item in normalized_adapter["bindings"]}
    if gate_id not in gate_by_id or gate_id not in binding_by_id:
        raise VerificationProfileError("run gate is not bound")
    receipt = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "work_id": work_id,
        "project_id": normalized_profile["project_id"],
        "project_revision": project_revision,
        "repository_revision": repository_revision,
        "profile_id": normalized_profile["profile_id"],
        "profile_sha256": normalized_profile["profile_sha256"],
        "adapter_id": normalized_adapter["adapter_id"],
        "adapter_sha256": normalized_adapter["adapter_sha256"],
        "gate_id": gate_id,
        "gate_kind": gate_by_id[gate_id]["gate_kind"],
        "invocation_sha256": binding_by_id[gate_id]["invocation_sha256"],
        "status": status,
        "started_at": started_at,
        "completed_at": completed_at,
        "evidence_steps": [copy.deepcopy(dict(item)) for item in evidence_steps],
        "measurements": [copy.deepcopy(dict(item)) for item in measurements],
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "0" * 64,
    }
    receipt["evidence_steps"] = sorted(
        receipt["evidence_steps"], key=lambda item: (item["observed_at"], item["kind"])
    )
    receipt["measurements"] = sorted(receipt["measurements"], key=lambda item: item["metric"])
    receipt["receipt_sha256"] = _digest(receipt, "receipt_sha256")
    _normalize_run_receipt(receipt, profile=normalized_profile, adapter=normalized_adapter)
    return copy.deepcopy(receipt)


def _current_observation(
    observation: Any,
    *,
    kind: str,
    evaluated_at: datetime,
) -> dict[str, Any]:
    fields = {
        f"{kind}_ref",
        "status",
        "source_kind",
        "observed_at",
        "expires_at",
        "evidence_refs",
    }
    if not isinstance(observation, Mapping) or set(observation) != fields:
        raise VerificationProfileError(f"{kind} observation fields are invalid")
    normalized = copy.deepcopy(dict(observation))
    _id(normalized[f"{kind}_ref"], f"{kind}_ref")
    allowed_status = {"met", "not-met", "unknown"} if kind == "condition" else {
        "available",
        "unavailable",
        "unknown",
    }
    if normalized["status"] not in allowed_status:
        raise VerificationProfileError(f"{kind} status is invalid")
    allowed_sources = (
        {"current-code", "current-state", "current-config"}
        if kind == "condition"
        else {"trusted-host-probe"}
    )
    if normalized["source_kind"] not in allowed_sources:
        raise VerificationProfileError(f"{kind} source is not trusted")
    observed = _timestamp(normalized["observed_at"], "observed_at")
    expires = _timestamp(normalized["expires_at"], "expires_at")
    if observed > evaluated_at or expires <= evaluated_at or expires <= observed:
        normalized["status"] = "unknown"
    _strings(normalized["evidence_refs"], "evidence_refs", allow_empty=False)
    return normalized


def _thresholds_pass(gate: Mapping[str, Any], receipt: Mapping[str, Any]) -> bool:
    measurements = {item["metric"]: item for item in receipt["measurements"]}
    for threshold in gate["thresholds"]:
        measured = measurements.get(threshold["metric"])
        if measured is None or measured["unit"] != threshold["unit"]:
            return False
        value = measured["value"]
        target = threshold["value"]
        if threshold["operator"] == "gte" and value < target:
            return False
        if threshold["operator"] == "lte" and value > target:
            return False
        if threshold["operator"] == "eq" and value != target:
            return False
    return True


def _outcome(
    gate: Mapping[str, Any],
    *,
    status: str,
    reason: str,
    receipt: Mapping[str, Any] | None,
    evidence_refs: Sequence[str],
) -> dict[str, Any]:
    return {
        "gate_id": gate["gate_id"],
        "gate_kind": gate["gate_kind"],
        "mode": gate["mode"],
        "status": status,
        "reason": reason,
        "non_blocking": gate["mode"] == "optional",
        "condition_ref": gate["condition_ref"],
        "capability_refs": list(gate["capability_refs"]),
        "run_receipt_sha256": receipt["receipt_sha256"] if receipt else None,
        "evidence_refs": sorted(set(evidence_refs)),
    }


def evaluate_verification_profile(
    *,
    decision_id: str,
    work_id: str,
    project_revision: int,
    profile: Mapping[str, Any],
    adapter: Mapping[str, Any],
    condition_observations: Sequence[Mapping[str, Any]],
    capability_observations: Sequence[Mapping[str, Any]],
    run_receipts: Sequence[Mapping[str, Any]],
    evaluated_at: str,
) -> dict[str, Any]:
    normalized_profile = _normalize_profile(profile, observed_at=evaluated_at)
    normalized_adapter = _normalize_adapter(adapter)
    if (
        normalized_adapter["project_id"] != normalized_profile["project_id"]
        or normalized_adapter["profile_id"] != normalized_profile["profile_id"]
        or normalized_adapter["profile_sha256"] != normalized_profile["profile_sha256"]
    ):
        raise VerificationProfileError("adapter does not bind the evaluated profile")
    evaluated = _timestamp(evaluated_at, "evaluated_at")
    conditions = [
        _current_observation(item, kind="condition", evaluated_at=evaluated)
        for item in condition_observations
    ]
    capabilities = [
        _current_observation(item, kind="capability", evaluated_at=evaluated)
        for item in capability_observations
    ]
    condition_by_ref = {item["condition_ref"]: item for item in conditions}
    capability_by_ref = {item["capability_ref"]: item for item in capabilities}
    if len(condition_by_ref) != len(conditions) or len(capability_by_ref) != len(capabilities):
        raise VerificationProfileError("verification observations must be unique")
    normalized_receipts = [
        _normalize_run_receipt(
            item,
            profile=normalized_profile,
            adapter=normalized_adapter,
            evaluation_time=evaluated,
        )
        for item in run_receipts
    ]
    receipt_by_gate = {item["gate_id"]: item for item in normalized_receipts}
    if len(receipt_by_gate) != len(normalized_receipts):
        raise VerificationProfileError("run receipts must be unique per gate")
    if any(item["work_id"] != work_id or item["project_revision"] != project_revision for item in normalized_receipts):
        raise VerificationProfileError("run receipt work or project revision mismatch")

    gates = {gate["gate_id"]: gate for gate in normalized_profile["gates"]}
    outcomes: dict[str, dict[str, Any]] = {}
    while len(outcomes) < len(gates):
        progressed = False
        for gate_id in sorted(gates):
            if gate_id in outcomes:
                continue
            gate = gates[gate_id]
            if any(dependency not in outcomes for dependency in gate["depends_on_gate_ids"]):
                continue
            progressed = True
            dependency_evidence = [
                ref
                for dependency in gate["depends_on_gate_ids"]
                for ref in outcomes[dependency]["evidence_refs"]
            ]
            if any(
                outcomes[dependency]["status"] not in {"satisfied", "skipped"}
                for dependency in gate["depends_on_gate_ids"]
            ):
                status = "skipped" if gate["mode"] == "optional" else "blocked"
                outcomes[gate_id] = _outcome(
                    gate,
                    status=status,
                    reason="dependency-unsatisfied",
                    receipt=None,
                    evidence_refs=dependency_evidence,
                )
                continue
            evidence_refs = list(dependency_evidence)
            if gate["mode"] == "conditional":
                condition = condition_by_ref.get(gate["condition_ref"])
                if condition is None or condition["status"] == "unknown":
                    outcomes[gate_id] = _outcome(
                        gate,
                        status="blocked",
                        reason="condition-unknown",
                        receipt=None,
                        evidence_refs=evidence_refs,
                    )
                    continue
                evidence_refs.extend(condition["evidence_refs"])
                if condition["status"] == "not-met":
                    outcomes[gate_id] = _outcome(
                        gate,
                        status="skipped",
                        reason="condition-not-met",
                        receipt=None,
                        evidence_refs=evidence_refs,
                    )
                    continue
            observed_capabilities = [
                capability_by_ref.get(ref) for ref in gate["capability_refs"]
            ]
            for observation in observed_capabilities:
                if observation is not None:
                    evidence_refs.extend(observation["evidence_refs"])
            if any(
                observation is None or observation["status"] != "available"
                for observation in observed_capabilities
            ):
                outcomes[gate_id] = _outcome(
                    gate,
                    status="skipped" if gate["mode"] == "optional" else "blocked",
                    reason="capability-unavailable",
                    receipt=None,
                    evidence_refs=evidence_refs,
                )
                continue
            receipt = receipt_by_gate.get(gate_id)
            if receipt is None:
                outcomes[gate_id] = _outcome(
                    gate,
                    status="skipped" if gate["mode"] == "optional" else "blocked",
                    reason="optional-not-run" if gate["mode"] == "optional" else "missing-run",
                    receipt=None,
                    evidence_refs=evidence_refs,
                )
                continue
            evidence_refs.extend(item["artifact_ref"] for item in receipt["evidence_steps"])
            if receipt["status"] == "failed":
                outcomes[gate_id] = _outcome(
                    gate,
                    status="failed",
                    reason="run-failed",
                    receipt=receipt,
                    evidence_refs=evidence_refs,
                )
                continue
            if not _thresholds_pass(gate, receipt):
                outcomes[gate_id] = _outcome(
                    gate,
                    status="failed",
                    reason="threshold-not-met",
                    receipt=receipt,
                    evidence_refs=evidence_refs,
                )
                continue
            outcomes[gate_id] = _outcome(
                gate,
                status="satisfied",
                reason="gate-satisfied",
                receipt=receipt,
                evidence_refs=evidence_refs,
            )
        if not progressed:
            raise VerificationProfileError("verification profile cannot be evaluated")
    ordered = [outcomes[gate_id] for gate_id in sorted(outcomes)]
    blocking_failed = any(
        item["status"] == "failed" and not item["non_blocking"] for item in ordered
    )
    overall = (
        "failed"
        if blocking_failed
        else "blocked"
        if any(item["status"] == "blocked" for item in ordered)
        else "satisfied"
    )
    decision = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decision_id": decision_id,
        "work_id": work_id,
        "project_id": normalized_profile["project_id"],
        "project_revision": project_revision,
        "profile_id": normalized_profile["profile_id"],
        "profile_sha256": normalized_profile["profile_sha256"],
        "adapter_id": normalized_adapter["adapter_id"],
        "adapter_sha256": normalized_adapter["adapter_sha256"],
        "gate_outcomes": ordered,
        "overall_status": overall,
        "evaluated_at": evaluated_at,
        "state_write_authority": False,
        "completion_authority": False,
        "decision_sha256": "0" * 64,
    }
    decision["decision_sha256"] = _digest(decision, "decision_sha256")
    validate_verification_decision(
        decision,
        profile=normalized_profile,
        adapter=normalized_adapter,
    )
    return copy.deepcopy(decision)


def validate_verification_decision(
    decision: Any,
    *,
    profile: Mapping[str, Any],
    adapter: Mapping[str, Any],
) -> None:
    normalized_profile = _normalize_profile(profile)
    normalized_adapter = _normalize_adapter(adapter)
    if not isinstance(decision, Mapping) or set(decision) != _DECISION_FIELDS:
        raise VerificationProfileError("verification decision fields are invalid")
    if decision["schema_version"] != DECISION_SCHEMA_VERSION:
        raise VerificationProfileError("verification decision version is invalid")
    for field in ("decision_id", "work_id", "project_id", "profile_id", "adapter_id"):
        _id(decision[field], field)
    _uint(decision["project_revision"], "project_revision")
    for field in ("profile_sha256", "adapter_sha256"):
        if not _SHA256_RE.fullmatch(decision[field] or ""):
            raise VerificationProfileError(f"{field} is invalid")
    if (
        decision["project_id"] != normalized_profile["project_id"]
        or decision["profile_id"] != normalized_profile["profile_id"]
        or decision["profile_sha256"] != normalized_profile["profile_sha256"]
        or decision["adapter_id"] != normalized_adapter["adapter_id"]
        or decision["adapter_sha256"] != normalized_adapter["adapter_sha256"]
    ):
        raise VerificationProfileError("verification decision profile binding mismatch")
    _timestamp(decision["evaluated_at"], "evaluated_at")
    outcomes = decision["gate_outcomes"]
    if not isinstance(outcomes, list) or not outcomes:
        raise VerificationProfileError("gate_outcomes are invalid")
    gate_ids: set[str] = set()
    profile_gates = {gate["gate_id"]: gate for gate in normalized_profile["gates"]}
    for outcome in outcomes:
        if not isinstance(outcome, Mapping) or set(outcome) != _OUTCOME_FIELDS:
            raise VerificationProfileError("gate outcome fields are invalid")
        gate_id = _id(outcome["gate_id"], "gate_id")
        if gate_id in gate_ids:
            raise VerificationProfileError("gate outcomes must be unique")
        gate_ids.add(gate_id)
        profile_gate = profile_gates.get(gate_id)
        if profile_gate is None:
            raise VerificationProfileError("verification decision contains an unknown gate")
        if (
            outcome["gate_kind"] != profile_gate["gate_kind"]
            or outcome["mode"] != profile_gate["mode"]
            or outcome["condition_ref"] != profile_gate["condition_ref"]
            or outcome["capability_refs"] != profile_gate["capability_refs"]
        ):
            raise VerificationProfileError("verification decision gate binding mismatch")
        if outcome["gate_kind"] not in _GATE_KINDS or outcome["mode"] not in _MODES:
            raise VerificationProfileError("gate outcome kind or mode is invalid")
        if outcome["status"] not in {"satisfied", "failed", "blocked", "skipped"}:
            raise VerificationProfileError("gate outcome status is invalid")
        if outcome["reason"] not in _OUTCOME_REASONS:
            raise VerificationProfileError("gate outcome reason is invalid")
        if outcome["non_blocking"] is not (outcome["mode"] == "optional"):
            raise VerificationProfileError("gate outcome blocking mode is invalid")
        if outcome["condition_ref"] is not None:
            _id(outcome["condition_ref"], "condition_ref")
        _strings(outcome["capability_refs"], "capability_refs", allow_empty=False, identifiers=True)
        receipt_digest = outcome["run_receipt_sha256"]
        if receipt_digest is not None and not _SHA256_RE.fullmatch(receipt_digest):
            raise VerificationProfileError("run_receipt_sha256 is invalid")
        if outcome["status"] in {"satisfied", "failed"} and receipt_digest is None:
            raise VerificationProfileError("run outcome requires a receipt")
        if outcome["status"] == "skipped" and outcome["reason"] not in {
            "condition-not-met",
            "optional-not-run",
            "capability-unavailable",
            "dependency-unsatisfied",
        }:
            raise VerificationProfileError("skipped outcome reason is invalid")
        _strings(outcome["evidence_refs"], "evidence_refs")
    if gate_ids != set(profile_gates):
        raise VerificationProfileError("verification decision must cover every profile gate")
    blocking_failed = any(
        item["status"] == "failed" and not item["non_blocking"] for item in outcomes
    )
    expected = (
        "failed"
        if blocking_failed
        else "blocked"
        if any(item["status"] == "blocked" for item in outcomes)
        else "satisfied"
    )
    if decision["overall_status"] != expected:
        raise VerificationProfileError("verification overall status is invalid")
    if decision["state_write_authority"] is not False or decision["completion_authority"] is not False:
        raise VerificationProfileError("verification decisions have no state or completion authority")
    digest = decision["decision_sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
        raise VerificationProfileError("decision_sha256 is invalid")
    if digest != _digest(decision, "decision_sha256"):
        raise VerificationProfileError("verification decision digest mismatch")
