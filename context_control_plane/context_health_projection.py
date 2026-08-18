"""Signed M9-04 Context, Reference, Harness Health and Replay projection."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from .artifact_store import ArtifactRef, LocalArtifactStore
from .checkpoint import CheckpointError, restore_checkpoint
from .context_accounting import ContextAccountingError, validate_context_accounting
from .context_trace import ContextTraceError, validate_context_trace_chain
from .context_trace_benchmark import REQUIRED_EVENT_NAMES
from .decision_evidence_projection import (
    DecisionEvidenceProjectionError,
    validate_decision_evidence_projection,
)
from .durable_continuation import (
    DurableContinuationError,
    evaluate_durable_recovery,
    validate_durable_continuation,
)
from .external_state_provider import (
    ExternalStateProjectionError,
    HMACExternalStateProjectionSigner,
    typed_state_snapshot_sha256,
    validate_external_state_projection,
)
from .harness_run import (
    HarnessRunError,
    replay_harness_events,
    validate_harness_run,
)
from .metric_evidence import MetricEvidenceError, validate_metric_evidence
from .postcompact_canary import (
    PostCompactCanaryError,
    evaluate_postcompact_canary,
)
from .reference_watcher import (
    ReferenceWatcherError,
    validate_reference_watch_decision,
)

CONTEXT_HEALTH_PROJECTION_SCHEMA_VERSION = (
    "context.context-health-projection/v1alpha1"
)

MAX_TRACE_EVENTS = 10_000
MAX_COMPACTION_RECORDS = 10_000
MAX_ACCOUNTING_RECORDS = 10_000
MAX_REFERENCE_RECORDS = 10_000
MAX_HARNESS_RUNS = 10_000
MAX_HARNESS_EVENTS = 50_000
MAX_RECOVERY_RECORDS = 10_000
MAX_RELATIONSHIPS = 50_000
MAX_DRILLDOWNS = 256
MAX_SOURCE_NODES = 200_000
MAX_SOURCE_STRING = 16_384

_AUTHORITY = {
    "state_write_authority": False,
    "completion_authority": False,
    "approval_authority": False,
    "provider_authority": 0,
    "external_effect_authority": 0,
}
_TOP_LEVEL_FIELDS = {
    "schema_version",
    "provider_id",
    "project_id",
    "state_revision",
    "state_schema_version",
    "state_sha256",
    "source_projection_sha256",
    "governance_ref",
    "decision_evidence_projection_sha256",
    "provenance_bundle_sha256",
    "observed_at",
    "source_bindings",
    "context_health",
    "reference_health",
    "harness_health",
    "replay_health",
    "drilldowns",
    "overall_status",
    "authority",
    "projection_sha256",
    "signature",
}
_REFERENCE_RECORD_FIELDS = {
    "reference_validity_watermark",
    "watch",
    "observation",
    "decision",
    "replacement_assertion",
}
_COMPACTION_RECORD_FIELDS = {
    "precompact_event_id",
    "postcompact_event_id",
    "trusted_binding",
    "restored_packet",
    "delta",
    "hook_receipt",
    "observed_host_metadata",
    "canary_receipt",
}
_ACCOUNTING_RECORD_FIELDS = {"trace_event_id", "receipt", "receipt_ref"}
_RECOVERY_RECORD_FIELDS = {
    "recovery_id",
    "checkpoint_ref",
    "canonical_plan_sha256",
    "effect_high_watermark",
    "durable_state",
    "restored_state",
    "proposed_first_action",
    "response_input_id",
    "requested_effect_id",
    "replay_requested",
    "recovery_reads",
    "recovery_budget_bytes",
    "observed_at",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ARTIFACT_URI_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_TOKEN_METRICS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
)


class ContextHealthProjectionError(ValueError):
    """Raised when an M9-04 source or projection cannot be trusted."""


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
        raise ContextHealthProjectionError("value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise ContextHealthProjectionError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ContextHealthProjectionError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ContextHealthProjectionError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContextHealthProjectionError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContextHealthProjectionError(f"{field} requires a timezone")
    return parsed


def _artifact_uri(value: str) -> str | None:
    return value if _ARTIFACT_URI_RE.fullmatch(value) is not None else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return round(ordered[index], 6)


def _bounded_nodes(values: Sequence[Any]) -> None:
    count = 0
    stack: list[Any] = list(values)
    while stack:
        value = stack.pop()
        count += 1
        if count > MAX_SOURCE_NODES:
            raise ContextHealthProjectionError("source capacity exceeds the contract")
        if isinstance(value, str):
            if len(value) > MAX_SOURCE_STRING:
                raise ContextHealthProjectionError(
                    "source string capacity exceeds the contract"
                )
        elif isinstance(value, Mapping):
            stack.extend(value.keys())
            stack.extend(value.values())
        elif isinstance(value, Sequence) and not isinstance(
            value, (bytes, bytearray, memoryview, str)
        ):
            stack.extend(value)


def _preflight_inputs(
    *,
    trace_events: Any,
    compaction_records: Any,
    accounting_records: Any,
    reference_records: Any,
    harness_runs: Any,
    harness_events: Any,
    recovery_records: Any,
) -> None:
    limits = (
        (trace_events, MAX_TRACE_EVENTS, "trace event"),
        (compaction_records, MAX_COMPACTION_RECORDS, "compaction record"),
        (accounting_records, MAX_ACCOUNTING_RECORDS, "accounting record"),
        (reference_records, MAX_REFERENCE_RECORDS, "reference record"),
        (harness_runs, MAX_HARNESS_RUNS, "Harness Run"),
        (harness_events, MAX_HARNESS_EVENTS, "Harness event"),
        (recovery_records, MAX_RECOVERY_RECORDS, "recovery record"),
    )
    for value, maximum, field in limits:
        if not isinstance(value, list):
            raise ContextHealthProjectionError(f"{field} source must be a list")
        if len(value) > maximum:
            raise ContextHealthProjectionError(f"{field} capacity exceeds the contract")
    relationships = 0
    for event in trace_events:
        refs = event.get("evidence_refs") if isinstance(event, Mapping) else None
        relationships += len(refs) if isinstance(refs, list) else 0
    for record in reference_records:
        if not isinstance(record, Mapping):
            continue
        for source in (record.get("watch"), record.get("observation"), record.get("decision")):
            if not isinstance(source, Mapping):
                continue
            for field in (
                "assertion_ids",
                "affected_assertion_ids",
                "stale_assertion_ids",
                "quarantined_assertion_ids",
                "superseded_assertion_ids",
                "completion_eligible_assertion_ids",
            ):
                values = source.get(field)
                relationships += len(values) if isinstance(values, list) else 0
    for run in harness_runs:
        if isinstance(run, Mapping):
            for field in ("scope_refs", "tool_grants"):
                values = run.get(field)
                relationships += len(values) if isinstance(values, list) else 0
    if relationships > MAX_RELATIONSHIPS:
        raise ContextHealthProjectionError("relationship capacity exceeds the contract")
    _bounded_nodes(
        [
            trace_events,
            compaction_records,
            accounting_records,
            reference_records,
            harness_runs,
            harness_events,
            recovery_records,
        ]
    )


def _source_artifact(value: Any) -> str:
    digest = hashlib.sha256(_canonical(value)).hexdigest()
    return f"artifact://sha256/{digest}"


def _drilldown(
    *,
    kind: str,
    object_id: str,
    status: str,
    source_sha256: str,
    artifact_refs: Sequence[str] = (),
    related_ids: Sequence[str] = (),
) -> dict[str, Any]:
    _identifier(object_id, "drilldown.object_id")
    refs = sorted(
        {
            ref
            for ref in artifact_refs
            if isinstance(ref, str) and _artifact_uri(ref) is not None
        }
    )
    return {
        "drilldown_id": f"drilldown-{_digest([kind, object_id, status])[:32]}",
        "kind": kind,
        "object_id": object_id,
        "status": status,
        "source_ref": f"context-health://{kind}/{object_id}",
        "source_sha256": _sha(source_sha256, "drilldown.source_sha256"),
        "artifact_refs": refs,
        "related_ids": sorted(set(related_ids)),
    }


def _event_projection(
    events: list[dict[str, Any]],
    *,
    source: dict[str, Any],
    observed: datetime,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    try:
        validate_context_trace_chain(events)
    except (ContextTraceError, TypeError) as exc:
        raise ContextHealthProjectionError("context trace chain is invalid") from exc
    snapshot = source["snapshot"]
    works = {item["work_id"]: item for item in snapshot["works"]}
    event_by_id: dict[str, dict[str, Any]] = {}
    summaries = []
    revision_mismatches = 0
    active_work_mismatches = 0
    observed_names: set[str] = set()
    delivery_keys: list[tuple[str, str]] = []
    for event in events:
        if event["project_id"] != source["project_id"]:
            raise ContextHealthProjectionError("trace project binding is invalid")
        if _timestamp(event["observed_at"], "trace.observed_at") > observed:
            raise ContextHealthProjectionError("trace event postdates the projection")
        if event["event_id"] in event_by_id:
            raise ContextHealthProjectionError("trace event IDs must be unique")
        event_by_id[event["event_id"]] = event
        observed_names.add(event["event_name"])
        current_revision = event["state_revision"] == source["state_revision"]
        if not current_revision:
            revision_mismatches += 1
        work = works.get(event["active_work_id"])
        work_binding = "historical" if not current_revision else "current"
        if current_revision and (
            work is None
            or event["active_work_id"] not in snapshot["project"]["active_work_ids"]
        ):
            work_binding = "missing"
            active_work_mismatches += 1
        if event["event_name"] == "context.delivery.accepted":
            delivery_keys.append((event["correlation_id"], event["operation_id"]))
        summaries.append(
            {
                "event_id": event["event_id"],
                "event_name": event["event_name"],
                "sequence": event["sequence"],
                "state_revision": event["state_revision"],
                "active_work_id": event["active_work_id"],
                "run_id": event["run_id"],
                "observed_at": event["observed_at"],
                "revision_status": "current" if current_revision else "stale",
                "work_binding": work_binding,
                "evidence_ref_count": len(event["evidence_refs"]),
                "event_sha256": event["event_sha256"],
            }
        )
    duplicate_delivery_count = len(delivery_keys) - len(set(delivery_keys))
    required_coverage = (
        len(observed_names & set(REQUIRED_EVENT_NAMES)) * 1_000_000
        // len(REQUIRED_EVENT_NAMES)
    )
    return (
        {
            "event_count": len(events),
            "event_summaries": summaries,
            "required_event_coverage_millionths": required_coverage,
            "revision_mismatch_count": revision_mismatches,
            "active_work_mismatch_count": active_work_mismatches,
            "duplicate_delivery_count": duplicate_delivery_count,
        },
        event_by_id,
        [],
    )


def _compaction_projection(
    records: list[dict[str, Any]],
    *,
    event_by_id: dict[str, dict[str, Any]],
    source: dict[str, Any],
    artifact_store: LocalArtifactStore,
    observed: datetime,
) -> tuple[dict[str, Any], set[str], list[dict[str, Any]]]:
    summaries = []
    paired_event_ids: set[str] = set()
    drilldowns: list[dict[str, Any]] = []
    restore_latencies: list[float] = []
    seen_canaries: set[str] = set()
    snapshot = source["snapshot"]
    works = {item["work_id"]: item for item in snapshot["works"]}
    for record in records:
        if not isinstance(record, dict) or set(record) != _COMPACTION_RECORD_FIELDS:
            raise ContextHealthProjectionError("compaction record fields are invalid")
        pre = event_by_id.get(record["precompact_event_id"])
        post = event_by_id.get(record["postcompact_event_id"])
        if pre is None or post is None:
            raise ContextHealthProjectionError("compaction trace event is missing")
        if (
            pre["event_name"] != "context.compaction.precompact"
            or post["event_name"] != "context.compaction.postcompact"
        ):
            raise ContextHealthProjectionError("compaction trace event kind is invalid")
        identity_fields = (
            "project_id",
            "state_revision",
            "active_work_id",
            "trace_id",
            "run_id",
            "operation_id",
            "correlation_id",
        )
        if any(pre[field] != post[field] for field in identity_fields):
            raise ContextHealthProjectionError("compaction trace identity is torn")
        if pre["sequence"] >= post["sequence"]:
            raise ContextHealthProjectionError("compaction trace order is invalid")
        canary = record["canary_receipt"]
        try:
            expected = evaluate_postcompact_canary(
                artifact_store=artifact_store,
                trusted_binding=record["trusted_binding"],
                restored_packet=record["restored_packet"],
                delta=record["delta"],
                hook_receipt=record["hook_receipt"],
                observed_host_metadata=record["observed_host_metadata"],
                observed_at=canary.get("observed_at") if isinstance(canary, dict) else None,
            )
        except (PostCompactCanaryError, TypeError) as exc:
            raise ContextHealthProjectionError("PostCompact canary is invalid") from exc
        if _canonical(expected) != _canonical(canary):
            raise ContextHealthProjectionError("PostCompact canary replay mismatch")
        if _timestamp(canary["observed_at"], "canary.observed_at") > observed:
            raise ContextHealthProjectionError("PostCompact canary postdates projection")
        if canary["canary_id"] in seen_canaries:
            raise ContextHealthProjectionError("canary IDs must be unique")
        seen_canaries.add(canary["canary_id"])
        work = works.get(canary["active_work_id"])
        if (
            canary["project_id"] != source["project_id"]
            or canary["project_revision"] != source["state_revision"]
            or canary["checkpoint_sha256"]
            != record["trusted_binding"]["checkpoint_ref"]["digest"]
            or canary["effect_high_watermark"]
            != snapshot["project"]["effect_high_watermark"]
            or work is None
            or canary["task_revision"] != work["revision"]
        ):
            raise ContextHealthProjectionError("compaction State binding is invalid")
        expected_pre_refs = {
            _source_artifact(record["delta"]),
            _source_artifact(record["hook_receipt"]),
        }
        expected_post_refs = {_source_artifact(canary)}
        if not expected_pre_refs.issubset(pre["evidence_refs"]) or not expected_post_refs.issubset(
            post["evidence_refs"]
        ):
            raise ContextHealthProjectionError("compaction trace evidence binding is invalid")
        latency = (
            _timestamp(post["observed_at"], "postcompact.observed_at")
            - _timestamp(pre["observed_at"], "precompact.observed_at")
        ).total_seconds() * 1000
        if latency < 0:
            raise ContextHealthProjectionError("compaction latency is invalid")
        restore_latencies.append(latency)
        paired_event_ids.update((pre["event_id"], post["event_id"]))
        summaries.append(
            {
                "canary_id": canary["canary_id"],
                "provider_id": canary["provider_id"],
                "active_work_id": canary["active_work_id"],
                "task_revision": canary["task_revision"],
                "checkpoint_sha256": canary["checkpoint_sha256"],
                "delta_sha256": canary["delta_sha256"],
                "canary_sha256": canary["canary_sha256"],
                "precompact_event_id": pre["event_id"],
                "postcompact_event_id": post["event_id"],
                "restore_latency_ms": round(latency, 6),
                "status": "passed",
            }
        )
    trace_compaction_ids = {
        event_id
        for event_id, event in event_by_id.items()
        if event["event_name"]
        in {"context.compaction.precompact", "context.compaction.postcompact"}
    }
    unbound = sorted(trace_compaction_ids - paired_event_ids)
    for event_id in unbound:
        event = event_by_id[event_id]
        drilldowns.append(
            _drilldown(
                kind="context-event",
                object_id=event_id,
                status="unbound",
                source_sha256=event["event_sha256"],
                artifact_refs=event["evidence_refs"],
            )
        )
    return (
        {
            "compaction_pair_count": len(summaries),
            "compaction_summaries": summaries,
            "unbound_compaction_event_ids": unbound,
            "restore_p50_ms": _percentile(restore_latencies, 0.50),
            "restore_p95_ms": _percentile(restore_latencies, 0.95),
        },
        paired_event_ids,
        drilldowns,
    )


def _accounting_projection(
    records: list[dict[str, Any]],
    *,
    event_by_id: dict[str, dict[str, Any]],
    observed: datetime,
    artifact_resolver: Callable[
        [str], bytes | bytearray | memoryview | None
    ]
    | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    summaries = []
    bound_event_ids: set[str] = set()
    provider_routes_measured = 0
    provider_routes_unavailable = 0
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "retrieval_read_bytes": 0,
        "retrieval_output_bytes": 0,
    }
    token_measurements = 0
    retrieval_measurements = 0
    for record in records:
        if not isinstance(record, dict) or set(record) != _ACCOUNTING_RECORD_FIELDS:
            raise ContextHealthProjectionError("accounting record fields are invalid")
        receipt = record["receipt"]
        try:
            validate_context_accounting(receipt)
        except (ContextAccountingError, TypeError) as exc:
            raise ContextHealthProjectionError("context accounting is invalid") from exc
        if _timestamp(receipt["observed_at"], "accounting.observed_at") > observed:
            raise ContextHealthProjectionError("context accounting postdates projection")
        expected_ref = _source_artifact(receipt)
        event = event_by_id.get(record["trace_event_id"])
        if (
            record["receipt_ref"] != expected_ref
            or event is None
            or record["receipt_ref"] not in event["evidence_refs"]
        ):
            raise ContextHealthProjectionError("context accounting trace binding is invalid")
        if record["trace_event_id"] in bound_event_ids:
            raise ContextHealthProjectionError("trace event has duplicate accounting")
        bound_event_ids.add(record["trace_event_id"])
        measured = unavailable = 0
        for route in receipt["routes"]:
            token_statuses = [route["metrics"][name]["status"] for name in _TOKEN_METRICS]
            if all(status == "measured" for status in token_statuses):
                provider_routes_measured += 1
                token_measurements += 1
            else:
                provider_routes_unavailable += 1
            if all(
                route["metrics"][name]["status"] == "measured"
                for name in ("retrieval_read_bytes", "retrieval_output_bytes")
            ):
                retrieval_measurements += 1
            for name, metric in route["metrics"].items():
                if metric["status"] == "measured":
                    try:
                        evidence = (
                            artifact_resolver(metric["evidence_ref"])
                            if artifact_resolver is not None
                            else None
                        )
                    except Exception as exc:
                        raise ContextHealthProjectionError(
                            "measured metric evidence resolver failed"
                        ) from exc
                    if not isinstance(evidence, (bytes, bytearray, memoryview)):
                        raise ContextHealthProjectionError(
                            "measured metric evidence is unavailable"
                        )
                    try:
                        validate_metric_evidence(
                            evidence,
                            accounting_id=receipt["accounting_id"],
                            accounting_sha256=receipt["accounting_sha256"],
                            corpus_id=receipt["corpus_id"],
                            corpus_sha256=receipt["corpus_sha256"],
                            route_id=route["route_id"],
                            provider_id=route["provider_id"],
                            trace_event_id=record["trace_event_id"],
                            run_id=event["run_id"],
                            observed_at=receipt["observed_at"],
                            metric_name=name,
                            value=metric["value"],
                            unit=metric["unit"],
                            source_kind=metric["source_kind"],
                        )
                    except MetricEvidenceError as exc:
                        raise ContextHealthProjectionError(
                            "measured metric evidence binding is invalid"
                        ) from exc
                    artifact_uri = _artifact_uri(metric["evidence_ref"])
                    if artifact_uri is not None and hashlib.sha256(evidence).hexdigest() != (
                        artifact_uri.rsplit("/", 1)[-1]
                    ):
                        raise ContextHealthProjectionError(
                            "measured metric evidence digest is invalid"
                        )
                    measured += 1
                    if name in totals:
                        totals[name] += metric["value"]
                else:
                    unavailable += 1
        summaries.append(
            {
                "accounting_id": receipt["accounting_id"],
                "corpus_id": receipt["corpus_id"],
                "budget_tokens": receipt["budget_tokens"],
                "provider_comparison_status": receipt[
                    "provider_comparison_status"
                ],
                "route_count": len(receipt["routes"]),
                "measured_metric_count": measured,
                "unavailable_metric_count": unavailable,
                "trace_event_id": record["trace_event_id"],
                "receipt_ref": record["receipt_ref"],
                "accounting_sha256": receipt["accounting_sha256"],
            }
        )
    token_measurement_complete = (
        provider_routes_measured > 0 and provider_routes_unavailable == 0
    )
    route_count = provider_routes_measured + provider_routes_unavailable
    retrieval_measurement_complete = (
        route_count > 0 and retrieval_measurements == route_count
    )
    return (
        {
            "accounting_record_count": len(records),
            "accounting_summaries": summaries,
            "provider_token_status": (
                "measured" if token_measurement_complete else "unavailable"
            ),
            "context_window_status": "unavailable",
            "retrieval_status": (
                "measured" if retrieval_measurement_complete else "unavailable"
            ),
            "provider_routes_measured": provider_routes_measured,
            "provider_routes_unavailable": provider_routes_unavailable,
            **{
                field: (
                    value
                    if (
                        (
                            field.startswith("retrieval_")
                            and retrieval_measurement_complete
                        )
                        or (
                            not field.startswith("retrieval_")
                            and token_measurement_complete
                        )
                    )
                    else None
                )
                for field, value in totals.items()
            },
        },
        [],
    )


def _validated_assertions(
    projection: dict[str, Any], provenance_bundle: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    projection_assertions: dict[str, str] = {}
    for evidence in projection["evidence_matrix"]:
        for item in evidence["provenance"]:
            assertion_id = item["assertion_id"]
            digest = item["assertion_record_sha256"]
            existing = projection_assertions.get(assertion_id)
            if existing is not None and existing != digest:
                raise ContextHealthProjectionError("assertion provenance is ambiguous")
            projection_assertions[assertion_id] = digest
    bundle_assertions = {
        item["assertion_id"]: item
        for item in provenance_bundle["assertion_records"]
        if item["assertion_id"] in projection_assertions
    }
    if set(bundle_assertions) != set(projection_assertions) or any(
        record["record_sha256"] != projection_assertions[assertion_id]
        for assertion_id, record in bundle_assertions.items()
    ):
        raise ContextHealthProjectionError("validated assertion source is ambiguous")
    return bundle_assertions


def _reference_projection(
    records: list[dict[str, Any]],
    *,
    validated_assertions: dict[str, dict[str, Any]],
    observed: datetime,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None] | None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None,
    trusted_time_verifier_resolver: Callable[[str, str], Mapping[str, Any] | None]
    | None,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None,
) -> tuple[dict[str, Any], int, list[dict[str, Any]]]:
    summaries = []
    stale: set[str] = set()
    quarantined: set[str] = set()
    current: set[str] = set()
    drilldowns = []
    watermarks: list[int] = []
    seen_watch_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != _REFERENCE_RECORD_FIELDS:
            raise ContextHealthProjectionError("reference record fields are invalid")
        watch = record["watch"]
        observation = record["observation"]
        decision = record["decision"]
        if not isinstance(watch, dict) or not isinstance(observation, dict) or not isinstance(
            decision, dict
        ):
            raise ContextHealthProjectionError("reference source is invalid")
        watermark = record["reference_validity_watermark"]
        if type(watermark) is not int or watermark != watch.get("watch_revision"):
            raise ContextHealthProjectionError("reference watermark is invalid")
        try:
            validate_reference_watch_decision(
                decision,
                expected_observation=observation,
                expected_watch=watch,
                artifact_resolver=artifact_resolver,
                trusted_time_verifier=trusted_time_verifier,
                trusted_time_verifier_resolver=trusted_time_verifier_resolver,
                expected_replacement_assertion=record["replacement_assertion"],
                evidence_resolver=evidence_resolver,
            )
        except (ReferenceWatcherError, TypeError) as exc:
            raise ContextHealthProjectionError("reference watch record is invalid") from exc
        if (
            _timestamp(observation["trusted_at"], "reference.trusted_at") > observed
            or _timestamp(decision["evaluated_at"], "reference.evaluated_at") > observed
        ):
            raise ContextHealthProjectionError("reference watch postdates projection")
        affected = set(decision["affected_assertion_ids"])
        eligible = set(decision["completion_eligible_assertion_ids"])
        missing = (affected | eligible) - set(validated_assertions)
        if missing:
            raise ContextHealthProjectionError(
                "reference assertion is absent from validated provenance"
            )
        for assertion_id in affected:
            assertion = validated_assertions[assertion_id]
            if not any(
                item["authority_kind"] == watch["authority_kind"]
                and item["source_ref"] == watch["source_ref"]
                and item["revision"] == watch["baseline_revision"]
                and item["sha256"] == watch["baseline_sha256"]
                for item in assertion["evidence"]
            ):
                raise ContextHealthProjectionError(
                    "reference assertion source binding is invalid"
                )
        for assertion_id in eligible - affected:
            assertion = validated_assertions[assertion_id]
            if not any(
                item["authority_kind"] == watch["authority_kind"]
                and item["source_ref"] == watch["source_ref"]
                and item["revision"] == observation["observed_revision"]
                and item["sha256"] == observation["observed_sha256"]
                for item in assertion["evidence"]
            ):
                raise ContextHealthProjectionError(
                    "replacement assertion source binding is invalid"
                )
        if watch["watch_id"] in seen_watch_ids:
            raise ContextHealthProjectionError("reference watch IDs must be unique")
        seen_watch_ids.add(watch["watch_id"])
        watermarks.append(watermark)
        stale.update(decision["stale_assertion_ids"])
        quarantined.update(decision["quarantined_assertion_ids"])
        current.update(decision["completion_eligible_assertion_ids"])
        summaries.append(
            {
                "watch_id": watch["watch_id"],
                "source_id": watch["source_id"],
                "source_identity_sha256": observation["source_identity_sha256"],
                "authority_kind": observation["authority_kind"],
                "watch_revision": watermark,
                "watch_outcome": observation["watch_outcome"],
                "freshness_status": observation["freshness_status"],
                "assertion_status": decision["assertion_status"],
                "affected_assertion_ids": sorted(affected),
                "stale_assertion_ids": sorted(decision["stale_assertion_ids"]),
                "quarantined_assertion_ids": sorted(
                    decision["quarantined_assertion_ids"]
                ),
                "completion_eligible_assertion_ids": sorted(eligible),
                "current_evidence_ref": observation["current_evidence_ref"],
                "observation_sha256": observation["observation_sha256"],
                "decision_sha256": decision["decision_sha256"],
                "evaluated_at": decision["evaluated_at"],
            }
        )
        if decision["assertion_status"] != "current":
            drilldowns.append(
                _drilldown(
                    kind="reference-watch",
                    object_id=watch["watch_id"],
                    status=decision["assertion_status"],
                    source_sha256=decision["decision_sha256"],
                    artifact_refs=[observation["current_evidence_ref"]],
                    related_ids=decision["affected_assertion_ids"],
                )
            )
    reference_status = (
        "failed"
        if stale or quarantined
        else ("passed" if records else "unavailable")
    )
    if (current & stale) or (current & quarantined) or (stale & quarantined):
        raise ContextHealthProjectionError(
            "reference assertion health states are conflicting"
        )
    return (
        {
            "status": reference_status,
            "record_count": len(records),
            "records": summaries,
            "current_assertion_ids": sorted(current),
            "stale_assertion_ids": sorted(stale),
            "quarantined_assertion_ids": sorted(quarantined),
            "validated_assertion_count": len(validated_assertions),
        },
        max(watermarks, default=0),
        drilldowns,
    )


def _harness_projection(
    runs: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    source: dict[str, Any],
    reference_watermark: int,
    checkpoint_digests: set[str],
    packet_digests: set[str],
    observed: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    snapshot = source["snapshot"]
    works = {item["work_id"]: item for item in snapshot["works"]}
    claims = {item["claim_id"]: item for item in snapshot["claims"]}
    normalized_runs: dict[str, dict[str, Any]] = {}
    summaries = []
    drilldowns = []
    for value in runs:
        try:
            run = validate_harness_run(value)
        except (HarnessRunError, TypeError) as exc:
            raise ContextHealthProjectionError("Harness Run is invalid") from exc
        if run["run_id"] in normalized_runs:
            raise ContextHealthProjectionError("Harness Run IDs must be unique")
        work = works.get(run["task_id"])
        claim = claims.get(run["claim_id"])
        if (
            run["project_id"] != source["project_id"]
            or work is None
            or run["task_revision"] != work["revision"]
            or claim is None
            or claim["status"] != "active"
            or claim["work_id"] != work["work_id"]
            or claim.get("expected_project_revision") != source["state_revision"]
            or claim.get("lease_epoch") != run["claim_lease_epoch"]
            or run["claim_fence"] != claim.get("lease_epoch")
            or run["effect_high_watermark"]
            != snapshot["project"]["effect_high_watermark"]
            or run["reference_validity_watermark"] != reference_watermark
            or run["checkpoint_id"] not in checkpoint_digests
            or run["execution_packet_sha256"] not in packet_digests
        ):
            raise ContextHealthProjectionError("Harness Run State binding is invalid")
        created = _timestamp(run["created_at"], "Harness Run created_at")
        updated = _timestamp(run["updated_at"], "Harness Run updated_at")
        lease_expires = _timestamp(claim["lease_expires_at"], "claim.lease_expires_at")
        if (
            created > updated
            or updated > observed
            or updated >= lease_expires
            or observed >= lease_expires
        ):
            raise ContextHealthProjectionError("Harness Run chronology is invalid")
        claim_scopes = {
            (item["scope_kind"], item["scope_ref"])
            for item in claim["scope_owners"]
        }
        run_scopes = {
            (item["scope_kind"], item["scope_ref"]) for item in run["scope_refs"]
        }
        if not run_scopes.issubset(claim_scopes):
            raise ContextHealthProjectionError("Harness Run scope exceeds its claim")
        normalized_runs[run["run_id"]] = run
    for run in normalized_runs.values():
        if run["parent_run_id"] is not None:
            parent = normalized_runs.get(run["parent_run_id"])
            if parent is None or any(
                run[field] != parent[field]
                for field in (
                    "project_id",
                    "task_id",
                    "task_revision",
                    "claim_id",
                    "claim_lease_epoch",
                    "claim_fence",
                )
            ):
                raise ContextHealthProjectionError("Harness Run tree binding is invalid")
        summary = {
            "run_id": run["run_id"],
            "task_id": run["task_id"],
            "task_revision": run["task_revision"],
            "claim_id": run["claim_id"],
            "claim_lease_epoch": run["claim_lease_epoch"],
            "provider": run["provider"],
            "status": run["status"],
            "parent_run_id": run["parent_run_id"],
            "checkpoint_id": run["checkpoint_id"],
            "execution_packet_sha256": run["execution_packet_sha256"],
            "skill_set_digest": run["skill_set_digest"],
            "updated_at": run["updated_at"],
            "run_sha256": run["run_sha256"],
        }
        summaries.append(summary)
        if run["status"] in {"failed", "quarantined"}:
            drilldowns.append(
                _drilldown(
                    kind="harness-run",
                    object_id=run["run_id"],
                    status=run["status"],
                    source_sha256=run["run_sha256"],
                    artifact_refs=[f"artifact://sha256/{run['checkpoint_id']}"],
                    related_ids=[run["task_id"], run["claim_id"]],
                )
            )
    try:
        replay = replay_harness_events(events, provider="other")
    except (HarnessRunError, TypeError) as exc:
        raise ContextHealthProjectionError("Harness event replay is invalid") from exc
    registered: dict[str, tuple[str, int]] = {}
    terminal_events: dict[str, tuple[str, int, str]] = {}
    for event in events:
        if event["project_id"] != source["project_id"]:
            raise ContextHealthProjectionError("Harness event project is invalid")
        if _timestamp(event["observed_at"], "Harness event observed_at") > observed:
            raise ContextHealthProjectionError("Harness event postdates projection")
        if event["event_type"] == "run-registered":
            run_id = event["payload"].get("run_id")
            run_sha256 = event["payload"].get("run_sha256")
            run = normalized_runs.get(run_id)
            if run is None or run_id in registered:
                raise ContextHealthProjectionError("Harness registration is torn")
            _sha(run_sha256, "Harness registration run_sha256")
            registered[run_id] = (run_sha256, event["sequence_no"])
        terminal: tuple[str, str, Any] | None = None
        if event["event_type"] == "worker-loss":
            terminal = (
                event["payload"].get("run_id"),
                "failed",
                event["payload"].get("run_sha256"),
            )
        elif event["event_type"] == "worker-completed":
            terminal = (
                event["payload"].get("run_id"),
                "completed",
                event["payload"].get("run_sha256"),
            )
        elif event["event_type"] == "fan-in":
            terminal = (
                event["payload"].get("parent_run_id"),
                "completed",
                event["payload"].get("run_sha256"),
            )
        if terminal is not None:
            run_id, status, run_sha256 = terminal
            if run_id not in normalized_runs or run_id in terminal_events:
                raise ContextHealthProjectionError(
                    "Harness Run lifecycle event is unbound or conflicting"
                )
            _sha(run_sha256, "Harness terminal run_sha256")
            terminal_events[run_id] = (
                status,
                event["sequence_no"],
                run_sha256,
            )
    if set(registered) != set(normalized_runs):
        raise ContextHealthProjectionError("Harness Run registration is incomplete")
    for run_id, run in normalized_runs.items():
        registered_sha256, registered_sequence = registered[run_id]
        if registered_sha256 == run["run_sha256"]:
            continue
        terminal = terminal_events.get(run_id)
        if (
            terminal is None
            or terminal[0] != run["status"]
            or terminal[1] <= registered_sequence
            or terminal[2] != run["run_sha256"]
        ):
            raise ContextHealthProjectionError(
                "Harness Run final state lacks a bound terminal digest"
            )
    summaries.sort(key=lambda item: item["run_id"])
    failed_count = sum(item["status"] == "failed" for item in summaries)
    quarantined_count = sum(item["status"] == "quarantined" for item in summaries)
    status = (
        "failed"
        if failed_count or quarantined_count
        else ("passed" if runs else "unavailable")
    )
    return (
        {
            "status": status,
            "run_count": len(runs),
            "runs": summaries,
            "failed_run_count": failed_count,
            "quarantined_run_count": quarantined_count,
            "terminal_run_count": sum(
                item["status"] in {"completed", "failed", "quarantined"}
                for item in summaries
            ),
            "event_count": replay["event_count"],
            "fan_out_count": replay["fan_out_count"],
            "fan_in_count": replay["fan_in_count"],
            "handoff_event_count": replay["handoff_event_count"],
            "effect_event_count": replay["effect_count"],
            "effect_health_status": "unavailable-v1-revision-semantics",
        },
        drilldowns,
    )


def _recovery_projection(
    records: list[dict[str, Any]],
    *,
    source: dict[str, Any],
    artifact_store: LocalArtifactStore,
    observed: datetime,
) -> tuple[dict[str, Any], set[str], list[dict[str, Any]]]:
    snapshot = source["snapshot"]
    works = {item["work_id"]: item for item in snapshot["works"]}
    summaries = []
    checkpoint_digests: set[str] = set()
    drilldowns = []
    seen_ids: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != _RECOVERY_RECORD_FIELDS:
            raise ContextHealthProjectionError("recovery record fields are invalid")
        recovery_id = _identifier(record["recovery_id"], "recovery_id")
        if recovery_id in seen_ids:
            raise ContextHealthProjectionError("recovery IDs must be unique")
        seen_ids.add(recovery_id)
        try:
            checkpoint_ref = ArtifactRef.from_document(record["checkpoint_ref"])
        except (TypeError, ValueError) as exc:
            raise ContextHealthProjectionError("checkpoint reference is invalid") from exc
        _sha(record["canonical_plan_sha256"], "canonical_plan_sha256")
        event_head = source["source"]["event_head"]
        try:
            restored_checkpoint = restore_checkpoint(
                checkpoint_ref,
                artifact_store,
                expected_project_id=source["project_id"],
                expected_revision=source["state_revision"],
                expected_event_head=event_head,
                expected_governance_ref=snapshot["project"]["governance_ref"],
                expected_plan_sha256=record["canonical_plan_sha256"],
                expected_registry_digest=source["source"]["registry_digest"],
            )
        except CheckpointError as exc:
            raise ContextHealthProjectionError("checkpoint replay is invalid") from exc
        if typed_state_snapshot_sha256(restored_checkpoint.snapshot) != source[
            "state_sha256"
        ]:
            raise ContextHealthProjectionError("checkpoint differs from signed State")
        watermark = record["effect_high_watermark"]
        if (
            type(watermark) is not int
            or watermark != snapshot["project"]["effect_high_watermark"]
            or watermark != restored_checkpoint.manifest["effect_high_watermark"]
        ):
            raise ContextHealthProjectionError("recovery effect watermark is invalid")
        try:
            durable = validate_durable_continuation(record["durable_state"])
        except (DurableContinuationError, TypeError) as exc:
            raise ContextHealthProjectionError("durable continuation is invalid") from exc
        work = works.get(durable["task_id"])
        if (
            durable["project_id"] != source["project_id"]
            or durable["project_revision"] != source["state_revision"]
            or durable["event_head"] != event_head
            or work is None
            or durable["task_revision"] != work["revision"]
        ):
            raise ContextHealthProjectionError("durable recovery State binding is invalid")
        record_observed = _timestamp(record["observed_at"], "recovery.observed_at")
        if record_observed > observed:
            raise ContextHealthProjectionError("recovery record postdates projection")
        authority = {
            "operation_id": durable["operation_id"],
            "project_id": source["project_id"],
            "project_revision": source["state_revision"],
            "task_id": durable["task_id"],
            "task_revision": durable["task_revision"],
            "event_head": copy.deepcopy(event_head),
        }
        try:
            receipt = evaluate_durable_recovery(
                trusted_state=record["durable_state"],
                restored_state=record["restored_state"],
                trusted_authority=authority,
                proposed_first_action=record["proposed_first_action"],
                response_input_id=record["response_input_id"],
                requested_effect_id=record["requested_effect_id"],
                replay_requested=record["replay_requested"],
                recovery_reads=record["recovery_reads"],
                recovery_budget_bytes=record["recovery_budget_bytes"],
                observed_at=record["observed_at"],
            )
        except DurableContinuationError as exc:
            receipt = None
            failure_kind = exc.code
        checkpoint_digests.add(checkpoint_ref.digest)
        if receipt is None:
            status = "failed"
            first_action_match = (
                False if failure_kind == "first-action-mismatch" else None
            )
            acknowledged_input_replays = None
            bytes_read = None
            receipt_sha256 = None
            drilldowns.append(
                _drilldown(
                    kind="recovery",
                    object_id=recovery_id,
                    status="failed",
                    source_sha256=_digest(record),
                    artifact_refs=[checkpoint_ref.uri],
                    related_ids=[durable["operation_id"], durable["task_id"]],
                )
            )
        else:
            status = "passed"
            failure_kind = "none"
            first_action_match = receipt["first_action"] == durable["next_action"]
            acknowledged_input_replays = receipt["acknowledged_input_replays"]
            bytes_read = receipt["recovery_read_receipt"]["bytes_read"]
            receipt_sha256 = receipt["receipt_sha256"]
        summaries.append(
            {
                "recovery_id": recovery_id,
                "operation_id": durable["operation_id"],
                "task_id": durable["task_id"],
                "task_revision": durable["task_revision"],
                "checkpoint_sha256": checkpoint_ref.digest,
                "durable_state_sha256": durable["state_sha256"],
                "phase": durable["phase"],
                "status": status,
                "failure_kind": failure_kind,
                "first_action_match": first_action_match,
                "acknowledged_input_replays": acknowledged_input_replays,
                "reserved_effect_count": len(durable["reserved_effects"]),
                "effect_high_watermark": watermark,
                "bytes_read": bytes_read,
                "receipt_sha256": receipt_sha256,
                "observed_at": record["observed_at"],
            }
        )
    summaries.sort(key=lambda item: item["recovery_id"])
    failed = sum(item["status"] == "failed" for item in summaries)
    return (
        {
            "status": "failed" if failed else ("passed" if records else "unavailable"),
            "recovery_count": len(records),
            "recoveries": summaries,
            "passed_recovery_count": len(records) - failed,
            "failed_recovery_count": failed,
            "first_action_mismatch_count": sum(
                item["first_action_match"] is False for item in summaries
            ),
            "acknowledged_input_replay_count": sum(
                item["acknowledged_input_replays"] or 0 for item in summaries
            ),
        },
        checkpoint_digests,
        drilldowns,
    )


def _status(*values: str) -> str:
    if "failed" in values:
        return "failed"
    if any(value in {"degraded", "unavailable"} for value in values):
        return "degraded"
    return "passed"


def build_context_health_projection(
    *,
    source_projection: dict[str, Any],
    decision_evidence_projection: dict[str, Any],
    provenance_bundle: dict[str, Any],
    trace_events: list[dict[str, Any]],
    compaction_records: list[dict[str, Any]],
    accounting_records: list[dict[str, Any]],
    reference_records: list[dict[str, Any]],
    harness_runs: list[dict[str, Any]],
    harness_events: list[dict[str, Any]],
    recovery_records: list[dict[str, Any]],
    artifact_store: LocalArtifactStore,
    provider_id: str,
    observed_at: str,
    signer: HMACExternalStateProjectionSigner,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None = None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None]
    | None = None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[[str, str], Mapping[str, Any] | None]
    | None = None,
) -> dict[str, Any]:
    """Build one signed, source-bound health and replay projection."""
    _preflight_inputs(
        trace_events=trace_events,
        compaction_records=compaction_records,
        accounting_records=accounting_records,
        reference_records=reference_records,
        harness_runs=harness_runs,
        harness_events=harness_events,
        recovery_records=recovery_records,
    )
    if not isinstance(artifact_store, LocalArtifactStore):
        raise ContextHealthProjectionError("artifact_store is invalid")
    provider_id = _identifier(provider_id, "provider_id")
    observed = _timestamp(observed_at, "observed_at")
    try:
        source = validate_external_state_projection(source_projection, signer=signer)
    except (ExternalStateProjectionError, ValueError) as exc:
        raise ContextHealthProjectionError("external State projection is invalid") from exc
    try:
        decision_projection = validate_decision_evidence_projection(
            decision_evidence_projection,
            source_projection=source_projection,
            signer=signer,
            provenance_bundle=provenance_bundle,
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
        )
    except (DecisionEvidenceProjectionError, TypeError) as exc:
        raise ContextHealthProjectionError(
            "Decision/Evidence projection is invalid"
        ) from exc
    if _timestamp(
        decision_projection["observed_at"], "Decision/Evidence observed_at"
    ) > observed:
        raise ContextHealthProjectionError(
            "Decision/Evidence projection postdates health projection"
        )

    context_base, event_by_id, context_drilldowns = _event_projection(
        trace_events,
        source=source,
        observed=observed,
    )
    compaction, _, compaction_drilldowns = _compaction_projection(
        compaction_records,
        event_by_id=event_by_id,
        source=source,
        artifact_store=artifact_store,
        observed=observed,
    )
    accounting, accounting_drilldowns = _accounting_projection(
        accounting_records,
        event_by_id=event_by_id,
        observed=observed,
        artifact_resolver=artifact_resolver,
    )
    validated_assertions = _validated_assertions(
        decision_projection, provenance_bundle
    )
    reference, reference_watermark, reference_drilldowns = _reference_projection(
        reference_records,
        validated_assertions=validated_assertions,
        observed=observed,
        artifact_resolver=artifact_resolver,
        trusted_time_verifier=trusted_time_verifier,
        trusted_time_verifier_resolver=trusted_time_verifier_resolver,
        evidence_resolver=evidence_resolver,
    )
    replay, checkpoint_digests, replay_drilldowns = _recovery_projection(
        recovery_records,
        source=source,
        artifact_store=artifact_store,
        observed=observed,
    )
    packet_digests = {
        record["restored_packet"]["packet_sha256"] for record in compaction_records
    }
    harness, harness_drilldowns = _harness_projection(
        harness_runs,
        harness_events,
        source=source,
        reference_watermark=reference_watermark,
        checkpoint_digests=checkpoint_digests,
        packet_digests=packet_digests,
        observed=observed,
    )
    context_status = "passed"
    if (
        context_base["required_event_coverage_millionths"] != 1_000_000
        or context_base["revision_mismatch_count"]
        or context_base["active_work_mismatch_count"]
        or context_base["duplicate_delivery_count"]
        or compaction["unbound_compaction_event_ids"]
    ):
        context_status = "failed"
    elif (
        accounting["provider_token_status"] == "unavailable"
        or accounting["context_window_status"] == "unavailable"
        or harness["effect_health_status"].startswith("unavailable")
    ):
        context_status = "degraded"
    context_health = {
        "status": context_status,
        **context_base,
        **compaction,
        **accounting,
        "slo_results": [
            {
                "slo_id": "required-event-coverage",
                "status": (
                    "passed"
                    if context_base["required_event_coverage_millionths"] == 1_000_000
                    else "failed"
                ),
                "observed": context_base[
                    "required_event_coverage_millionths"
                ],
                "threshold": 1_000_000,
                "unit": "millionths",
            },
            {
                "slo_id": "same-revision",
                "status": (
                    "passed"
                    if context_base["revision_mismatch_count"] == 0
                    else "failed"
                ),
                "observed": context_base["revision_mismatch_count"],
                "threshold": 0,
                "unit": "count",
            },
            {
                "slo_id": "compaction-restore-p95",
                "status": (
                    "unavailable"
                    if compaction["restore_p95_ms"] is None
                    else (
                        "passed"
                        if compaction["restore_p95_ms"] < 2_000
                        else "failed"
                    )
                ),
                "observed": compaction["restore_p95_ms"],
                "threshold": 2_000,
                "unit": "milliseconds",
            },
            {
                "slo_id": "provider-token-observability",
                "status": (
                    "passed"
                    if accounting["provider_token_status"] == "measured"
                    else "unavailable"
                ),
                "observed": accounting["provider_routes_measured"],
                "threshold": 1,
                "unit": "routes",
            },
            {
                "slo_id": "context-window-observability",
                "status": accounting["context_window_status"],
                "observed": None,
                "threshold": None,
                "unit": "tokens",
            },
        ],
    }
    drilldowns = sorted(
        context_drilldowns
        + compaction_drilldowns
        + accounting_drilldowns
        + reference_drilldowns
        + harness_drilldowns
        + replay_drilldowns,
        key=lambda item: item["drilldown_id"],
    )
    if len(drilldowns) > MAX_DRILLDOWNS:
        raise ContextHealthProjectionError("drilldown capacity exceeds the contract")
    trace_head = trace_events[-1]["event_sha256"] if trace_events else None
    projection: dict[str, Any] = {
        "schema_version": CONTEXT_HEALTH_PROJECTION_SCHEMA_VERSION,
        "provider_id": provider_id,
        "project_id": source["project_id"],
        "state_revision": source["state_revision"],
        "state_schema_version": source["snapshot"]["schema_version"],
        "state_sha256": source["state_sha256"],
        "source_projection_sha256": source["projection_sha256"],
        "governance_ref": source["snapshot"]["project"]["governance_ref"],
        "decision_evidence_projection_sha256": decision_projection[
            "projection_sha256"
        ],
        "provenance_bundle_sha256": provenance_bundle["bundle_sha256"],
        "observed_at": observed_at,
        "source_bindings": {
            "trace_event_count": len(trace_events),
            "trace_head_sha256": trace_head,
            "trace_source_sha256": _digest(trace_events),
            "compaction_source_sha256": _digest(compaction_records),
            "accounting_source_sha256": _digest(accounting_records),
            "reference_source_sha256": _digest(reference_records),
            "reference_validity_watermark": reference_watermark,
            "harness_source_sha256": _digest(
                {"runs": harness_runs, "events": harness_events}
            ),
            "harness_event_head_sha256": (
                harness_events[-1]["event_sha256"] if harness_events else None
            ),
            "recovery_source_sha256": _digest(recovery_records),
            "checkpoint_sha256s": sorted(checkpoint_digests),
        },
        "context_health": context_health,
        "reference_health": reference,
        "harness_health": harness,
        "replay_health": replay,
        "drilldowns": drilldowns,
        "overall_status": _status(
            context_status,
            reference["status"],
            harness["status"],
            replay["status"],
        ),
        "authority": copy.deepcopy(_AUTHORITY),
    }
    projection["projection_sha256"] = _digest(projection)
    projection["signature"] = signer.sign(projection)
    return projection


def validate_context_health_projection(
    projection: dict[str, Any],
    *,
    source_projection: dict[str, Any],
    decision_evidence_projection: dict[str, Any],
    provenance_bundle: dict[str, Any],
    trace_events: list[dict[str, Any]],
    compaction_records: list[dict[str, Any]],
    accounting_records: list[dict[str, Any]],
    reference_records: list[dict[str, Any]],
    harness_runs: list[dict[str, Any]],
    harness_events: list[dict[str, Any]],
    recovery_records: list[dict[str, Any]],
    artifact_store: LocalArtifactStore,
    signer: HMACExternalStateProjectionSigner,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None = None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None]
    | None = None,
    trusted_time_verifier: Callable[[str, str, bytes], bool] | None = None,
    trusted_time_verifier_resolver: Callable[[str, str], Mapping[str, Any] | None]
    | None = None,
) -> dict[str, Any]:
    """Rebuild and compare the complete M9-04 view against trusted sources."""
    if not isinstance(projection, dict) or set(projection) != _TOP_LEVEL_FIELDS:
        raise ContextHealthProjectionError("projection fields are invalid")
    if projection["schema_version"] != CONTEXT_HEALTH_PROJECTION_SCHEMA_VERSION:
        raise ContextHealthProjectionError("projection version is invalid")
    if projection.get("authority") != _AUTHORITY:
        raise ContextHealthProjectionError("projection authority must remain zero")
    if not isinstance(projection.get("drilldowns"), list) or len(
        projection["drilldowns"]
    ) > MAX_DRILLDOWNS:
        raise ContextHealthProjectionError("projection capacity exceeds the contract")
    _sha(projection["projection_sha256"], "projection_sha256")
    unsigned = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    if projection["projection_sha256"] != _digest(unsigned):
        raise ContextHealthProjectionError("projection digest mismatch")
    signed = {key: value for key, value in projection.items() if key != "signature"}
    if not signer.verify(signed, projection["signature"]):
        raise ContextHealthProjectionError("projection signature is invalid")
    try:
        expected = build_context_health_projection(
            source_projection=source_projection,
            decision_evidence_projection=decision_evidence_projection,
            provenance_bundle=provenance_bundle,
            trace_events=trace_events,
            compaction_records=compaction_records,
            accounting_records=accounting_records,
            reference_records=reference_records,
            harness_runs=harness_runs,
            harness_events=harness_events,
            recovery_records=recovery_records,
            artifact_store=artifact_store,
            provider_id=projection["provider_id"],
            observed_at=projection["observed_at"],
            signer=signer,
            evidence_resolver=evidence_resolver,
            artifact_resolver=artifact_resolver,
            trusted_time_verifier=trusted_time_verifier,
            trusted_time_verifier_resolver=trusted_time_verifier_resolver,
        )
    except (ContextHealthProjectionError, TypeError) as exc:
        raise ContextHealthProjectionError("projection cannot be rebuilt") from exc
    if _canonical(projection) != _canonical(expected):
        raise ContextHealthProjectionError(
            "projection does not match its signed health sources"
        )
    return copy.deepcopy(projection)


__all__ = [
    "CONTEXT_HEALTH_PROJECTION_SCHEMA_VERSION",
    "ContextHealthProjectionError",
    "build_context_health_projection",
    "validate_context_health_projection",
]
