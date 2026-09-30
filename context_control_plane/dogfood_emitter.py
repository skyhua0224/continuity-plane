"""Typed project dogfood observations emitted through ``context.*`` traces."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Mapping, Sequence

from .context_trace import LocalContextTraceEmitter

OBSERVATION_SCHEMA_VERSION = "context.dogfood-event/v1alpha1"
COVERAGE_SCHEMA_VERSION = "context.dogfood-coverage/v1alpha1"
_KINDS = {
    "input-routing", "compaction", "skill-selection", "plan-revision",
    "agent-dispatch", "agent-handoff", "delivery",
}
_ROUTES = {None, "continue", "capture-and-continue", "interrupt", "switch", "correction", "status"}
_OBSERVATION_FIELDS = {
    "schema_version", "observation_id", "event_kind", "subject_id", "active_work_before",
    "active_work_after", "return_point_work_id", "route", "interrupted", "candidate_only",
    "acknowledged_input_replayed", "first_action_match", "target_revision", "canary_sequence",
    "first_side_effect_sequence", "provider_metrics_status", "evidence_refs", "trace_event_id",
    "trace_event_name", "trace_event_sha256", "observed_at", "state_write_authority",
    "provider_native_authority", "observation_sha256",
}
_EXPECTATION_FIELDS = {
    "eligible_ingress_ids", "visible_compaction_ids", "skill_selection_ids",
    "target_revisions", "agent_dispatch_ids", "delivery_ids",
}
_COVERAGE_KEYS = {
    "eligible_ingress", "visible_compaction", "skill_selection", "plan_revision",
    "agent_dispatch", "agent_handoff", "delivery",
}
_COVERAGE_FIELDS = {
    "schema_version", "generated_at", "observation_count", "expectations", "denominators",
    "covered", "coverage_millionths", "overall_coverage_millionths", "veto_failures",
    "status", "state_write_authority", "provider_native_authority", "coverage_sha256",
}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class DogfoodObservationError(ValueError):
    """Raised when an observation is malformed or exceeds its authority."""


class DogfoodCoverageError(DogfoodObservationError):
    """Raised when required project observation coverage has regressed."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DogfoodObservationError("dogfood data must be canonical JSON") from exc


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise DogfoodObservationError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise DogfoodObservationError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DogfoodObservationError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise DogfoodObservationError(f"{field} must include timezone")
    return value


def _body(value: Mapping[str, Any], digest_field: str) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != digest_field}


def _digest(value: Mapping[str, Any], digest_field: str) -> str:
    return hashlib.sha256(_canonical(_body(value, digest_field))).hexdigest()


def canonical_dogfood_observation_bytes(observation: Mapping[str, Any]) -> bytes:
    validate_dogfood_observation(observation)
    return _canonical(dict(observation))


