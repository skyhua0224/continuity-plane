"""M3-02 deterministic sticky task routing without state side effects."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from .typed_state import TypedStateError, validate_typed_state


REQUEST_SCHEMA_VERSION = "context.task-route-request/v1alpha1"
DECISION_SCHEMA_VERSION = "context.task-route-decision/v1alpha1"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "project_id",
    "expected_project_revision",
    "active_work_id",
    "expected_active_work_revision",
    "input_ref",
    "input_sha256",
    "input_kind",
    "classifier_confidence_millionths",
    "classifier_provenance_ref",
    "target_work_id",
    "user_authorization_candidate",
    "authorization_candidate_ref",
    "evidence_refs",
}
_DECISION_FIELDS = {
    "schema_version",
    "request_id",
    "request_sha256",
    "project_id",
    "project_revision",
    "input_ref",
    "input_sha256",
    "input_kind",
    "classifier_provenance_ref",
    "route",
    "reason_code",
    "active_work_id_before",
    "active_work_id_after",
    "active_work_revision_before",
    "active_work_revision_after",
    "target_work_id",
    "target_work_revision",
    "authorization_candidate_ref",
    "authorization_verified",
    "checkpoint_required_before_activation",
    "route_proposal_required",
    "review_required",
    "write_protection_required",
    "evidence_refs",
    "state_write_authority",
}
_INPUT_KINDS = {
    "continue",
    "status_query",
    "discussion_request",
    "context_addition",
    "idea",
    "correction",
    "child_work",
    "interrupt",
    "switch",
}
_ROUTES = {
    "continue",
    "capture-candidate-and-continue",
    "propose-correction",
    "propose-child",
    "propose-switch",
}
_EXECUTABLE_KINDS = {"work", "experiment"}
_LOW_CONFIDENCE_THRESHOLD = 500_000


class StickyRouteError(ValueError):
    """Raised when a route request cannot be bound to current authority."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StickyRouteError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _uint(value: Any, field: str, *, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise StickyRouteError(f"{field} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise StickyRouteError(f"{field} exceeds its maximum")
    return value


