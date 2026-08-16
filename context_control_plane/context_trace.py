"""Provider-neutral, append-only ``context.*`` trace events."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "context.trace-event/v1alpha1"
OTEL_EXPORT_SCHEMA_VERSION = "context.otel-export/v1alpha1"
GENESIS_EVENT_SHA256 = "0" * 64

_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "event_name",
    "sequence",
    "previous_event_sha256",
    "project_id",
    "state_revision",
    "active_work_id",
    "trace_id",
    "span_id",
    "run_id",
    "operation_id",
    "correlation_id",
    "source",
    "evidence_refs",
    "observed_at",
    "attributes",
    "authority",
    "event_sha256",
}
_BINDING_FIELDS = {
    "project_id",
    "state_revision",
    "active_work_id",
    "trace_id",
    "span_id",
    "run_id",
    "operation_id",
    "correlation_id",
}
_SOURCE_FIELDS = {"kind", "provider", "adapter", "source_ref"}
_OTEL_RECEIPT_FIELDS = {"status", "exported_events", "exporter_id", "evidence_ref"}
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_EVENT_NAME_RE = re.compile(r"^context\.[a-z][a-z0-9_.-]{0,126}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_MAX_ATTRIBUTES_BYTES = 32 * 1024
_MAX_EVIDENCE_REFS = 64


class ContextTraceError(ValueError):
    """Raised when trace evidence is malformed or overstates its authority."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContextTraceError("trace data must be canonical JSON") from exc


def canonical_context_trace_event_bytes(event: Mapping[str, Any]) -> bytes:
    """Return stable JSON bytes for persistence, comparison, and transport."""
    if not isinstance(event, Mapping):
        raise ContextTraceError("event must be an object")
    return _canonical_bytes(dict(event))


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise ContextTraceError(f"{field} is invalid")
    return value


def _reference(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or any(character in value for character in "\r\n\x00")
    ):
        raise ContextTraceError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContextTraceError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextTraceError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ContextTraceError(f"{field} must include a timezone")
    return value


def _validate_binding(binding: Any) -> dict[str, Any]:
    if not isinstance(binding, Mapping) or set(binding) != _BINDING_FIELDS:
        raise ContextTraceError("binding fields are invalid")
    normalized = dict(binding)
    for field in (
        "project_id",
        "active_work_id",
        "run_id",
        "operation_id",
        "correlation_id",
    ):
        _safe_id(normalized[field], field)
    revision = normalized["state_revision"]
    if type(revision) is not int or revision < 0:
        raise ContextTraceError("state_revision is invalid")
    trace_id = normalized["trace_id"]
    if not isinstance(trace_id, str) or _TRACE_ID_RE.fullmatch(trace_id) is None or set(trace_id) == {"0"}:
        raise ContextTraceError("trace_id is invalid")
    span_id = normalized["span_id"]
    if not isinstance(span_id, str) or _SPAN_ID_RE.fullmatch(span_id) is None or set(span_id) == {"0"}:
        raise ContextTraceError("span_id is invalid")
    return normalized


def _validate_source(source: Any) -> dict[str, str]:
    if not isinstance(source, Mapping) or set(source) != _SOURCE_FIELDS:
        raise ContextTraceError("source fields are invalid")
    normalized = dict(source)
    for field in ("kind", "provider", "adapter"):
        _safe_id(normalized[field], f"source.{field}")
    _reference(normalized["source_ref"], "source.source_ref")
    return normalized


def _normalize_source(source: Any) -> dict[str, str]:
    if isinstance(source, Mapping) and set(source) == {"kind", "component"}:
        kind = _safe_id(source["kind"], "source.kind")
        component = _safe_id(source["component"], "source.component")
        return {
            "kind": kind,
            "provider": "local",
            "adapter": component,
            "source_ref": f"component://{component}",
        }
    return _validate_source(source)


def _event_body(event: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key != "event_sha256"}


def _event_sha256(event: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(_event_body(event))).hexdigest()


