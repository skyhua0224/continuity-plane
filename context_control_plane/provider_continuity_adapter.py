"""Open provider observation SPI and explicit live continuity event reducer."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any


MANIFEST_SCHEMA_VERSION = "context.provider-continuity-capability/v1alpha1"
EVENT_SCHEMA_VERSION = "context.provider-continuity-event/v1alpha1"

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ZERO_SHA = "0" * 64
_MANIFEST_FIELDS = {
    "schema_version",
    "provider_contract_id",
    "adapter_id",
    "adapter_sha256",
    "usage_counter_mode",
    "context_window_source",
    "lifecycle_signals",
    "raw_transcript_admission",
    "state_write_authority",
    "completion_authority",
    "manifest_sha256",
}
_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "sequence_no",
    "previous_event_sha256",
    "event_type",
    "occurred_at",
    "payload",
    "event_sha256",
}
_EVENT_TYPES = {
    "usage",
    "ingress",
    "response",
    "error",
    "precompact",
    "postcompact",
    "read",
    "production-action",
}
_COUNTER_MODES = {"delta", "cumulative"}
_WINDOW_SOURCES = {"provider-trace", "provider-config", "unavailable"}
_ACTION_FIELDS = {"kind", "target", "request_sha256"}
_USAGE_FIELDS = {
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
}
_INGRESS_FIELDS = {"input_id", "input_kind", "input_sha256", "table_requested"}
_RESPONSE_FIELDS = {
    "response_id",
    "input_id",
    "boundary_id",
    "response_sha256",
    "direct_answer",
    "table_present",
    "recovery_narration",
    "assessment_kind",
    "assessment_ref",
}
_ERROR_FIELDS = {"error_code", "error_sha256"}
_PRECOMPACT_FIELDS = {
    "boundary_id",
    "project_revision",
    "active_work_ref",
    "claim_ref",
    "expected_first_action",
    "acknowledged_input_ids",
    "packet_ref",
    "packet_sha256",
    "packet_bytes",
}
_POSTCOMPACT_FIELDS = {
    "boundary_id",
    "project_revision",
    "active_work_ref",
    "claim_ref",
    "canary_passed",
    "canary_ref",
    "canary_sha256",
}
_READ_FIELDS = {
    "boundary_id",
    "source_kind",
    "source_ref",
    "content_sha256",
    "bytes_read",
}
_PRODUCTION_FIELDS = {"boundary_id", "action"}
_READ_KINDS = {
    "bootstrap",
    "execution-packet",
    "skill",
    "document",
    "code",
    "evidence",
    "memory",
    "other",
}
_ASSESSMENT_KINDS = {"deterministic-contract", "human", "independent-model"}


class ProviderContinuityAdapterError(ValueError):
    """Raised when provider telemetry cannot prove a continuity observation."""


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
        raise ProviderContinuityAdapterError("provider value is not canonical JSON") from exc


def _digest(document: dict[str, Any], digest_field: str) -> str:
    body = copy.deepcopy(document)
    body.pop(digest_field, None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise ProviderContinuityAdapterError(f"{field} fields are invalid")
    return value


def _id(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ProviderContinuityAdapterError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ProviderContinuityAdapterError(f"{field} is invalid")
    return value


def _uint(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise ProviderContinuityAdapterError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProviderContinuityAdapterError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderContinuityAdapterError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderContinuityAdapterError(f"{field} requires timezone")
    return value


def _ids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ProviderContinuityAdapterError(f"{field} must be a list")
    normalized = [_id(item, field) for item in value]
    if len(normalized) != len(set(normalized)):
        raise ProviderContinuityAdapterError(f"{field} must contain unique values")
    return sorted(normalized)


def _validate_action(value: Any) -> dict[str, Any]:
    action = _object(value, _ACTION_FIELDS, "action")
    _id(action["kind"], "action.kind")
    _id(action["target"], "action.target")
    _sha(action["request_sha256"], "action.request_sha256")
    return action


def _action_digest(value: dict[str, Any]) -> str:
    _validate_action(value)
    return hashlib.sha256(_canonical(value)).hexdigest()


def validate_provider_capability_manifest(
    value: Any, *, verify_digest: bool = True
) -> None:
    manifest = _object(value, _MANIFEST_FIELDS, "provider capability manifest")
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise ProviderContinuityAdapterError("manifest schema_version is unsupported")
    _id(manifest["provider_contract_id"], "provider_contract_id")
    _id(manifest["adapter_id"], "adapter_id")
    _sha(manifest["adapter_sha256"], "adapter_sha256")
    if manifest["usage_counter_mode"] not in _COUNTER_MODES:
        raise ProviderContinuityAdapterError("usage counter mode is invalid")
    if manifest["context_window_source"] not in _WINDOW_SOURCES:
        raise ProviderContinuityAdapterError("context window source is invalid")
    signals = _ids(manifest["lifecycle_signals"], "lifecycle_signals")
    if not set(signals) <= _EVENT_TYPES:
        raise ProviderContinuityAdapterError("lifecycle signal is unsupported")
    for field in (
        "raw_transcript_admission",
        "state_write_authority",
        "completion_authority",
    ):
        if manifest[field] is not False:
            raise ProviderContinuityAdapterError(f"{field} must remain false")
    _sha(manifest["manifest_sha256"], "manifest_sha256")
    if verify_digest and manifest["manifest_sha256"] != _digest(manifest, "manifest_sha256"):
        raise ProviderContinuityAdapterError("manifest digest mismatch")


def compose_provider_capability_manifest(
    *,
    provider_contract_id: str,
    adapter_id: str,
    adapter_sha256: str,
    usage_counter_mode: str,
    context_window_source: str,
    lifecycle_signals: list[str],
) -> dict[str, Any]:
    """Register an open provider contract without adding it to a core enum."""
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "provider_contract_id": provider_contract_id,
        "adapter_id": adapter_id,
        "adapter_sha256": adapter_sha256,
        "usage_counter_mode": usage_counter_mode,
        "context_window_source": context_window_source,
        "lifecycle_signals": sorted(lifecycle_signals),
        "raw_transcript_admission": False,
        "state_write_authority": False,
        "completion_authority": False,
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = _digest(manifest, "manifest_sha256")
    validate_provider_capability_manifest(manifest)
    return manifest


def _validate_payload(event_type: str, value: Any) -> None:
    if event_type == "usage":
        payload = _object(value, _USAGE_FIELDS, "usage payload")
        for field in _USAGE_FIELDS:
            _uint(payload[field], f"usage.{field}")
        return
    if event_type == "ingress":
        payload = _object(value, _INGRESS_FIELDS, "ingress payload")
        for field in ("input_id", "input_kind"):
            _id(payload[field], f"ingress.{field}")
        _sha(payload["input_sha256"], "ingress.input_sha256")
        if type(payload["table_requested"]) is not bool:
            raise ProviderContinuityAdapterError("ingress.table_requested is invalid")
        return
    if event_type == "response":
        payload = _object(value, _RESPONSE_FIELDS, "response payload")
        for field in ("response_id", "input_id", "assessment_ref"):
            _id(payload[field], f"response.{field}")
        if payload["boundary_id"] is not None:
            _id(payload["boundary_id"], "response.boundary_id")
        _sha(payload["response_sha256"], "response.response_sha256")
        for field in ("direct_answer", "table_present", "recovery_narration"):
            if type(payload[field]) is not bool:
                raise ProviderContinuityAdapterError(f"response.{field} is invalid")
        if payload["assessment_kind"] not in _ASSESSMENT_KINDS:
            raise ProviderContinuityAdapterError("response assessment is not independent")
        return
    if event_type == "error":
        payload = _object(value, _ERROR_FIELDS, "error payload")
        _id(payload["error_code"], "error.error_code")
        _sha(payload["error_sha256"], "error.error_sha256")
        return
    if event_type == "precompact":
        payload = _object(value, _PRECOMPACT_FIELDS, "precompact payload")
        for field in ("boundary_id", "active_work_ref", "claim_ref", "packet_ref"):
            _id(payload[field], f"precompact.{field}")
        _uint(payload["project_revision"], "precompact.project_revision", positive=True)
        _validate_action(payload["expected_first_action"])
        _ids(payload["acknowledged_input_ids"], "precompact.acknowledged_input_ids")
        _sha(payload["packet_sha256"], "precompact.packet_sha256")
        _uint(payload["packet_bytes"], "precompact.packet_bytes", positive=True)
        return
    if event_type == "postcompact":
        payload = _object(value, _POSTCOMPACT_FIELDS, "postcompact payload")
        for field in ("boundary_id", "active_work_ref", "claim_ref", "canary_ref"):
            _id(payload[field], f"postcompact.{field}")
        _uint(payload["project_revision"], "postcompact.project_revision", positive=True)
        if type(payload["canary_passed"]) is not bool:
            raise ProviderContinuityAdapterError("postcompact.canary_passed is invalid")
        _sha(payload["canary_sha256"], "postcompact.canary_sha256")
        return
    if event_type == "read":
        payload = _object(value, _READ_FIELDS, "read payload")
        for field in ("boundary_id", "source_ref"):
            _id(payload[field], f"read.{field}")
        if payload["source_kind"] not in _READ_KINDS:
            raise ProviderContinuityAdapterError("read.source_kind is invalid")
        _sha(payload["content_sha256"], "read.content_sha256")
        _uint(payload["bytes_read"], "read.bytes_read", positive=True)
        return
    if event_type == "production-action":
        payload = _object(value, _PRODUCTION_FIELDS, "production action payload")
        _id(payload["boundary_id"], "production-action.boundary_id")
        _validate_action(payload["action"])
        return
    raise ProviderContinuityAdapterError("event_type is unsupported")


def validate_provider_continuity_event(value: Any, *, verify_digest: bool = True) -> None:
    event = _object(value, _EVENT_FIELDS, "provider continuity event")
    if event["schema_version"] != EVENT_SCHEMA_VERSION:
        raise ProviderContinuityAdapterError("event schema_version is unsupported")
    _id(event["event_id"], "event_id")
    _uint(event["sequence_no"], "sequence_no", positive=True)
    _sha(event["previous_event_sha256"], "previous_event_sha256")
    if event["event_type"] not in _EVENT_TYPES:
        raise ProviderContinuityAdapterError("event_type is unsupported")
    _timestamp(event["occurred_at"], "occurred_at")
    _validate_payload(event["event_type"], event["payload"])
    _sha(event["event_sha256"], "event_sha256")
    if verify_digest and event["event_sha256"] != _digest(event, "event_sha256"):
        raise ProviderContinuityAdapterError("event digest mismatch")


def compose_provider_continuity_event(
    *,
    previous_event: dict[str, Any] | None,
    event_id: str,
    sequence_no: int,
    event_type: str,
    occurred_at: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Compose one normalized, append-only provider event."""
    if previous_event is None:
        previous_sha = _ZERO_SHA
        if sequence_no != 1:
            raise ProviderContinuityAdapterError("first event sequence must be one")
    else:
        validate_provider_continuity_event(previous_event)
        previous_sha = previous_event["event_sha256"]
        if sequence_no != previous_event["sequence_no"] + 1:
            raise ProviderContinuityAdapterError("event sequence is not contiguous")
    event = {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": event_id,
        "sequence_no": sequence_no,
        "previous_event_sha256": previous_sha,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "payload": copy.deepcopy(payload),
        "event_sha256": "",
    }
    event["event_sha256"] = _digest(event, "event_sha256")
    validate_provider_continuity_event(event)
    return event


