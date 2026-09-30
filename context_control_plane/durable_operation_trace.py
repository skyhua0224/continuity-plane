"""Idempotent M8 trace records for durable operation journal revisions."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, ClassVar

from .context_trace import (
    ContextTraceError,
    LocalContextTraceEmitter,
    validate_context_trace_event,
)
from .durable_operation import validate_durable_operation

_MANIFEST_FIELDS = {
    "schema_version",
    "adapter_id",
    "adapter_version",
    "source_ref",
    "event_schema_version",
    "state_write_authority",
    "provider_native_authority",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class DurableOperationTraceError(RuntimeError):
    """Raised when an operation revision cannot be recorded exactly once."""


def validate_durable_trace_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise DurableOperationTraceError("durable trace manifest is invalid")
    normalized = copy.deepcopy(manifest)
    if normalized["schema_version"] != "context.durable-trace-adapter/v1alpha1":
        raise DurableOperationTraceError("durable trace manifest version is invalid")
    for field in ("adapter_id", "adapter_version"):
        value = normalized[field]
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            raise DurableOperationTraceError(f"durable trace {field} is invalid")
    source_ref = normalized["source_ref"]
    if not isinstance(source_ref, str) or not source_ref or any(
        character in source_ref for character in "\r\n\x00"
    ):
        raise DurableOperationTraceError("durable trace source_ref is invalid")
    if normalized["event_schema_version"] != "context.trace-event/v1alpha1":
        raise DurableOperationTraceError("durable trace event version is invalid")
    if normalized["state_write_authority"] is not False:
        raise DurableOperationTraceError("durable trace cannot claim State authority")
    if normalized["provider_native_authority"] is not False:
        raise DurableOperationTraceError("durable trace cannot claim provider authority")
    return normalized


def validate_durable_operation_trace_event(
    event: Any, *, operation: dict[str, Any]
) -> dict[str, Any]:
    current = validate_durable_operation(copy.deepcopy(operation))
    try:
        validate_context_trace_event(event)
    except ContextTraceError as exc:
        raise DurableOperationTraceError("durable operation trace event is invalid") from exc
    expected_binding = LocalDurableOperationTraceRecorder._binding_for(current)
    for field, value in expected_binding.items():
        if event[field] != value:
            raise DurableOperationTraceError(
                f"durable operation trace {field} does not match operation"
            )
    if event["event_name"] != "context.durable.operation.transition":
        raise DurableOperationTraceError("durable operation trace event name is invalid")
    expected_attributes = {
        "record_revision": current["record_revision"],
        "record_sha256": current["record_sha256"],
        "phase": current["phase"],
        "effect_id": current["effect"]["effect_id"],
        "effect_key": current["effect"]["effect_key"],
        "continuation_sha256": current["continuation_sha256"],
        "checkpoint_sha256": current["checkpoint_ref"]["digest"],
    }
    if event["attributes"] != expected_attributes:
        raise DurableOperationTraceError(
            "durable operation trace attributes do not match operation"
        )
    required_ref = "operation-record://sha256/" + current["record_sha256"]
    if required_ref not in event["evidence_refs"]:
        raise DurableOperationTraceError("durable operation trace evidence is incomplete")
    return copy.deepcopy(event)


class LocalDurableOperationTraceRecorder:
    """Append one deterministic context.* event per operation record revision."""

    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-trace-adapter/v1alpha1",
        "adapter_id": "context.local-durable-trace/v1",
        "adapter_version": "1.0.0-alpha.1",
        "source_ref": "component://context.context-trace",
        "event_schema_version": "context.trace-event/v1alpha1",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(self, *, output_path: str | Path, source: dict[str, str]) -> None:
        self.output_path = Path(output_path)
        self.source = copy.deepcopy(source)
        self._emitter: LocalContextTraceEmitter | None = None
        self._binding: dict[str, Any] | None = None

    @staticmethod
    def _binding_for(operation: dict[str, Any]) -> dict[str, Any]:
        return {
            "project_id": operation["project_id"],
            "state_revision": operation["authority"]["project_revision"],
            "active_work_id": operation["work_id"],
            "trace_id": operation["trace_binding"]["trace_id"],
            "span_id": operation["trace_binding"]["span_id"],
            "run_id": operation["trace_binding"]["run_id"],
            "operation_id": operation["operation_id"],
            "correlation_id": operation["trace_binding"]["correlation_id"],
        }

    def _emitter_for(self, operation: dict[str, Any]) -> LocalContextTraceEmitter:
        binding = self._binding_for(operation)
        if self._emitter is None:
            try:
                self._emitter = LocalContextTraceEmitter(
                    binding=binding,
                    source=self.source,
                    output_path=self.output_path,
                )
            except ContextTraceError as exc:
                raise DurableOperationTraceError("durable trace initialization failed") from exc
            self._binding = binding
        elif binding != self._binding:
            raise DurableOperationTraceError(
                "durable trace recorder cannot mix operation bindings"
            )
        return self._emitter

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        if self._emitter is None:
            return ()
        return self._emitter.events

    def record(self, operation: dict[str, Any]) -> dict[str, Any]:
        current = validate_durable_operation(copy.deepcopy(operation))
        emitter = self._emitter_for(current)
        event_id = (
            f"event/{current['operation_id']}/revision/{current['record_revision']}"
        )
        attributes = {
            "record_revision": current["record_revision"],
            "record_sha256": current["record_sha256"],
            "phase": current["phase"],
            "effect_id": current["effect"]["effect_id"],
            "effect_key": current["effect"]["effect_key"],
            "continuation_sha256": current["continuation_sha256"],
            "checkpoint_sha256": current["checkpoint_ref"]["digest"],
        }
        evidence_refs = [
            "operation-record://sha256/" + current["record_sha256"]
        ]
        for field in ("intent_ref", "settlement_ref", "result_ref", "state_commit_ref"):
            if current[field] is not None:
                evidence_refs.append(current[field])
        existing = next(
            (event for event in emitter.events if event["event_id"] == event_id),
            None,
        )
        if existing is not None:
            if (
                existing["attributes"] != attributes
                or existing["evidence_refs"] != evidence_refs
                or existing["observed_at"] != current["updated_at"]
            ):
                raise DurableOperationTraceError(
                    "durable trace event identity conflicts with operation record"
                )
            return copy.deepcopy(existing)
        try:
            return emitter.emit(
                "context.durable.operation.transition",
                evidence_refs=evidence_refs,
                observed_at=current["updated_at"],
                attributes=attributes,
                event_id=event_id,
            )
        except ContextTraceError as exc:
            raise DurableOperationTraceError("durable trace append failed") from exc
