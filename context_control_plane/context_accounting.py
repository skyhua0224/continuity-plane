"""Provider-neutral, evidence-bound context cost and latency accounting."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any


SCHEMA_VERSION = "context.context-accounting/v1alpha1"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_METRIC_FIELDS = {
    "status",
    "value",
    "unit",
    "source_kind",
    "evidence_ref",
    "unavailable_reason",
}
_ROUTE_FIELDS = {
    "route_id",
    "provider_id",
    "route_threshold_tokens",
    "model_free_pruning",
    "metrics",
}
_RECEIPT_FIELDS = {
    "schema_version",
    "accounting_id",
    "corpus_id",
    "corpus_sha256",
    "budget_tokens",
    "routes",
    "provider_comparison_status",
    "real_provider_improvement_claimed",
    "byte_token_proxy_used",
    "state_write_authority",
    "provider_native_authority",
    "observed_at",
    "accounting_sha256",
}
_METRIC_UNITS = {
    "input_tokens": "tokens",
    "output_tokens": "tokens",
    "cache_read_tokens": "tokens",
    "cache_write_tokens": "tokens",
    "billing_usd_micros": "usd_micros",
    "retrieval_queries": "count",
    "retrieval_read_bytes": "bytes",
    "retrieval_output_bytes": "bytes",
    "retrieval_latency_ms": "milliseconds",
    "compaction_latency_ms": "milliseconds",
    "cut_point_tokens": "tokens",
    "cache_invalidated": "boolean",
}
_PROVIDER_TRACE_METRICS = {
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "billing_usd_micros",
    "cache_invalidated",
}
_SOURCE_KINDS = {
    "provider_trace",
    "retrieval_receipt",
    "monotonic_clock",
    "host_hook",
}
_ALLOWED_MEASURED_SOURCES = {
    "retrieval_queries": {"retrieval_receipt"},
    "retrieval_read_bytes": {"retrieval_receipt"},
    "retrieval_output_bytes": {"retrieval_receipt"},
    "retrieval_latency_ms": {"retrieval_receipt", "monotonic_clock"},
    "compaction_latency_ms": {"provider_trace", "host_hook", "monotonic_clock"},
    "cut_point_tokens": {"provider_trace", "host_hook"},
}


class ContextAccountingError(ValueError):
    """Raised when an accounting receipt overstates or obscures its evidence."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise ContextAccountingError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ContextAccountingError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextAccountingError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise ContextAccountingError(f"{field} must include a timezone")
    return value


def measured_metric(
    value: int | float | bool,
    unit: str,
    *,
    source_kind: str,
    evidence_ref: str,
) -> dict[str, Any]:
    """Build an explicit measurement; metric-specific source checks occur at receipt validation."""
    return {
        "status": "measured",
        "value": value,
        "unit": unit,
        "source_kind": source_kind,
        "evidence_ref": evidence_ref,
        "unavailable_reason": None,
    }


def unavailable_metric(unit: str, reason: str) -> dict[str, Any]:
    """Build an explicit absence without a proxy value or fabricated evidence reference."""
    return {
        "status": "unavailable",
        "value": None,
        "unit": unit,
        "source_kind": None,
        "evidence_ref": None,
        "unavailable_reason": reason,
    }


