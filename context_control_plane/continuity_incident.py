"""Fail-closed continuity incident detection for project dogfooding."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .context_trace import LocalContextTraceEmitter
from .input_progression import (
    InputProgressionError,
    canonical_progression_decision_bytes,
)

SCHEMA_VERSION = "context.continuity-incident/v1alpha1"
OBLIGATION_SCHEMA_VERSION = "context.continuation-obligation/v1alpha1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {
    "schema_version",
    "incident_id",
    "incident_kind",
    "measurement_scope",
    "status",
    "project_id",
    "campaign_id",
    "completed_work_id",
    "active_work_id",
    "project_revision",
    "run_ref",
    "continuation_basis_kind",
    "continuation_basis_ref",
    "continuation_basis_sha256",
    "expected_action",
    "actual_action",
    "termination_kind",
    "termination_evidence_sha256",
    "remaining_ready_work_ids",
    "typed_blocker_id",
    "terminated_at",
    "termination_time_status",
    "detected_at",
    "detection_source",
    "evidence_refs",
    "veto_failures",
    "trace_event_id",
    "trace_event_name",
    "trace_event_sha256",
    "trace_authority",
    "state_write_authority",
    "completion_authority",
    "provider_native_authority",
    "incident_sha256",
}
_OBLIGATION_FIELDS = {
    "schema_version",
    "obligation_id",
    "project_id",
    "campaign_id",
    "active_work_id",
    "project_revision",
    "governance_revision",
    "expected_action",
    "ready_work_ids",
    "typed_blocker_id",
    "observed_at",
    "evidence_refs",
    "state_write_authority",
    "completion_authority",
    "obligation_sha256",
}


class ContinuityIncidentError(ValueError):
    """Raised when continuity evidence is malformed or internally inconsistent."""


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
        raise ContinuityIncidentError("continuity evidence must be canonical JSON") from exc


def _digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "incident_sha256"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _obligation_digest(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "obligation_sha256"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ContinuityIncidentError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise ContinuityIncidentError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContinuityIncidentError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ContinuityIncidentError(f"{field} must include timezone")
    return parsed


def _unique_strings(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise ContinuityIncidentError(f"{field} is invalid")
    if len(value) > 64 or any(
        not isinstance(item, str) or not item.strip() or len(item.encode("utf-8")) > 512
        for item in value
    ):
        raise ContinuityIncidentError(f"{field} is invalid")
    if len(value) != len(set(value)):
        raise ContinuityIncidentError(f"{field} must be unique")
    return value


def validate_continuation_obligation(obligation: Any) -> None:
    """Validate a reconstructed governance basis without treating it as runtime state."""
    if not isinstance(obligation, Mapping) or set(obligation) != _OBLIGATION_FIELDS:
        raise ContinuityIncidentError("continuation obligation fields are invalid")
    if obligation["schema_version"] != OBLIGATION_SCHEMA_VERSION:
        raise ContinuityIncidentError("continuation obligation version is invalid")
    for field in ("obligation_id", "project_id", "campaign_id", "active_work_id"):
        _non_empty(obligation[field], field)
    for field in ("project_revision", "governance_revision"):
        value = obligation[field]
        if type(value) is not int or value < 0:
            raise ContinuityIncidentError(f"{field} is invalid")
    if obligation["expected_action"] != "continue":
        raise ContinuityIncidentError("continuation obligation action is invalid")
    ready = _unique_strings(obligation["ready_work_ids"], "ready_work_ids")
    if obligation["active_work_id"] not in ready:
        raise ContinuityIncidentError("continuation obligation active work is not ready")
    if obligation["typed_blocker_id"] is not None:
        raise ContinuityIncidentError("continuation obligation cannot carry a blocker")
    _timestamp(obligation["observed_at"], "observed_at")
    _unique_strings(obligation["evidence_refs"], "evidence_refs")
    if obligation["state_write_authority"] is not False or obligation["completion_authority"] is not False:
        raise ContinuityIncidentError("continuation obligation has no authority")
    digest = obligation["obligation_sha256"]
    if (
        not isinstance(digest, str)
        or not _SHA256_RE.fullmatch(digest)
        or digest != _obligation_digest(obligation)
    ):
        raise ContinuityIncidentError("continuation obligation digest mismatch")


def build_continuation_obligation(
    *,
    obligation_id: str,
    project_id: str,
    campaign_id: str,
    active_work_id: str,
    project_revision: int,
    governance_revision: int,
    expected_action: str,
    ready_work_ids: Sequence[str],
    typed_blocker_id: str | None,
    observed_at: str,
    evidence_refs: Sequence[str],
) -> dict[str, Any]:
    """Build a hash-bound retrospective continuation obligation."""
    obligation = {
        "schema_version": OBLIGATION_SCHEMA_VERSION,
        "obligation_id": obligation_id,
        "project_id": project_id,
        "campaign_id": campaign_id,
        "active_work_id": active_work_id,
        "project_revision": project_revision,
        "governance_revision": governance_revision,
        "expected_action": expected_action,
        "ready_work_ids": sorted(ready_work_ids),
        "typed_blocker_id": typed_blocker_id,
        "observed_at": observed_at,
        "evidence_refs": list(evidence_refs),
        "state_write_authority": False,
        "completion_authority": False,
        "obligation_sha256": "",
    }
    obligation["obligation_sha256"] = _obligation_digest(obligation)
    validate_continuation_obligation(obligation)
    return copy.deepcopy(obligation)


def validate_continuity_incident(incident: Any) -> None:
    """Validate a derived incident receipt without granting state authority."""
    if not isinstance(incident, Mapping) or set(incident) != _FIELDS:
        raise ContinuityIncidentError("continuity incident fields are invalid")
    if incident["schema_version"] != SCHEMA_VERSION:
        raise ContinuityIncidentError("continuity incident version is invalid")
    for field in (
        "incident_id",
        "project_id",
        "campaign_id",
        "completed_work_id",
        "active_work_id",
        "run_ref",
        "continuation_basis_ref",
    ):
        _non_empty(incident[field], field)
    if incident["incident_kind"] != "premature-stop":
        raise ContinuityIncidentError("incident_kind is invalid")
    if incident["measurement_scope"] != "live-observation" or incident["status"] != "regressed":
        raise ContinuityIncidentError("live incident status is invalid")
    revision = incident["project_revision"]
    if type(revision) is not int or revision < 0:
        raise ContinuityIncidentError("project_revision is invalid")
    if incident["continuation_basis_kind"] not in {"m3-decision", "governance-obligation"}:
        raise ContinuityIncidentError("continuation_basis_kind is invalid")
    if not isinstance(incident["continuation_basis_sha256"], str) or not _SHA256_RE.fullmatch(
        incident["continuation_basis_sha256"]
    ):
        raise ContinuityIncidentError("continuation_basis_sha256 is invalid")
    if incident["expected_action"] != "continue" or incident["actual_action"] != "stop":
        raise ContinuityIncidentError("premature-stop action pair is invalid")
    if incident["termination_kind"] not in {"assistant-final", "worker-exit", "process-exit"}:
        raise ContinuityIncidentError("termination_kind is invalid")
    termination_digest = incident["termination_evidence_sha256"]
    if not isinstance(termination_digest, str) or not _SHA256_RE.fullmatch(termination_digest):
        raise ContinuityIncidentError("termination_evidence_sha256 is invalid")
    ready = _unique_strings(incident["remaining_ready_work_ids"], "remaining_ready_work_ids")
    if incident["active_work_id"] not in ready:
        raise ContinuityIncidentError("active work must remain ready")
    if incident["typed_blocker_id"] is not None:
        raise ContinuityIncidentError("premature-stop cannot carry a typed blocker")
    time_status = incident["termination_time_status"]
    if time_status not in {"measured", "unavailable"}:
        raise ContinuityIncidentError("termination_time_status is invalid")
    if time_status == "measured":
        terminated = _timestamp(incident["terminated_at"], "terminated_at")
    elif incident["terminated_at"] is not None:
        raise ContinuityIncidentError("unavailable termination time must be null")
    else:
        terminated = None
    detected = _timestamp(incident["detected_at"], "detected_at")
    if terminated is not None and detected < terminated:
        raise ContinuityIncidentError("incident detection precedes termination")
    if incident["detection_source"] not in {"runtime-detector", "replay-audit", "user-correction"}:
        raise ContinuityIncidentError("detection_source is invalid")
    _unique_strings(incident["evidence_refs"], "evidence_refs")
    if incident["veto_failures"] != ["premature-stop"]:
        raise ContinuityIncidentError("premature-stop must remain a veto failure")
    _non_empty(incident["trace_event_id"], "trace_event_id")
    if incident["trace_event_name"] != "context.dogfood.continuity-incident":
        raise ContinuityIncidentError("trace_event_name is invalid")
    trace_digest = incident["trace_event_sha256"]
    if not isinstance(trace_digest, str) or not _SHA256_RE.fullmatch(trace_digest):
        raise ContinuityIncidentError("trace_event_sha256 is invalid")
    if any(
        incident[field] is not False
        for field in (
            "trace_authority",
            "state_write_authority",
            "completion_authority",
            "provider_native_authority",
        )
    ):
        raise ContinuityIncidentError("continuity incidents have no state or completion authority")
    digest = incident["incident_sha256"]
    if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest) or digest != _digest(incident):
        raise ContinuityIncidentError("continuity incident digest mismatch")


def detect_continuity_incident(
    *,
    termination: Mapping[str, Any],
    continuation_basis: Mapping[str, Any],
    trace_emitter: LocalContextTraceEmitter,
    detected_at: str,
    detection_source: str,
    evidence_refs: Sequence[str],
) -> dict[str, Any] | None:
    """Derive a premature-stop receipt from termination and continuation evidence."""
    required_termination = {
        "run_ref",
        "project_id",
        "campaign_id",
        "completed_work_id",
        "active_work_id",
        "project_revision",
        "termination_kind",
        "ready_work_ids",
        "typed_blocker_id",
        "terminated_at",
        "termination_time_status",
    }
    if not isinstance(termination, Mapping) or set(termination) != required_termination:
        raise ContinuityIncidentError("termination evidence fields are invalid")
    if not isinstance(continuation_basis, dict):
        raise ContinuityIncidentError("continuation basis is invalid")
    if not isinstance(trace_emitter, LocalContextTraceEmitter):
        raise ContinuityIncidentError("trace_emitter is invalid")
    _non_empty(termination["project_id"], "termination.project_id")
    _non_empty(termination["campaign_id"], "termination.campaign_id")
    _non_empty(termination["completed_work_id"], "termination.completed_work_id")
    if termination["typed_blocker_id"] is not None:
        _non_empty(termination["typed_blocker_id"], "termination.typed_blocker_id")
        return None
    revision = termination["project_revision"]
    active_work = termination["active_work_id"]
    if continuation_basis.get("schema_version") == OBLIGATION_SCHEMA_VERSION:
        validate_continuation_obligation(continuation_basis)
        basis_kind = "governance-obligation"
        basis_ref = continuation_basis["obligation_id"]
        basis_digest = continuation_basis["obligation_sha256"]
        basis_project = continuation_basis["project_id"]
        basis_revision = continuation_basis["project_revision"]
        basis_active_work = continuation_basis["active_work_id"]
        basis_action = continuation_basis["expected_action"]
        basis_campaign = continuation_basis["campaign_id"]
        basis_time = _timestamp(continuation_basis["observed_at"], "continuation.observed_at")
    else:
        try:
            decision_bytes = canonical_progression_decision_bytes(continuation_basis)
        except InputProgressionError as exc:
            raise ContinuityIncidentError("continuation decision is invalid") from exc
        basis_kind = "m3-decision"
        basis_ref = continuation_basis["request_id"]
        basis_digest = hashlib.sha256(decision_bytes).hexdigest()
        basis_project = continuation_basis["project_id"]
        basis_revision = continuation_basis["project_revision"]
        basis_active_work = continuation_basis["active_work_id_after"]
        basis_action = continuation_basis["action"]
        basis_campaign = termination["campaign_id"]
        basis_time = _timestamp(continuation_basis["observed_at"], "continuation.observed_at")
    if basis_revision != revision:
        raise ContinuityIncidentError("continuation basis revision mismatch")
    if basis_project != termination["project_id"]:
        raise ContinuityIncidentError("continuation basis project mismatch")
    if basis_campaign != termination["campaign_id"]:
        raise ContinuityIncidentError("continuation basis campaign mismatch")
    if basis_time > _timestamp(detected_at, "detected_at"):
        raise ContinuityIncidentError("continuation basis is from the future")
    if basis_active_work != active_work:
        raise ContinuityIncidentError("continuation active work mismatch")
    if termination["typed_blocker_id"] is not None:
        return None
    ready = _unique_strings(termination["ready_work_ids"], "ready_work_ids", allow_empty=True)
    action = basis_action
    if action == "stop-complete" and active_work is None and not ready:
        return None
    if (
        action not in {"continue", "continue-active", "capture-and-continue", "select-next-ready"}
        or termination["termination_kind"] not in {"assistant-final", "worker-exit", "process-exit"}
        or not isinstance(active_work, str)
        or active_work not in ready
    ):
        raise ContinuityIncidentError("termination cannot be classified safely")
    run_ref = _non_empty(termination["run_ref"], "run_ref")
    termination_digest = hashlib.sha256(_canonical(dict(termination))).hexdigest()
    incident_boundary = hashlib.sha256(
        _canonical(
            {
                "run_ref": run_ref,
                "termination_evidence_sha256": termination_digest,
                "continuation_basis_sha256": basis_digest,
            }
        )
    ).hexdigest()[:24]
    bound_evidence_refs = list(dict.fromkeys([*evidence_refs, f"sha256:{termination_digest}"]))
    trace_event = trace_emitter.emit(
        "context.dogfood.continuity-incident",
        evidence_refs=bound_evidence_refs,
        observed_at=detected_at,
        attributes={
            "incident_kind": "premature-stop",
            "termination_evidence_sha256": termination_digest,
            "continuation_basis_sha256": basis_digest,
            "status": "regressed",
        },
        event_id=f"event/continuity/{incident_boundary}",
    )
    if (
        trace_event["project_id"] != termination["project_id"]
        or trace_event["state_revision"] != revision
        or trace_event["active_work_id"] != active_work
    ):
        raise ContinuityIncidentError("trace binding does not match termination evidence")
    incident = {
        "schema_version": SCHEMA_VERSION,
        "incident_id": f"incident/premature-stop/{incident_boundary}",
        "incident_kind": "premature-stop",
        "measurement_scope": "live-observation",
        "status": "regressed",
        "project_id": termination["project_id"],
        "campaign_id": termination["campaign_id"],
        "completed_work_id": termination["completed_work_id"],
        "active_work_id": active_work,
        "project_revision": revision,
        "run_ref": run_ref,
        "continuation_basis_kind": basis_kind,
        "continuation_basis_ref": basis_ref,
        "continuation_basis_sha256": basis_digest,
        "expected_action": "continue",
        "actual_action": "stop",
        "termination_kind": termination["termination_kind"],
        "termination_evidence_sha256": termination_digest,
        "remaining_ready_work_ids": sorted(ready),
        "typed_blocker_id": None,
        "terminated_at": termination["terminated_at"],
        "termination_time_status": termination["termination_time_status"],
        "detected_at": detected_at,
        "detection_source": detection_source,
        "evidence_refs": bound_evidence_refs,
        "veto_failures": ["premature-stop"],
        "trace_event_id": trace_event["event_id"],
        "trace_event_name": trace_event["event_name"],
        "trace_event_sha256": trace_event["event_sha256"],
        "trace_authority": trace_event["authority"],
        "state_write_authority": False,
        "completion_authority": False,
        "provider_native_authority": False,
        "incident_sha256": "",
    }
    incident["incident_sha256"] = _digest(incident)
    validate_continuity_incident(incident)
    return copy.deepcopy(incident)


def admit_continuity_incident(
    existing: Sequence[Mapping[str, Any]], candidate: Mapping[str, Any]
) -> dict[str, Any]:
    """Admit one idempotent incident or quarantine an ID conflict."""
    validate_continuity_incident(candidate)
    if isinstance(existing, (str, bytes, bytearray)) or not isinstance(existing, Sequence):
        raise ContinuityIncidentError("existing incidents must be a sequence")
    for item in existing:
        validate_continuity_incident(item)
        if item["incident_id"] != candidate["incident_id"]:
            continue
        if item["incident_sha256"] != candidate["incident_sha256"]:
            raise ContinuityIncidentError("continuity incident ID conflict")
        return copy.deepcopy(dict(item))
    return copy.deepcopy(dict(candidate))


def build_continuity_record(document: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Rebuild a live incident receipt and its trace from admitted source metadata."""
    fields = {
        "schema_version",
        "termination",
        "continuation_obligation",
        "trace_binding",
        "trace_source",
        "detected_at",
        "detection_source",
        "evidence_refs",
    }
    if not isinstance(document, Mapping) or set(document) != fields:
        raise ContinuityIncidentError("continuity record input fields are invalid")
    if document["schema_version"] != "context.continuity-incident-input/v1alpha1":
        raise ContinuityIncidentError("continuity record input version is invalid")
    obligation = document["continuation_obligation"]
    validate_continuation_obligation(obligation)
    emitter = LocalContextTraceEmitter(
        binding=document["trace_binding"],
        source=document["trace_source"],
    )
    incident = detect_continuity_incident(
        termination=document["termination"],
        continuation_basis=obligation,
        trace_emitter=emitter,
        detected_at=document["detected_at"],
        detection_source=document["detection_source"],
        evidence_refs=document["evidence_refs"],
    )
    if incident is None:
        raise ContinuityIncidentError("continuity record input does not describe an incident")
    return incident, [copy.deepcopy(event) for event in emitter.events]
