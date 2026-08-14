"""Provider-neutral, single-CAS application of M3 route decisions."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable

from .artifact_store import ArtifactRef, ArtifactStoreError
from .checkpoint import CheckpointError, verify_historical_checkpoint
from .effect_scope_gate import evaluate_claim_scope_gate
from .state_events import (
    StateEventError,
    build_state_event,
    replay_state_events,
)
from .state_store import (
    StateStoreBusy,
    StateStoreConflict,
    StateStoreError,
    StateStoreIntegrityError,
    StateStoreNotFound,
)
from .sticky_router import (
    _target_rejection_reason,
    _validate_decision,
    canonical_route_decision_bytes,
)
from .typed_state import TypedStateError, canonical_state_bytes, validate_typed_state


REQUEST_SCHEMA_VERSION = "context.task-route-apply-request/v1alpha1"
_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "project_id",
    "proposal_sha256",
    "operation",
    "expected_project_revision",
    "expected_active_work_id",
    "expected_active_work_revision",
    "target_work_id",
    "target_work_revision",
    "authorization_ref",
    "checkpoint_ref",
    "checkpoint_binding",
    "child_work",
    "correction_changes",
    "supersedes_event_id",
    "causation_ref",
    "correlation_ref",
}
_DIGEST_RE = r"^[0-9a-f]{64}$"


class RouteApplyError(RuntimeError):
    """Raised when a route proposal cannot be applied atomically."""


def _non_empty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RouteApplyError(f"{field} must be a non-empty string")
    return value


def _digest(value: Any, field: str) -> str:
    import re

    if not isinstance(value, str) or re.fullmatch(_DIGEST_RE, value) is None:
        raise RouteApplyError(f"{field} must be lowercase SHA-256")
    return value


def _uint(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise RouteApplyError(f"{field} must be a non-negative integer")
    return value


def _context_subject(context: Any) -> str:
    if isinstance(context, dict):
        return _non_empty(context.get("subject_ref"), "context.subject_ref")
    return _non_empty(getattr(context, "subject_ref", None), "context.subject_ref")


def _context_authorization(context: Any) -> str:
    if isinstance(context, dict):
        return _non_empty(context.get("authorization_ref"), "context.authorization_ref")
    return _non_empty(getattr(context, "authorization_ref", None), "context.authorization_ref")


def _validate_request(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise RouteApplyError("route apply request fields do not match the contract")
    request = copy.deepcopy(request)
    if request["schema_version"] != REQUEST_SCHEMA_VERSION:
        raise RouteApplyError("unsupported route apply schema_version")
    for field in ("request_id", "project_id", "expected_active_work_id", "causation_ref", "correlation_ref"):
        _non_empty(request[field], field)
    _digest(request["proposal_sha256"], "proposal_sha256")
    _uint(request["expected_project_revision"], "expected_project_revision")
    _uint(request["expected_active_work_revision"], "expected_active_work_revision")
    if request["operation"] not in {"continue", "child", "interrupt", "switch", "correction"}:
        raise RouteApplyError("operation is unsupported")
    target_id = request["target_work_id"]
    if target_id is not None:
        _non_empty(target_id, "target_work_id")
        _uint(request["target_work_revision"], "target_work_revision")
    elif request["target_work_revision"] is not None:
        raise RouteApplyError("target_work_revision requires target_work_id")
    auth = request["authorization_ref"]
    if auth is not None:
        _non_empty(auth, "authorization_ref")
    if request["checkpoint_ref"] is not None:
        _validate_artifact_ref(request["checkpoint_ref"], "checkpoint_ref")
    if request["checkpoint_binding"] is not None:
        _validate_checkpoint_binding(request["checkpoint_binding"])
    if request["child_work"] is not None and not isinstance(request["child_work"], dict):
        raise RouteApplyError("child_work must be an object")
    if request["correction_changes"] is not None and not isinstance(request["correction_changes"], list):
        raise RouteApplyError("correction_changes must be a list")
    if request["correction_changes"] is not None:
        seen_correction_keys: set[tuple[str, str]] = set()
        for change in request["correction_changes"]:
            if not isinstance(change, dict):
                continue
            collection = change.get("collection")
            object_id = change.get("object_id")
            if not isinstance(collection, str) or not isinstance(object_id, str):
                continue
            key = (collection, object_id)
            if key in seen_correction_keys:
                raise RouteApplyError("duplicate correction object change")
            seen_correction_keys.add(key)
    if request["supersedes_event_id"] is not None:
        _non_empty(request["supersedes_event_id"], "supersedes_event_id")
    return request


def _request_sha256(request: dict[str, Any]) -> str:
    canonical = json.dumps(
        request,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_artifact_ref(value: Any, field: str) -> None:
    if not isinstance(value, dict) or set(value) != {
        "schema_version", "digest_algorithm", "digest", "size_bytes", "artifact_uri"
    }:
        raise RouteApplyError(f"{field} fields are invalid")
    if value["schema_version"] != "context.artifact-ref/v1alpha1" or value["digest_algorithm"] != "sha-256":
        raise RouteApplyError(f"{field} schema is unsupported")
    digest = _digest(value["digest"], f"{field}.digest")
    _uint(value["size_bytes"], f"{field}.size_bytes")
    if value["artifact_uri"] != f"artifact://sha256/{digest}":
        raise RouteApplyError(f"{field}.artifact_uri does not match digest")


def _validate_checkpoint_binding(value: Any) -> None:
    expected = {"checkpoint_revision", "checkpoint_event_head", "return_work_id", "return_work_revision"}
    if not isinstance(value, dict) or set(value) != expected:
        raise RouteApplyError("checkpoint_binding fields are invalid")
    _uint(value["checkpoint_revision"], "checkpoint_binding.checkpoint_revision")
    _non_empty(value["return_work_id"], "checkpoint_binding.return_work_id")
    _uint(value["return_work_revision"], "checkpoint_binding.return_work_revision")
    head = value["checkpoint_event_head"]
    if head is not None:
        if not isinstance(head, dict) or set(head) != {"sequence_no", "event_sha256"}:
            raise RouteApplyError("checkpoint_binding.checkpoint_event_head is invalid")
        if type(head["sequence_no"]) is not int or head["sequence_no"] <= 0:
            raise RouteApplyError("checkpoint_binding event sequence is invalid")
        _digest(head["event_sha256"], "checkpoint_binding event hash")


def _replace(snapshot: dict[str, Any], collection: str, object_id: str, value: dict[str, Any], id_field: str) -> None:
    for index, existing in enumerate(snapshot[collection]):
        if existing[id_field] == object_id:
            snapshot[collection][index] = copy.deepcopy(value)
            return
    snapshot[collection].append(copy.deepcopy(value))


def _event_head(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    return {"sequence_no": events[-1]["sequence_no"], "event_sha256": events[-1]["event_sha256"]}


def _response(snapshot: dict[str, Any], event: dict[str, Any] | None, *, status: str, return_frame: dict[str, Any] | None = None, event_head: dict[str, Any] | None = None) -> dict[str, Any]:
    result = {"status": status, "snapshot": copy.deepcopy(snapshot), "revision": snapshot["project"]["revision"], "event": copy.deepcopy(event), "event_head": copy.deepcopy(event_head if event_head is not None else (_event_head([event]) if event else None)), "return_frame": copy.deepcopy(return_frame)}
    return result


def _event_matches_request(event: dict[str, Any], request: dict[str, Any]) -> bool:
    transition = event.get("task_transition")
    if not isinstance(transition, dict):
        return False
    if (
        transition.get("route_apply_request_sha256") != _request_sha256(request)
        or transition.get("route_kind") != request["operation"]
        or event["revision_before"] != request["expected_project_revision"]
        or event["causation_ref"] != request["causation_ref"]
        or event["correlation_ref"] != request["correlation_ref"]
    ):
        return False
    task_events = transition.get("task_events", [])
    if request["operation"] == "child":
        if len(task_events) != 1 or request["child_work"] is None:
            return False
        task_event = task_events[0]
        return (
            task_event["work_id"] == request["child_work"].get("work_id")
            and task_event["return_work_id"] == request["expected_active_work_id"]
            and any(
                change["collection"] == "works"
                and change["object_id"] == task_event["work_id"]
                and change["value"] == request["child_work"]
                for change in event["changes"]
            )
        )
    if request["operation"] in {"interrupt", "switch"}:
        if len(task_events) != 2 or request["checkpoint_binding"] is None:
            return False
        suspended, activated = task_events
        head_before = (
            None
            if event["sequence_no"] == 1
            else {
                "sequence_no": event["sequence_no"] - 1,
                "event_sha256": event["previous_event_sha256"],
            }
        )
        binding = request["checkpoint_binding"]
        return (
            suspended["work_id"] == request["expected_active_work_id"]
            and suspended["work_revision"] == request["expected_active_work_revision"]
            and suspended["checkpoint_ref"] == request["checkpoint_ref"]
            and activated["work_id"] == request["target_work_id"]
            and activated["work_revision"] == request["target_work_revision"] + 1
            and binding["checkpoint_revision"] == event["revision_before"]
            and binding["checkpoint_event_head"] == head_before
            and binding["return_work_id"] == suspended["work_id"]
            and binding["return_work_revision"] == suspended["work_revision"]
        )
    if request["operation"] == "correction":
        if event["supersedes_event_id"] != request["supersedes_event_id"]:
            return False
        requested_changes = request["correction_changes"] or []
        return all(change in event["changes"] for change in requested_changes)
    return False


def _commit_route_event(store: Any, *, project_id: str, current: dict[str, Any], events: list[dict[str, Any]], candidate: dict[str, Any], changes: list[dict[str, Any]], request: dict[str, Any], transition: dict[str, Any], event_id: str, actor_ref: str) -> dict[str, Any]:
    unique_changes: dict[tuple[str, str], dict[str, Any]] = {}
    for change in changes:
        unique_changes[(change["collection"], change["object_id"])] = copy.deepcopy(change)
    changes = list(unique_changes.values())
    previous_hash = events[-1]["event_sha256"] if events else None
    sequence_no = events[-1]["sequence_no"] + 1 if events else 1
    event = build_state_event(
        event_id=event_id,
        event_type="correction" if request["operation"] == "correction" else "state-transition",
        project_id=project_id,
        sequence_no=sequence_no,
        revision_before=current["project"]["revision"],
        occurred_at=candidate["project"]["updated_at"],
        actor_ref=actor_ref,
        causation_ref=request["causation_ref"],
        correlation_ref=request["correlation_ref"],
        previous_event_sha256=previous_hash,
        supersedes_event_id=request["supersedes_event_id"],
        changes=changes,
        project_after=candidate["project"],
        task_transition=transition,
    )
    expected = replay_state_events(
        current,
        [event],
        starting_sequence_no=sequence_no,
        previous_event_sha256=previous_hash,
        prior_events=events if event["supersedes_event_id"] is not None else None,
    )
    store.commit_event(
        project_id=project_id,
        expected_revision=current["project"]["revision"],
        event=event,
        expected_snapshot=expected,
    )
    return event, expected


def apply_route(
    store: Any,
    request: dict[str, Any],
    *,
    decision: dict[str, Any],
    context: Any,
    authorizer: Any,
    clock: Callable[[], str],
    event_id_factory: Callable[[str], str],
    artifact_store: Any | None = None,
    expected_plan_sha256: str | None = None,
    expected_registry_digest: str | None = None,
) -> dict[str, Any]:
    """Apply one trusted M3-02 proposal through one backend CAS."""
    request = _validate_request(request)
    request_hash = _request_sha256(request)
    try:
        _validate_decision(decision)
    except Exception as exc:
        raise RouteApplyError("route decision is invalid") from exc
    proposal_hash = hashlib.sha256(canonical_route_decision_bytes(decision)).hexdigest()
    if request["proposal_sha256"] != proposal_hash:
        raise RouteApplyError("proposal hash does not match decision")
    route_for_operation = {
        "continue": {"continue", "capture-candidate-and-continue"},
        "child": {"propose-child"},
        "interrupt": {"propose-switch"},
        "switch": {"propose-switch"},
        "correction": {"propose-correction"},
    }
    if decision["route"] not in route_for_operation[request["operation"]]:
        raise RouteApplyError("operation does not match route decision")
    if (
        request["operation"] in {"interrupt", "switch"}
        and decision["input_kind"] != request["operation"]
    ):
        raise RouteApplyError("operation does not match route decision input kind")
    for request_field, decision_field in (
        ("project_id", "project_id"),
        ("expected_project_revision", "project_revision"),
        ("expected_active_work_id", "active_work_id_before"),
        ("expected_active_work_revision", "active_work_revision_before"),
        ("target_work_id", "target_work_id"),
        ("target_work_revision", "target_work_revision"),
    ):
        if request[request_field] != decision[decision_field]:
            if request_field in {"expected_project_revision", "expected_active_work_revision"}:
                raise RouteApplyError(f"stale {request_field}")
            raise RouteApplyError(f"{request_field} does not match route decision")
    actor_ref = _context_subject(context)
    if not callable(authorizer) and not hasattr(authorizer, "authorize"):
        raise RouteApplyError("authorization provider is required")
    authorization_ref = request["authorization_ref"]
    if request["operation"] in {"interrupt", "switch", "correction"}:
        if authorization_ref is None:
            raise RouteApplyError("trusted authorization is required")
        if authorization_ref == decision.get("authorization_candidate_ref"):
            raise RouteApplyError("candidate authorization cannot be used as trusted authorization")
        if authorization_ref != _context_authorization(context):
            raise RouteApplyError("authorization context does not match request")
        authorized = authorizer(context, "state.route.apply", request["project_id"]) if callable(authorizer) else authorizer.authorize(context, "state.route.apply", request["project_id"])
        if authorized is not True:
            raise RouteApplyError("authorization was denied")

    try:
        current = store.read_project(request["project_id"])
        events = store.read_events(request["project_id"])
    except (StateStoreError, StateStoreConflict, StateStoreBusy, StateStoreNotFound, StateStoreIntegrityError) as exc:
        raise RouteApplyError("state read failed") from exc
    event_id = event_id_factory(request["request_id"])
    if not isinstance(event_id, str) or not event_id.strip():
        raise RouteApplyError("event_id_factory returned an invalid ID")
    existing = next((event for event in events if event["event_id"] == event_id), None)
    if existing is not None:
        if (
            existing.get("task_transition", {}).get("route_decision_sha256")
            != proposal_hash
            or not _event_matches_request(existing, request)
        ):
            raise RouteApplyError("request identity conflicts with a different route proposal")
        if events[-1]["event_id"] != event_id or current["project"]["revision"] != existing["revision_after"]:
            raise RouteApplyError("route receipt is no longer current")
        return _response(current, existing, status="applied", return_frame=_return_frame(existing, request))
    if current["project"]["revision"] != request["expected_project_revision"]:
        raise RouteApplyError("stale project revision")
    active = next((item for item in current["works"] if item["work_id"] == request["expected_active_work_id"]), None)
    if active is None or active["revision"] != request["expected_active_work_revision"] or active["status"] != "active":
        raise RouteApplyError("stale active Work revision")
    if request["operation"] == "continue":
        return _response(current, None, status="continued", event_head=_event_head(events))

    now = clock()
    candidate = copy.deepcopy(current)
    revision_after = current["project"]["revision"] + 1
    changes: list[dict[str, Any]] = []
    active_claims = [claim for claim in candidate["claims"] if claim["status"] == "active"]
    for claim in active_claims:
        claim["expected_project_revision"] = revision_after
        changes.append({"collection": "claims", "object_id": claim["claim_id"], "value": copy.deepcopy(claim)})

    if request["operation"] == "child":
        child = request["child_work"]
        if child is None or child.get("status") != "proposed" or child.get("parent_work_id") != active["work_id"]:
            raise RouteApplyError("child proposal must be proposed and parented to the active Work")
        if any(item["work_id"] == child.get("work_id") for item in candidate["works"]):
            raise RouteApplyError("child Work identity already exists")
        _replace(candidate, "works", child["work_id"], child, "work_id")
        changes.append({"collection": "works", "object_id": child["work_id"], "value": copy.deepcopy(child)})
        transition = {"route_decision_sha256": proposal_hash, "route_apply_request_sha256": request_hash, "route_kind": "child", "task_events": [{"task_event_id": f"task-{request['request_id']}", "event_kind": "child_proposed", "work_id": child["work_id"], "work_revision": child["revision"], "return_work_id": active["work_id"], "checkpoint_ref": None, "related_event_id": None}]}
        status = "applied"
        return_frame = {"return_work_id": active["work_id"], "return_work_revision": active["revision"], "old_project_revision": current["project"]["revision"], "proposal_sha256": proposal_hash}
    elif request["operation"] in {"interrupt", "switch"}:
        if request["checkpoint_ref"] is None or request["checkpoint_binding"] is None:
            raise RouteApplyError("checkpoint binding is required before activation")
        binding = request["checkpoint_binding"]
        if binding["checkpoint_revision"] != current["project"]["revision"] or binding["return_work_id"] != active["work_id"] or binding["return_work_revision"] != active["revision"]:
            raise RouteApplyError("checkpoint binding is stale")
        if binding["checkpoint_event_head"] != _event_head(events):
            raise RouteApplyError("checkpoint event head is stale")
        if (
            artifact_store is None
            or expected_plan_sha256 is None
            or expected_registry_digest is None
        ):
            raise RouteApplyError("checkpoint artifact verification dependencies are required")
        try:
            checkpoint_ref = ArtifactRef.from_document(request["checkpoint_ref"])
            restored_checkpoint = verify_historical_checkpoint(
                checkpoint_ref,
                artifact_store,
                binding={
                    "project_id": request["project_id"],
                    "checkpoint_ref": request["checkpoint_ref"],
                    **binding,
                },
                expected_plan_sha256=expected_plan_sha256,
                expected_registry_digest=expected_registry_digest,
            )
        except (ArtifactStoreError, CheckpointError, TypeError, ValueError) as exc:
            raise RouteApplyError("checkpoint artifact verification failed") from exc
        if canonical_state_bytes(restored_checkpoint.snapshot) != canonical_state_bytes(current):
            raise RouteApplyError("checkpoint artifact snapshot does not match current authority")
        target = next((item for item in candidate["works"] if item["work_id"] == request["target_work_id"]), None)
        if target is None or target["status"] != "ready" or target["kind"] not in {"work", "experiment"} or target["revision"] != request["target_work_revision"]:
            raise RouteApplyError("target Work is stale or not ready")
        rejection_reason = _target_rejection_reason(
            target,
            {item["work_id"]: item for item in candidate["works"]},
            candidate,
        )
        if rejection_reason is not None:
            raise RouteApplyError(rejection_reason)
        if any(
            effect["work_id"] == active["work_id"]
            and effect["status"] in {"authorized", "started"}
            for effect in candidate["effects"]
        ):
            raise RouteApplyError("active source Work has a pending Effect")
        old_work = copy.deepcopy(active)
        old_work["status"] = "ready"
        old_work["revision"] += 1
        target = copy.deepcopy(target)
        target["status"] = "active"
        target["revision"] += 1
        old_claim = next(claim for claim in candidate["claims"] if claim["status"] == "active" and claim["work_id"] == active["work_id"])
        claim_gate_snapshot = copy.deepcopy(candidate)
        next(
            claim
            for claim in claim_gate_snapshot["claims"]
            if claim["claim_id"] == old_claim["claim_id"]
        )["status"] = "released"
        claim_gate = evaluate_claim_scope_gate(
            claim_gate_snapshot,
            actor_ref=actor_ref,
            work_id=target["work_id"],
            expected_revision=current["project"]["revision"],
            requested_scopes=target["scope_refs"],
        )
        if claim_gate["decision"] != "allow":
            raise RouteApplyError(claim_gate["reason"])
        old_claim["status"] = "released"
        old_claim["released_at"] = now
        target_claim_id = "claim-route-" + hashlib.sha256(
            f"{request['project_id']}:{request['request_id']}".encode("utf-8")
        ).hexdigest()
        if any(claim["claim_id"] == target_claim_id for claim in candidate["claims"]):
            raise RouteApplyError("derived target Claim identity already exists")
        target_claim = {"claim_id": target_claim_id, "work_id": target["work_id"], "actor_ref": actor_ref, "status": "active", "expected_project_revision": revision_after, "claimed_at": now, "lease_expires_at": now, "released_at": None, "scope_owners": copy.deepcopy(target["scope_refs"])}
        # Keep the existing lease horizon when the backend has one.
        target_claim["lease_expires_at"] = next((claim["lease_expires_at"] for claim in candidate["claims"] if claim["claim_id"] == old_claim["claim_id"]), now)
        if target_claim["lease_expires_at"] <= now:
            raise RouteApplyError("target claim lease is not valid")
        _replace(candidate, "works", old_work["work_id"], old_work, "work_id")
        _replace(candidate, "works", target["work_id"], target, "work_id")
        _replace(candidate, "claims", old_claim["claim_id"], old_claim, "claim_id")
        _replace(candidate, "claims", target_claim["claim_id"], target_claim, "claim_id")
        changes.extend([
            {"collection": "works", "object_id": old_work["work_id"], "value": old_work},
            {"collection": "works", "object_id": target["work_id"], "value": target},
            {"collection": "claims", "object_id": old_claim["claim_id"], "value": old_claim},
            {"collection": "claims", "object_id": target_claim["claim_id"], "value": target_claim},
        ])
        candidate["project"]["active_work_ids"] = [target["work_id"]]
        candidate["project"]["primary_work_id"] = target["work_id"]
        transition = {"route_decision_sha256": proposal_hash, "route_apply_request_sha256": request_hash, "route_kind": request["operation"], "task_events": [{"task_event_id": f"task-suspended-{request['request_id']}", "event_kind": "task_suspended", "work_id": old_work["work_id"], "work_revision": active["revision"], "return_work_id": old_work["work_id"], "checkpoint_ref": request["checkpoint_ref"], "related_event_id": None}, {"task_event_id": f"task-activated-{request['request_id']}", "event_kind": "task_activated", "work_id": target["work_id"], "work_revision": target["revision"], "return_work_id": None, "checkpoint_ref": None, "related_event_id": f"task-suspended-{request['request_id']}"}]}
        status = "applied"
        return_frame = {"checkpoint_ref": copy.deepcopy(request["checkpoint_ref"]), "old_project_revision": current["project"]["revision"], "old_work_id": active["work_id"], "old_work_revision": active["revision"], "return_work_id": active["work_id"], "return_work_revision": active["revision"], "current_work_id": target["work_id"], "proposal_sha256": proposal_hash}
    else:
        if not request["supersedes_event_id"] or not request["correction_changes"]:
            raise RouteApplyError("correction requires supersedes_event_id and changes")
        superseded_event = next(
            (
                item
                for item in events
                if item["event_id"] == request["supersedes_event_id"]
            ),
            None,
        )
        if superseded_event is None:
            raise RouteApplyError("correction target event does not exist")
        if any(
            item["supersedes_event_id"] == request["supersedes_event_id"]
            for item in events
        ):
            raise RouteApplyError("correction target event is already superseded")
        collection_id_fields = {
            "works": "work_id",
            "claims": "claim_id",
            "ideas": "idea_id",
            "decisions": "decision_id",
            "constraints": "constraint_id",
            "evidence": "evidence_id",
            "blockers": "blocker_id",
            "effects": "effect_id",
        }
        changed_keys: set[tuple[str, str]] = set()
        for change in request["correction_changes"]:
            if (
                not isinstance(change, dict)
                or set(change) != {"collection", "object_id", "value"}
                or change["collection"] not in collection_id_fields
                or not isinstance(change["object_id"], str)
                or not change["object_id"].strip()
                or not isinstance(change["value"], dict)
            ):
                raise RouteApplyError("correction change is invalid")
            changed_keys.add((change["collection"], change["object_id"]))
        superseded_keys = {
            (change["collection"], change["object_id"])
            for change in superseded_event["changes"]
        }
        if not changed_keys.intersection(superseded_keys):
            raise RouteApplyError("correction changed keys do not intersect target event")
        transition = {"route_decision_sha256": proposal_hash, "route_apply_request_sha256": request_hash, "route_kind": "correction", "task_events": [{"task_event_id": f"task-correction-{request['request_id']}", "event_kind": "correction_applied", "work_id": active["work_id"], "work_revision": active["revision"], "return_work_id": None, "checkpoint_ref": None, "related_event_id": request["supersedes_event_id"]}]}
        for change in request["correction_changes"]:
            _replace(
                candidate,
                change["collection"],
                change["object_id"],
                change["value"],
                collection_id_fields[change["collection"]],
            )
            changes.append(copy.deepcopy(change))
        status = "applied"
        return_frame = {"old_project_revision": current["project"]["revision"], "proposal_sha256": proposal_hash}

    candidate["project"]["revision"] = revision_after
    candidate["project"]["updated_at"] = now
    event, expected = _commit_route_event(store, project_id=request["project_id"], current=current, events=events, candidate=candidate, changes=changes, request=request, transition=transition, event_id=event_id, actor_ref=actor_ref)
    return _response(expected, event, status=status, return_frame=_return_frame(event, request))


def _return_frame(event: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    events = event.get("task_transition", {}).get("task_events", [])
    route_kind = event.get("task_transition", {}).get("route_kind")
    proposal_sha256 = event.get("task_transition", {}).get("route_decision_sha256")
    if route_kind == "child":
        child = events[0]
        return {
            "return_work_id": child["return_work_id"],
            "return_work_revision": request["expected_active_work_revision"],
            "old_project_revision": event["revision_before"],
            "proposal_sha256": proposal_sha256,
        }
    if route_kind == "correction":
        return {
            "old_project_revision": event["revision_before"],
            "proposal_sha256": proposal_sha256,
        }
    suspended = next((item for item in events if item["event_kind"] == "task_suspended"), None)
    frame = {
        "checkpoint_ref": copy.deepcopy(suspended["checkpoint_ref"]) if suspended else None,
        "old_work_id": suspended["work_id"] if suspended else request["expected_active_work_id"],
        "old_work_revision": suspended["work_revision"] if suspended else request["expected_active_work_revision"],
        "proposal_sha256": proposal_sha256,
    }
    if suspended:
        activated = next((item for item in events if item["event_kind"] == "task_activated"), None)
        frame.update({
            "old_project_revision": event["revision_before"],
            "return_work_id": frame["old_work_id"],
            "return_work_revision": frame["old_work_revision"],
            "current_work_id": activated["work_id"] if activated else None,
        })
    return frame
