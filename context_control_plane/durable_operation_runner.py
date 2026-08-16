"""Local durable operation runner over explicit authority and effect adapters."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

from .durable_checkpoint_gate import (
    DurableCheckpointGateError,
    validate_checkpoint_gate_manifest,
    validate_durable_checkpoint_receipt,
)
from .durable_operation import (
    DurableOperationError,
    advance_durable_operation,
    validate_durable_operation,
    validate_operation_continuation_binding,
    validate_operation_continuation_transition,
)
from .durable_operation_store import (
    DurableOperationStoreConflict,
    DurableOperationStoreNotFound,
    SQLiteDurableOperationStore,
)
from .durable_operation_trace import (
    DurableOperationTraceError,
    validate_durable_operation_trace_event,
    validate_durable_trace_manifest,
)
from .durable_state_authority import (
    DurableStateAuthorityError,
    durable_state_receipt_ref,
    validate_durable_authority_adapter_manifest,
    validate_durable_state_receipt,
)


class DurableOperationRunnerError(RuntimeError):
    """Raised when an adapter violates the durable operation contract."""


_ADAPTER_MANIFEST_FIELDS = {
    "schema_version",
    "adapter_id",
    "adapter_version",
    "implementation_sha256",
    "source_ref",
    "idempotency_modes",
    "status_lookups",
    "replay_policies",
    "state_write_authority",
    "provider_native_authority",
}
_ADAPTER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def validate_effect_adapter_manifest(manifest: Any) -> dict[str, Any]:
    """Validate immutable identity and execution capabilities for one adapter."""
    if not isinstance(manifest, dict) or set(manifest) != _ADAPTER_MANIFEST_FIELDS:
        raise DurableOperationRunnerError("effect adapter manifest fields are invalid")
    normalized = copy.deepcopy(manifest)
    if normalized["schema_version"] != "context.durable-effect-adapter/v1alpha1":
        raise DurableOperationRunnerError("effect adapter manifest version is invalid")
    for field in ("adapter_id", "adapter_version"):
        value = normalized[field]
        if not isinstance(value, str) or _ADAPTER_ID_RE.fullmatch(value) is None:
            raise DurableOperationRunnerError(f"effect adapter {field} is invalid")
    digest = normalized["implementation_sha256"]
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise DurableOperationRunnerError(
            "effect adapter implementation_sha256 is invalid"
        )
    source_ref = normalized["source_ref"]
    if (
        not isinstance(source_ref, str)
        or not source_ref
        or len(source_ref) > 512
        or any(character in source_ref for character in "\r\n\x00")
    ):
        raise DurableOperationRunnerError("effect adapter source_ref is invalid")
    capability_fields = {
        "idempotency_modes": {"effect-key", "none"},
        "status_lookups": {"none", "supported", "required"},
        "replay_policies": {"safe", "never"},
    }
    for field, allowed in capability_fields.items():
        values = normalized[field]
        if (
            not isinstance(values, list)
            or not values
            or values != sorted(values)
            or len(values) != len(set(values))
            or any(value not in allowed for value in values)
        ):
            raise DurableOperationRunnerError(
                f"effect adapter {field} capabilities are invalid"
            )
    if normalized["state_write_authority"] is not False:
        raise DurableOperationRunnerError("effect adapter cannot claim State authority")
    if normalized["provider_native_authority"] is not False:
        raise DurableOperationRunnerError("effect adapter cannot claim provider authority")
    return normalized


def _adapter_result(value: Any, *, allow_absent: bool) -> dict[str, str]:
    if not isinstance(value, dict) or "status" not in value:
        raise DurableOperationRunnerError("effect adapter returned an invalid result")
    if value["status"] == "absent" and allow_absent and set(value) == {"status"}:
        return {"status": "absent"}
    fields = {"status", "request_sha256", "result_ref", "settlement_ref"}
    if set(value) != fields or value["status"] != "settled":
        raise DurableOperationRunnerError("effect adapter returned an invalid settlement")
    for field in ("request_sha256", "result_ref", "settlement_ref"):
        if not isinstance(value[field], str) or not value[field]:
            raise DurableOperationRunnerError(f"effect adapter {field} is invalid")
    return copy.deepcopy(value)


class LocalDurableOperationRunner:
    """Advance one local operation while preserving all external authority boundaries."""

    def __init__(
        self,
        *,
        store: SQLiteDurableOperationStore,
        effect_adapter: Any,
        authority_adapter: Any,
        checkpoint_gate: Any,
        trace_recorder: Any,
        continuation_state: Callable[[dict[str, Any], str], dict[str, Any]],
        clock: Callable[[], str],
        fault_hook: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        if not isinstance(store, SQLiteDurableOperationStore):
            raise TypeError("store must be a SQLiteDurableOperationStore")
        if not callable(getattr(effect_adapter, "lookup", None)) or not callable(
            getattr(effect_adapter, "apply", None)
        ):
            raise TypeError("effect_adapter must provide lookup and apply")
        self.effect_adapter_manifest = validate_effect_adapter_manifest(
            getattr(effect_adapter, "capability_manifest", None)
        )
        if not callable(getattr(authority_adapter, "commit_intent", None)) or not callable(
            getattr(authority_adapter, "commit_state", None)
        ):
            raise TypeError("authority_adapter must provide commit_intent and commit_state")
        try:
            self.authority_adapter_manifest = validate_durable_authority_adapter_manifest(
                getattr(authority_adapter, "capability_manifest", None)
            )
        except DurableStateAuthorityError as exc:
            raise DurableOperationRunnerError(
                "durable authority adapter manifest failed"
            ) from exc
        if not callable(getattr(checkpoint_gate, "verify", None)):
            raise TypeError("checkpoint_gate must provide verify")
        try:
            self.checkpoint_gate_manifest = validate_checkpoint_gate_manifest(
                getattr(checkpoint_gate, "capability_manifest", None)
            )
        except DurableCheckpointGateError as exc:
            raise DurableOperationRunnerError(
                "durable checkpoint gate manifest failed"
            ) from exc
        if not callable(getattr(trace_recorder, "record", None)):
            raise TypeError("trace_recorder must provide record")
        try:
            self.trace_recorder_manifest = validate_durable_trace_manifest(
                getattr(trace_recorder, "capability_manifest", None)
            )
        except DurableOperationTraceError as exc:
            raise DurableOperationRunnerError(
                "durable trace recorder manifest failed"
            ) from exc
        for callback in (continuation_state, clock):
            if not callable(callback):
                raise TypeError("durable operation callbacks must be callable")
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable")
        self.store = store
        self.effect_adapter = effect_adapter
        self.authority_adapter = authority_adapter
        self.checkpoint_gate = checkpoint_gate
        self.trace_recorder = trace_recorder
        self.continuation_state = continuation_state
        self.clock = clock
        self.fault_hook = fault_hook

    def _fault(self, point: str, operation: dict[str, Any]) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point, copy.deepcopy(operation))

    def _current_continuation(self, operation: dict[str, Any]) -> dict[str, Any]:
        try:
            state = self.continuation_state(
                copy.deepcopy(operation), operation["phase"]
            )
            return validate_operation_continuation_binding(
                operation,
                state,
                expected_sha256=operation["continuation_sha256"],
            )
        except (DurableOperationError, TypeError, ValueError) as exc:
            raise DurableOperationRunnerError(
                "durable continuation binding failed"
            ) from exc

    def _continuation(self, operation: dict[str, Any], phase: str) -> str:
        current = self._current_continuation(operation)
        try:
            candidate = self.continuation_state(copy.deepcopy(operation), phase)
            validated = validate_operation_continuation_transition(
                operation,
                current,
                candidate,
                operation_phase=phase,
            )
        except (DurableOperationError, TypeError, ValueError) as exc:
            raise DurableOperationRunnerError(
                "durable continuation transition failed"
            ) from exc
        return validated["state_sha256"]

    def _append(
        self,
        current: dict[str, Any],
        advanced: dict[str, Any],
        *,
        exclusive: bool = False,
    ) -> dict[str, Any]:
        append = (
            self.store.append_transition_owned
            if exclusive
            else self.store.append_transition
        )
        committed = append(
            advanced,
            expected_record_sha256=current["record_sha256"],
        )
        self._record_trace(committed)
        return committed

    def _record_trace(self, operation: dict[str, Any]) -> None:
        try:
            validate_durable_operation_trace_event(
                self.trace_recorder.record(copy.deepcopy(operation)),
                operation=operation,
            )
        except (DurableOperationTraceError, TypeError, ValueError) as exc:
            raise DurableOperationRunnerError(
                "durable operation trace receipt failed"
            ) from exc

    def _commit_intent(self, operation: dict[str, Any]) -> dict[str, Any]:
        try:
            return validate_durable_state_receipt(
                self.authority_adapter.commit_intent(copy.deepcopy(operation)),
                operation=operation,
                expected_action="authorize",
            )
        except (DurableStateAuthorityError, TypeError, ValueError) as exc:
            raise DurableOperationRunnerError(
                "durable authority intent receipt failed"
            ) from exc

    def _append_state_settlement(
        self,
        current: dict[str, Any],
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        if receipt["effect_status"] != "succeeded" or receipt["result_ref"] is None:
            raise DurableOperationRunnerError(
                "State authority receipt does not prove a settled effect"
            )
        advanced = advance_durable_operation(
            current,
            phase="effect-settled",
            observed_at=self.clock(),
            continuation_sha256=self._continuation(current, "effect-settled"),
            settlement_ref=durable_state_receipt_ref(receipt) + "#effect-succeeded",
            result_ref=receipt["result_ref"],
            reconciliation_reason="state-authority-confirmed",
        )
        committed = self._append(current, advanced)
        self._fault("after-effect-settlement", committed)
        return committed

    def _existing_or_create(
        self, prepared: dict[str, Any]
    ) -> dict[str, Any]:
        prepared = validate_durable_operation(copy.deepcopy(prepared))
        if prepared["phase"] != "prepared":
            raise DurableOperationRunnerError("runner input must be prepared")
        effect = prepared["effect"]
        manifest = self.effect_adapter_manifest
        if manifest["adapter_id"] != effect["adapter_id"]:
            raise DurableOperationRunnerError("effect adapter identity does not match operation")
        for field, manifest_field in (
            ("idempotency_mode", "idempotency_modes"),
            ("status_lookup", "status_lookups"),
            ("replay_policy", "replay_policies"),
        ):
            if effect[field] not in manifest[manifest_field]:
                raise DurableOperationRunnerError(
                    f"effect adapter does not support operation {field}"
                )
        try:
            validate_durable_checkpoint_receipt(
                self.checkpoint_gate.verify(copy.deepcopy(prepared)),
                operation=prepared,
            )
        except (DurableCheckpointGateError, TypeError, ValueError) as exc:
            raise DurableOperationRunnerError(
                "durable checkpoint execution gate failed"
            ) from exc
        self._current_continuation(prepared)
        try:
            history = self.store.read_history(prepared["operation_id"])
        except DurableOperationStoreNotFound:
            created = self.store.create_operation(prepared)
            self._record_trace(created)
            self._fault("after-prepared", created)
            return created
        if history[0] != prepared:
            raise DurableOperationStoreConflict(
                "operation identity was reused for a different prepared intent"
            )
        self._record_trace(history[-1])
        return history[-1]

    def run(self, prepared: dict[str, Any]) -> dict[str, Any]:
        """Run or resume one operation until terminal or manual reconciliation."""
        current = self._existing_or_create(prepared)
        started_in_this_run = False
        intent_receipt: dict[str, Any] | None = None
        while True:
            phase = current["phase"]
            if phase in {"terminal", "quarantined"}:
                return current
            if phase == "prepared":
                intent_receipt = self._commit_intent(current)
                intent_ref = durable_state_receipt_ref(intent_receipt)
                self._fault("after-intent-commit", current)
                advanced = advance_durable_operation(
                    current,
                    phase="intent-committed",
                    observed_at=self.clock(),
                    continuation_sha256=self._continuation(
                        current, "intent-committed"
                    ),
                    intent_ref=intent_ref,
                )
                current = self._append(current, advanced)
                self._fault("after-intent-record", current)
                continue
            if phase == "intent-committed":
                if intent_receipt is None:
                    intent_receipt = self._commit_intent(current)
                advanced = advance_durable_operation(
                    current,
                    phase="effect-in-flight",
                    observed_at=self.clock(),
                    continuation_sha256=self._continuation(
                        current, "effect-in-flight"
                    ),
                    start_ref=(
                        "state-reconciliation://sha256/"
                        + intent_receipt["receipt_sha256"]
                        if intent_receipt["effect_status"] == "succeeded"
                        else (
                            f"attempt://{current['effect']['effect_id']}/"
                            f"{current['attempt_count'] + 1}"
                        )
                    ),
                )
                current = self._append(current, advanced, exclusive=True)
                started_in_this_run = intent_receipt["effect_status"] != "succeeded"
                self._fault("after-effect-start", current)
                continue
            if phase == "outcome-unknown":
                effect = current["effect"]
                if (
                    effect["replay_policy"] != "safe"
                    or effect["idempotency_mode"] != "effect-key"
                ):
                    if intent_receipt is None:
                        intent_receipt = self._commit_intent(current)
                    if intent_receipt["effect_status"] == "succeeded":
                        current = self._append_state_settlement(
                            current, intent_receipt
                        )
                        intent_receipt = None
                        continue
                    return current
                outcome = {"status": "absent"}
                if effect["status_lookup"] != "none":
                    outcome = _adapter_result(
                        self.effect_adapter.lookup(
                            effect["effect_key"], effect["request_sha256"]
                        ),
                        allow_absent=True,
                    )
                if outcome["status"] == "settled":
                    if outcome["request_sha256"] != effect["request_sha256"]:
                        raise DurableOperationRunnerError(
                            "effect settlement does not match the request digest"
                        )
                    advanced = advance_durable_operation(
                        current,
                        phase="effect-settled",
                        observed_at=self.clock(),
                        continuation_sha256=self._continuation(
                            current, "effect-settled"
                        ),
                        settlement_ref=outcome["settlement_ref"],
                        result_ref=outcome["result_ref"],
                        reconciliation_reason="adapter-status-confirmed",
                    )
                    current = self._append(current, advanced)
                    self._fault("after-effect-settlement", current)
                    continue
                if intent_receipt is None:
                    intent_receipt = self._commit_intent(current)
                if intent_receipt["effect_status"] == "succeeded":
                    current = self._append_state_settlement(current, intent_receipt)
                    intent_receipt = None
                    continue
                advanced = advance_durable_operation(
                    current,
                    phase="effect-in-flight",
                    observed_at=self.clock(),
                    continuation_sha256=self._continuation(
                        current, "effect-in-flight"
                    ),
                    start_ref=(
                        f"attempt://{effect['effect_id']}/"
                        f"{current['attempt_count'] + 1}"
                    ),
                )
                current = self._append(current, advanced, exclusive=True)
                started_in_this_run = True
                self._fault("after-effect-start", current)
                continue
            if phase == "effect-in-flight":
                effect = current["effect"]
                outcome: dict[str, str]
                if not started_in_this_run and effect["status_lookup"] != "none":
                    outcome = _adapter_result(
                        self.effect_adapter.lookup(
                            effect["effect_key"], effect["request_sha256"]
                        ),
                        allow_absent=True,
                    )
                else:
                    outcome = {"status": "absent"}

                if outcome["status"] == "absent":
                    if intent_receipt is None:
                        intent_receipt = self._commit_intent(current)
                    if intent_receipt["effect_status"] == "succeeded":
                        current = self._append_state_settlement(
                            current, intent_receipt
                        )
                        started_in_this_run = False
                        intent_receipt = None
                        continue

                if (
                    outcome["status"] == "absent"
                    and not started_in_this_run
                    and effect["replay_policy"] != "safe"
                ):
                    advanced = advance_durable_operation(
                        current,
                        phase="outcome-unknown",
                        observed_at=self.clock(),
                        continuation_sha256=self._continuation(
                            current, "outcome-unknown"
                        ),
                        reconciliation_reason="effect-outcome-requires-verification",
                    )
                    return self._append(current, advanced)
                if outcome["status"] == "absent":
                    outcome = _adapter_result(
                        self.effect_adapter.apply(
                            effect["effect_key"], effect["request_sha256"]
                        ),
                        allow_absent=False,
                    )
                    self._fault("after-external-effect", current)
                if outcome["request_sha256"] != effect["request_sha256"]:
                    raise DurableOperationRunnerError(
                        "effect settlement does not match the request digest"
                    )
                advanced = advance_durable_operation(
                    current,
                    phase="effect-settled",
                    observed_at=self.clock(),
                    continuation_sha256=self._continuation(
                        current, "effect-settled"
                    ),
                    settlement_ref=outcome["settlement_ref"],
                    result_ref=outcome["result_ref"],
                    reconciliation_reason=(
                        "adapter-status-confirmed" if not started_in_this_run else None
                    ),
                )
                current = self._append(current, advanced)
                self._fault("after-effect-settlement", current)
                started_in_this_run = False
                intent_receipt = None
                continue
            if phase == "effect-settled":
                try:
                    state_receipt = validate_durable_state_receipt(
                        self.authority_adapter.commit_state(copy.deepcopy(current)),
                        operation=current,
                        expected_action="complete",
                    )
                    state_commit_ref = durable_state_receipt_ref(state_receipt)
                except (DurableStateAuthorityError, TypeError, ValueError) as exc:
                    raise DurableOperationRunnerError(
                        "durable authority completion receipt failed"
                    ) from exc
                self._fault("after-state-commit", current)
                advanced = advance_durable_operation(
                    current,
                    phase="response-committed",
                    observed_at=self.clock(),
                    continuation_sha256=self._continuation(
                        current, "response-committed"
                    ),
                    state_commit_ref=state_commit_ref,
                )
                current = self._append(current, advanced)
                self._fault("after-response-record", current)
                continue
            if phase == "response-committed":
                advanced = advance_durable_operation(
                    current,
                    phase="terminal",
                    observed_at=self.clock(),
                    continuation_sha256=self._continuation(current, "terminal"),
                )
                current = self._append(current, advanced)
                self._fault("after-terminal", current)
                continue
            raise DurableOperationError(f"unsupported durable operation phase: {phase}")