def validate_context_trace_event(
    event: Any,
    *,
    expected_sequence: int | None = None,
    expected_previous_hash: str | None = None,
) -> None:
    """Validate one event and, when supplied, its expected chain position."""
    if not isinstance(event, Mapping) or set(event) != _EVENT_FIELDS:
        raise ContextTraceError("event fields are invalid")
    if event["schema_version"] != SCHEMA_VERSION:
        raise ContextTraceError("schema_version is invalid")
    _safe_id(event["event_id"], "event_id")
    name = event["event_name"]
    if not isinstance(name, str) or _EVENT_NAME_RE.fullmatch(name) is None:
        raise ContextTraceError("event_name must use the context.* namespace")
    sequence = event["sequence"]
    if type(sequence) is not int or sequence <= 0:
        raise ContextTraceError("sequence is invalid")
    if expected_sequence is not None and sequence != expected_sequence:
        raise ContextTraceError("sequence does not match append-only position")
    previous_hash = event["previous_event_sha256"]
    if not isinstance(previous_hash, str) or _SHA256_RE.fullmatch(previous_hash) is None:
        raise ContextTraceError("previous_event_sha256 is invalid")
    if expected_previous_hash is not None and previous_hash != expected_previous_hash:
        raise ContextTraceError("previous_event_sha256 does not match chain")

    _validate_binding({field: event[field] for field in _BINDING_FIELDS})
    _validate_source(event["source"])
    evidence_refs = event["evidence_refs"]
    if (
        not isinstance(evidence_refs, list)
        or not evidence_refs
        or len(evidence_refs) > _MAX_EVIDENCE_REFS
    ):
        raise ContextTraceError("evidence_refs must be a bounded non-empty list")
    for evidence_ref in evidence_refs:
        _reference(evidence_ref, "evidence_ref")
    if len(evidence_refs) != len(set(evidence_refs)):
        raise ContextTraceError("evidence_refs must be unique")
    _timestamp(event["observed_at"], "observed_at")
    attributes = event["attributes"]
    if not isinstance(attributes, dict):
        raise ContextTraceError("attributes must be an object")
    if len(_canonical_bytes(attributes)) > _MAX_ATTRIBUTES_BYTES:
        raise ContextTraceError("attributes exceed the bounded trace payload")
    if event["authority"] is not False:
        raise ContextTraceError("trace events never grant authority")
    event_hash = event["event_sha256"]
    if not isinstance(event_hash, str) or _SHA256_RE.fullmatch(event_hash) is None:
        raise ContextTraceError("event_sha256 is invalid")
    if event_hash != _event_sha256(event):
        raise ContextTraceError("event_sha256 does not match canonical event body")


def validate_context_trace_chain(events: Sequence[Mapping[str, Any]]) -> None:
    """Validate sequence, hash linkage, and every event in an append-only chain."""
    if isinstance(events, (str, bytes, bytearray)) or not isinstance(events, Sequence):
        raise ContextTraceError("trace chain must be a sequence")
    previous_hash = GENESIS_EVENT_SHA256
    event_ids: set[str] = set()
    for expected_sequence, event in enumerate(events, start=1):
        validate_context_trace_event(
            event,
            expected_sequence=expected_sequence,
            expected_previous_hash=previous_hash,
        )
        if event["event_id"] in event_ids:
            raise ContextTraceError("event_id must be unique within a chain")
        event_ids.add(event["event_id"])
        previous_hash = event["event_sha256"]