def validate_provider_continuity_event_chain(events: Any) -> None:
    if not isinstance(events, list) or not events:
        raise ProviderContinuityAdapterError("provider event chain is empty")
    event_ids: list[str] = []
    previous = _ZERO_SHA
    for index, event in enumerate(events, start=1):
        validate_provider_continuity_event(event)
        if event["sequence_no"] != index:
            raise ProviderContinuityAdapterError("provider event sequence has a gap")
        if event["previous_event_sha256"] != previous:
            raise ProviderContinuityAdapterError("provider event hash chain is broken")
        event_ids.append(event["event_id"])
        previous = event["event_sha256"]
    if len(event_ids) != len(set(event_ids)):
        raise ProviderContinuityAdapterError("provider event IDs must be unique")


def _usage(events: list[dict[str, Any]], counter_mode: str) -> dict[str, Any]:
    samples = [item["payload"] for item in events if item["event_type"] == "usage"]
    evidence_ref = f"provider-trace://{events[-1]['event_sha256']}"
    if not samples:
        return {
            "status": "unavailable",
            **{field: None for field in _USAGE_FIELDS},
            "evidence_ref": None,
        }
    if counter_mode == "delta":
        totals = {field: sum(item[field] for item in samples) for field in _USAGE_FIELDS}
    else:
        if len(samples) < 2:
            raise ProviderContinuityAdapterError("cumulative usage needs start and end counters")
        for previous, current in zip(samples, samples[1:]):
            if any(current[field] < previous[field] for field in _USAGE_FIELDS):
                raise ProviderContinuityAdapterError("cumulative usage counter reset")
        totals = {field: samples[-1][field] - samples[0][field] for field in _USAGE_FIELDS}
    return {"status": "measured", **totals, "evidence_ref": evidence_ref}


