"""M3-08 deterministic input progression and bounded escalation."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from .project_governance_profile import (
    ProjectGovernanceProfileError,
    validate_project_governance_profile,
)
from .sticky_router import StickyRouteError, canonical_route_decision_bytes
from .typed_state import TypedStateError, validate_typed_state

REQUEST_SCHEMA_VERSION = "context.continuation-dispatch-request/v1alpha1"
DECISION_SCHEMA_VERSION = "context.continuation-dispatch-decision/v1alpha1"
BLOCKING_DECISION_SCHEMA_VERSION = "context.blocking-decision/v1alpha1"

_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "project_id",
    "expected_project_revision",
    "governance_profile_id",
    "expected_governance_revision",
    "intent_kind",
    "route_decision_sha256",
    "blocking_decision_sha256",
    "observed_at",
}
_INTENT_KINDS = {"routed_input", "blocking_decision", "dispatch_tick"}
_BLOCKING_DECISION_FIELDS = {
    "schema_version",
    "blocking_decision_id",
    "project_id",
    "project_revision",
    "blocker_id",
    "blocker_kind",
    "reason",
    "affected_work_ids",
    "evidence_ids",
    "affected_scope_refs",
    "decision_options",
    "default_option_id",
    "resume_condition",
    "resolution_actor",
    "safe_reversible_default_available",
    "state_write_authority",
}
_BLOCKER_KINDS = {
    "irreversible-effect-authorization-required",
    "approved-constraint-conflict",
    "acceptance-output-choice-required",
    "external-completion-evidence-unavailable",
}
_ASK_BLOCKER_KINDS = _BLOCKER_KINDS - {
    "external-completion-evidence-unavailable"
}
_SCOPE_FIELDS = {"scope_kind", "scope_ref"}
_SCOPE_KINDS = {"repo", "directory", "file", "symbol", "capability", "effect"}
_OPTION_FIELDS = {"option_id", "summary"}
_RESUME_FIELDS = {"kind", "refs"}
_RESUME_KINDS = {
    "authorization",
    "decision",
    "evidence",
    "blocker_resolved",
    "new_ready_work",
}
_DECISION_FIELDS = {
    "schema_version",
    "request_id",
    "request_sha256",
    "project_id",
    "project_revision",
    "governance_revision",
    "observed_at",
    "intent_kind",
    "input_ref",
    "input_sha256",
    "input_kind",
    "classifier_provenance_ref",
    "action",
    "reason_code",
    "active_work_id_before",
    "active_work_id_after",
    "active_work_revision_before",
    "active_work_revision_after",
    "selected_work_id",
    "selected_work_revision",
    "blocker_id",
    "blocker_kind",
    "reason",
    "affected_work_ids",
    "evidence_ids",
    "governance_evidence_refs",
    "decision_options",
    "default_behavior",
    "affected_scope_refs",
    "resume_condition",
    "pending_route_decision_sha256",
    "state_write_authority",
}
_ACTIONS = {
    "continue-active",
    "capture-and-continue",
    "select-next-ready",
    "ask-user",
    "stop-blocked",
    "stop-complete",
}
_ROUTED_INPUT_KINDS = {
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
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class InputProgressionError(ValueError):
    """Raised when an M3-08 decision cannot bind to current authority."""


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputProgressionError(f"{field} must be a non-empty string")
    return value


def _uint(value: Any, field: str, *, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise InputProgressionError(f"{field} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise InputProgressionError(f"{field} exceeds its maximum")
    return value


def _strings(value: Any, field: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise InputProgressionError(
            f"{field} must contain unique non-empty strings"
        )
    return value


def _optional_digest(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise InputProgressionError(f"{field} must be lowercase SHA-256 or null")
    return value


def _optional_text(value: Any, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _optional_uint(value: Any, field: str) -> int | None:
    if value is None:
        return None
    return _uint(value, field)


def _validate_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise InputProgressionError("progression request fields do not match the contract")
    request = copy.deepcopy(request)
    if request["schema_version"] != REQUEST_SCHEMA_VERSION:
        raise InputProgressionError("unsupported progression request schema_version")
    for field in (
        "request_id",
        "project_id",
        "governance_profile_id",
        "observed_at",
    ):
        _text(request[field], field)
    for field in ("expected_project_revision", "expected_governance_revision"):
        _uint(request[field], field)
    if request["intent_kind"] not in _INTENT_KINDS:
        raise InputProgressionError("intent_kind is unsupported by M3-08")
    route_digest = _optional_digest(
        request["route_decision_sha256"], "route_decision_sha256"
    )
    blocker_digest = _optional_digest(
        request["blocking_decision_sha256"], "blocking_decision_sha256"
    )
    intent_kind = request["intent_kind"]
    if (intent_kind == "routed_input") != (route_digest is not None):
        raise InputProgressionError("routed_input requires only route_decision_sha256")
    if (intent_kind == "blocking_decision") != (blocker_digest is not None):
        raise InputProgressionError(
            "blocking_decision intent requires only blocking_decision_sha256"
        )
    if intent_kind == "dispatch_tick" and (
        route_digest is not None or blocker_digest is not None
    ):
        raise InputProgressionError("dispatch_tick cannot bind an input decision")
    if route_digest is not None and blocker_digest is not None:
        raise InputProgressionError("request cannot bind route and blocker decisions")
    return request


def canonical_progression_request_bytes(request: dict[str, Any]) -> bytes:
    """Return deterministic request bytes for replay and provenance binding."""
    canonical = _validate_request(request)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_blocking_decision(decision: Any) -> dict[str, Any]:
    if not isinstance(decision, dict) or set(decision) != _BLOCKING_DECISION_FIELDS:
        raise InputProgressionError(
            "blocking decision fields do not match the contract"
        )
    decision = copy.deepcopy(decision)
    if decision["schema_version"] != BLOCKING_DECISION_SCHEMA_VERSION:
        raise InputProgressionError("unsupported blocking decision schema_version")
    for field in (
        "blocking_decision_id",
        "project_id",
        "blocker_id",
        "reason",
    ):
        _text(decision[field], field)
    _uint(decision["project_revision"], "project_revision")
    if decision["blocker_kind"] not in _BLOCKER_KINDS:
        raise InputProgressionError("blocker_kind is unsupported")
    affected_work_ids = _strings(
        decision["affected_work_ids"], "affected_work_ids"
    )
    evidence_ids = _strings(decision["evidence_ids"], "evidence_ids")
    if not affected_work_ids or not evidence_ids:
        raise InputProgressionError(
            "blocking decision requires affected Work and evidence"
        )
    scopes = decision["affected_scope_refs"]
    if not isinstance(scopes, list) or not scopes:
        raise InputProgressionError("affected_scope_refs must be non-empty")
    scope_keys: list[tuple[str, str]] = []
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
            raise InputProgressionError("affected scope fields are invalid")
        if scope["scope_kind"] not in _SCOPE_KINDS:
            raise InputProgressionError("affected scope kind is unsupported")
        scope_keys.append((scope["scope_kind"], _text(scope["scope_ref"], "scope_ref")))
    if len(scope_keys) != len(set(scope_keys)):
        raise InputProgressionError("affected scopes must be unique")

    options = decision["decision_options"]
    if not isinstance(options, list):
        raise InputProgressionError("decision_options must be an object list")
    option_ids: list[str] = []
    for option in options:
        if not isinstance(option, dict) or set(option) != _OPTION_FIELDS:
            raise InputProgressionError("decision option fields are invalid")
        option_ids.append(_text(option["option_id"], "option_id"))
        _text(option["summary"], "option.summary")
    if len(option_ids) != len(set(option_ids)):
        raise InputProgressionError("decision option IDs must be unique")

    default_option_id = decision["default_option_id"]
    if default_option_id is not None:
        _text(default_option_id, "default_option_id")
    resume = decision["resume_condition"]
    if not isinstance(resume, dict) or set(resume) != _RESUME_FIELDS:
        raise InputProgressionError("resume condition fields are invalid")
    if resume["kind"] not in _RESUME_KINDS:
        raise InputProgressionError("resume condition kind is unsupported")
    if not _strings(resume["refs"], "resume_condition.refs"):
        raise InputProgressionError("resume condition refs must be non-empty")
    if decision["resolution_actor"] not in {"user", "external_system"}:
        raise InputProgressionError("resolution_actor is unsupported")
    if not isinstance(decision["safe_reversible_default_available"], bool):
        raise InputProgressionError(
            "safe_reversible_default_available must be boolean"
        )
    if decision["state_write_authority"] is not False:
        raise InputProgressionError("blocking decision cannot claim write authority")

    if decision["blocker_kind"] in _ASK_BLOCKER_KINDS:
        if (
            decision["resolution_actor"] != "user"
            or len(options) < 2
            or default_option_id not in option_ids
        ):
            raise InputProgressionError(
                "user-resolvable blocker requires options and a default"
            )
    elif (
        decision["resolution_actor"] != "external_system"
        or options
        or default_option_id is not None
    ):
        raise InputProgressionError(
            "external blocker cannot contain user decision options"
        )
    return decision


def canonical_blocking_decision_bytes(decision: dict[str, Any]) -> bytes:
    """Return deterministic typed blocker bytes for request binding."""
    canonical = _validate_blocking_decision(decision)
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_progression_decision(decision: Any) -> dict[str, Any]:
    if not isinstance(decision, dict) or set(decision) != _DECISION_FIELDS:
        raise InputProgressionError(
            "progression decision fields do not match the contract"
        )
    decision = copy.deepcopy(decision)
    if decision["schema_version"] != DECISION_SCHEMA_VERSION:
        raise InputProgressionError("unsupported progression decision schema_version")
    for field in (
        "request_id",
        "project_id",
        "observed_at",
        "reason_code",
    ):
        _text(decision[field], field)
    if not isinstance(decision["request_sha256"], str) or not _DIGEST_RE.fullmatch(
        decision["request_sha256"]
    ):
        raise InputProgressionError("request_sha256 must be lowercase SHA-256")
    for field in ("project_revision", "governance_revision"):
        _uint(decision[field], field)
    if decision["intent_kind"] not in _INTENT_KINDS:
        raise InputProgressionError("decision intent_kind is unsupported")
    if decision["action"] not in _ACTIONS:
        raise InputProgressionError("decision action is unsupported")
    if decision["state_write_authority"] is not False:
        raise InputProgressionError("progression decision cannot claim write authority")

    routed_values = (
        decision["input_ref"],
        decision["input_sha256"],
        decision["input_kind"],
        decision["classifier_provenance_ref"],
    )
    if decision["intent_kind"] == "routed_input":
        for value, field in zip(
            routed_values,
            (
                "input_ref",
                "input_sha256",
                "input_kind",
                "classifier_provenance_ref",
            ),
            strict=True,
        ):
            _text(value, field)
        if not _DIGEST_RE.fullmatch(decision["input_sha256"]):
            raise InputProgressionError("input_sha256 must be lowercase SHA-256")
        if decision["input_kind"] not in _ROUTED_INPUT_KINDS:
            raise InputProgressionError("decision input_kind is unsupported")
    elif any(value is not None for value in routed_values):
        raise InputProgressionError("non-routed decision cannot contain input fields")

    active_ids = (
        _optional_text(decision["active_work_id_before"], "active_work_id_before"),
        _optional_text(decision["active_work_id_after"], "active_work_id_after"),
    )
    active_revisions = (
        _optional_uint(
            decision["active_work_revision_before"], "active_work_revision_before"
        ),
        _optional_uint(
            decision["active_work_revision_after"], "active_work_revision_after"
        ),
    )
    if (active_ids[0] is None) != (active_ids[1] is None) or (
        active_revisions[0] is None
    ) != (active_revisions[1] is None):
        raise InputProgressionError("active Work fields must be present together")
    if (active_ids[0] is None) != (active_revisions[0] is None):
        raise InputProgressionError("active Work ID and revision must be present together")
    if active_ids[0] != active_ids[1] or active_revisions[0] != active_revisions[1]:
        raise InputProgressionError("progression decision cannot change active Work")
    selected_id = _optional_text(decision["selected_work_id"], "selected_work_id")
    selected_revision = _optional_uint(
        decision["selected_work_revision"], "selected_work_revision"
    )
    if (selected_id is None) != (selected_revision is None):
        raise InputProgressionError("selected Work ID and revision must be present together")
    blocker_id = _optional_text(decision["blocker_id"], "blocker_id")
    blocker_kind = decision["blocker_kind"]
    if blocker_kind is not None and blocker_kind not in _BLOCKER_KINDS:
        raise InputProgressionError("decision blocker_kind is unsupported")
    reason = _optional_text(decision["reason"], "reason")
    affected_work_ids = _strings(
        decision["affected_work_ids"], "affected_work_ids"
    )
    evidence_ids = _strings(decision["evidence_ids"], "evidence_ids")
    governance_evidence_refs = _strings(
        decision["governance_evidence_refs"], "governance_evidence_refs"
    )
    pending_route = _optional_digest(
        decision["pending_route_decision_sha256"],
        "pending_route_decision_sha256",
    )

    scopes = decision["affected_scope_refs"]
    if not isinstance(scopes, list):
        raise InputProgressionError("affected_scope_refs must be an object list")
    scope_keys: list[tuple[str, str]] = []
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != _SCOPE_FIELDS:
            raise InputProgressionError("affected scope fields are invalid")
        if scope["scope_kind"] not in _SCOPE_KINDS:
            raise InputProgressionError("affected scope kind is unsupported")
        scope_keys.append((scope["scope_kind"], _text(scope["scope_ref"], "scope_ref")))
    if len(scope_keys) != len(set(scope_keys)):
        raise InputProgressionError("affected scopes must be unique")
    options = decision["decision_options"]
    if not isinstance(options, list):
        raise InputProgressionError("decision_options must be an object list")
    option_ids: list[str] = []
    for option in options:
        if not isinstance(option, dict) or set(option) != _OPTION_FIELDS:
            raise InputProgressionError("decision option fields are invalid")
        option_ids.append(_text(option["option_id"], "option_id"))
        _text(option["summary"], "option.summary")
    if len(option_ids) != len(set(option_ids)):
        raise InputProgressionError("decision option IDs must be unique")
    default_behavior = _optional_text(
        decision["default_behavior"], "default_behavior"
    )
    resume = decision["resume_condition"]
    if resume is not None:
        if not isinstance(resume, dict) or set(resume) != _RESUME_FIELDS:
            raise InputProgressionError("resume condition fields are invalid")
        if resume["kind"] not in _RESUME_KINDS:
            raise InputProgressionError("resume condition kind is unsupported")
        if not _strings(resume["refs"], "resume_condition.refs"):
            raise InputProgressionError("resume condition refs must be non-empty")

    action = decision["action"]
    escalation_empty = (
        blocker_id is None
        and blocker_kind is None
        and reason is None
        and not affected_work_ids
        and not evidence_ids
        and not governance_evidence_refs
        and not options
        and default_behavior is None
        and not scopes
        and resume is None
    )
    if action in {"continue-active", "capture-and-continue"}:
        if active_ids[0] is None or selected_id is not None or not escalation_empty:
            raise InputProgressionError(f"{action} fields are inconsistent")
        if action == "capture-and-continue" and decision["input_kind"] != "idea":
            raise InputProgressionError("capture-and-continue requires Idea input")
    elif action == "select-next-ready":
        if active_ids[0] is not None or selected_id is None or not escalation_empty:
            raise InputProgressionError("select-next-ready fields are inconsistent")
        if pending_route is not None:
            raise InputProgressionError("selection cannot contain a pending route")
    elif action == "ask-user":
        if (
            selected_id is not None
            or blocker_id is None
            or blocker_kind not in _ASK_BLOCKER_KINDS
            or reason is None
            or not affected_work_ids
            or not evidence_ids
            or governance_evidence_refs
            or len(options) < 2
            or default_behavior not in option_ids
            or not scopes
            or resume is None
            or pending_route is not None
        ):
            raise InputProgressionError("ask-user requires a complete typed blocker")
    elif action == "stop-blocked":
        if (
            selected_id is not None
            or blocker_id is None
            or blocker_kind != "external-completion-evidence-unavailable"
            or reason is None
            or not affected_work_ids
            or not evidence_ids
            or governance_evidence_refs
            or options
            or default_behavior is not None
            or not scopes
            or resume is None
            or pending_route is not None
        ):
            raise InputProgressionError("stop-blocked requires a complete typed blocker")
    elif action == "stop-complete":
        if (
            active_ids[0] is not None
            or selected_id is not None
            or blocker_id is not None
            or blocker_kind is not None
            or reason is None
            or affected_work_ids
            or not governance_evidence_refs
            or options
            or default_behavior is not None
            or scopes
            or resume is None
            or pending_route is not None
        ):
            raise InputProgressionError("stop-complete requires closure evidence")
    if pending_route is not None and action != "continue-active":
        raise InputProgressionError("pending route requires continue-active")
    return decision


def canonical_progression_decision_bytes(decision: dict[str, Any]) -> bytes:
    """Return normalized progression decision bytes for replay."""
    canonical = _validate_progression_decision(decision)
    for field in ("affected_work_ids", "evidence_ids", "governance_evidence_refs"):
        canonical[field] = sorted(canonical[field])
    canonical["affected_scope_refs"] = sorted(
        canonical["affected_scope_refs"],
        key=lambda scope: (scope["scope_kind"], scope["scope_ref"]),
    )
    canonical["decision_options"] = sorted(
        canonical["decision_options"], key=lambda option: option["option_id"]
    )
    if canonical["resume_condition"] is not None:
        canonical["resume_condition"]["refs"] = sorted(
            canonical["resume_condition"]["refs"]
        )
    return json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validated_decision(decision: dict[str, Any]) -> dict[str, Any]:
    _validate_progression_decision(decision)
    return decision


def _base_decision(
    request: dict[str, Any],
    state: dict[str, Any],
    governance_profile: dict[str, Any],
    route_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    project = state["project"]
    active_work_id = project["primary_work_id"]
    work_by_id = {work["work_id"]: work for work in state["works"]}
    active_revision = (
        work_by_id[active_work_id]["revision"] if active_work_id is not None else None
    )
    return {
        "schema_version": DECISION_SCHEMA_VERSION,
        "request_id": request["request_id"],
        "request_sha256": hashlib.sha256(
            canonical_progression_request_bytes(request)
        ).hexdigest(),
        "project_id": project["project_id"],
        "project_revision": project["revision"],
        "governance_revision": governance_profile["profile"]["revision"],
        "observed_at": request["observed_at"],
        "intent_kind": request["intent_kind"],
        "input_ref": route_decision["input_ref"] if route_decision else None,
        "input_sha256": route_decision["input_sha256"] if route_decision else None,
        "input_kind": route_decision["input_kind"] if route_decision else None,
        "classifier_provenance_ref": (
            route_decision["classifier_provenance_ref"] if route_decision else None
        ),
        "action": (
            "capture-and-continue"
            if route_decision
            and route_decision["route"] == "capture-candidate-and-continue"
            else "continue-active"
        ),
        "reason_code": (
            "idea-candidate"
            if route_decision
            and route_decision["route"] == "capture-candidate-and-continue"
            else "active-leaf-sticky"
        ),
        "active_work_id_before": active_work_id,
        "active_work_id_after": active_work_id,
        "active_work_revision_before": active_revision,
        "active_work_revision_after": active_revision,
        "selected_work_id": None,
        "selected_work_revision": None,
        "blocker_id": None,
        "blocker_kind": None,
        "reason": None,
        "affected_work_ids": [],
        "evidence_ids": [],
        "governance_evidence_refs": [],
        "decision_options": [],
        "default_behavior": None,
        "affected_scope_refs": [],
        "resume_condition": None,
        "pending_route_decision_sha256": (
            request["route_decision_sha256"]
            if route_decision and route_decision["route"].startswith("propose-")
            else None
        ),
        "state_write_authority": False,
    }


def _required_ready_candidates(
    state: dict[str, Any], governance_profile: dict[str, Any]
) -> list[dict[str, Any]]:
    work_by_id = {work["work_id"]: work for work in state["works"]}
    source_by_work_id = {
        source["work_id"]: source for source in governance_profile["work_sources"]
    }
    obligation_by_work_id = {
        obligation["work_id"]: obligation
        for obligation in governance_profile["obligations"]
    }
    blocker_by_id = {
        blocker["blocker_id"]: blocker for blocker in state["blockers"]
    }
    active_protected_work_ids = {
        work_id
        for protection in state.get("correction_protections", [])
        if protection["status"] == "active"
        for work_id in protection["affected_work_ids"]
    }
    candidates: list[dict[str, Any]] = []
    for work_id, source in source_by_work_id.items():
        work = work_by_id.get(work_id)
        obligation = obligation_by_work_id[work_id]
        if work is None:
            raise InputProgressionError(
                "governance work source is missing from typed state"
            )
        if set(source["dependency_ids"]) != set(work["dependency_ids"]):
            raise InputProgressionError(
                "state and governance dependency sets differ"
            )
        if not (
            work["kind"] in {"work", "experiment"}
            and work["status"] == "ready"
            and source["readiness"] == "ready"
            and obligation["mode"] == "required"
            and obligation["status"] == "pending"
            and obligation["authority"]["kind"] == "project-governance"
        ):
            continue
        dependencies = work["dependency_ids"]
        if any(
            dependency_id not in source_by_work_id
            or work_by_id[dependency_id]["status"] != "completed"
            or source_by_work_id[dependency_id]["readiness"] != "completed"
            for dependency_id in dependencies
        ):
            continue
        blocked = any(
            blocker["status"] == "open"
            and (
                blocker["blocker_id"] in work["blocker_ids"]
                or work_id in blocker["blocked_work_ids"]
            )
            for blocker in blocker_by_id.values()
        )
        if blocked or work_id in active_protected_work_ids:
            continue
        candidates.append(work)
    return sorted(candidates, key=lambda work: work["work_id"])


def _required_closure_evidence(
    state: dict[str, Any], governance_profile: dict[str, Any]
) -> tuple[list[str], list[str]] | None:
    required = [
        obligation
        for obligation in governance_profile["obligations"]
        if obligation["mode"] == "required"
    ]
    if not required or any(
        obligation["status"] not in {"satisfied", "waived"}
        for obligation in required
    ):
        return None
    work_by_id = {work["work_id"]: work for work in state["works"]}
    state_evidence_ids = sorted(
        {
            evidence_id
            for obligation in required
            for evidence_id in work_by_id[obligation["work_id"]]["evidence_ids"]
        }
    )
    governance_evidence_refs = sorted(
        {
            evidence_ref
            for obligation in required
            for evidence_ref in obligation["evidence_refs"]
        }
    )
    if not governance_evidence_refs:
        raise InputProgressionError(
            "closed required obligations require governance evidence"
        )
    return state_evidence_ids, governance_evidence_refs


def _bind_blocking_decision(
    decision: dict[str, Any], state: dict[str, Any]
) -> dict[str, Any]:
    decision = _validate_blocking_decision(decision)
    project = state["project"]
    if (
        decision["project_id"] != project["project_id"]
        or decision["project_revision"] != project["revision"]
    ):
        raise InputProgressionError("blocking decision does not bind current project")
    blocker_by_id = {
        blocker["blocker_id"]: blocker for blocker in state["blockers"]
    }
    blocker = blocker_by_id.get(decision["blocker_id"])
    if blocker is None or blocker["status"] != "open":
        raise InputProgressionError("blocking decision requires a current open blocker")
    if (
        decision["reason"] != blocker["reason"]
        or set(decision["affected_work_ids"]) != set(blocker["blocked_work_ids"])
        or set(decision["evidence_ids"]) != set(blocker["evidence_ids"])
    ):
        raise InputProgressionError("blocking decision does not match typed blocker")
    evidence_by_id = {
        evidence["evidence_id"]: evidence for evidence in state["evidence"]
    }
    if any(
        evidence_by_id[evidence_id]["validity"] != "verified"
        or evidence_by_id[evidence_id]["verified_at"] is None
        for evidence_id in decision["evidence_ids"]
    ):
        raise InputProgressionError("blocking decision evidence is not verified")
    work_by_id = {work["work_id"]: work for work in state["works"]}
    allowed_scopes = {
        (scope["scope_kind"], scope["scope_ref"])
        for work_id in decision["affected_work_ids"]
        for scope in work_by_id[work_id]["scope_refs"]
    }
    requested_scopes = {
        (scope["scope_kind"], scope["scope_ref"])
        for scope in decision["affected_scope_refs"]
    }
    if not requested_scopes.issubset(allowed_scopes):
        raise InputProgressionError("blocking decision scope is not owned by affected Work")
    return decision


def decide_input_progression(
    request: dict[str, Any],
    state: dict[str, Any],
    governance_profile: dict[str, Any],
    *,
    route_decision: dict[str, Any] | None,
    blocking_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the next progression action without mutating authority."""
    request = _validate_request(request)
    try:
        validate_typed_state(state)
        validate_project_governance_profile(
            governance_profile,
            observed_at=request["observed_at"],
        )
    except (TypedStateError, ProjectGovernanceProfileError, TypeError) as exc:
        raise InputProgressionError("current state or governance profile is invalid") from exc

    project = state["project"]
    profile = governance_profile["profile"]
    if request["project_id"] != project["project_id"] or profile["project_id"] != project["project_id"]:
        raise InputProgressionError("request, state and governance project IDs differ")
    if request["governance_profile_id"] != profile["profile_id"]:
        raise InputProgressionError("governance profile ID does not match request")
    if request["expected_project_revision"] != project["revision"]:
        raise InputProgressionError("expected project revision does not match state")
    if request["expected_governance_revision"] != profile["revision"]:
        raise InputProgressionError("expected governance revision does not match profile")
    if profile["governance_ref"] != project["governance_ref"]:
        raise InputProgressionError("state and profile governance refs differ")
    intent_kind = request["intent_kind"]
    if intent_kind == "routed_input":
        if route_decision is None or blocking_decision is not None:
            raise InputProgressionError("routed_input requires only a route decision")
        try:
            route_bytes = canonical_route_decision_bytes(route_decision)
        except (StickyRouteError, TypeError) as exc:
            raise InputProgressionError("route decision is invalid") from exc
        if hashlib.sha256(route_bytes).hexdigest() != request["route_decision_sha256"]:
            raise InputProgressionError("route decision hash does not match request")
        if (
            route_decision["project_id"] != project["project_id"]
            or route_decision["project_revision"] != project["revision"]
            or route_decision["active_work_id_before"] != project["primary_work_id"]
            or route_decision["active_work_id_after"] != project["primary_work_id"]
        ):
            raise InputProgressionError("route decision does not bind current active state")
        return _validated_decision(
            _base_decision(request, state, governance_profile, route_decision)
        )
    if route_decision is not None:
        raise InputProgressionError("only routed_input accepts a route decision")
    if intent_kind == "blocking_decision":
        if blocking_decision is None:
            raise InputProgressionError(
                "blocking_decision intent requires a typed decision"
            )
        blocker_bytes = canonical_blocking_decision_bytes(blocking_decision)
        if (
            hashlib.sha256(blocker_bytes).hexdigest()
            != request["blocking_decision_sha256"]
        ):
            raise InputProgressionError("blocking decision hash does not match request")
        bound = _bind_blocking_decision(blocking_decision, state)
        active_work_id = project["primary_work_id"]
        if (
            active_work_id is not None
            and active_work_id not in bound["affected_work_ids"]
        ):
            raise InputProgressionError(
                "blocking decision does not affect the active Work"
            )
        if bound["safe_reversible_default_available"]:
            if active_work_id is None:
                candidates = _required_ready_candidates(state, governance_profile)
                if candidates:
                    selected = candidates[0]
                    decision = _base_decision(
                        request, state, governance_profile, None
                    )
                    decision.update(
                        {
                            "action": "select-next-ready",
                            "reason_code": "required-ready-work",
                            "selected_work_id": selected["work_id"],
                            "selected_work_revision": selected["revision"],
                        }
                    )
                    return _validated_decision(decision)
                raise InputProgressionError(
                    "safe default cannot continue without an active or ready Work"
                )
            decision = _base_decision(request, state, governance_profile, None)
            decision["reason_code"] = "safe-reversible-default"
            return _validated_decision(decision)
        if active_work_id is None:
            candidates = _required_ready_candidates(state, governance_profile)
            if candidates:
                selected = candidates[0]
                decision = _base_decision(
                    request, state, governance_profile, None
                )
                decision.update(
                    {
                        "action": "select-next-ready",
                        "reason_code": "required-ready-work",
                        "selected_work_id": selected["work_id"],
                        "selected_work_revision": selected["revision"],
                    }
                )
                return _validated_decision(decision)
        action = (
            "ask-user"
            if bound["blocker_kind"] in _ASK_BLOCKER_KINDS
            else "stop-blocked"
        )
        decision = _base_decision(request, state, governance_profile, None)
        decision.update(
            {
                "action": action,
                "reason_code": bound["blocker_kind"],
                "blocker_id": bound["blocker_id"],
                "blocker_kind": bound["blocker_kind"],
                "reason": bound["reason"],
                "affected_work_ids": copy.deepcopy(bound["affected_work_ids"]),
                "evidence_ids": copy.deepcopy(bound["evidence_ids"]),
                "decision_options": copy.deepcopy(bound["decision_options"]),
                "default_behavior": bound["default_option_id"],
                "affected_scope_refs": copy.deepcopy(
                    bound["affected_scope_refs"]
                ),
                "resume_condition": copy.deepcopy(bound["resume_condition"]),
            }
        )
        return _validated_decision(decision)
    if blocking_decision is not None:
        raise InputProgressionError(
            "only blocking_decision intent accepts a typed blocker"
        )
    if project["primary_work_id"] is None:
        candidates = _required_ready_candidates(state, governance_profile)
        decision = _base_decision(request, state, governance_profile, None)
        if candidates:
            selected = candidates[0]
            decision.update(
                {
                    "action": "select-next-ready",
                    "reason_code": "required-ready-work",
                    "selected_work_id": selected["work_id"],
                    "selected_work_revision": selected["revision"],
                }
            )
            return _validated_decision(decision)
        closure_evidence = _required_closure_evidence(state, governance_profile)
        if closure_evidence is not None:
            state_evidence_ids, governance_evidence_refs = closure_evidence
            decision.update(
                {
                    "action": "stop-complete",
                    "reason_code": "required-obligations-closed",
                    "reason": "All required obligations are satisfied or waived.",
                    "evidence_ids": state_evidence_ids,
                    "governance_evidence_refs": governance_evidence_refs,
                    "resume_condition": {
                        "kind": "new_ready_work",
                        "refs": [governance_profile["profile"]["governance_ref"]],
                    },
                }
            )
            return _validated_decision(decision)
        raise InputProgressionError("no ready required work is selectable")
    raise InputProgressionError("dispatch_tick requires no active leaf")