def append_context_trace_event(
    chain: list[dict[str, Any]],
    *,
    event_name: str,
    binding: Mapping[str, Any],
    source: Mapping[str, Any],
    evidence_refs: Sequence[str],
    observed_at: str,
    attributes: Mapping[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Append one validated event without replacing or rewriting earlier entries."""
    if not isinstance(chain, list):
        raise ContextTraceError("trace chain must be a mutable list")
    validate_context_trace_chain(chain)
    normalized_binding = _validate_binding(binding)
    normalized_source = _normalize_source(source)
    if isinstance(evidence_refs, (str, bytes, bytearray)) or not isinstance(
        evidence_refs, Sequence
    ):
        raise ContextTraceError("evidence_refs must be a sequence")
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "event_id": event_id or f"event/{uuid.uuid4()}",
        "event_name": event_name,
        "sequence": len(chain) + 1,
        "previous_event_sha256": (
            chain[-1]["event_sha256"] if chain else GENESIS_EVENT_SHA256
        ),
        **normalized_binding,
        "source": normalized_source,
        "evidence_refs": list(evidence_refs),
        "observed_at": observed_at,
        "attributes": copy.deepcopy(dict(attributes or {})),
        "authority": False,
        "event_sha256": "",
    }
    event["event_sha256"] = _event_sha256(event)
    validate_context_trace_event(
        event,
        expected_sequence=len(chain) + 1,
        expected_previous_hash=(
            chain[-1]["event_sha256"] if chain else GENESIS_EVENT_SHA256
        ),
    )
    chain.append(event)
    return event


def _binding_from_event(event: Mapping[str, Any]) -> dict[str, Any]:
    return {field: event[field] for field in _BINDING_FIELDS}


@contextmanager
def _exclusive_trace_lock(output_path: Path):
    """Serialize local JSONL readers and appenders across processes."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = output_path.with_name(output_path.name + ".lock")
    stream = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


class LocalContextTraceEmitter:
    """Append validated trace events to memory and, optionally, a local JSONL file."""

    def __init__(
        self,
        *,
        binding: Mapping[str, Any],
        source: Mapping[str, Any],
        output_path: str | Path | None = None,
    ) -> None:
        self._binding = _validate_binding(binding)
        self._source = _normalize_source(source)
        self._output_path = Path(output_path) if output_path is not None else None
        self._events: list[dict[str, Any]] = []
        if self._output_path is not None:
            with _exclusive_trace_lock(self._output_path):
                self._events = self._read_persisted_events()

    def _read_persisted_events(self) -> list[dict[str, Any]]:
        if self._output_path is None or not self._output_path.exists():
            return []
        try:
            lines = self._output_path.read_text(encoding="utf-8").splitlines()
            events = [json.loads(line) for line in lines if line.strip()]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ContextTraceError("local trace file is invalid") from exc
        validate_context_trace_chain(events)
        if any(_binding_from_event(event) != self._binding for event in events):
            raise ContextTraceError("existing trace binding does not match emitter binding")
        return events

    def _matching_event(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        event_id: str | None,
        event_name: str,
        evidence_refs: Sequence[str],
        observed_at: str,
        attributes: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        if event_id is None:
            return None
        existing = next(
            (event for event in events if event["event_id"] == event_id),
            None,
        )
        if existing is None:
            return None
        expected = {
            "event_name": event_name,
            **self._binding,
            "source": self._source,
            "evidence_refs": list(evidence_refs),
            "observed_at": observed_at,
            "attributes": copy.deepcopy(dict(attributes or {})),
            "authority": False,
        }
        if any(existing[field] != value for field, value in expected.items()):
            raise ContextTraceError("event_id conflicts with an existing trace event")
        return copy.deepcopy(existing)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        if self._output_path is not None:
            with _exclusive_trace_lock(self._output_path):
                self._events = self._read_persisted_events()
        return tuple(copy.deepcopy(self._events))

    def emit(
        self,
        event_name: str,
        *,
        evidence_refs: Sequence[str],
        observed_at: str,
        attributes: Mapping[str, Any] | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        if self._output_path is None:
            existing = self._matching_event(
                self._events,
                event_id=event_id,
                event_name=event_name,
                evidence_refs=evidence_refs,
                observed_at=observed_at,
                attributes=attributes,
            )
            if existing is not None:
                return existing
            candidate = copy.deepcopy(self._events)
            event = append_context_trace_event(
                candidate,
                event_name=event_name,
                binding=self._binding,
                source=self._source,
                evidence_refs=evidence_refs,
                observed_at=observed_at,
                attributes=attributes,
                event_id=event_id,
            )
            self._events = candidate
            return copy.deepcopy(event)

        with _exclusive_trace_lock(self._output_path):
            persisted = self._read_persisted_events()
            existing = self._matching_event(
                persisted,
                event_id=event_id,
                event_name=event_name,
                evidence_refs=evidence_refs,
                observed_at=observed_at,
                attributes=attributes,
            )
            if existing is not None:
                self._events = persisted
                return existing
            candidate = copy.deepcopy(persisted)
            event = append_context_trace_event(
                candidate,
                event_name=event_name,
                binding=self._binding,
                source=self._source,
                evidence_refs=evidence_refs,
                observed_at=observed_at,
                attributes=attributes,
                event_id=event_id,
            )
            try:
                with self._output_path.open("ab") as stream:
                    stream.write(canonical_context_trace_event_bytes(event) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                raise ContextTraceError("local trace append failed") from exc
            self._events = candidate
            return copy.deepcopy(event)


def _otel_result(
    *,
    status: str,
    configured: bool,
    attempted: bool,
    exported_events: int,
    exporter_id: str | None,
    evidence_ref: str | None,
    unavailable_reason: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": OTEL_EXPORT_SCHEMA_VERSION,
        "status": status,
        "configured": configured,
        "attempted": attempted,
        "exported_events": exported_events,
        "exporter_id": exporter_id,
        "evidence_ref": evidence_ref,
        "unavailable_reason": unavailable_reason,
        "authority": False,
    }


def export_context_trace_to_otel(
    events: Sequence[Mapping[str, Any]],
    *,
    exporter: Callable[[tuple[dict[str, Any], ...]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Export through an adapter while keeping absence and failure explicit."""
    validate_context_trace_chain(events)
    if exporter is None:
        return _otel_result(
            status="unavailable",
            configured=False,
            attempted=False,
            exported_events=0,
            exporter_id=None,
            evidence_ref=None,
            unavailable_reason="OTel exporter is not configured",
        )
    if not events:
        raise ContextTraceError("OTel exporter cannot report success for an empty trace chain")
    try:
        raw_receipt = exporter(tuple(copy.deepcopy(list(events))))
    except Exception as exc:  # noqa: BLE001 - exporter failures become explicit receipts
        reason = str(exc).strip() or exc.__class__.__name__
        return _otel_result(
            status="failed",
            configured=True,
            attempted=True,
            exported_events=0,
            exporter_id=None,
            evidence_ref=None,
            unavailable_reason=reason[:512],
        )
    if not isinstance(raw_receipt, Mapping) or set(raw_receipt) != _OTEL_RECEIPT_FIELDS:
        raise ContextTraceError("OTel exporter must return an explicit evidence receipt")
    if raw_receipt["status"] != "exported":
        raise ContextTraceError("OTel exporter receipt status is invalid")
    count = raw_receipt["exported_events"]
    if type(count) is not int or count != len(events):
        raise ContextTraceError("OTel exporter receipt count does not match the trace chain")
    exporter_id = _safe_id(raw_receipt["exporter_id"], "exporter_id")
    evidence_ref = _reference(raw_receipt["evidence_ref"], "OTel evidence_ref")
    return _otel_result(
        status="exported",
        configured=True,
        attempted=True,
        exported_events=count,
        exporter_id=exporter_id,
        evidence_ref=evidence_ref,
        unavailable_reason=None,
    )