def _strings(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise StickyRouteError(f"{field} must contain unique non-empty strings")
    return value


def _validate_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise StickyRouteError("route request fields do not match the contract")
    request = copy.deepcopy(request)
    if request["schema_version"] != REQUEST_SCHEMA_VERSION:
        raise StickyRouteError("unsupported route request schema_version")
    for field in (
        "request_id",
        "project_id",
        "active_work_id",
        "input_ref",
        "classifier_provenance_ref",
    ):
        _text(request[field], field)
    _uint(request["expected_project_revision"], "expected_project_revision")
    _uint(request["expected_active_work_revision"], "expected_active_work_revision")
    if not isinstance(request["input_sha256"], str) or not _DIGEST_RE.fullmatch(
        request["input_sha256"]
    ):
        raise StickyRouteError("input_sha256 must be lowercase SHA-256")
    if request["input_kind"] not in _INPUT_KINDS:
        raise StickyRouteError("input_kind is unsupported by M3-02")
    _uint(
        request["classifier_confidence_millionths"],
        "classifier_confidence_millionths",
        maximum=1_000_000,
    )
    _optional_text(request["target_work_id"], "target_work_id")
    if not isinstance(request["user_authorization_candidate"], bool):
        raise StickyRouteError("user_authorization_candidate must be boolean")
    _optional_text(request["authorization_candidate_ref"], "authorization_candidate_ref")
    _strings(request["evidence_refs"], "evidence_refs")
    if request["input_kind"] == "child_work" and request["target_work_id"] is not None:
        raise StickyRouteError("child_work candidate cannot identify an existing target")
    if request["input_kind"] not in {"interrupt", "switch"} and (
        request["user_authorization_candidate"]
        or request["authorization_candidate_ref"] is not None
    ):
        raise StickyRouteError("authorization candidate is only valid for switch inputs")
    if (
        not request["user_authorization_candidate"]
        and request["authorization_candidate_ref"] is not None
    ):
        raise StickyRouteError("authorization candidate ref requires a candidate signal")
    return request


def _validate_decision(decision: Any) -> None:
    if not isinstance(decision, dict) or set(decision) != _DECISION_FIELDS:
        raise StickyRouteError("route decision fields do not match the contract")
    if decision["schema_version"] != DECISION_SCHEMA_VERSION:
        raise StickyRouteError("unsupported route decision schema_version")
    for field in (
        "request_id",
        "project_id",
        "input_ref",
        "input_kind",
        "classifier_provenance_ref",
        "route",
        "reason_code",
        "active_work_id_before",
        "active_work_id_after",
    ):
        _text(decision[field], field)
    for field in ("request_sha256", "input_sha256"):
        if not isinstance(decision[field], str) or not _DIGEST_RE.fullmatch(decision[field]):
            raise StickyRouteError(f"{field} must be lowercase SHA-256")
    if decision["input_kind"] not in _INPUT_KINDS:
        raise StickyRouteError("decision input_kind is unsupported")
    if decision["route"] not in _ROUTES:
        raise StickyRouteError("decision route is unsupported")
    for field in (
        "project_revision",
        "active_work_revision_before",
        "active_work_revision_after",
    ):
        _uint(decision[field], field)
    _optional_text(decision["target_work_id"], "target_work_id")
    if decision["target_work_revision"] is not None:
        _uint(decision["target_work_revision"], "target_work_revision")
    _optional_text(decision["authorization_candidate_ref"], "authorization_candidate_ref")
    for field in (
        "authorization_verified",
        "checkpoint_required_before_activation",
        "route_proposal_required",
        "review_required",
        "write_protection_required",
        "state_write_authority",
    ):
        if not isinstance(decision[field], bool):
            raise StickyRouteError(f"{field} must be boolean")
    _strings(decision["evidence_refs"], "evidence_refs")
    if decision["active_work_id_before"] != decision["active_work_id_after"]:
        raise StickyRouteError("M3-02 route decision cannot change the active leaf")
    if decision["active_work_revision_before"] != decision["active_work_revision_after"]:
        raise StickyRouteError("M3-02 route decision cannot change active Work revision")
    if decision["authorization_verified"]:
        raise StickyRouteError("M3-02 cannot verify authorization")
    if decision["state_write_authority"]:
        raise StickyRouteError("M3-02 cannot claim state write authority")
    target_present = decision["target_work_id"] is not None
    if target_present != (decision["target_work_revision"] is not None):
        raise StickyRouteError("target ID and revision must be present together")
    route = decision["route"]
    input_kind = decision["input_kind"]
    flags = (
        decision["checkpoint_required_before_activation"],
        decision["route_proposal_required"],
        decision["review_required"],
        decision["write_protection_required"],
    )
    if route == "continue":
        if input_kind not in {
            "continue",
            "status_query",
            "discussion_request",
            "context_addition",
            "interrupt",
            "switch",
        } or any(flags):
            raise StickyRouteError("continue decision fields are inconsistent")
    elif route == "capture-candidate-and-continue":
        if input_kind != "idea" or any(flags) or target_present:
            raise StickyRouteError("Idea candidate decision fields are inconsistent")
    elif route == "propose-correction":
        if input_kind != "correction" or flags != (False, True, True, True) or target_present:
            raise StickyRouteError("correction proposal fields are inconsistent")
    elif route == "propose-child":
        if input_kind != "child_work" or flags != (False, True, True, False) or target_present:
            raise StickyRouteError("child proposal fields are inconsistent")
    elif route == "propose-switch":
        if (
            input_kind not in {"interrupt", "switch"}
            or flags != (True, True, True, False)
            or not target_present
            or decision["authorization_candidate_ref"] is None
        ):
            raise StickyRouteError("switch proposal fields are inconsistent")
    if input_kind not in {"interrupt", "switch"} and decision[
        "authorization_candidate_ref"
    ] is not None:
        raise StickyRouteError("authorization candidate is invalid for this input kind")
    if input_kind not in {"interrupt", "switch"} and target_present:
        raise StickyRouteError("target is invalid for this input kind")


def canonical_route_request_bytes(request: dict[str, Any]) -> bytes:
    """Return normalized request bytes for replay and provenance binding."""
    canonical = _validate_request(request)
    canonical["evidence_refs"] = sorted(canonical["evidence_refs"])
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _target_rejection_reason(
    target: dict[str, Any], work_by_id: dict[str, dict[str, Any]], state: dict[str, Any]
) -> str | None:
    if target["kind"] not in _EXECUTABLE_KINDS or target["status"] != "ready":
        return "target-not-ready"
    if any(work_by_id[dependency_id]["status"] != "completed" for dependency_id in target["dependency_ids"]):
        return "target-dependencies-incomplete"
    blocker_by_id = {item["blocker_id"]: item for item in state["blockers"]}
    if any(
        blocker["status"] == "open"
        and (
            blocker["blocker_id"] in target["blocker_ids"]
            or target["work_id"] in blocker["blocked_work_ids"]
        )
        for blocker in blocker_by_id.values()
    ):
        return "target-blocked"
    return None


def route_task_input(request: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    """Return a replayable sticky route decision without mutating authority."""
    request = _validate_request(request)
    try:
        validate_typed_state(state)
    except (TypedStateError, TypeError) as exc:
        raise StickyRouteError("typed state is invalid") from exc

    project = state["project"]
    if request["project_id"] != project["project_id"]:
        raise StickyRouteError("route request project_id does not match state")
    if request["expected_project_revision"] != project["revision"]:
        raise StickyRouteError("route request revision does not match state")
    if request["active_work_id"] != project["primary_work_id"]:
        raise StickyRouteError("route request active leaf does not match state")

    work_by_id = {work["work_id"]: work for work in state["works"]}
    active_work = work_by_id.get(request["active_work_id"])
    if (
        active_work is None
        or active_work["kind"] not in _EXECUTABLE_KINDS
        or active_work["status"] != "active"
        or request["active_work_id"] not in project["active_work_ids"]
    ):
        raise StickyRouteError("current active work is not an executable active leaf")
    if request["expected_active_work_revision"] != active_work["revision"]:
        raise StickyRouteError("route request active Work revision does not match state")

    input_kind = request["input_kind"]
    target_work_id = request["target_work_id"]
    target = work_by_id.get(target_work_id) if target_work_id is not None else None

    route = "continue"
    reason_code = "sticky-active-leaf"
    checkpoint_required = False
    proposal_required = False
    review_required = False
    write_protection_required = False
    confidence = request["classifier_confidence_millionths"]

    if input_kind == "idea":
        route = "capture-candidate-and-continue"
        reason_code = (
            "low-confidence-idea-candidate"
            if confidence < _LOW_CONFIDENCE_THRESHOLD
            else "idea-candidate"
        )
    elif input_kind == "correction":
        route = "propose-correction"
        reason_code = (
            "low-confidence-correction-review-required"
            if confidence < _LOW_CONFIDENCE_THRESHOLD
            else "correction-review-required"
        )
        proposal_required = True
        review_required = True
        write_protection_required = True
    elif input_kind == "child_work":
        route = "propose-child"
        reason_code = "child-candidate"
        proposal_required = True
        review_required = True
    elif input_kind in {"interrupt", "switch"}:
        if target is None:
            reason_code = "target-unknown" if target_work_id is not None else "target-required"
        else:
            reason_code = _target_rejection_reason(target, work_by_id, state) or ""
        if not reason_code:
            if not request["user_authorization_candidate"]:
                reason_code = "authorization-candidate-required"
            elif request["authorization_candidate_ref"] is None:
                reason_code = "authorization-candidate-ref-required"
            else:
                route = "propose-switch"
                reason_code = (
                    "low-confidence-switch-candidate-requires-review"
                    if confidence < _LOW_CONFIDENCE_THRESHOLD
                    else "authorization-candidate-requires-m3-03-verification"
                )
                checkpoint_required = True
                proposal_required = True
                review_required = True
    elif confidence < _LOW_CONFIDENCE_THRESHOLD:
        reason_code = "low-confidence-sticky-default"

    request_sha256 = hashlib.sha256(canonical_route_request_bytes(request)).hexdigest()
    decision_target_id = (
        target_work_id
        if input_kind in {"interrupt", "switch"} and target is not None
        else None
    )
    decision = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "project_id": project["project_id"],
        "project_revision": project["revision"],
        "input_ref": request["input_ref"],
        "input_sha256": request["input_sha256"],
        "input_kind": input_kind,
        "classifier_provenance_ref": request["classifier_provenance_ref"],
        "route": route,
        "reason_code": reason_code,
        "active_work_id_before": active_work["work_id"],
        "active_work_id_after": active_work["work_id"],
        "active_work_revision_before": active_work["revision"],
        "active_work_revision_after": active_work["revision"],
        "target_work_id": decision_target_id,
        "target_work_revision": (
            target["revision"]
            if target is not None and input_kind in {"interrupt", "switch"}
            else None
        ),
        "authorization_candidate_ref": (
            request["authorization_candidate_ref"]
            if input_kind in {"interrupt", "switch"}
            else None
        ),
        "authorization_verified": False,
        "checkpoint_required_before_activation": checkpoint_required,
        "route_proposal_required": proposal_required,
        "review_required": review_required,
        "write_protection_required": write_protection_required,
        "evidence_refs": sorted(copy.deepcopy(request["evidence_refs"])),
        "state_write_authority": False,
    }
    _validate_decision(decision)
    return decision


def canonical_route_decision_bytes(decision: dict[str, Any]) -> bytes:
    """Return deterministic bytes for M3-02 replay and evidence binding."""
    _validate_decision(decision)
    canonical = copy.deepcopy(decision)
    canonical["evidence_refs"] = sorted(canonical["evidence_refs"])
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
