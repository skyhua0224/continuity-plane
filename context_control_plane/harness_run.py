"""Provider-neutral Harness Run and multi-worker coordination contract.

The module is deliberately transport- and provider-independent.  It models
the admission and replay rules that a State MCP or a future adapter must
enforce; it does not execute provider tools or write external effects itself.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

HARNESS_RUN_SCHEMA_VERSION = "context.harness-run/v1alpha1"
HARNESS_EVENT_SCHEMA_VERSION = "context.harness-event/v1alpha1"
HANDOFF_SCHEMA_VERSION = "context.harness-handoff/v1alpha1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_PROVIDERS = {"codex", "claude", "deepseek", "pi", "other"}
_STATUSES = {"proposed", "running", "waiting", "verifying", "completed", "failed", "quarantined"}
_TERMINAL_STATUSES = {"completed", "failed", "quarantined"}
_SCOPE_KINDS = {"repo", "directory", "file", "symbol", "capability", "effect"}


class HarnessRunError(ValueError):
    """Raised when a run, worker, effect, or handoff fails an admission gate."""


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
        raise HarnessRunError("value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _id(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return value
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise HarnessRunError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise HarnessRunError(f"{field} must be lowercase SHA-256")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) > 64:
        raise HarnessRunError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HarnessRunError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HarnessRunError(f"{field} requires a timezone")
    return value


def _positive(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise HarnessRunError(f"{field} must be positive")
    return value


def _non_negative(value: Any, field: str) -> int:
    if type(value) is not int or value < 0:
        raise HarnessRunError(f"{field} must be non-negative")
    return value


def _scopes(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise HarnessRunError("scope_refs must be a non-empty list")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"scope_kind", "scope_ref"}:
            raise HarnessRunError("scope reference fields are invalid")
        if item["scope_kind"] not in _SCOPE_KINDS or not isinstance(item["scope_ref"], str):
            raise HarnessRunError("scope reference is invalid")
        if not item["scope_ref"].strip():
            raise HarnessRunError("scope reference is empty")
        normalized_item = {"scope_kind": item["scope_kind"], "scope_ref": item["scope_ref"]}
        encoded = _canonical(normalized_item).decode("utf-8")
        if encoded in seen:
            raise HarnessRunError("scope references must be unique")
        seen.add(encoded)
        normalized.append(normalized_item)
    return sorted(normalized, key=lambda item: (item["scope_kind"], item["scope_ref"]))


def _sorted_ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or value != sorted(set(value)):
        raise HarnessRunError(f"{field} must be sorted and unique")
    for item in value:
        _id(item, field)
    return copy.deepcopy(value)


def _run_body(run: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(run)
    body.pop("run_sha256", None)
    return body


def _rehash_run(run: dict[str, Any]) -> dict[str, Any]:
    normalized = copy.deepcopy(run)
    normalized["run_sha256"] = _digest(_run_body(normalized))
    return normalized


def validate_harness_run(value: Any) -> dict[str, Any]:
    """Validate a complete run and its authority-denial flags."""
    fields = {
        "schema_version", "run_id", "project_id", "task_id", "task_revision",
        "claim_id", "claim_lease_epoch", "claim_fence", "provider",
        "provider_contract_version", "execution_packet_sha256", "skill_set_digest",
        "tool_grants", "checkpoint_id", "effect_high_watermark",
        "verification_profile_id", "reference_validity_watermark", "trace_id",
        "status", "parent_run_id", "scope_refs", "created_at", "updated_at",
        "state_write_authority", "effect_dispatch_authority", "provider_native_authority",
        "run_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise HarnessRunError("Harness Run fields are invalid")
    run = copy.deepcopy(value)
    if run["schema_version"] != HARNESS_RUN_SCHEMA_VERSION:
        raise HarnessRunError("Harness Run schema version is invalid")
    for field in ("run_id", "project_id", "task_id", "claim_id", "provider_contract_version", "checkpoint_id", "verification_profile_id", "trace_id"):
        _id(run[field], field)
    _positive(run["task_revision"], "task_revision")
    _positive(run["claim_lease_epoch"], "claim_lease_epoch")
    _positive(run["claim_fence"], "claim_fence")
    if run["claim_fence"] != run["claim_lease_epoch"]:
        raise HarnessRunError("claim fence must equal lease epoch")
    if run["provider"] not in _PROVIDERS:
        raise HarnessRunError("provider is unsupported")
    for field in ("execution_packet_sha256", "skill_set_digest"):
        _sha(run[field], field)
    run["tool_grants"] = _sorted_ids(run["tool_grants"], "tool_grants")
    _non_negative(run["effect_high_watermark"], "effect_high_watermark")
    _non_negative(run["reference_validity_watermark"], "reference_validity_watermark")
    if run["status"] not in _STATUSES:
        raise HarnessRunError("status is invalid")
    if run["parent_run_id"] is not None:
        _id(run["parent_run_id"], "parent_run_id")
        if run["parent_run_id"] == run["run_id"]:
            raise HarnessRunError("run cannot parent itself")
    run["scope_refs"] = _scopes(run["scope_refs"])
    _timestamp(run["created_at"], "created_at")
    _timestamp(run["updated_at"], "updated_at")
    if run["state_write_authority"] is not False or run["effect_dispatch_authority"] is not False or run["provider_native_authority"] is not False:
        raise HarnessRunError("provider run cannot claim authority")
    _sha(run["run_sha256"], "run_sha256")
    if run["run_sha256"] != _digest(_run_body(run)):
        raise HarnessRunError("Harness Run hash mismatch")
    return run


def create_harness_run(
    *,
    run_id: str,
    project_id: str,
    task_id: str,
    task_revision: int,
    claim_id: str,
    claim_lease_epoch: int,
    claim_fence: int,
    provider: str,
    provider_contract_version: str,
    execution_packet_sha256: str,
    skill_set_digest: str,
    tool_grants: list[str],
    checkpoint_id: str,
    effect_high_watermark: int,
    verification_profile_id: str,
    reference_validity_watermark: int,
    trace_id: str,
    status: str,
    parent_run_id: str | None,
    scope_refs: list[dict[str, str]],
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    run = {
        "schema_version": HARNESS_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "project_id": project_id,
        "task_id": task_id,
        "task_revision": task_revision,
        "claim_id": claim_id,
        "claim_lease_epoch": claim_lease_epoch,
        "claim_fence": claim_fence,
        "provider": provider,
        "provider_contract_version": provider_contract_version,
        "execution_packet_sha256": execution_packet_sha256,
        "skill_set_digest": skill_set_digest,
        "tool_grants": tool_grants,
        "checkpoint_id": checkpoint_id,
        "effect_high_watermark": effect_high_watermark,
        "verification_profile_id": verification_profile_id,
        "reference_validity_watermark": reference_validity_watermark,
        "trace_id": trace_id,
        "status": status,
        "parent_run_id": parent_run_id,
        "scope_refs": scope_refs,
        "created_at": created_at,
        "updated_at": updated_at,
        "state_write_authority": False,
        "effect_dispatch_authority": False,
        "provider_native_authority": False,
        "run_sha256": "",
    }
    # A provider may not create a run without a State claim.  This is the
    # admission boundary that prevents unclaimed worker effects.
    if not isinstance(claim_id, str) or not claim_id:
        raise HarnessRunError("active claim is required")
    run = _rehash_run(run)
    return validate_harness_run(run)


def _event_body(event: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(event)
    body.pop("event_sha256", None)
    return body


def _event(
    events: list[dict[str, Any]],
    *,
    event_type: str,
    project_id: str,
    task_revision: int,
    payload: dict[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    sequence_no = len(events) + 1
    previous = events[-1]["event_sha256"] if events else None
    event = {
        "schema_version": HARNESS_EVENT_SCHEMA_VERSION,
        "event_id": f"hce_{_digest([project_id, sequence_no, event_type, payload])[:32]}",
        "sequence_no": sequence_no,
        "event_type": event_type,
        "project_id": project_id,
        "task_revision": task_revision,
        "observed_at": observed_at,
        "payload": copy.deepcopy(payload),
        "previous_event_sha256": previous,
        "event_sha256": "",
    }
    event["event_sha256"] = _digest(_event_body(event))
    events.append(event)
    return copy.deepcopy(event)


class HarnessCoordinator:
    """Reference fan-out/fan-in coordinator with deterministic event replay."""

    def __init__(
        self,
        *,
        project_id: str,
        task_revision: int,
        provider_contract_version: str,
        clock: Callable[[], str],
        effect_dispatcher: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.project_id = _id(project_id, "project_id")
        self.task_revision = _positive(task_revision, "task_revision")
        self.provider_contract_version = _id(provider_contract_version, "provider_contract_version")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self.clock = clock
        self.effect_dispatcher = effect_dispatcher
        self.runs: dict[str, dict[str, Any]] = {}
        self.effects: dict[str, dict[str, Any]] = {}
        self.handoffs: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []

    def authority_snapshot(self) -> dict[str, Any]:
        """Return only State MCP-owned fields, excluding provider run status."""
        return {
            "project_id": self.project_id,
            "task_revision": self.task_revision,
            "active_claim_ids": sorted(run["claim_id"] for run in self.runs.values()),
            "effect_ids": sorted(self.effects),
        }

    def register_run(self, run: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_harness_run(run)
        if normalized["project_id"] != self.project_id:
            raise HarnessRunError("run project does not match coordinator")
        if normalized["task_revision"] != self.task_revision:
            raise HarnessRunError("stale task revision")
        if normalized["run_id"] in self.runs:
            raise HarnessRunError("run identity already exists")
        self.runs[normalized["run_id"]] = normalized
        _event(
            self.events,
            event_type="run-registered",
            project_id=self.project_id,
            task_revision=self.task_revision,
            payload={"run_id": normalized["run_id"], "run_sha256": normalized["run_sha256"]},
            observed_at=self.clock(),
        )
        return copy.deepcopy(normalized)

    def dispatch_worker(self, parent_run_id: str, worker: dict[str, Any]) -> dict[str, Any]:
        parent = self._run(parent_run_id)
        if parent["status"] != "running":
            raise HarnessRunError("parent run is not running")
        normalized = validate_harness_run(worker)
        if normalized["parent_run_id"] != parent_run_id:
            raise HarnessRunError("worker parent binding is invalid")
        if normalized["provider_contract_version"] != self.provider_contract_version:
            raise HarnessRunError("provider contract drift")
        self.register_run(normalized)
        _event(
            self.events,
            event_type="worker-dispatch",
            project_id=self.project_id,
            task_revision=self.task_revision,
            payload={
                "parent_run_id": parent_run_id,
                "worker_run_id": normalized["run_id"],
                "provider": normalized["provider"],
                "provider_contract_version": normalized["provider_contract_version"],
                "execution_packet_sha256": normalized["execution_packet_sha256"],
                "claim_id": normalized["claim_id"],
            },
            observed_at=self.clock(),
        )
        return copy.deepcopy(normalized)

    def mark_worker_lost(self, run_id: str) -> dict[str, Any]:
        run = self._run(run_id)
        if run["status"] in _TERMINAL_STATUSES:
            raise HarnessRunError("worker is already terminal")
        run["status"] = "failed"
        run["updated_at"] = self.clock()
        run = _rehash_run(run)
        self.runs[run_id] = validate_harness_run(run)
        _event(
            self.events,
            event_type="worker-loss",
            project_id=self.project_id,
            task_revision=self.task_revision,
            payload={
                "run_id": run_id,
                "run_sha256": run["run_sha256"],
                "authority_changed": False,
            },
            observed_at=self.clock(),
        )
        return {"run_id": run_id, "status": "lost", "authority_changed": False}

    def complete_worker(self, run_id: str, *, evidence_sha256: str) -> dict[str, Any]:
        _sha(evidence_sha256, "evidence_sha256")
        run = self._run(run_id)
        if run["status"] not in {"running", "verifying"}:
            raise HarnessRunError("worker is not completable")
        run["status"] = "completed"
        run["updated_at"] = self.clock()
        run = _rehash_run(run)
        self.runs[run_id] = validate_harness_run(run)
        _event(
            self.events,
            event_type="worker-completed",
            project_id=self.project_id,
            task_revision=self.task_revision,
            payload={
                "run_id": run_id,
                "run_sha256": run["run_sha256"],
                "evidence_sha256": evidence_sha256,
            },
            observed_at=self.clock(),
        )
        return {"run_id": run_id, "status": "completed", "evidence_sha256": evidence_sha256}

    def fan_in(self, parent_run_id: str, worker_run_ids: list[str]) -> dict[str, Any]:
        parent = self._run(parent_run_id)
        if parent["status"] != "running":
            raise HarnessRunError("parent run is not running")
        if not worker_run_ids or worker_run_ids != sorted(set(worker_run_ids)):
            raise HarnessRunError("fan-in worker identities are invalid")
        workers = [self._run(run_id) for run_id in worker_run_ids]
        if any(worker["parent_run_id"] != parent_run_id for worker in workers):
            raise HarnessRunError("fan-in worker parent mismatch")
        if any(worker["status"] not in _TERMINAL_STATUSES for worker in workers):
            raise HarnessRunError("fan-in requires terminal workers")
        parent["status"] = "completed"
        parent["updated_at"] = self.clock()
        self.runs[parent_run_id] = validate_harness_run(_rehash_run(parent))
        receipt = {
            "status": "completed",
            "parent_run_id": parent_run_id,
            "worker_run_ids": copy.deepcopy(worker_run_ids),
            "fan_in_count": len(worker_run_ids),
            "fan_in_sha256": _digest(worker_run_ids),
            "run_sha256": self.runs[parent_run_id]["run_sha256"],
        }
        _event(
            self.events,
            event_type="fan-in",
            project_id=self.project_id,
            task_revision=self.task_revision,
            payload=receipt,
            observed_at=self.clock(),
        )
        return receipt

    def commit_effect(
        self,
        run_id: str,
        *,
        effect_id: str,
        effect_key: str,
        operation: str,
        scope_ref: dict[str, str],
        expected_project_revision: int,
    ) -> dict[str, Any]:
        run = self._run(run_id)
        if run["status"] != "running":
            raise HarnessRunError("effect requires a running worker")
        if expected_project_revision != self.task_revision:
            raise HarnessRunError("stale project revision")
        _id(effect_id, "effect_id")
        _id(effect_key, "effect_key")
        _id(operation, "operation")
        normalized_scope = _scopes([scope_ref])[0]
        if normalized_scope not in run["scope_refs"]:
            raise HarnessRunError("effect scope is outside claim scope")
        if effect_id in self.effects:
            existing = self.effects[effect_id]
            if existing["effect_key"] != effect_key:
                raise HarnessRunError("effect identity is reused for a different key")
            return copy.deepcopy(existing)
        if self.effect_dispatcher is None:
            raise HarnessRunError("State MCP effect dispatch is required")
        intent = {
            "effect_id": effect_id,
            "effect_key": effect_key,
            "operation": operation,
            "scope_ref": normalized_scope,
            "project_id": self.project_id,
            "task_revision": self.task_revision,
            "claim_id": run["claim_id"],
            "claim_lease_epoch": run["claim_lease_epoch"],
            "claim_fence": run["claim_fence"],
        }
        result = self.effect_dispatcher(copy.deepcopy(intent))
        if not isinstance(result, dict) or result.get("accepted") is not True:
            raise HarnessRunError("State MCP effect dispatch was not accepted")
        receipt = {
            **intent,
            "status": "started",
            "provider_authority": False,
            "receipt_sha256": _digest({**intent, "status": "started"}),
        }
        self.effects[effect_id] = receipt
        _event(
            self.events,
            event_type="effect-dispatch",
            project_id=self.project_id,
            task_revision=self.task_revision,
            payload={"run_id": run_id, "receipt": receipt},
            observed_at=self.clock(),
        )
        return copy.deepcopy(receipt)

    def create_handoff(
        self,
        source_run_id: str,
        target_run_id: str,
        *,
        checkpoint_id: str,
        next_action: str,
        expected_task_revision: int,
    ) -> dict[str, Any]:
        source = self._run(source_run_id)
        target = self._run(target_run_id)
        if source["status"] not in {"waiting", "failed", "completed"}:
            raise HarnessRunError("source run is not handoff-ready")
        if target["status"] not in {"proposed", "running"}:
            raise HarnessRunError("target run is not handoff-ready")
        if expected_task_revision != self.task_revision or target["task_revision"] != self.task_revision:
            raise HarnessRunError("stale handoff")
        _id(checkpoint_id, "checkpoint_id")
        if not isinstance(next_action, str) or not next_action.strip() or len(next_action) > 512:
            raise HarnessRunError("next_action is invalid")
        handoff = {
            "schema_version": HANDOFF_SCHEMA_VERSION,
            "handoff_id": f"handoff_{_digest([source_run_id, target_run_id, checkpoint_id, next_action])[:32]}",
            "source_run_id": source_run_id,
            "target_run_id": target_run_id,
            "task_revision": self.task_revision,
            "checkpoint_id": checkpoint_id,
            "next_action": next_action,
            "status": "pending",
            "actual_first_action": None,
            "created_at": self.clock(),
            "updated_at": self.clock(),
            "handoff_sha256": "",
        }
        handoff["handoff_sha256"] = _digest({k: v for k, v in handoff.items() if k != "handoff_sha256"})
        self.handoffs[handoff["handoff_id"]] = handoff
        _event(
            self.events,
            event_type="handoff-created",
            project_id=self.project_id,
            task_revision=self.task_revision,
            payload=handoff,
            observed_at=self.clock(),
        )
        return copy.deepcopy(handoff)

    def acknowledge_handoff(self, handoff_id: str, actual_first_action: str) -> dict[str, Any]:
        _id(handoff_id, "handoff_id")
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            raise HarnessRunError("handoff is unknown")
        if handoff["status"] != "pending":
            raise HarnessRunError("handoff is not pending")
        if actual_first_action != handoff["next_action"]:
            raise HarnessRunError("first action does not match handoff")
        accepted = copy.deepcopy(handoff)
        accepted["status"] = "accepted"
        accepted["updated_at"] = self.clock()
        accepted["actual_first_action"] = actual_first_action
        accepted["handoff_sha256"] = _digest({k: v for k, v in accepted.items() if k != "handoff_sha256"})
        self.handoffs[handoff_id] = accepted
        _event(
            self.events,
            event_type="handoff-accepted",
            project_id=self.project_id,
            task_revision=self.task_revision,
            payload={"handoff_id": handoff_id, "actual_first_action": actual_first_action},
            observed_at=self.clock(),
        )
        return copy.deepcopy(accepted)

    def _run(self, run_id: str) -> dict[str, Any]:
        _id(run_id, "run_id")
        run = self.runs.get(run_id)
        if run is None:
            raise HarnessRunError("run is unknown")
        return run


def replay_harness_events(events: list[dict[str, Any]], *, provider: str) -> dict[str, Any]:
    """Replay event identities without consulting provider-native history."""
    if provider not in _PROVIDERS:
        raise HarnessRunError("provider is unsupported")
    if not isinstance(events, list):
        raise HarnessRunError("events must be a list")
    previous = None
    fan_in_count = 0
    fan_out_count = 0
    effect_count = 0
    handoff_count = 0
    for index, event in enumerate(events, start=1):
        if not isinstance(event, dict) or event.get("schema_version") != HARNESS_EVENT_SCHEMA_VERSION:
            raise HarnessRunError("event schema version is invalid")
        if event.get("sequence_no") != index or event.get("previous_event_sha256") != previous:
            raise HarnessRunError("event sequence or chain is invalid")
        expected = _digest(_event_body(event))
        if event.get("event_sha256") != expected:
            raise HarnessRunError("event hash mismatch")
        previous = expected
        kind = event["event_type"]
        fan_out_count += kind == "worker-dispatch"
        fan_in_count += kind == "fan-in"
        effect_count += kind == "effect-dispatch"
        handoff_count += kind in {"handoff-created", "handoff-accepted"}
    return {
        "event_count": len(events),
        "fan_out_count": fan_out_count,
        "fan_in_count": fan_in_count,
        "effect_count": effect_count,
        "handoff_event_count": handoff_count,
        "provider": "provider-neutral",
        "authority_source": "state-mcp",
    }


__all__ = [
    "HANDOFF_SCHEMA_VERSION",
    "HARNESS_EVENT_SCHEMA_VERSION",
    "HARNESS_RUN_SCHEMA_VERSION",
    "HarnessCoordinator",
    "HarnessRunError",
    "create_harness_run",
    "replay_harness_events",
    "validate_harness_run",
]
