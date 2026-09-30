"""Provider-neutral claim, lease, and scope ownership reference core.

The class in this module deliberately keeps storage in memory.  SQLite, PostgreSQL,
an offline outbox, and provider adapters can use the same transition contract without
giving any of them implicit authority over a worker's fence.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .effect_scope_gate import scope_covers, scopes_overlap, validate_scope

WORK_LEDGER_SCHEMA_VERSION = "context.work-ledger/v1alpha1"
TRANSITION_SCHEMA_VERSION = "context.work-claim-transition/v1alpha1"
RECEIPT_SCHEMA_VERSION = "context.work-claim-receipt/v1alpha1"
VERDICT_SCHEMA_VERSION = "context.work-dispatch-verdict/v1alpha1"

_TERMINAL_WORK_STATUSES = {"completed", "rejected", "reverted", "superseded"}
_CLAIMABLE_WORK_STATUSES = {"ready", "active", "verifying"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ClaimLifecycleError(ValueError):
    """A deterministic denial that leaves the ledger unchanged."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


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
        raise ClaimLifecycleError("non_canonical_input") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ClaimLifecycleError("invalid_identifier", f"{field} is invalid")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ClaimLifecycleError("invalid_integer", f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ClaimLifecycleError("invalid_sha256", f"{field} is invalid")
    return value


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ClaimLifecycleError("invalid_timestamp", f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ClaimLifecycleError("invalid_timestamp", f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ClaimLifecycleError("invalid_timestamp", f"{field} requires timezone")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _bounded_ttl(value: Any, max_ttl_ms: int) -> int:
    ttl = _integer(value, "requested_ttl_ms", minimum=1)
    if ttl > max_ttl_ms:
        raise ClaimLifecycleError("ttl_out_of_bounds")
    return ttl


def _scopes(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or len(value) > 128:
        raise ClaimLifecycleError("invalid_scope_owners")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        try:
            normalized = copy.deepcopy(validate_scope(item))
        except (TypeError, ValueError) as exc:
            raise ClaimLifecycleError("scope_invalid") from exc
        key = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
        if key in seen:
            raise ClaimLifecycleError("duplicate_scope")
        seen.add(key)
        result.append(normalized)
    return result


def _work_record(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClaimLifecycleError("invalid_work")
    work_id = _identifier(value.get("work_id"), "work_id")
    status = value.get("status")
    if status not in _TERMINAL_WORK_STATUSES | _CLAIMABLE_WORK_STATUSES | {
        "proposed",
        "blocked",
    }:
        raise ClaimLifecycleError("invalid_work_status")
    identity_key = value.get("identity_key")
    if identity_key is None:
        identity_key = value.get("work_identity_sha256") or work_id
    identity_key = _identifier(identity_key, "identity_key")
    scope_refs = value.get("scope_refs", [])
    if not isinstance(scope_refs, list):
        raise ClaimLifecycleError("invalid_work_scopes")
    normalized_scopes: list[dict[str, str]] = []
    for scope in scope_refs:
        try:
            normalized_scopes.append(copy.deepcopy(validate_scope(scope)))
        except (TypeError, ValueError) as exc:
            raise ClaimLifecycleError("scope_invalid") from exc
    record = copy.deepcopy(value)
    record.update(
        {
            "work_id": work_id,
            "status": status,
            "identity_key": identity_key,
            "scope_refs": normalized_scopes,
        }
    )
    return record


class WorkLedger:
    """In-memory Work/Claim ledger with deterministic fenced transitions."""

    def __init__(
        self,
        *,
        project_id: str,
        project_revision: int = 0,
        works: list[dict[str, Any]] | None = None,
        max_ttl_ms: int = 300_000,
    ) -> None:
        self.project_id = _identifier(project_id, "project_id")
        self._project_revision = _integer(project_revision, "project_revision")
        self._max_ttl_ms = _bounded_ttl(max_ttl_ms, max_ttl_ms)
        self._works: dict[str, dict[str, Any]] = {}
        for item in works or []:
            normalized = _work_record(item)
            if normalized["work_id"] in self._works:
                raise ClaimLifecycleError("duplicate_work_id")
            self._works[normalized["work_id"]] = normalized
        self._claims: dict[str, dict[str, Any]] = {}
        self._effects: dict[str, dict[str, Any]] = {}
        self._dispatch_replays: dict[str, dict[str, Any]] = {}
        self._transitions: list[dict[str, Any]] = []
        self._next_lease_epoch = 0

    @property
    def project_revision(self) -> int:
        return self._project_revision

    def snapshot(self) -> dict[str, Any]:
        """Return a detached state projection suitable for a test or adapter."""
        return {
            "schema_version": WORK_LEDGER_SCHEMA_VERSION,
            "project_id": self.project_id,
            "project_revision": self._project_revision,
            "works": copy.deepcopy(list(self._works.values())),
            "claims": copy.deepcopy(list(self._claims.values())),
            "effects": copy.deepcopy(list(self._effects.values())),
            "transitions": copy.deepcopy(self._transitions),
            "next_lease_epoch": self._next_lease_epoch,
        }

    def acquire_claim(
        self,
        *,
        work_id: str,
        actor_ref: str,
        expected_project_revision: int,
        observed_at: str,
        requested_ttl_ms: int,
        claim_id: str,
        scope_owners: list[dict[str, str]],
    ) -> dict[str, Any]:
        observed = _parse_timestamp(observed_at, "observed_at")
        actor_ref = _identifier(actor_ref, "actor_ref")
        claim_id = _identifier(claim_id, "claim_id")
        self._check_revision(expected_project_revision)
        ttl = _bounded_ttl(requested_ttl_ms, self._max_ttl_ms)
        owners = _scopes(scope_owners)
        work = self._work(work_id)
        if work["status"] in _TERMINAL_WORK_STATUSES:
            raise ClaimLifecycleError("terminal_work")
        if work["status"] not in _CLAIMABLE_WORK_STATUSES:
            raise ClaimLifecycleError("work_not_claimable")
        if claim_id in self._claims:
            raise ClaimLifecycleError("claim_id_reused")
        if self._active_claim_for_work(work_id) is not None:
            raise ClaimLifecycleError("work_already_claimed")
        self._check_duplicate_identity(work)
        self._check_scope_overlap(owners)

        next_epoch = self._next_lease_epoch + 1
        claim = self._new_claim(
            claim_id=claim_id,
            work_id=work_id,
            actor_ref=actor_ref,
            expected_project_revision=self._project_revision + 1,
            claimed_at=observed,
            lease_expires_at=observed + timedelta(milliseconds=ttl),
            scope_owners=owners,
            lease_epoch=next_epoch,
            reclaimed_from_claim_id=None,
        )
        work_after = copy.deepcopy(work)
        work_after["status"] = "active"
        return self._commit(
            operation="acquire",
            actor_ref=actor_ref,
            observed_at=observed,
            primary_claim=claim,
            claim_updates={claim_id: claim},
            work_updates={work_id: work_after},
            changes={
                "work_before": work,
                "work_after": work_after,
                "claim_before": None,
                "claim_after": claim,
            },
        )

    def heartbeat_claim(
        self,
        *,
        claim_id: str,
        actor_ref: str,
        expected_project_revision: int,
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
        observed_at: str,
        requested_ttl_ms: int,
    ) -> dict[str, Any]:
        observed = _parse_timestamp(observed_at, "observed_at")
        actor_ref = _identifier(actor_ref, "actor_ref")
        self._check_revision(expected_project_revision)
        ttl = _bounded_ttl(requested_ttl_ms, self._max_ttl_ms)
        current = self._active_claim(claim_id, observed)
        self._check_actor(current, actor_ref)
        self._check_tokens(current, expected_claim_revision, lease_epoch, fence)
        after = copy.deepcopy(current)
        after["claim_revision"] += 1
        after["last_heartbeat_at"] = _format_timestamp(observed)
        after["lease_expires_at"] = _format_timestamp(
            max(
                _parse_timestamp(current["lease_expires_at"], "lease_expires_at"),
                observed + timedelta(milliseconds=ttl),
            )
        )
        return self._commit(
            operation="heartbeat",
            actor_ref=actor_ref,
            observed_at=observed,
            primary_claim=after,
            claim_updates={claim_id: after},
            changes={"claim_before": current, "claim_after": after},
        )

    def release_claim(
        self,
        *,
        claim_id: str,
        actor_ref: str,
        expected_project_revision: int,
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
        observed_at: str,
    ) -> dict[str, Any]:
        observed = _parse_timestamp(observed_at, "observed_at")
        actor_ref = _identifier(actor_ref, "actor_ref")
        self._check_revision(expected_project_revision)
        current = self._active_claim(claim_id, observed)
        self._check_actor(current, actor_ref)
        self._check_tokens(current, expected_claim_revision, lease_epoch, fence)
        after = self._terminal_claim(
            current,
            status="released",
            close_reason="worker_release",
            actor_ref=actor_ref,
            observed=observed,
        )
        return self._commit(
            operation="release",
            actor_ref=actor_ref,
            observed_at=observed,
            primary_claim=after,
            claim_updates={claim_id: after},
            changes={"claim_before": current, "claim_after": after},
        )

    def complete_work(
        self,
        *,
        work_id: str,
        claim_id: str,
        actor_ref: str,
        expected_project_revision: int,
        expected_work_revision: int,
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
        observed_at: str,
        evidence_ids: list[str],
        verification_decision_sha256: str,
        claim_evidence_verdict_sha256: str,
    ) -> dict[str, Any]:
        """Atomically complete verified Work and release its fenced Claim."""
        observed = _parse_timestamp(observed_at, "observed_at")
        work_id = _identifier(work_id, "work_id")
        claim_id = _identifier(claim_id, "claim_id")
        actor_ref = _identifier(actor_ref, "actor_ref")
        self._check_revision(expected_project_revision)
        expected_work_revision = _integer(
            expected_work_revision, "expected_work_revision"
        )
        verification_decision_sha256 = _sha256(
            verification_decision_sha256, "verification_decision_sha256"
        )
        claim_evidence_verdict_sha256 = _sha256(
            claim_evidence_verdict_sha256, "claim_evidence_verdict_sha256"
        )
        if (
            not isinstance(evidence_ids, list)
            or not evidence_ids
            or len(evidence_ids) != len(set(evidence_ids))
        ):
            raise ClaimLifecycleError("missing_completion_evidence")
        normalized_evidence = sorted(
            _identifier(item, "evidence_id") for item in evidence_ids
        )

        work = self._work(work_id)
        if work.get("revision") != expected_work_revision:
            raise ClaimLifecycleError("work_revision_mismatch")
        if work["status"] not in {"active", "verifying"}:
            raise ClaimLifecycleError("work_not_completable")
        current = self._active_claim(claim_id, observed)
        if current["work_id"] != work_id:
            raise ClaimLifecycleError("work_mismatch")
        self._check_actor(current, actor_ref)
        self._check_tokens(
            current, expected_claim_revision, lease_epoch, fence
        )

        work_after = copy.deepcopy(work)
        work_after["status"] = "completed"
        work_after["revision"] = expected_work_revision + 1
        work_after["evidence_ids"] = sorted(
            set(work_after.get("evidence_ids", [])) | set(normalized_evidence)
        )
        claim_after = self._terminal_claim(
            current,
            status="released",
            close_reason="worker_release",
            actor_ref=actor_ref,
            observed=observed,
        )
        return self._commit(
            operation="complete_work",
            actor_ref=actor_ref,
            observed_at=observed,
            primary_claim=claim_after,
            claim_updates={claim_id: claim_after},
            work_updates={work_id: work_after},
            changes={
                "work_before": work,
                "work_after": work_after,
                "claim_before": current,
                "claim_after": claim_after,
                "evidence_ids": normalized_evidence,
                "verification_decision_sha256": verification_decision_sha256,
                "claim_evidence_verdict_sha256": claim_evidence_verdict_sha256,
            },
            extra_result={
                "work": work_after,
                "verification_decision_sha256": verification_decision_sha256,
                "claim_evidence_verdict_sha256": claim_evidence_verdict_sha256,
            },
        )

    def revoke_claim(
        self,
        *,
        claim_id: str,
        revoker_ref: str,
        expected_project_revision: int,
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
        observed_at: str,
        reason: str,
    ) -> dict[str, Any]:
        observed = _parse_timestamp(observed_at, "observed_at")
        revoker_ref = _identifier(revoker_ref, "revoker_ref")
        self._check_revision(expected_project_revision)
        current = self._active_claim(claim_id, observed)
        self._check_tokens(current, expected_claim_revision, lease_epoch, fence)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise ClaimLifecycleError("invalid_revoke_reason")
        after = self._terminal_claim(
            current,
            status="revoked",
            close_reason="administrative_revoke",
            actor_ref=revoker_ref,
            observed=observed,
        )
        return self._commit(
            operation="revoke",
            actor_ref=revoker_ref,
            observed_at=observed,
            primary_claim=after,
            claim_updates={claim_id: after},
            changes={
                "claim_before": current,
                "claim_after": after,
                "revoke_reason": reason,
            },
        )

    def expire_claim(
        self,
        *,
        claim_id: str,
        expected_project_revision: int,
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
        observed_at: str,
    ) -> dict[str, Any]:
        observed = _parse_timestamp(observed_at, "observed_at")
        self._check_revision(expected_project_revision)
        current = self._claim(claim_id)
        if current["status"] != "active":
            raise ClaimLifecycleError("claim_not_active")
        self._check_tokens(current, expected_claim_revision, lease_epoch, fence)
        if observed < _parse_timestamp(current["lease_expires_at"], "lease_expires_at"):
            raise ClaimLifecycleError("lease_not_expired")
        after = self._terminal_claim(
            current,
            status="expired",
            close_reason="lease_expired",
            actor_ref="system/expiry",
            observed=observed,
        )
        return self._commit(
            operation="expire",
            actor_ref="system/expiry",
            observed_at=observed,
            primary_claim=after,
            claim_updates={claim_id: after},
            changes={"claim_before": current, "claim_after": after},
        )

    def reclaim_claim(
        self,
        *,
        old_claim_id: str,
        new_claim_id: str,
        new_actor_ref: str,
        expected_project_revision: int,
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
        observed_at: str,
        requested_ttl_ms: int,
        scope_owners: list[dict[str, str]],
    ) -> dict[str, Any]:
        observed = _parse_timestamp(observed_at, "observed_at")
        new_actor_ref = _identifier(new_actor_ref, "new_actor_ref")
        old_claim_id = _identifier(old_claim_id, "old_claim_id")
        new_claim_id = _identifier(new_claim_id, "new_claim_id")
        self._check_revision(expected_project_revision)
        ttl = _bounded_ttl(requested_ttl_ms, self._max_ttl_ms)
        owners = _scopes(scope_owners)
        if new_claim_id in self._claims:
            raise ClaimLifecycleError("claim_id_reused")
        old = self._claim(old_claim_id)
        self._check_tokens(old, expected_claim_revision, lease_epoch, fence)
        if old["status"] not in {"active", "expired"}:
            raise ClaimLifecycleError("claim_not_reclaimable")
        expiry = _parse_timestamp(old["lease_expires_at"], "lease_expires_at")
        if old["status"] == "active" and observed < expiry:
            raise ClaimLifecycleError("lease_not_expired")
        work = self._work(old["work_id"])
        if work["status"] in _TERMINAL_WORK_STATUSES:
            raise ClaimLifecycleError("terminal_work")
        self._check_scope_overlap(owners, excluded_claim_id=old_claim_id)
        old_after = copy.deepcopy(old)
        old_after["claim_revision"] += 1
        if old_after["status"] == "active":
            old_after["status"] = "expired"
            old_after["released_at"] = _format_timestamp(observed)
            old_after["closed_at"] = _format_timestamp(observed)
            old_after["closed_by_ref"] = "system/reclaim"
            old_after["close_reason"] = "lease_expired"
        next_epoch = self._next_lease_epoch + 1
        new_claim = self._new_claim(
            claim_id=new_claim_id,
            work_id=old["work_id"],
            actor_ref=new_actor_ref,
            expected_project_revision=self._project_revision + 1,
            claimed_at=observed,
            lease_expires_at=observed + timedelta(milliseconds=ttl),
            scope_owners=owners,
            lease_epoch=next_epoch,
            reclaimed_from_claim_id=old_claim_id,
        )
        return self._commit(
            operation="reclaim",
            actor_ref=new_actor_ref,
            observed_at=observed,
            primary_claim=new_claim,
            claim_updates={old_claim_id: old_after, new_claim_id: new_claim},
            extra_result={"reclaimed_claim": old_after},
            changes={
                "claim_before": old,
                "claim_after": old_after,
                "reclaimed_claim": new_claim,
            },
        )

    def dispatch_gate(
        self,
        *,
        claim_id: str,
        work_id: str,
        actor_ref: str,
        expected_project_revision: int,
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
        observed_at: str,
        requested_scope: dict[str, str],
    ) -> dict[str, Any]:
        """Evaluate the lease/revision/scope fence immediately before an effect."""
        observed = _parse_timestamp(observed_at, "observed_at")
        actor_ref = _identifier(actor_ref, "actor_ref")
        requested = _scopes([requested_scope])[0]
        reason = "authorized"
        if expected_project_revision != self._project_revision:
            reason = "stale_revision"
        elif claim_id not in self._claims:
            reason = "claim_not_found"
        else:
            claim = self._claims[claim_id]
            if claim["work_id"] != work_id:
                reason = "work_mismatch"
            elif claim["status"] != "active":
                reason = "claim_not_active"
            elif claim["expected_project_revision"] != self._project_revision:
                reason = "claim_project_revision_mismatch"
            elif claim["actor_ref"] != actor_ref:
                reason = "actor_mismatch"
            elif (
                type(expected_claim_revision) is not int
                or claim["claim_revision"] != expected_claim_revision
            ):
                reason = "claim_revision_mismatch"
            elif type(lease_epoch) is not int or claim["lease_epoch"] != lease_epoch:
                reason = "lease_epoch_mismatch"
            elif type(fence) is not int or claim["lease_epoch"] != fence:
                reason = "fence_mismatch"
            elif observed >= _parse_timestamp(
                claim["lease_expires_at"], "lease_expires_at"
            ):
                reason = "claim_expired"
            elif not any(
                self._scope_covers(owner, requested) for owner in claim["scope_owners"]
            ):
                reason = "scope_not_owned"
        verdict = {
            "schema_version": VERDICT_SCHEMA_VERSION,
            "decision": "allow" if reason == "authorized" else "deny",
            "reason": reason,
            "project_id": self.project_id,
            "project_revision": self._project_revision,
            "claim_id": claim_id,
            "work_id": work_id,
            "fence": (
                self._claims[claim_id]["lease_epoch"]
                if claim_id in self._claims
                else lease_epoch
            ),
            "observed_at": _format_timestamp(observed),
        }
        verdict["verdict_sha256"] = _digest(verdict)
        return verdict

    def start_effect_dispatch(
        self,
        *,
        request_id: str,
        effect_id: str,
        effect_key: str,
        request_sha256: str,
        claim_id: str,
        work_id: str,
        actor_ref: str,
        expected_project_revision: int,
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
        observed_at: str,
        operation: str,
        scope_ref: dict[str, str],
    ) -> dict[str, Any]:
        """Atomically fence a worker and persist the start of one external effect."""
        request_id = _identifier(request_id, "request_id")
        effect_id = _identifier(effect_id, "effect_id")
        effect_key = _identifier(effect_key, "effect_key")
        request_sha256 = _sha256(request_sha256, "request_sha256")
        claim_id = _identifier(claim_id, "claim_id")
        work_id = _identifier(work_id, "work_id")
        actor_ref = _identifier(actor_ref, "actor_ref")
        operation = _identifier(operation, "operation")
        observed = _parse_timestamp(observed_at, "observed_at")
        requested_scope = _scopes([scope_ref])[0]
        request_payload = {
            "request_id": request_id,
            "effect_id": effect_id,
            "effect_key": effect_key,
            "request_sha256": request_sha256,
            "claim_id": claim_id,
            "work_id": work_id,
            "actor_ref": actor_ref,
            "expected_project_revision": expected_project_revision,
            "expected_claim_revision": expected_claim_revision,
            "lease_epoch": lease_epoch,
            "fence": fence,
            "observed_at": _format_timestamp(observed),
            "operation": operation,
            "scope_ref": requested_scope,
        }
        request_payload_sha256 = _digest(request_payload)
        replay = self._dispatch_replays.get(request_id)
        if replay is not None:
            if replay["request_payload_sha256"] != request_payload_sha256:
                raise ClaimLifecycleError("request_replay_conflict")
            return copy.deepcopy(replay["result"])

        verdict = self.dispatch_gate(
            claim_id=claim_id,
            work_id=work_id,
            actor_ref=actor_ref,
            expected_project_revision=expected_project_revision,
            expected_claim_revision=expected_claim_revision,
            lease_epoch=lease_epoch,
            fence=fence,
            observed_at=_format_timestamp(observed),
            requested_scope=requested_scope,
        )
        if verdict["decision"] != "allow":
            raise ClaimLifecycleError(verdict["reason"])
        if effect_id in self._effects:
            raise ClaimLifecycleError("effect_id_reused")
        if any(effect["effect_key"] == effect_key for effect in self._effects.values()):
            raise ClaimLifecycleError("effect_key_reused")

        revision_before = self._project_revision
        revision_after = revision_before + 1
        effect = {
            "effect_id": effect_id,
            "effect_key": effect_key,
            "work_id": work_id,
            "claim_id": claim_id,
            "status": "started",
            "operation": operation,
            "scope_ref": copy.deepcopy(requested_scope),
            "expected_project_revision": revision_after,
            "sequence_no": len(self._effects) + 1,
            "evidence_ids": [],
            "result_ref": None,
            "requested_at": _format_timestamp(observed),
            "completed_at": None,
            "attempt_id": None,
            "request_sha256": request_sha256,
            "lease_epoch": lease_epoch,
            "dispatch_receipt_sha256": None,
            "dispatch_started_at": _format_timestamp(observed),
        }
        receipt_body = {
            "schema_version": "context.effect-dispatch-receipt/v1alpha1",
            "request_id": request_id,
            "project_id": self.project_id,
            "project_revision": revision_after,
            "work_id": work_id,
            "claim_id": claim_id,
            "claim_revision": expected_claim_revision,
            "lease_epoch": lease_epoch,
            "fence": lease_epoch,
            "effect_id": effect_id,
            "effect_key": effect_key,
            "request_sha256": request_sha256,
            "operation": operation,
            "scope_ref": copy.deepcopy(requested_scope),
            "dispatch_started_at": _format_timestamp(observed),
        }
        receipt = {**receipt_body, "receipt_sha256": _digest(receipt_body)}
        effect["dispatch_receipt_sha256"] = receipt["receipt_sha256"]

        claim_updates: dict[str, dict[str, Any]] = {}
        claims_before: list[dict[str, Any]] = []
        claims_after: list[dict[str, Any]] = []
        for current in self._claims.values():
            if current["status"] != "active":
                continue
            claims_before.append(copy.deepcopy(current))
            after = copy.deepcopy(current)
            after["expected_project_revision"] = revision_after
            claim_updates[after["claim_id"]] = after
            claims_after.append(copy.deepcopy(after))
        effect_updates: dict[str, dict[str, Any]] = {}
        effects_before: list[dict[str, Any]] = []
        effects_after: list[dict[str, Any]] = []
        for current in self._effects.values():
            if current["status"] not in {"planned", "authorized", "started"}:
                continue
            effects_before.append(copy.deepcopy(current))
            after = copy.deepcopy(current)
            after["expected_project_revision"] = revision_after
            effect_updates[after["effect_id"]] = after
            effects_after.append(copy.deepcopy(after))
        transition_body = {
            "schema_version": TRANSITION_SCHEMA_VERSION,
            "operation": "start-effect-dispatch",
            "project_id": self.project_id,
            "project_revision_before": revision_before,
            "project_revision_after": revision_after,
            "actor_ref": actor_ref,
            "observed_at": _format_timestamp(observed),
            "previous_transition_sha256": (
                self._transitions[-1]["transition_sha256"]
                if self._transitions
                else None
            ),
            "changes": {
                "claims_before": claims_before,
                "claims_after": claims_after,
                "effects_before": effects_before,
                "effects_after": effects_after,
                "effect_before": None,
                "effect_after": effect,
            },
        }
        transition_sha256 = _digest(transition_body)
        transition = {
            **transition_body,
            "transition_id": f"transition-{transition_sha256[:32]}",
            "transition_sha256": transition_sha256,
        }
        for update_claim_id, update in claim_updates.items():
            self._claims[update_claim_id] = copy.deepcopy(update)
        for update_effect_id, update in effect_updates.items():
            self._effects[update_effect_id] = copy.deepcopy(update)
        self._effects[effect_id] = copy.deepcopy(effect)
        self._project_revision = revision_after
        self._transitions.append(copy.deepcopy(transition))
        result = {
            "schema_version": "context.effect-dispatch-receipt/v1alpha1",
            "operation": "start-effect-dispatch",
            "status": "accepted",
            "project_revision": revision_after,
            "effect": copy.deepcopy(effect),
            "receipt": receipt,
            "transition": copy.deepcopy(transition),
        }
        self._dispatch_replays[request_id] = {
            "request_payload_sha256": request_payload_sha256,
            "result": copy.deepcopy(result),
        }
        return result

    # Short names make the reference core convenient for adapters while retaining
    # explicit method names for wire-bound State MCP implementations.
    acquire = acquire_claim
    heartbeat = heartbeat_claim
    release = release_claim
    revoke = revoke_claim
    expire = expire_claim
    reclaim = reclaim_claim

    def _work(self, work_id: str) -> dict[str, Any]:
        work_id = _identifier(work_id, "work_id")
        try:
            return self._works[work_id]
        except KeyError as exc:
            raise ClaimLifecycleError("work_not_found") from exc

    def _claim(self, claim_id: str) -> dict[str, Any]:
        claim_id = _identifier(claim_id, "claim_id")
        try:
            return self._claims[claim_id]
        except KeyError as exc:
            raise ClaimLifecycleError("claim_not_found") from exc

    def _active_claim(self, claim_id: str, observed: datetime) -> dict[str, Any]:
        claim = self._claim(claim_id)
        if claim["status"] != "active":
            raise ClaimLifecycleError("claim_not_active")
        if observed >= _parse_timestamp(claim["lease_expires_at"], "lease_expires_at"):
            raise ClaimLifecycleError("claim_expired")
        return claim

    def _active_claim_for_work(self, work_id: str) -> dict[str, Any] | None:
        return next(
            (
                claim
                for claim in self._claims.values()
                if claim["work_id"] == work_id and claim["status"] == "active"
            ),
            None,
        )

    def _check_revision(self, expected: int) -> None:
        if type(expected) is not int or expected != self._project_revision:
            raise ClaimLifecycleError("stale_revision")

    def _check_actor(self, claim: dict[str, Any], actor_ref: str) -> None:
        if claim["actor_ref"] != actor_ref:
            raise ClaimLifecycleError("actor_mismatch")

    def _check_tokens(
        self,
        claim: dict[str, Any],
        expected_claim_revision: int,
        lease_epoch: int,
        fence: int,
    ) -> None:
        if (
            type(expected_claim_revision) is not int
            or claim["claim_revision"] != expected_claim_revision
        ):
            raise ClaimLifecycleError("claim_revision_mismatch")
        if type(lease_epoch) is not int or claim["lease_epoch"] != lease_epoch:
            raise ClaimLifecycleError("lease_epoch_mismatch")
        if type(fence) is not int or claim["lease_epoch"] != fence:
            raise ClaimLifecycleError("fence_mismatch")

    def _check_duplicate_identity(self, work: dict[str, Any]) -> None:
        identity = work["identity_key"]
        for other in self._works.values():
            if other["work_id"] == work["work_id"] or other["identity_key"] != identity:
                continue
            if other["status"] in _TERMINAL_WORK_STATUSES:
                raise ClaimLifecycleError("duplicate_terminal_work")
            if other["status"] == "active" or self._active_claim_for_work(
                other["work_id"]
            ):
                raise ClaimLifecycleError("duplicate_work_identity")

    def _check_scope_overlap(
        self,
        owners: list[dict[str, str]],
        *,
        excluded_claim_id: str | None = None,
    ) -> None:
        for claim in self._claims.values():
            if claim["status"] != "active" or claim["claim_id"] == excluded_claim_id:
                continue
            try:
                overlap = any(
                    scopes_overlap(left, right)
                    for left in claim["scope_owners"]
                    for right in owners
                )
            except ValueError as exc:
                raise ClaimLifecycleError("scope_invalid") from exc
            if overlap:
                raise ClaimLifecycleError("scope_overlap")

    @staticmethod
    def _scope_covers(owner: dict[str, str], requested: dict[str, str]) -> bool:
        try:
            return scope_covers(owner, requested)
        except ValueError as exc:
            raise ClaimLifecycleError("scope_invalid") from exc

    @staticmethod
    def _new_claim(
        *,
        claim_id: str,
        work_id: str,
        actor_ref: str,
        expected_project_revision: int,
        claimed_at: datetime,
        lease_expires_at: datetime,
        scope_owners: list[dict[str, str]],
        lease_epoch: int,
        reclaimed_from_claim_id: str | None,
    ) -> dict[str, Any]:
        return {
            "claim_id": claim_id,
            "work_id": work_id,
            "actor_ref": actor_ref,
            "status": "active",
            "expected_project_revision": expected_project_revision,
            "claimed_at": _format_timestamp(claimed_at),
            "last_heartbeat_at": _format_timestamp(claimed_at),
            "lease_expires_at": _format_timestamp(lease_expires_at),
            "released_at": None,
            "closed_at": None,
            "closed_by_ref": None,
            "close_reason": None,
            "reclaimed_from_claim_id": reclaimed_from_claim_id,
            "scope_owners": copy.deepcopy(scope_owners),
            "claim_revision": 1,
            "lease_epoch": lease_epoch,
        }

    @staticmethod
    def _terminal_claim(
        current: dict[str, Any],
        *,
        status: str,
        close_reason: str,
        actor_ref: str,
        observed: datetime,
    ) -> dict[str, Any]:
        after = copy.deepcopy(current)
        after["status"] = status
        after["claim_revision"] += 1
        after["released_at"] = _format_timestamp(observed)
        after["closed_at"] = _format_timestamp(observed)
        after["closed_by_ref"] = actor_ref
        after["close_reason"] = close_reason
        return after

    def _commit(
        self,
        *,
        operation: str,
        actor_ref: str,
        observed_at: datetime,
        primary_claim: dict[str, Any],
        claim_updates: dict[str, dict[str, Any]],
        changes: dict[str, Any],
        work_updates: dict[str, dict[str, Any]] | None = None,
        extra_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        revision_before = self._project_revision
        revision_after = revision_before + 1
        for claim in claim_updates.values():
            claim["expected_project_revision"] = revision_after
        claim_rebases: list[dict[str, Any]] = []
        for claim_id, current in self._claims.items():
            if claim_id in claim_updates or current["status"] != "active":
                continue
            after = copy.deepcopy(current)
            after["expected_project_revision"] = revision_after
            claim_updates[claim_id] = after
            claim_rebases.append(
                {
                    "claim_before": copy.deepcopy(current),
                    "claim_after": copy.deepcopy(after),
                }
            )
        effect_updates: dict[str, dict[str, Any]] = {}
        effect_rebases: list[dict[str, Any]] = []
        for effect_id, current in self._effects.items():
            if current["status"] not in {"planned", "authorized", "started"}:
                continue
            after = copy.deepcopy(current)
            after["expected_project_revision"] = revision_after
            effect_updates[effect_id] = after
            effect_rebases.append(
                {
                    "effect_before": copy.deepcopy(current),
                    "effect_after": copy.deepcopy(after),
                }
            )
        committed_changes = copy.deepcopy(changes)
        if claim_rebases:
            committed_changes["claim_authority_rebases"] = claim_rebases
        if effect_rebases:
            committed_changes["effect_authority_rebases"] = effect_rebases
        transition_body = {
            "schema_version": TRANSITION_SCHEMA_VERSION,
            "operation": operation,
            "project_id": self.project_id,
            "project_revision_before": revision_before,
            "project_revision_after": revision_after,
            "actor_ref": actor_ref,
            "observed_at": _format_timestamp(observed_at),
            "previous_transition_sha256": (
                self._transitions[-1]["transition_sha256"]
                if self._transitions
                else None
            ),
            "changes": committed_changes,
        }
        transition_sha256 = _digest(transition_body)
        transition = {
            **transition_body,
            "transition_id": f"transition-{transition_sha256[:32]}",
            "transition_sha256": transition_sha256,
        }
        receipt_body = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "operation": operation,
            "status": "accepted",
            "project_id": self.project_id,
            "project_revision": revision_after,
            "transition_id": transition["transition_id"],
            "transition_sha256": transition_sha256,
            "fence": primary_claim["lease_epoch"],
            "claim": copy.deepcopy(primary_claim),
        }
        receipt = {**receipt_body, "receipt_sha256": _digest(receipt_body)}
        for claim_id, claim in claim_updates.items():
            self._claims[claim_id] = copy.deepcopy(claim)
        for effect_id, effect in effect_updates.items():
            self._effects[effect_id] = copy.deepcopy(effect)
        for work_id, work in (work_updates or {}).items():
            self._works[work_id] = copy.deepcopy(work)
        self._project_revision = revision_after
        self._next_lease_epoch = max(
            self._next_lease_epoch,
            *(claim["lease_epoch"] for claim in claim_updates.values()),
        )
        self._transitions.append(copy.deepcopy(transition))
        result = {
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "operation": operation,
            "status": "accepted",
            "project_revision": revision_after,
            "claim": copy.deepcopy(self._claims[primary_claim["claim_id"]]),
            "receipt": receipt,
            "transition": copy.deepcopy(transition),
        }
        if extra_result:
            result.update(copy.deepcopy(extra_result))
        return result


__all__ = [
    "WORK_LEDGER_SCHEMA_VERSION",
    "ClaimLifecycleError",
    "WorkLedger",
]
