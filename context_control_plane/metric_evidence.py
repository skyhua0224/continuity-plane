"""Typed evidence envelopes for measured context accounting metrics."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

SCHEMA_VERSION = "context.metric-evidence/v1alpha1"
MAX_METRICS = 32

METRIC_NAMES = frozenset(
    {
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "billing_usd_micros",
        "retrieval_queries",
        "retrieval_read_bytes",
        "retrieval_output_bytes",
        "retrieval_latency_ms",
        "compaction_latency_ms",
        "cut_point_tokens",
        "cache_invalidated",
    }
)
METRIC_UNITS = frozenset(
    {"tokens", "usd_micros", "count", "bytes", "milliseconds", "boolean"}
)
SOURCE_KINDS = frozenset(
    {"provider_trace", "retrieval_receipt", "monotonic_clock", "host_hook"}
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_FIELDS = {
    "schema_version",
    "evidence_id",
    "accounting_id",
    "accounting_sha256",
    "corpus_id",
    "corpus_sha256",
    "route_id",
    "provider_id",
    "trace_event_id",
    "run_id",
    "observed_at",
    "metrics",
    "state_write_authority",
    "provider_native_authority",
    "evidence_sha256",
}
_METRIC_FIELDS = {"value", "unit", "source_kind"}


class MetricEvidenceError(ValueError):
    """Raised when measured metric evidence is malformed or unbound."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise MetricEvidenceError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MetricEvidenceError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MetricEvidenceError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise MetricEvidenceError(f"{field} requires a timezone")
    return value


def _metric_value(value: Any, field: str) -> int | float | bool:
    if isinstance(value, bool):
        return value
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise MetricEvidenceError(f"{field} is invalid")
    return value


def _digest(document: dict[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "evidence_sha256"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def validate_metric_evidence(
    evidence: bytes | bytearray | memoryview,
    *,
    accounting_id: str,
    accounting_sha256: str,
    corpus_id: str,
    corpus_sha256: str,
    route_id: str,
    provider_id: str,
    trace_event_id: str,
    run_id: str,
    observed_at: str,
    metric_name: str,
    value: float | bool,
    unit: str,
    source_kind: str,
) -> dict[str, Any]:
    """Validate an evidence envelope and bind one metric to its trace identity."""
    if not isinstance(evidence, (bytes, bytearray, memoryview)):
        raise MetricEvidenceError("metric evidence bytes are unavailable")
    try:
        document = json.loads(bytes(evidence).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MetricEvidenceError("metric evidence is not canonical JSON") from exc
    if not isinstance(document, dict) or set(document) != _FIELDS:
        raise MetricEvidenceError("metric evidence fields are invalid")
    if document["schema_version"] != SCHEMA_VERSION:
        raise MetricEvidenceError("metric evidence schema_version is unsupported")
    for field in (
        "evidence_id",
        "accounting_id",
        "corpus_id",
        "route_id",
        "provider_id",
        "trace_event_id",
        "run_id",
    ):
        _id(document[field], field)
    for field in ("accounting_sha256", "corpus_sha256"):
        if not isinstance(document[field], str) or _SHA_RE.fullmatch(document[field]) is None:
            raise MetricEvidenceError(f"{field} is invalid")
    _timestamp(document["observed_at"], "observed_at")
    metrics = document["metrics"]
    if not isinstance(metrics, dict) or not metrics or len(metrics) > MAX_METRICS:
        raise MetricEvidenceError("metric evidence metrics are invalid")
    for name, metric in metrics.items():
        if name not in METRIC_NAMES:
            raise MetricEvidenceError("metric_name is unsupported")
        if not isinstance(metric, dict) or set(metric) != _METRIC_FIELDS:
            raise MetricEvidenceError("metric evidence entry fields are invalid")
        _metric_value(metric["value"], f"metrics.{name}.value")
        if metric["unit"] not in METRIC_UNITS:
            raise MetricEvidenceError(f"metrics.{name}.unit is unsupported")
        if metric["source_kind"] not in SOURCE_KINDS:
            raise MetricEvidenceError(f"metrics.{name}.source_kind is unsupported")
    expected_binding = {
        "accounting_id": accounting_id,
        "accounting_sha256": accounting_sha256,
        "corpus_id": corpus_id,
        "corpus_sha256": corpus_sha256,
        "route_id": route_id,
        "provider_id": provider_id,
        "trace_event_id": trace_event_id,
        "run_id": run_id,
        "observed_at": observed_at,
    }
    if any(document[field] != expected for field, expected in expected_binding.items()):
        raise MetricEvidenceError("metric evidence binding does not match accounting trace")
    metric = metrics.get(metric_name)
    if (
        not isinstance(metric, dict)
        or type(metric.get("value")) is not type(value)
        or metric.get("value") != value
        or metric.get("unit") != unit
        or metric.get("source_kind") != source_kind
    ):
        raise MetricEvidenceError("metric evidence binding does not match measured value")
    if document["state_write_authority"] is not False:
        raise MetricEvidenceError("metric evidence cannot claim State authority")
    if document["provider_native_authority"] is not False:
        raise MetricEvidenceError("metric evidence cannot claim provider authority")
    digest = document["evidence_sha256"]
    if not isinstance(digest, str) or _SHA_RE.fullmatch(digest) is None:
        raise MetricEvidenceError("metric evidence digest is invalid")
    if digest != _digest(document):
        raise MetricEvidenceError("metric evidence digest does not match content")
    if bytes(evidence) != _canonical(document):
        raise MetricEvidenceError("metric evidence JSON is not canonical")
    return copy.deepcopy(document)


def compose_metric_evidence(
    *,
    accounting_id: str,
    accounting_sha256: str,
    corpus_id: str,
    corpus_sha256: str,
    route_id: str,
    provider_id: str,
    trace_event_id: str,
    run_id: str,
    observed_at: str,
    metrics: dict[str, dict[str, Any]],
) -> bytes:
    """Compose canonical evidence for one accounting route and trace event."""
    evidence_id = f"metric-evidence:{accounting_id}:{route_id}"
    document = {
        "schema_version": SCHEMA_VERSION,
        "evidence_id": evidence_id,
        "accounting_id": accounting_id,
        "accounting_sha256": accounting_sha256,
        "corpus_id": corpus_id,
        "corpus_sha256": corpus_sha256,
        "route_id": route_id,
        "provider_id": provider_id,
        "trace_event_id": trace_event_id,
        "run_id": run_id,
        "observed_at": observed_at,
        "metrics": copy.deepcopy(metrics),
        "state_write_authority": False,
        "provider_native_authority": False,
        "evidence_sha256": "0" * 64,
    }
    document["evidence_sha256"] = _digest(document)
    encoded = _canonical(document)
    first_name = next(iter(metrics), None)
    if first_name is None:
        raise MetricEvidenceError("metric evidence metrics are invalid")
    first = metrics[first_name]
    validate_metric_evidence(
        encoded,
        accounting_id=accounting_id,
        accounting_sha256=accounting_sha256,
        corpus_id=corpus_id,
        corpus_sha256=corpus_sha256,
        route_id=route_id,
        provider_id=provider_id,
        trace_event_id=trace_event_id,
        run_id=run_id,
        observed_at=observed_at,
        metric_name=first_name,
        value=first["value"],
        unit=first["unit"],
        source_kind=first["source_kind"],
    )
    return encoded


__all__ = [
    "MAX_METRICS",
    "METRIC_NAMES",
    "METRIC_UNITS",
    "SCHEMA_VERSION",
    "SOURCE_KINDS",
    "MetricEvidenceError",
    "compose_metric_evidence",
    "validate_metric_evidence",
]