def validate_dogfood_observation(observation: Any) -> None:
    if not isinstance(observation, Mapping) or set(observation) != _OBSERVATION_FIELDS:
        raise DogfoodObservationError("dogfood observation fields are invalid")
    if observation["schema_version"] != OBSERVATION_SCHEMA_VERSION:
        raise DogfoodObservationError("dogfood observation version is invalid")
    for field in (
        "observation_id", "subject_id", "active_work_before", "active_work_after",
        "return_point_work_id", "trace_event_id",
    ):
        _safe_id(observation[field], field)
    kind = observation["event_kind"]
    if kind not in _KINDS:
        raise DogfoodObservationError("event_kind is invalid")
    if observation["route"] not in _ROUTES:
        raise DogfoodObservationError("route is invalid")
    for field in (
        "interrupted", "candidate_only", "acknowledged_input_replayed", "first_action_match",
        "state_write_authority", "provider_native_authority",
    ):
        if type(observation[field]) is not bool:
            raise DogfoodObservationError(f"{field} is invalid")
    if observation["state_write_authority"] or observation["provider_native_authority"]:
        raise DogfoodObservationError("dogfood observations have no execution authority")
    revision = observation["target_revision"]
    if type(revision) is not int or revision < 0:
        raise DogfoodObservationError("target_revision is invalid")
    for field in ("canary_sequence", "first_side_effect_sequence"):
        value = observation[field]
        if value is not None and (type(value) is not int or value <= 0):
            raise DogfoodObservationError(f"{field} is invalid")
    if kind == "compaction":
        if observation["canary_sequence"] is None or observation["first_side_effect_sequence"] is None:
            raise DogfoodObservationError("compaction sequence evidence is required")
    elif observation["canary_sequence"] is not None or observation["first_side_effect_sequence"] is not None:
        raise DogfoodObservationError("only compaction observations carry sequence evidence")
    if kind == "input-routing" and observation["route"] is None:
        raise DogfoodObservationError("input routing requires a route")
    if kind != "input-routing" and observation["route"] is not None:
        raise DogfoodObservationError("route is limited to input observations")
    if observation["candidate_only"] and (
        observation["active_work_before"] != observation["active_work_after"]
        or observation["route"] != "capture-and-continue"
    ):
        raise DogfoodObservationError("candidate observations cannot switch active work")
    if observation["provider_metrics_status"] not in {"measured", "unavailable"}:
        raise DogfoodObservationError("provider_metrics_status is invalid")
    evidence_refs = observation["evidence_refs"]
    if not isinstance(evidence_refs, list) or not evidence_refs or len(evidence_refs) > 64:
        raise DogfoodObservationError("evidence_refs are invalid")
    if any(not isinstance(ref, str) or not ref or len(ref) > 512 for ref in evidence_refs):
        raise DogfoodObservationError("evidence_ref is invalid")
    if len(evidence_refs) != len(set(evidence_refs)):
        raise DogfoodObservationError("evidence_refs must be unique")
    expected_name = f"context.dogfood.{kind}"
    if observation["trace_event_name"] != expected_name:
        raise DogfoodObservationError("trace_event_name does not match event kind")
    for field in ("trace_event_sha256", "observation_sha256"):
        if not isinstance(observation[field], str) or _SHA256_RE.fullmatch(observation[field]) is None:
            raise DogfoodObservationError(f"{field} is invalid")
    _timestamp(observation["observed_at"], "observed_at")
    if observation["observation_sha256"] != _digest(observation, "observation_sha256"):
        raise DogfoodObservationError("observation digest mismatch")


class DogfoodObservationEmitter:
    """Emit strict observations through a local append-only context trace."""

    def __init__(self, trace_emitter: LocalContextTraceEmitter) -> None:
        if not isinstance(trace_emitter, LocalContextTraceEmitter):
            raise DogfoodObservationError("trace_emitter is invalid")
        self._trace_emitter = trace_emitter

    def emit(
        self,
        *,
        event_kind: str,
        subject_id: str,
        active_work_before: str,
        active_work_after: str,
        return_point_work_id: str,
        route: str | None,
        interrupted: bool,
        candidate_only: bool,
        acknowledged_input_replayed: bool,
        first_action_match: bool,
        target_revision: int,
        canary_sequence: int | None,
        first_side_effect_sequence: int | None,
        provider_metrics_status: str,
        evidence_refs: Sequence[str],
        observed_at: str,
    ) -> dict[str, Any]:
        observation_id = f"dogfood/{event_kind}/{subject_id}"
        event_name = f"context.dogfood.{event_kind}"
        trace_event = self._trace_emitter.emit(
            event_name,
            evidence_refs=list(evidence_refs),
            observed_at=observed_at,
            attributes={
                "observation_id": observation_id,
                "subject_id": subject_id,
                "active_work_before": active_work_before,
                "active_work_after": active_work_after,
                "return_point_work_id": return_point_work_id,
                "target_revision": target_revision,
            },
            event_id=f"event/{observation_id}/{len(self._trace_emitter.events) + 1}",
        )
        observation = {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "observation_id": observation_id,
            "event_kind": event_kind,
            "subject_id": subject_id,
            "active_work_before": active_work_before,
            "active_work_after": active_work_after,
            "return_point_work_id": return_point_work_id,
            "route": route,
            "interrupted": interrupted,
            "candidate_only": candidate_only,
            "acknowledged_input_replayed": acknowledged_input_replayed,
            "first_action_match": first_action_match,
            "target_revision": target_revision,
            "canary_sequence": canary_sequence,
            "first_side_effect_sequence": first_side_effect_sequence,
            "provider_metrics_status": provider_metrics_status,
            "evidence_refs": list(evidence_refs),
            "trace_event_id": trace_event["event_id"],
            "trace_event_name": trace_event["event_name"],
            "trace_event_sha256": trace_event["event_sha256"],
            "observed_at": observed_at,
            "state_write_authority": False,
            "provider_native_authority": False,
            "observation_sha256": "",
        }
        observation["observation_sha256"] = _digest(observation, "observation_sha256")
        validate_dogfood_observation(observation)
        return copy.deepcopy(observation)