def derive_provider_continuity_observation(
    *,
    manifest: dict[str, Any],
    events: list[dict[str, Any]],
    source_ref: str,
    source_sha256: str,
) -> dict[str, Any]:
    """Reduce explicit provider events into sanitized segment inputs."""
    validate_provider_capability_manifest(manifest)
    validate_provider_continuity_event_chain(events)
    _id(source_ref, "source_ref")
    _sha(source_sha256, "source_sha256")
    supported = set(manifest["lifecycle_signals"])
    for event in events:
        if event["event_type"] in {
            "usage",
            "precompact",
            "postcompact",
            "read",
            "production-action",
        } and event["event_type"] not in supported:
            raise ProviderContinuityAdapterError("event exceeds manifest capability")

    ingress: dict[str, dict[str, Any]] = {}
    responses: list[dict[str, Any]] = []
    boundaries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        event_type = event["event_type"]
        payload = event["payload"]
        if event_type == "ingress":
            if payload["input_id"] in ingress:
                raise ProviderContinuityAdapterError("input identity is duplicated")
            ingress[payload["input_id"]] = payload
        elif event_type == "response":
            source = ingress.get(payload["input_id"])
            if source is None:
                raise ProviderContinuityAdapterError("response has no ingress")
            response = {
                "response_id": payload["response_id"],
                "input_id": payload["input_id"],
                "input_kind": source["input_kind"],
                "input_sha256": source["input_sha256"],
                "response_sha256": payload["response_sha256"],
                "direct_answer": payload["direct_answer"],
                "table_present": payload["table_present"],
                "table_requested": source["table_requested"],
                "recovery_narration": payload["recovery_narration"],
                "assessment_kind": payload["assessment_kind"],
                "assessment_ref": payload["assessment_ref"],
            }
            responses.append(response)
            boundary_id = payload["boundary_id"]
            if boundary_id is not None:
                boundary = boundaries.get(boundary_id)
                if boundary is None or not boundary.get("post_seen"):
                    raise ProviderContinuityAdapterError("response boundary is not postcompact")
                boundary["responded_input_ids"].add(payload["input_id"])
        elif event_type == "precompact":
            boundary_id = payload["boundary_id"]
            if boundary_id in boundaries:
                raise ProviderContinuityAdapterError("compaction boundary is duplicated")
            boundaries[boundary_id] = {
                "compaction_id": boundary_id,
                "occurred_at": event["occurred_at"],
                "pre": {
                    "project_revision": payload["project_revision"],
                    "active_work_ref": payload["active_work_ref"],
                    "claim_ref": payload["claim_ref"],
                    "expected_first_action_sha256": _action_digest(
                        payload["expected_first_action"]
                    ),
                    "acknowledged_input_ids": sorted(payload["acknowledged_input_ids"]),
                },
                "post": None,
                "post_seen": False,
                "action_seen": False,
                "responded_input_ids": set(),
                "recovery_reads": [],
            }
            order.append(boundary_id)
        elif event_type == "postcompact":
            boundary = boundaries.get(payload["boundary_id"])
            if boundary is None or boundary["post_seen"]:
                raise ProviderContinuityAdapterError("postcompact has no unique precompact")
            if payload["canary_passed"] is not True:
                raise ProviderContinuityAdapterError("postcompact canary did not pass")
            boundary["post_seen"] = True
            boundary["post"] = {
                "project_revision": payload["project_revision"],
                "active_work_ref": payload["active_work_ref"],
                "claim_ref": payload["claim_ref"],
                "actual_first_action_sha256": None,
                "responded_input_ids": [],
            }
        elif event_type == "read":
            boundary = boundaries.get(payload["boundary_id"])
            if boundary is None or not boundary["post_seen"] or boundary["action_seen"]:
                raise ProviderContinuityAdapterError("recovery read is outside the recovery interval")
            boundary["recovery_reads"].append(
                {
                    "source_kind": payload["source_kind"],
                    "source_ref": payload["source_ref"],
                    "content_sha256": payload["content_sha256"],
                    "bytes_read": payload["bytes_read"],
                }
            )
        elif event_type == "production-action":
            boundary = boundaries.get(payload["boundary_id"])
            if boundary is None or not boundary["post_seen"] or boundary["action_seen"]:
                raise ProviderContinuityAdapterError("production action is not the first postcompact action")
            boundary["action_seen"] = True
            boundary["post"]["actual_first_action_sha256"] = _action_digest(payload["action"])

    compactions: list[dict[str, Any]] = []
    for boundary_id in order:
        boundary = boundaries[boundary_id]
        if not boundary["post_seen"] or not boundary["action_seen"]:
            raise ProviderContinuityAdapterError("compaction boundary is incomplete")
        boundary["post"]["responded_input_ids"] = sorted(boundary["responded_input_ids"])
        compactions.append(
            {
                key: copy.deepcopy(boundary[key])
                for key in ("compaction_id", "occurred_at", "pre", "post", "recovery_reads")
            }
        )
    return {
        "trace": {
            "source_kind": "provider-export",
            "source_ref": source_ref,
            "source_sha256": source_sha256,
            "adapter_id": manifest["adapter_id"],
            "adapter_sha256": manifest["adapter_sha256"],
            "observed_at": events[-1]["occurred_at"],
        },
        "provider_usage": _usage(events, manifest["usage_counter_mode"]),
        "compactions": compactions,
        "responses": responses,
        "event_head_sha256": events[-1]["event_sha256"],
        "raw_transcript_admission": False,
        "state_write_authority": False,
        "completion_authority": False,
    }


__all__ = [
    "ProviderContinuityAdapterError",
    "compose_provider_capability_manifest",
    "compose_provider_continuity_event",
    "derive_provider_continuity_observation",
    "validate_provider_capability_manifest",
    "validate_provider_continuity_event",
    "validate_provider_continuity_event_chain",
]