def _validate_metric(name: str, metric: Any) -> None:
    if not isinstance(metric, dict) or set(metric) != _METRIC_FIELDS:
        raise ContextAccountingError(f"metric {name} fields are invalid")
    if metric["unit"] != _METRIC_UNITS[name]:
        raise ContextAccountingError(f"metric {name} unit is invalid")
    status = metric["status"]
    if status == "unavailable":
        if any(metric[field] is not None for field in ("value", "source_kind", "evidence_ref")):
            raise ContextAccountingError(f"unavailable metric {name} cannot carry a value or evidence")
        reason = metric["unavailable_reason"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise ContextAccountingError(f"unavailable metric {name} requires a bounded reason")
        return
    if status != "measured" or metric["unavailable_reason"] is not None:
        raise ContextAccountingError(f"metric {name} status is invalid")
    source = metric["source_kind"]
    if source not in _SOURCE_KINDS:
        raise ContextAccountingError(f"metric {name} source_kind is invalid")
    evidence_ref = metric["evidence_ref"]
    if (
        not isinstance(evidence_ref, str)
        or not evidence_ref
        or len(evidence_ref) > 512
        or any(character in evidence_ref for character in "\r\n\x00")
    ):
        raise ContextAccountingError(f"metric {name} evidence_ref is invalid")
    value = metric["value"]
    if name == "cache_invalidated":
        if type(value) is not bool:
            raise ContextAccountingError(f"metric {name} value is invalid")
    elif name.endswith("_ms"):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ContextAccountingError(f"metric {name} value is invalid")
    elif type(value) is not int or value < 0:
        raise ContextAccountingError(f"metric {name} value is invalid")
    if name in _PROVIDER_TRACE_METRICS and source != "provider_trace":
        raise ContextAccountingError(f"metric {name} requires provider_trace evidence")
    allowed_sources = _ALLOWED_MEASURED_SOURCES.get(name)
    if allowed_sources is not None and source not in allowed_sources:
        raise ContextAccountingError(f"metric {name} measured source is invalid")


def _validate_route(route: Any, *, budget_tokens: int) -> None:
    if not isinstance(route, dict) or set(route) != _ROUTE_FIELDS:
        raise ContextAccountingError("route fields are invalid")
    _safe_id(route["route_id"], "route_id")
    _safe_id(route["provider_id"], "provider_id")
    threshold = route["route_threshold_tokens"]
    if type(threshold) is not int or threshold <= 0 or threshold > budget_tokens:
        raise ContextAccountingError("route threshold must fit the shared token budget")
    if type(route["model_free_pruning"]) is not bool:
        raise ContextAccountingError("model_free_pruning is invalid")
    metrics = route["metrics"]
    if not isinstance(metrics, dict) or set(metrics) != set(_METRIC_UNITS):
        raise ContextAccountingError("route metrics are incomplete")
    for name, metric in metrics.items():
        _validate_metric(name, metric)


def _provider_comparison_status(routes: list[dict[str, Any]]) -> str:
    if not isinstance(routes, list):
        return "unavailable"
    for route in routes:
        if not isinstance(route, dict) or not isinstance(route.get("metrics"), dict):
            return "unavailable"
        if any(
            not isinstance(route["metrics"].get(name), dict)
            or route["metrics"][name].get("status") != "measured"
            for name in _PROVIDER_TRACE_METRICS
        ):
            return "unavailable"
    return (
        "measured"
        if routes
        else "unavailable"
    )


def _payload_sha256(receipt: dict[str, Any]) -> str:
    payload = {key: value for key, value in receipt.items() if key != "accounting_sha256"}
    return hashlib.sha256(_canonical(payload)).hexdigest()


def validate_context_accounting(receipt: Any) -> None:
    """Validate strict accounting semantics and evidence authority boundaries."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise ContextAccountingError("context accounting fields are invalid")
    if receipt["schema_version"] != SCHEMA_VERSION:
        raise ContextAccountingError("context accounting schema_version is unsupported")
    _safe_id(receipt["accounting_id"], "accounting_id")
    _safe_id(receipt["corpus_id"], "corpus_id")
    if not isinstance(receipt["corpus_sha256"], str) or _SHA256_RE.fullmatch(receipt["corpus_sha256"]) is None:
        raise ContextAccountingError("corpus_sha256 is invalid")
    budget_tokens = receipt["budget_tokens"]
    if type(budget_tokens) is not int or budget_tokens <= 0:
        raise ContextAccountingError("budget_tokens is invalid")
    routes = receipt["routes"]
    if not isinstance(routes, list) or len(routes) < 2:
        raise ContextAccountingError("at least two comparable routes are required")
    for route in routes:
        _validate_route(route, budget_tokens=budget_tokens)
    route_ids = [route["route_id"] for route in routes]
    provider_ids = [route["provider_id"] for route in routes]
    if len(route_ids) != len(set(route_ids)):
        raise ContextAccountingError("route_id values must be unique")
    if len(provider_ids) != len(set(provider_ids)):
        raise ContextAccountingError("provider_id values must be unique for a provider comparison")
    expected_status = _provider_comparison_status(routes)
    if receipt["provider_comparison_status"] != expected_status:
        raise ContextAccountingError("provider comparison status does not match trace availability")
    for field in (
        "real_provider_improvement_claimed",
        "byte_token_proxy_used",
        "state_write_authority",
        "provider_native_authority",
    ):
        if type(receipt[field]) is not bool:
            raise ContextAccountingError(f"{field} is invalid")
    if receipt["byte_token_proxy_used"]:
        raise ContextAccountingError("byte values cannot be used as a provider token proxy")
    if receipt["state_write_authority"] or receipt["provider_native_authority"]:
        raise ContextAccountingError("accounting receipts have no state or provider authority")
    if receipt["real_provider_improvement_claimed"] and expected_status != "measured":
        raise ContextAccountingError("provider improvement cannot be claimed without complete traces")
    _timestamp(receipt["observed_at"], "observed_at")
    if not isinstance(receipt["accounting_sha256"], str) or _SHA256_RE.fullmatch(receipt["accounting_sha256"]) is None:
        raise ContextAccountingError("accounting_sha256 is invalid")
    if receipt["accounting_sha256"] != _payload_sha256(receipt):
        raise ContextAccountingError("accounting_sha256 does not match the receipt")


def compose_context_accounting(
    *,
    accounting_id: str,
    corpus_id: str,
    corpus_sha256: str,
    budget_tokens: int,
    routes: list[dict[str, Any]],
    observed_at: str,
    real_provider_improvement_claimed: bool = False,
) -> dict[str, Any]:
    """Compose one shared-corpus, shared-budget accounting receipt."""
    copied_routes = copy.deepcopy(routes)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "accounting_id": accounting_id,
        "corpus_id": corpus_id,
        "corpus_sha256": corpus_sha256,
        "budget_tokens": budget_tokens,
        "routes": copied_routes,
        "provider_comparison_status": _provider_comparison_status(copied_routes),
        "real_provider_improvement_claimed": real_provider_improvement_claimed,
        "byte_token_proxy_used": False,
        "state_write_authority": False,
        "provider_native_authority": False,
        "observed_at": observed_at,
        "accounting_sha256": "",
    }
    receipt["accounting_sha256"] = _payload_sha256(receipt)
    validate_context_accounting(receipt)
    return receipt


def canonical_context_accounting_bytes(receipt: dict[str, Any]) -> bytes:
    validate_context_accounting(receipt)
    return _canonical(receipt)
