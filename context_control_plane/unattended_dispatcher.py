"""Provider-neutral unattended required-Work dispatcher."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Protocol

from .input_progression import (
    InputProgressionError,
    canonical_blocking_decision_bytes,
)
from .project_governance_profile import (
    ProjectGovernanceProfileError,
    validate_project_governance_profile,
)
from .typed_state import TypedStateError, validate_typed_state
from .unattended_cursor_store import (
    UnattendedCursorConflict,
    build_campaign_cursor,
    evolve_campaign_cursor,
)
from .unattended_receipts import (
    UnattendedReceiptError,
    validate_unattended_campaign_receipt,
    validate_unattended_dispatch_step,
)

STEP_SCHEMA_VERSION = "context.unattended-dispatch-step/v1alpha1"
CAMPAIGN_RECEIPT_SCHEMA_VERSION = "context.unattended-campaign-receipt/v1alpha1"
CONDITION_DECISION_SCHEMA_VERSION = "context.condition-decision/v1alpha1"
CONDITION_BLOCKING_DECISION_SCHEMA_VERSION = "context.blocking-decision/v2alpha1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UnattendedDispatcherError(ValueError):
    """A deterministic fail-closed dispatcher denial."""


class UnattendedRuntime(Protocol):
    """Authority-bearing ports used by the authority-free dispatcher."""

    def read(self) -> dict[str, Any]: ...

    def claim(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def compose(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def execute(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def verify(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def complete(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def block_and_release(self, intent: dict[str, Any]) -> dict[str, Any]: ...

    def trusted_now(self) -> str: ...

    def resolve_receipt(
        self, action: str, request_id: str
    ) -> dict[str, Any] | None: ...


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
        raise UnattendedDispatcherError("dispatcher value is not canonical") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise UnattendedDispatcherError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise UnattendedDispatcherError(f"{field} is invalid")
    return value


def _receipt(body: dict[str, Any], digest_field: str) -> dict[str, Any]:
    result = copy.deepcopy(body)
    result[digest_field] = _digest(result)
    return result


def _runtime_method(runtime: Any, name: str):
    method = getattr(runtime, name, None)
    if not callable(method):
        raise TypeError(f"runtime must provide {name}()")
    return method


def _bundle(runtime: UnattendedRuntime) -> dict[str, Any]:
    bundle = _runtime_method(runtime, "read")()
    if not isinstance(bundle, dict):
        raise UnattendedDispatcherError("runtime read must return an object")
    required = {
        "state",
        "governance_profile",
        "condition_decisions",
        "blocking_decisions",
    }
    if set(bundle) != required:
        raise UnattendedDispatcherError("runtime read fields are invalid")
    state = copy.deepcopy(bundle["state"])
    profile = copy.deepcopy(bundle["governance_profile"])
    conditions = copy.deepcopy(bundle["condition_decisions"])
    blockers = copy.deepcopy(bundle["blocking_decisions"])
    if not isinstance(conditions, list) or not isinstance(blockers, list):
        raise UnattendedDispatcherError("runtime decisions are invalid")
    try:
        validate_typed_state(state)
        validate_project_governance_profile(
            profile,
            observed_at=state["project"]["updated_at"],
        )
    except (TypedStateError, ProjectGovernanceProfileError, KeyError, TypeError) as exc:
        raise UnattendedDispatcherError(
            "current state or governance profile is invalid"
        ) from exc
    project = state["project"]
    profile_header = profile["profile"]
    if (
        project["project_id"] != profile_header["project_id"]
        or project["governance_ref"] != profile_header["governance_ref"]
    ):
        raise UnattendedDispatcherError("state and governance profile do not bind")
    return {
        "state": state,
        "governance_profile": profile,
        "condition_decisions": conditions,
        "blocking_decisions": blockers,
    }


def _verified_evidence_ids(state: dict[str, Any]) -> set[str]:
    return {
        item["evidence_id"]
        for item in state["evidence"]
        if item["validity"] == "verified" and item["verified_at"] is not None
    }


def _bound_blocking_decision(
    bundle: dict[str, Any], work_id: str
) -> dict[str, Any] | None:
    state = bundle["state"]
    project = state["project"]
    work_by_id = {item["work_id"]: item for item in state["works"]}
    blocker_by_id = {item["blocker_id"]: item for item in state["blockers"]}
    verified = _verified_evidence_ids(state)
    for candidate in bundle["blocking_decisions"]:
        try:
            if (
                candidate.get("schema_version")
                == CONDITION_BLOCKING_DECISION_SCHEMA_VERSION
                and candidate.get("blocker_kind")
                == "condition-evidence-unavailable"
            ):
                compatibility = copy.deepcopy(candidate)
                compatibility["schema_version"] = "context.blocking-decision/v1alpha1"
                compatibility["blocker_kind"] = (
                    "external-completion-evidence-unavailable"
                )
                canonical_blocking_decision_bytes(compatibility)
            else:
                canonical_blocking_decision_bytes(candidate)
        except (InputProgressionError, TypeError, KeyError):
            continue
        if work_id not in candidate["affected_work_ids"]:
            continue
        blocker = blocker_by_id.get(candidate["blocker_id"])
        if (
            candidate["project_id"] != project["project_id"]
            or candidate["project_revision"] != project["revision"]
            or blocker is None
            or blocker["status"] != "open"
            or blocker["reason"] != candidate["reason"]
            or set(blocker["blocked_work_ids"])
            != set(candidate["affected_work_ids"])
            or set(blocker["evidence_ids"]) != set(candidate["evidence_ids"])
            or not set(candidate["evidence_ids"]).issubset(verified)
        ):
            continue
        allowed_scopes = {
            (scope["scope_kind"], scope["scope_ref"])
            for affected_work_id in candidate["affected_work_ids"]
            for scope in work_by_id[affected_work_id]["scope_refs"]
        }
        requested_scopes = {
            (scope["scope_kind"], scope["scope_ref"])
            for scope in candidate["affected_scope_refs"]
        }
        if not requested_scopes or not requested_scopes.issubset(allowed_scopes):
            continue
        if candidate["blocker_kind"] == "condition-evidence-unavailable":
            obligation_by_work = {
                item["work_id"]: item
                for item in bundle["governance_profile"]["obligations"]
            }
            condition_refs = sorted(
                {
                    obligation_by_work[affected_work_id]["condition_ref"]
                    for affected_work_id in candidate["affected_work_ids"]
                    if affected_work_id in obligation_by_work
                    and obligation_by_work[affected_work_id]["mode"] == "conditional"
                    and obligation_by_work[affected_work_id]["condition_ref"] is not None
                }
            )
            if candidate["resume_condition"] != {
                "kind": "evidence",
                "refs": condition_refs,
            }:
                continue
        return copy.deepcopy(candidate)
    return None


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise UnattendedDispatcherError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnattendedDispatcherError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise UnattendedDispatcherError(f"{field} requires timezone")
    return parsed


def _condition_resolution(
    bundle: dict[str, Any], obligation: dict[str, Any]
) -> tuple[str | None, dict[str, Any] | None]:
    if obligation["mode"] != "conditional":
        return None, None
    project = bundle["state"]["project"]
    profile = bundle["governance_profile"]["profile"]
    verified = _verified_evidence_ids(bundle["state"])
    matches = [
        item
        for item in bundle["condition_decisions"]
        if isinstance(item, dict)
        and item.get("condition_ref") == obligation["condition_ref"]
    ]
    if len(matches) != 1:
        return "unknown", None
    decision = matches[0]
    expected_fields = {
        "schema_version",
        "decision_id",
        "condition_ref",
        "project_id",
        "project_revision",
        "profile_id",
        "governance_revision",
        "obligation_id",
        "obligation_revision",
        "outcome",
        "evidence_ids",
        "observed_at",
        "state_write_authority",
        "decision_sha256",
    }
    if set(decision) != expected_fields:
        return "unknown", None
    unsigned = {key: value for key, value in decision.items() if key != "decision_sha256"}
    if (
        decision["schema_version"] != CONDITION_DECISION_SCHEMA_VERSION
        or _ID_RE.fullmatch(decision["decision_id"]) is None
        or decision["outcome"] not in {"met", "not-met", "unknown"}
        or decision["project_id"] != project["project_id"]
        or decision["project_revision"] != project["revision"]
        or decision["profile_id"] != profile["profile_id"]
        or decision["governance_revision"] != profile["revision"]
        or decision["obligation_id"] != obligation["obligation_id"]
        or decision["obligation_revision"] != obligation["revision"]
        or not isinstance(decision["evidence_ids"], list)
        or not decision["evidence_ids"]
        or len(decision["evidence_ids"]) != len(set(decision["evidence_ids"]))
        or not set(decision["evidence_ids"]).issubset(verified)
        or decision["state_write_authority"] is not False
        or decision["decision_sha256"] != _digest(unsigned)
    ):
        return "unknown", None
    try:
        if _timestamp(decision["observed_at"], "condition observed_at") > _timestamp(
            project["updated_at"], "project updated_at"
        ):
            return "unknown", None
    except UnattendedDispatcherError:
        return "unknown", None
    return decision["outcome"], copy.deepcopy(decision)


def _classify(bundle: dict[str, Any], *, actor_ref: str) -> dict[str, Any]:
    state = bundle["state"]
    profile = bundle["governance_profile"]
    work_by_id = {item["work_id"]: item for item in state["works"]}
    source_by_work = {item["work_id"]: item for item in profile["work_sources"]}
    obligations = {item["work_id"]: item for item in profile["obligations"]}
    optional_ids = sorted(
        item["work_id"] for item in profile["obligations"] if item["mode"] == "optional"
    )
    completed_ids: list[str] = []
    not_applicable_ids: list[str] = []
    condition_decisions: list[dict[str, Any]] = []
    active_claims_by_work: dict[str, list[dict[str, Any]]] = {}
    for claim in state["claims"]:
        if claim["status"] == "active":
            active_claims_by_work.setdefault(claim["work_id"], []).append(claim)
    candidates: list[
        tuple[
            dict[str, Any],
            dict[str, Any],
            dict[str, Any] | None,
            dict[str, Any] | None,
        ]
    ] = []
    blocked_required: list[str] = []
    for work_id, obligation in obligations.items():
        work = work_by_id.get(work_id)
        source = source_by_work.get(work_id)
        if work is None or source is None:
            raise UnattendedDispatcherError("obligation Work projection is missing")
        if set(work["dependency_ids"]) != set(source["dependency_ids"]):
            raise UnattendedDispatcherError("state and source dependencies differ")
        if obligation["mode"] == "optional":
            continue
        if obligation["status"] in {"satisfied", "waived", "expired"}:
            if work["status"] == "completed":
                completed_ids.append(work_id)
            continue
        if work["status"] == "completed":
            completed_ids.append(work_id)
            continue
        condition, condition_decision = _condition_resolution(bundle, obligation)
        if condition == "not-met":
            not_applicable_ids.append(work_id)
            condition_decisions.append(condition_decision)
            continue
        if condition == "unknown":
            blocked_required.append(work_id)
            continue
        if obligation["automation_class"] != "autonomous":
            blocked_required.append(work_id)
            continue
        dependencies_ready = all(
            work_by_id[dependency_id]["status"] == "completed"
            for dependency_id in work["dependency_ids"]
        )
        if (
            work["status"] == "ready"
            and source["readiness"] == "ready"
            and dependencies_ready
            and not work["blocker_ids"]
        ):
            candidates.append((work, obligation, None, condition_decision))
        elif work["status"] in {"active", "verifying"}:
            active_claims = active_claims_by_work.get(work_id, [])
            if (
                len(active_claims) == 1
                and active_claims[0]["actor_ref"] == actor_ref
                and dependencies_ready
                and not work["blocker_ids"]
            ):
                candidates.append(
                    (work, obligation, active_claims[0], condition_decision)
                )
            else:
                blocked_required.append(work_id)
        elif work["status"] == "blocked" or work["blocker_ids"]:
            blocked_required.append(work_id)
    if candidates:
        work, obligation, active_claim, condition_decision = min(
            candidates, key=lambda item: item[0]["work_id"]
        )
        return {
            "action": "execute",
            "work": work,
            "obligation": obligation,
            "active_claim": active_claim,
            "condition_decision": condition_decision,
            "completed_work_ids": sorted(completed_ids),
            "conditional_not_applicable_work_ids": sorted(not_applicable_ids),
            "remaining_optional_work_ids": optional_ids,
            "condition_decisions": sorted(
                condition_decisions, key=lambda item: item["decision_id"]
            ),
        }
    if blocked_required:
        for work_id in sorted(set(blocked_required)):
            decision = _bound_blocking_decision(bundle, work_id)
            if decision is not None:
                return {
                    "action": "blocked",
                    "blocking_decision": decision,
                    "completed_work_ids": sorted(completed_ids),
                    "conditional_not_applicable_work_ids": sorted(
                        not_applicable_ids
                    ),
                    "remaining_optional_work_ids": optional_ids,
                    "condition_decisions": sorted(
                        condition_decisions, key=lambda item: item["decision_id"]
                    ),
                }
        raise UnattendedDispatcherError(
            "pending required Work needs a current typed blocker and resume condition"
        )
    pending_required = [
        item["work_id"]
        for item in profile["obligations"]
        if item["mode"] != "optional"
        and item["status"] == "pending"
        and item["work_id"] not in completed_ids
        and item["work_id"] not in not_applicable_ids
    ]
    if pending_required:
        raise UnattendedDispatcherError(
            "pending required Work has no ready autonomous leaf or typed blocker"
        )
    return {
        "action": "complete",
        "completed_work_ids": sorted(completed_ids),
        "conditional_not_applicable_work_ids": sorted(not_applicable_ids),
        "remaining_optional_work_ids": optional_ids,
        "condition_decisions": sorted(
            condition_decisions, key=lambda item: item["decision_id"]
        ),
    }


class UnattendedDispatcher:
    """Drive one campaign through authority-bearing, hash-bound runtime ports."""

    def __init__(self, runtime: UnattendedRuntime, *, actor_ref: str) -> None:
        self.runtime = runtime
        self.actor_ref = _id(actor_ref, "actor_ref")
        self.campaign_run_id = _id(
            getattr(runtime, "campaign_run_id", None), "campaign_run_id"
        )
        self.cursor_store = getattr(runtime, "cursor_store", None)
        if not callable(getattr(self.cursor_store, "read", None)) or not callable(
            getattr(self.cursor_store, "compare_and_set", None)
        ):
            raise TypeError("runtime must provide a durable cursor_store")
        for method in (
            "read",
            "claim",
            "compose",
            "execute",
            "verify",
            "complete",
            "block_and_release",
            "trusted_now",
            "resolve_receipt",
        ):
            _runtime_method(runtime, method)

    def _trusted_now(self) -> datetime:
        try:
            value = _runtime_method(self.runtime, "trusted_now")()
        except Exception as exc:
            raise UnattendedDispatcherError("trusted runtime time is unavailable") from exc
        return _timestamp(value, "trusted runtime time")

    def _resolve_receipt(
        self, action: str, request_id: str
    ) -> dict[str, Any] | None:
        try:
            resolved = _runtime_method(self.runtime, "resolve_receipt")(
                action, request_id
            )
        except Exception as exc:
            raise UnattendedDispatcherError(
                "durable port receipt lookup failed"
            ) from exc
        store = getattr(self.runtime, "receipt_store", None)
        store_read = getattr(store, "read", None)
        stored = None
        if callable(store_read):
            try:
                stored = store_read(action, request_id)
            except Exception as exc:
                raise UnattendedDispatcherError(
                    "durable receipt store lookup failed"
                ) from exc
        if resolved is not None and not isinstance(resolved, dict):
            raise UnattendedDispatcherError("durable port receipt is invalid")
        if stored is not None and not isinstance(stored, dict):
            raise UnattendedDispatcherError("durable receipt store value is invalid")
        if resolved is not None and stored is not None and resolved != stored:
            raise UnattendedDispatcherError("durable port receipt stores disagree")
        return copy.deepcopy(resolved if resolved is not None else stored)

    def _call_port(self, action: str, intent: dict[str, Any]) -> dict[str, Any]:
        request_id = _id(intent.get("request_id"), f"{action} request_id")
        resolved = self._resolve_receipt(action, request_id)
        if resolved is not None:
            return resolved
        method_name = "block_and_release" if action == "block" else action
        result = _runtime_method(self.runtime, method_name)(copy.deepcopy(intent))
        if not isinstance(result, dict):
            raise UnattendedDispatcherError(f"{action} port receipt is invalid")
        store = getattr(self.runtime, "receipt_store", None)
        store_write = getattr(store, "write", None)
        if callable(store_write):
            try:
                store_write(action, request_id, result)
            except Exception as exc:
                raise UnattendedDispatcherError(
                    f"{action} durable receipt store write failed"
                ) from exc
        committed = self._resolve_receipt(action, request_id)
        if committed is None or committed != result:
            raise UnattendedDispatcherError(
                f"{action} port receipt was not durably committed"
            )
        return committed

    def _assert_claim_lease(self, claim: dict[str, Any]) -> None:
        try:
            expires_at = _timestamp(claim["lease_expires_at"], "claim lease_expires_at")
        except (KeyError, TypeError) as exc:
            raise UnattendedDispatcherError("campaign cursor active claim is invalid") from exc
        if expires_at <= self._trusted_now():
            raise UnattendedDispatcherError("campaign cursor claim lease expired")

    @staticmethod
    def _operation_id(prefix: str, *parts: Any) -> str:
        return f"m8-09-{prefix}-{_digest(parts)[:24]}"

    def _commit_cursor(
        self,
        cursor: dict[str, Any],
        **changes: Any,
    ) -> dict[str, Any]:
        candidate = evolve_campaign_cursor(cursor, **changes)
        try:
            return self.cursor_store.compare_and_set(
                self.campaign_run_id,
                expected_cursor_revision=cursor["cursor_revision"],
                cursor=candidate,
            )
        except UnattendedCursorConflict as exc:
            raise UnattendedDispatcherError("campaign cursor CAS conflict") from exc

    def _read_or_create_cursor(self, bundle: dict[str, Any]) -> dict[str, Any]:
        state = bundle["state"]
        project = state["project"]
        profile = bundle["governance_profile"]["profile"]
        cursor = self.cursor_store.read(self.campaign_run_id)
        if cursor is None:
            if any(claim["status"] == "active" for claim in state["claims"]):
                raise UnattendedDispatcherError(
                    "active claim has no prepared cursor"
                )
            initial = build_campaign_cursor(
                campaign_run_id=self.campaign_run_id,
                project_id=project["project_id"],
                profile_id=profile["profile_id"],
                governance_revision=profile["revision"],
                start_project_revision=project["revision"],
            )
            try:
                return self.cursor_store.compare_and_set(
                    self.campaign_run_id,
                    expected_cursor_revision=None,
                    cursor=initial,
                )
            except UnattendedCursorConflict as exc:
                raise UnattendedDispatcherError("campaign cursor create conflict") from exc
        if (
            cursor["project_id"] != project["project_id"]
            or cursor["profile_id"] != profile["profile_id"]
            or cursor["governance_revision"] != profile["revision"]
        ):
            raise UnattendedDispatcherError(
                "campaign cursor does not bind current governance"
            )
        return cursor

    def _prepare_selection(
        self,
        cursor: dict[str, Any],
        bundle: dict[str, Any],
        classification: dict[str, Any],
    ) -> dict[str, Any]:
        project = bundle["state"]["project"]
        profile = bundle["governance_profile"]["profile"]
        work = classification["work"]
        obligation = classification["obligation"]
        identity = (
            self.campaign_run_id,
            project["project_id"],
            cursor["current_project_revision"],
            profile["profile_id"],
            profile["revision"],
            obligation["obligation_id"],
            obligation["revision"],
            work["work_id"],
            work["revision"],
            1,
        )
        selection_id = self._operation_id("selection", *identity)
        attempt_id = self._operation_id("attempt", selection_id, 1)
        active_claim = classification["active_claim"]
        expected_claim_id = self._operation_id(
            "claim-record", selection_id, attempt_id
        )
        if active_claim is not None and active_claim.get("claim_id") != expected_claim_id:
            raise UnattendedDispatcherError(
                "active claim is not bound to the current campaign"
            )
        claim_id = expected_claim_id
        request_ids = {
            name: self._operation_id(name, selection_id, attempt_id)
            for name in (
                "claim",
                "packet",
                "execute",
                "verify",
                "complete",
                "block",
            )
        }
        selection = {
            "selection_id": selection_id,
            "attempt_id": attempt_id,
            "attempt_no": 1,
            "attempt_budget": 1,
            "project_revision_before": cursor["current_project_revision"],
            "work_id": work["work_id"],
            "work_revision": work["revision"],
            "scope_owners": copy.deepcopy(work["scope_refs"]),
            "obligation_id": obligation["obligation_id"],
            "obligation_revision": obligation["revision"],
            "verification_profile_ref": obligation["verification_profile_ref"],
            "condition_decision": copy.deepcopy(
                classification["condition_decision"]
            ),
            "claim_id": claim_id,
            "request_ids": request_ids,
            "claim": (
                {
                    "status": "accepted",
                    "operation": "adopt",
                    "project_revision": project["revision"],
                    "claim": copy.deepcopy(active_claim),
                }
                if active_claim is not None
                else None
            ),
            "packet": None,
            "execution": None,
            "verification": None,
            "completion": None,
            "block": None,
        }
        if active_claim is not None:
            return self._commit_cursor(
                cursor,
                phase="claimed",
                next_action="compose",
                current_project_revision=project["revision"],
                selection=selection,
            )
        return self._commit_cursor(
            cursor, phase="prepared", next_action="claim", selection=selection
        )

    @staticmethod
    def _selection_bindings(
        cursor: dict[str, Any],
        bundle: dict[str, Any],
        *,
        actor_ref: str,
        trusted_now: datetime,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        selection = cursor["selection"]
        if not isinstance(selection, dict):
            raise UnattendedDispatcherError("campaign cursor selection is missing")
        work = next(
            (
                item
                for item in bundle["state"]["works"]
                if item["work_id"] == selection.get("work_id")
            ),
            None,
        )
        obligation = next(
            (
                item
                for item in bundle["governance_profile"]["obligations"]
                if item["obligation_id"] == selection.get("obligation_id")
            ),
            None,
        )
        if (
            work is None
            or obligation is None
            or not (
                work["revision"] == selection.get("work_revision")
                or (
                    cursor["phase"] == "completed"
                    and work["status"] == "completed"
                    and work["revision"] == selection.get("work_revision") + 1
                )
            )
            or obligation["work_id"] != work["work_id"]
            or obligation["revision"] != selection.get("obligation_revision")
            or obligation["verification_profile_ref"]
            != selection.get("verification_profile_ref")
        ):
            raise UnattendedDispatcherError(
                "campaign cursor selection does not bind current Work"
            )
        if cursor["phase"] in {
            "claimed",
            "composed",
            "executed",
            "verified",
            "verification_failed",
        }:
            persisted_claim = selection.get("claim", {}).get("claim")
            active_claims = [
                item
                for item in bundle["state"]["claims"]
                if item["status"] == "active" and item["work_id"] == work["work_id"]
            ]
            if (
                not isinstance(persisted_claim, dict)
                or len(active_claims) != 1
                or any(
                    active_claims[0].get(field) != persisted_claim.get(field)
                    for field in (
                        "claim_id",
                        "work_id",
                        "actor_ref",
                        "claim_revision",
                        "lease_epoch",
                        "scope_owners",
                    )
                )
                or active_claims[0].get("actor_ref") != actor_ref
                or active_claims[0].get("expected_project_revision")
                != bundle["state"]["project"]["revision"]
            ):
                raise UnattendedDispatcherError(
                    "campaign cursor active claim no longer matches authority"
                )
            try:
                if _timestamp(
                    active_claims[0]["lease_expires_at"], "claim lease_expires_at"
                ) <= trusted_now:
                    raise UnattendedDispatcherError("campaign cursor claim lease expired")
            except (KeyError, TypeError) as exc:
                raise UnattendedDispatcherError(
                    "campaign cursor active claim is invalid"
                ) from exc
        return selection, work, obligation

    @staticmethod
    def _validate_completion_receipt(
        completed: Any, selection: dict[str, Any]
    ) -> dict[str, Any]:
        claim = selection["claim"]
        verification = selection["verification"]
        expected_project_revision = claim["project_revision"] + 1
        if (
            not isinstance(completed, dict)
            or completed.get("status") != "accepted"
            or completed.get("operation") != "complete_work"
            or completed.get("project_revision") != expected_project_revision
            or completed.get("work", {}).get("work_id") != selection["work_id"]
            or completed.get("work", {}).get("status") != "completed"
            or completed.get("work", {}).get("revision")
            != selection["work_revision"] + 1
            or completed.get("claim", {}).get("claim_id") != selection["claim_id"]
            or completed.get("claim", {}).get("status") != "released"
            or completed.get("verification_decision_sha256")
            != verification.get("verification_decision_sha256")
            or completed.get("claim_evidence_verdict_sha256")
            != verification.get("claim_evidence_verdict_sha256")
        ):
            raise UnattendedDispatcherError("completion receipt binding is invalid")
        nested = completed.get("receipt")
        if not isinstance(nested, dict):
            raise UnattendedDispatcherError("completion receipt binding is invalid")
        unsigned = {key: value for key, value in nested.items() if key != "receipt_sha256"}
        if nested.get("receipt_sha256") != _digest(unsigned):
            raise UnattendedDispatcherError("completion receipt digest is invalid")
        return copy.deepcopy(completed)

    @staticmethod
    def _validate_block_receipt(
        blocked: Any, selection: dict[str, Any]
    ) -> dict[str, Any]:
        claim = selection["claim"]
        verification = selection["verification"]
        blocked_unsigned = (
            {key: value for key, value in blocked.items() if key != "receipt_sha256"}
            if isinstance(blocked, dict)
            else {}
        )
        if (
            not isinstance(blocked, dict)
            or blocked.get("status") != "blocked"
            or blocked.get("work_id") != selection["work_id"]
            or blocked.get("project_revision") != claim["project_revision"] + 1
            or blocked.get("claim", {}).get("claim_id") != selection["claim_id"]
            or blocked.get("claim", {}).get("status") != "released"
            or blocked.get("work", {}).get("work_id") != selection["work_id"]
            or blocked.get("work", {}).get("status") != "blocked"
            or blocked.get("work", {}).get("revision")
            != selection["work_revision"] + 1
            or blocked.get("blocker", {}).get("blocker_id")
            != blocked.get("blocker_id")
            or blocked.get("blocker", {}).get("status") != "open"
            or blocked.get("blocker", {}).get("blocked_work_ids")
            != [selection["work_id"]]
            or blocked.get("blocker", {}).get("evidence_ids")
            != sorted(verification.get("evidence_ids", []))
            or blocked.get("attempt_id") != selection["attempt_id"]
            or blocked.get("attempt_no") != selection["attempt_no"]
            or blocked.get("attempt_budget") != selection["attempt_budget"]
            or blocked.get("verification_decision_sha256")
            != verification.get("verification_decision_sha256")
            or blocked.get("evidence_ids")
            != sorted(verification.get("evidence_ids", []))
            or not isinstance(blocked.get("resume_condition"), dict)
            or not blocked["resume_condition"].get("refs")
            or blocked.get("state_write_authority") is not False
            or blocked.get("completion_authority") is not False
            or blocked.get("provider_authority") != 0
            or blocked.get("external_effect_authority") != 0
            or blocked.get("receipt_sha256") != _digest(blocked_unsigned)
        ):
            raise UnattendedDispatcherError(
                "failed verification was not atomically blocked and released"
            )
        return copy.deepcopy(blocked)

    @staticmethod
    def _campaign_block_receipt(
        blocked: dict[str, Any], bundle: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            **copy.deepcopy(blocked),
            "completed_work_ids": sorted(
                item["work_id"]
                for item in bundle["state"]["works"]
                if item["status"] == "completed"
            ),
            "conditional_not_applicable_work_ids": [],
            "remaining_optional_work_ids": sorted(
                item["work_id"]
                for item in bundle["governance_profile"]["obligations"]
                if item["mode"] == "optional"
            ),
        }

    def _reconcile_committed_port(
        self, cursor: dict[str, Any], bundle: dict[str, Any]
    ) -> dict[str, Any]:
        selection = cursor.get("selection")
        if not isinstance(selection, dict):
            return cursor
        if cursor["phase"] == "verified":
            completed = self._resolve_receipt(
                "complete", selection["request_ids"]["complete"]
            )
            if completed is not None:
                completed = self._validate_completion_receipt(completed, selection)
                reconciled = copy.deepcopy(selection)
                reconciled["completion"] = completed
                return self._commit_cursor(
                    cursor,
                    phase="completed",
                    next_action="record_step",
                    current_project_revision=completed["project_revision"],
                    selection=reconciled,
                )
        if cursor["phase"] == "verification_failed":
            blocked = self._resolve_receipt("block", selection["request_ids"]["block"])
            if blocked is not None:
                blocked = self._validate_block_receipt(blocked, selection)
                reconciled = copy.deepcopy(selection)
                reconciled["block"] = self._campaign_block_receipt(blocked, bundle)
                return self._commit_cursor(
                    cursor,
                    phase="blocked",
                    next_action="terminal",
                    current_project_revision=blocked["project_revision"],
                    selection=reconciled,
                )
        return cursor

    def step(self) -> dict[str, Any]:
        bundle = _bundle(self.runtime)
        project = bundle["state"]["project"]
        profile = bundle["governance_profile"]["profile"]
        cursor = self._read_or_create_cursor(bundle)
        if cursor["phase"] == "closed":
            return copy.deepcopy(cursor["terminal_receipt"])
        cursor = self._reconcile_committed_port(cursor, bundle)
        if cursor["phase"] == "blocked":
            block = cursor["selection"].get("block")
            if not isinstance(block, dict):
                raise UnattendedDispatcherError("blocked cursor receipt is missing")
            return copy.deepcopy(block)
        classification: dict[str, Any] | None = None
        if cursor["phase"] == "selecting":
            classification = _classify(bundle, actor_ref=self.actor_ref)
            if classification["action"] != "execute":
                return copy.deepcopy(classification)
            cursor = self._prepare_selection(cursor, bundle, classification)
        selection, work, obligation = self._selection_bindings(
            cursor,
            bundle,
            actor_ref=self.actor_ref,
            trusted_now=self._trusted_now(),
        )
        claim_id = selection["claim_id"]

        if cursor["phase"] == "prepared":
            claim = self._call_port(
                "claim",
                {
                    "request_id": selection["request_ids"]["claim"],
                    "project_id": project["project_id"],
                    "expected_project_revision": selection[
                        "project_revision_before"
                    ],
                    "work_id": work["work_id"],
                    "work_revision": work["revision"],
                    "claim_id": claim_id,
                    "scope_owners": copy.deepcopy(selection["scope_owners"]),
                    "actor_ref": self.actor_ref,
                }
            )
            if not isinstance(claim, dict) or claim.get("status") != "accepted":
                raise UnattendedDispatcherError("claim port did not accept the Work")
            claim_record = claim.get("claim")
            if (
                not isinstance(claim_record, dict)
                or claim_record.get("claim_id") != claim_id
                or claim_record.get("work_id") != work["work_id"]
                or claim_record.get("actor_ref") != self.actor_ref
                or claim_record.get("status") != "active"
                or claim.get("project_revision")
                != claim_record.get("expected_project_revision")
            ):
                raise UnattendedDispatcherError("claim receipt binding is invalid")
            selection = copy.deepcopy(selection)
            selection["claim"] = copy.deepcopy(claim)
            cursor = self._commit_cursor(
                cursor,
                phase="claimed",
                next_action="compose",
                current_project_revision=claim["project_revision"],
                selection=selection,
            )
        selection = cursor["selection"]
        claim = selection["claim"]
        claim_record = claim["claim"]
        claim_revision = claim_record["claim_revision"]
        lease_epoch = claim_record["lease_epoch"]
        if type(claim_revision) is not int or type(lease_epoch) is not int:
            raise UnattendedDispatcherError("claim receipt tokens are invalid")
        self._assert_claim_lease(claim_record)

        if cursor["phase"] == "claimed":
            packet = self._call_port(
                "compose",
                {
                "request_id": selection["request_ids"]["packet"],
                "project_id": project["project_id"],
                "project_revision": claim["project_revision"],
                "work_id": work["work_id"],
                "work_revision": work["revision"],
                "claim_id": claim_id,
                "claim_revision": claim_revision,
                "lease_epoch": lease_epoch,
                "fence": lease_epoch,
                "verification_profile_ref": obligation["verification_profile_ref"],
                }
            )
            if (
                not isinstance(packet, dict)
                or packet.get("status") != "accepted"
                or packet.get("project_id") != project["project_id"]
                or packet.get("project_revision") != claim["project_revision"]
                or packet.get("work_id") != work["work_id"]
                or packet.get("work_revision") != work["revision"]
                or packet.get("claim_id") != claim_id
                or packet.get("lease_epoch") != lease_epoch
                or packet.get("fence") != lease_epoch
            ):
                raise UnattendedDispatcherError("Execution Packet binding is invalid")
            selection = copy.deepcopy(selection)
            selection["packet"] = copy.deepcopy(packet)
            cursor = self._commit_cursor(
                cursor,
                phase="composed",
                next_action="execute",
                selection=selection,
            )
        selection = cursor["selection"]
        packet = selection["packet"]
        packet_sha256 = _sha256(packet.get("packet_sha256"), "packet_sha256")

        if cursor["phase"] == "composed":
            self._assert_claim_lease(claim_record)
            execution = self._call_port(
                "execute",
                {
                "request_id": selection["request_ids"]["execute"],
                "project_id": project["project_id"],
                "project_revision": claim["project_revision"],
                "work_id": work["work_id"],
                "work_revision": work["revision"],
                "claim_id": claim_id,
                "claim_revision": claim_revision,
                "lease_epoch": lease_epoch,
                "fence": lease_epoch,
                "packet_sha256": packet_sha256,
                }
            )
            if (
                not isinstance(execution, dict)
                or execution.get("status") != "succeeded"
                or execution.get("work_id") != work["work_id"]
                or execution.get("packet_sha256") != packet_sha256
                or execution.get("claim_id") != claim_id
                or execution.get("lease_epoch") != lease_epoch
                or execution.get("fence") != lease_epoch
            ):
                raise UnattendedDispatcherError("execution receipt binding is invalid")
            selection = copy.deepcopy(selection)
            selection["execution"] = copy.deepcopy(execution)
            cursor = self._commit_cursor(
                cursor,
                phase="executed",
                next_action="verify",
                selection=selection,
            )
        selection = cursor["selection"]
        execution = selection["execution"]
        execution_sha256 = _sha256(
            execution.get("execution_sha256"), "execution_sha256"
        )

        if cursor["phase"] == "executed":
            self._assert_claim_lease(claim_record)
            verification = self._call_port(
                "verify",
                {
                "request_id": selection["request_ids"]["verify"],
                "project_id": project["project_id"],
                "project_revision": claim["project_revision"],
                "work_id": work["work_id"],
                "work_revision": work["revision"],
                "claim_id": claim_id,
                "claim_revision": claim_revision,
                "lease_epoch": lease_epoch,
                "fence": lease_epoch,
                "execution_sha256": execution_sha256,
                "verification_profile_ref": obligation["verification_profile_ref"],
                }
            )
            if not isinstance(verification, dict):
                raise UnattendedDispatcherError("verification receipt is invalid")
            selection = copy.deepcopy(selection)
            selection["verification"] = copy.deepcopy(verification)
            next_action = (
                "complete"
                if verification.get("decision") == "satisfied"
                else "block_and_release"
            )
            cursor = self._commit_cursor(
                cursor,
                phase=(
                    "verified"
                    if next_action == "complete"
                    else "verification_failed"
                ),
                next_action=next_action,
                selection=selection,
            )
        selection = cursor["selection"]
        verification = selection["verification"]

        if cursor["phase"] == "verification_failed":
            self._assert_claim_lease(claim_record)
            blocked = self._call_port(
                "block",
                {
                    "request_id": selection["request_ids"]["block"],
                    "project_id": project["project_id"],
                    "expected_project_revision": claim["project_revision"],
                    "work_id": work["work_id"],
                    "expected_work_revision": work["revision"],
                    "claim_id": claim_id,
                    "expected_claim_revision": claim_revision,
                    "lease_epoch": lease_epoch,
                    "fence": lease_epoch,
                    "attempt_id": selection["attempt_id"],
                    "attempt_no": selection["attempt_no"],
                    "attempt_budget": selection["attempt_budget"],
                    "verification": copy.deepcopy(verification),
                }
            )
            blocked = self._validate_block_receipt(blocked, selection)
            block_receipt = self._campaign_block_receipt(blocked, bundle)
            selection = copy.deepcopy(selection)
            selection["block"] = copy.deepcopy(block_receipt)
            self._commit_cursor(
                cursor,
                phase="blocked",
                next_action="terminal",
                current_project_revision=blocked["project_revision"],
                selection=selection,
            )
            return block_receipt

        evidence_ids = verification.get("evidence_ids")
        if (
            not isinstance(evidence_ids, list)
            or not evidence_ids
            or len(evidence_ids) != len(set(evidence_ids))
            or verification.get("work_id") != work["work_id"]
            or verification.get("execution_sha256") != execution_sha256
            or verification.get("verifier_ref") == self.actor_ref
            or verification.get("completion_authority") is not False
        ):
            raise UnattendedDispatcherError("verification receipt binding is invalid")
        verification_sha256 = _sha256(
            verification.get("verification_decision_sha256"),
            "verification_decision_sha256",
        )
        claim_evidence_sha256 = _sha256(
            verification.get("claim_evidence_verdict_sha256"),
            "claim_evidence_verdict_sha256",
        )
        if cursor["phase"] == "verified":
            self._assert_claim_lease(claim_record)
            completed = self._call_port(
                "complete",
                {
                "request_id": selection["request_ids"]["complete"],
                "project_id": project["project_id"],
                "expected_project_revision": claim["project_revision"],
                "work_id": work["work_id"],
                "expected_work_revision": work["revision"],
                "claim_id": claim_id,
                "expected_claim_revision": claim_revision,
                "lease_epoch": lease_epoch,
                "fence": lease_epoch,
                "evidence_ids": sorted(evidence_ids),
                "verification_decision_sha256": verification_sha256,
                "claim_evidence_verdict_sha256": claim_evidence_sha256,
                }
            )
            completed = self._validate_completion_receipt(completed, selection)
            selection = copy.deepcopy(selection)
            selection["completion"] = copy.deepcopy(completed)
            cursor = self._commit_cursor(
                cursor,
                phase="completed",
                next_action="record_step",
                current_project_revision=completed["project_revision"],
                selection=selection,
            )
        selection = cursor["selection"]
        completed = selection["completion"]
        step_body = {
            "schema_version": STEP_SCHEMA_VERSION,
            "status": "completed",
            "project_id": project["project_id"],
            "project_revision_before": selection["project_revision_before"],
            "project_revision_after": completed["project_revision"],
            "profile_id": profile["profile_id"],
            "governance_revision": profile["revision"],
            "obligation_id": obligation["obligation_id"],
            "work_id": work["work_id"],
            "work_revision_before": selection["work_revision"],
            "work_revision_after": completed["work"]["revision"],
            "claim_id": claim_id,
            "lease_epoch": lease_epoch,
            "fence": lease_epoch,
            "packet_sha256": packet_sha256,
            "execution_sha256": execution_sha256,
            "verification_decision_sha256": verification_sha256,
            "claim_evidence_verdict_sha256": claim_evidence_sha256,
            "completion_receipt_sha256": completed["receipt"]["receipt_sha256"],
            "evidence_ids": sorted(evidence_ids),
            "condition_decision_sha256": (
                selection["condition_decision"]["decision_sha256"]
                if selection["condition_decision"] is not None
                else None
            ),
            "condition_evidence_ids": (
                sorted(selection["condition_decision"]["evidence_ids"])
                if selection["condition_decision"] is not None
                else []
            ),
            "state_write_authority": False,
            "completion_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        }
        try:
            step_receipt = validate_unattended_dispatch_step(
                _receipt(step_body, "step_sha256")
            )
        except UnattendedReceiptError as exc:
            raise UnattendedDispatcherError(
                "unattended dispatch step is invalid"
            ) from exc
        completed_steps = copy.deepcopy(cursor["completed_steps"])
        if not any(
            item.get("step_sha256") == step_receipt["step_sha256"]
            for item in completed_steps
        ):
            completed_steps.append(step_receipt)
        self._commit_cursor(
            cursor,
            phase="selecting",
            next_action="select",
            selection=None,
            completed_steps=completed_steps,
        )
        return step_receipt

    def run(self, *, max_steps: int) -> dict[str, Any]:
        if type(max_steps) is not int or max_steps <= 0:
            raise UnattendedDispatcherError("max_steps must be positive")
        new_step_count = 0
        for _ in range(max_steps + 1):
            outcome = self.step()
            if outcome.get("schema_version") == CAMPAIGN_RECEIPT_SCHEMA_VERSION:
                return copy.deepcopy(outcome)
            action = outcome["action"] if "action" in outcome else outcome["status"]
            if action == "completed" and outcome.get("schema_version") == STEP_SCHEMA_VERSION:
                new_step_count += 1
                if new_step_count > max_steps:
                    raise UnattendedDispatcherError(
                        "max_steps reached before required closure"
                    )
                continue
            if action == "complete":
                cursor = self.cursor_store.read(self.campaign_run_id)
                if cursor is None or cursor["phase"] != "selecting":
                    raise UnattendedDispatcherError(
                        "campaign cursor is not ready for closure"
                    )
                body = {
                    "schema_version": CAMPAIGN_RECEIPT_SCHEMA_VERSION,
                    "status": "completed",
                    "campaign_run_id": self.campaign_run_id,
                    "project_id": cursor["project_id"],
                    "profile_id": cursor["profile_id"],
                    "governance_revision": cursor["governance_revision"],
                    "start_project_revision": cursor["start_project_revision"],
                    "end_project_revision": cursor["current_project_revision"],
                    "completed_work_ids": outcome["completed_work_ids"],
                    "conditional_not_applicable_work_ids": outcome[
                        "conditional_not_applicable_work_ids"
                    ],
                    "remaining_optional_work_ids": outcome[
                        "remaining_optional_work_ids"
                    ],
                    "steps": copy.deepcopy(cursor["completed_steps"]),
                    "condition_decisions": copy.deepcopy(
                        outcome["condition_decisions"]
                    ),
                    "blocker_id": None,
                    "evidence_ids": [],
                    "resume_condition": None,
                    "state_write_authority": False,
                    "completion_authority": False,
                    "provider_authority": 0,
                    "external_effect_authority": 0,
                }
                try:
                    receipt = validate_unattended_campaign_receipt(
                        _receipt(body, "receipt_sha256")
                    )
                except UnattendedReceiptError as exc:
                    raise UnattendedDispatcherError(
                        "unattended campaign receipt is invalid"
                    ) from exc
                committed = self._commit_cursor(
                    cursor,
                    phase="closed",
                    next_action="terminal",
                    terminal_receipt=receipt,
                )
                return copy.deepcopy(committed["terminal_receipt"])
            if action == "blocked":
                cursor = self.cursor_store.read(self.campaign_run_id)
                if cursor is None:
                    raise UnattendedDispatcherError("campaign cursor is missing")
                decision = outcome.get("blocking_decision")
                blocker_id = (
                    decision["blocker_id"] if decision is not None else outcome["blocker_id"]
                )
                evidence_ids = (
                    decision["evidence_ids"]
                    if decision is not None
                    else outcome["evidence_ids"]
                )
                resume_condition = (
                    decision["resume_condition"]
                    if decision is not None
                    else outcome["resume_condition"]
                )
                body = {
                    "schema_version": CAMPAIGN_RECEIPT_SCHEMA_VERSION,
                    "status": "blocked",
                    "campaign_run_id": self.campaign_run_id,
                    "project_id": cursor["project_id"],
                    "profile_id": cursor["profile_id"],
                    "governance_revision": cursor["governance_revision"],
                    "start_project_revision": cursor["start_project_revision"],
                    "end_project_revision": cursor["current_project_revision"],
                    "completed_work_ids": outcome["completed_work_ids"],
                    "conditional_not_applicable_work_ids": outcome[
                        "conditional_not_applicable_work_ids"
                    ],
                    "remaining_optional_work_ids": outcome[
                        "remaining_optional_work_ids"
                    ],
                    "steps": copy.deepcopy(cursor["completed_steps"]),
                    "condition_decisions": copy.deepcopy(
                        outcome.get("condition_decisions", [])
                    ),
                    "blocker_id": blocker_id,
                    "evidence_ids": sorted(evidence_ids),
                    "resume_condition": copy.deepcopy(resume_condition),
                    "state_write_authority": False,
                    "completion_authority": False,
                    "provider_authority": 0,
                    "external_effect_authority": 0,
                }
                try:
                    return validate_unattended_campaign_receipt(
                        _receipt(body, "receipt_sha256")
                    )
                except UnattendedReceiptError as exc:
                    raise UnattendedDispatcherError(
                        "unattended campaign receipt is invalid"
                    ) from exc
            raise UnattendedDispatcherError("dispatcher step returned an invalid action")
        raise UnattendedDispatcherError("dispatcher did not reach a terminal state")


__all__ = [
    "CAMPAIGN_RECEIPT_SCHEMA_VERSION",
    "STEP_SCHEMA_VERSION",
    "UnattendedDispatcher",
    "UnattendedDispatcherError",
    "UnattendedRuntime",
]