def _normalize_expectations(expectations: Any) -> dict[str, list[Any]]:
    if not isinstance(expectations, Mapping) or set(expectations) != _EXPECTATION_FIELDS:
        raise DogfoodObservationError("coverage expectation fields are invalid")
    normalized: dict[str, list[Any]] = {}
    for field in sorted(_EXPECTATION_FIELDS):
        values = expectations[field]
        if not isinstance(values, list) or len(values) != len(set(values)):
            raise DogfoodObservationError(f"{field} must contain unique values")
        if field == "target_revisions":
            if any(type(value) is not int or value < 0 for value in values):
                raise DogfoodObservationError("target_revisions are invalid")
        else:
            for value in values:
                _safe_id(value, field)
        normalized[field] = sorted(values)
    return normalized


def assess_dogfood_coverage(
    observations: Sequence[Mapping[str, Any]], *, expectations: Mapping[str, Any]
) -> dict[str, Any]:
    if isinstance(observations, (str, bytes, bytearray)) or not isinstance(observations, Sequence):
        raise DogfoodObservationError("observations must be a sequence")
    expected = _normalize_expectations(expectations)
    valid: list[dict[str, Any]] = []
    vetoes: list[str] = []
    for index, observation in enumerate(observations):
        try:
            validate_dogfood_observation(observation)
        except DogfoodObservationError:
            vetoes.append(f"invalid-observation:{index}")
        else:
            valid.append(dict(observation))
    by_kind = {kind: [item for item in valid if item["event_kind"] == kind] for kind in _KINDS}
    sets = {kind: {item["subject_id"] for item in items} for kind, items in by_kind.items()}
    denominators = {
        "eligible_ingress": len(expected["eligible_ingress_ids"]),
        "visible_compaction": len(expected["visible_compaction_ids"]),
        "skill_selection": len(expected["skill_selection_ids"]),
        "plan_revision": len(expected["target_revisions"]),
        "agent_dispatch": len(expected["agent_dispatch_ids"]),
        "agent_handoff": len(expected["agent_dispatch_ids"]),
        "delivery": len(expected["delivery_ids"]),
    }
    covered = {
        "eligible_ingress": len(set(expected["eligible_ingress_ids"]) & sets["input-routing"]),
        "visible_compaction": len(set(expected["visible_compaction_ids"]) & sets["compaction"]),
        "skill_selection": len(set(expected["skill_selection_ids"]) & sets["skill-selection"]),
        "plan_revision": len(set(expected["target_revisions"]) & {item["target_revision"] for item in by_kind["plan-revision"]}),
        "agent_dispatch": len(set(expected["agent_dispatch_ids"]) & sets["agent-dispatch"]),
        "agent_handoff": len(set(expected["agent_dispatch_ids"]) & sets["agent-handoff"]),
        "delivery": len(set(expected["delivery_ids"]) & sets["delivery"]),
    }
    coverage = {
        key: (1_000_000 if denominators[key] == 0 else covered[key] * 1_000_000 // denominators[key])
        for key in sorted(_COVERAGE_KEYS)
    }
    for key in sorted(_COVERAGE_KEYS):
        if coverage[key] != 1_000_000:
            vetoes.append(f"coverage:{key}")
    for item in by_kind["compaction"]:
        if (
            item["canary_sequence"] >= item["first_side_effect_sequence"]
            or not item["first_action_match"]
            or item["acknowledged_input_replayed"]
        ):
            vetoes.append(f"compaction:{item['subject_id']}")
    for item in by_kind["input-routing"]:
        if item["acknowledged_input_replayed"] or not item["return_point_work_id"]:
            vetoes.append(f"input-routing:{item['subject_id']}")
    overall = min(coverage.values()) if coverage else 1_000_000
    generated_at = max((item["observed_at"] for item in valid), default="1970-01-01T00:00:00Z")
    receipt = {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "generated_at": generated_at,
        "observation_count": len(observations),
        "expectations": expected,
        "denominators": denominators,
        "covered": covered,
        "coverage_millionths": coverage,
        "overall_coverage_millionths": overall,
        "veto_failures": sorted(set(vetoes)),
        "status": "pass" if overall == 1_000_000 and not vetoes else "regressed",
        "state_write_authority": False,
        "provider_native_authority": False,
        "coverage_sha256": "",
    }
    receipt["coverage_sha256"] = _digest(receipt, "coverage_sha256")
    validate_dogfood_coverage(receipt)
    return receipt


def validate_dogfood_coverage(receipt: Any) -> None:
    if not isinstance(receipt, Mapping) or set(receipt) != _COVERAGE_FIELDS:
        raise DogfoodObservationError("dogfood coverage fields are invalid")
    if receipt["schema_version"] != COVERAGE_SCHEMA_VERSION:
        raise DogfoodObservationError("dogfood coverage version is invalid")
    _timestamp(receipt["generated_at"], "generated_at")
    if type(receipt["observation_count"]) is not int or receipt["observation_count"] < 0:
        raise DogfoodObservationError("observation_count is invalid")
    _normalize_expectations(receipt["expectations"])
    for field in ("denominators", "covered", "coverage_millionths"):
        value = receipt[field]
        if not isinstance(value, Mapping) or set(value) != _COVERAGE_KEYS:
            raise DogfoodObservationError(f"{field} fields are invalid")
        if any(type(item) is not int or item < 0 for item in value.values()):
            raise DogfoodObservationError(f"{field} values are invalid")
    for key in _COVERAGE_KEYS:
        denominator = receipt["denominators"][key]
        covered = receipt["covered"][key]
        expected = 1_000_000 if denominator == 0 else covered * 1_000_000 // denominator
        if covered > denominator or receipt["coverage_millionths"][key] != expected:
            raise DogfoodObservationError("coverage arithmetic is invalid")
    expected_overall = min(receipt["coverage_millionths"].values())
    if receipt["overall_coverage_millionths"] != expected_overall:
        raise DogfoodObservationError("overall coverage is invalid")
    vetoes = receipt["veto_failures"]
    if not isinstance(vetoes, list) or vetoes != sorted(set(vetoes)):
        raise DogfoodObservationError("veto_failures are invalid")
    expected_status = "pass" if expected_overall == 1_000_000 and not vetoes else "regressed"
    if receipt["status"] != expected_status:
        raise DogfoodObservationError("coverage status is invalid")
    if receipt["state_write_authority"] is not False or receipt["provider_native_authority"] is not False:
        raise DogfoodObservationError("coverage receipts have no authority")
    if not isinstance(receipt["coverage_sha256"], str) or _SHA256_RE.fullmatch(receipt["coverage_sha256"]) is None:
        raise DogfoodObservationError("coverage_sha256 is invalid")
    if receipt["coverage_sha256"] != _digest(receipt, "coverage_sha256"):
        raise DogfoodObservationError("coverage digest mismatch")


def require_dogfood_coverage_gate(receipt: Mapping[str, Any]) -> None:
    validate_dogfood_coverage(receipt)
    if receipt["status"] != "pass":
        raise DogfoodCoverageError("dogfood coverage gate regressed")
