"""Evidence-bound live continuity and context-efficiency comparison gates."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any


SEGMENT_SCHEMA_VERSION = "context.live-continuity-segment/v1alpha1"
REPORT_SCHEMA_VERSION = "context.live-continuity-report/v1alpha1"
STUDY_SCHEMA_VERSION = "context.live-continuity-study/v1alpha1"
MIN_SAMPLES_PER_ARM = 3
MAX_RECOVERY_READ_BYTES = 12 * 1024

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,511}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SEGMENT_FIELDS = {
    "schema_version",
    "segment_id",
    "arm",
    "match",
    "trace",
    "provider_usage",
    "token_attribution",
    "accepted_work",
    "compactions",
    "responses",
    "duration_ms",
    "state_write_authority",
    "completion_authority",
    "segment_sha256",
}
_MATCH_FIELDS = {
    "provider_contract_id",
    "model_id",
    "model_revision",
    "reasoning_effort",
    "context_window_tokens",
    "auto_compact_token_limit",
    "cache_policy",
    "tool_policy_sha256",
    "sandbox_policy_sha256",
    "adapter_sha256",
    "task_class",
    "project_profile_sha256",
    "project_class",
    "repository_topology",
    "collaboration_mode",
    "repository_revision",
    "state_schema_version",
    "starting_state_sha256",
    "verification_profile_sha256",
}
_TRACE_FIELDS = {
    "source_kind",
    "source_ref",
    "source_sha256",
    "adapter_id",
    "adapter_sha256",
    "observed_at",
}
_USAGE_FIELDS = {
    "status",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "evidence_ref",
}
_ATTRIBUTION_FIELDS = {
    "status",
    "tokenizer_id",
    "authoritative_current_tokens",
    "task_relevant_tokens",
    "skill_tokens",
    "repeated_history_tokens",
    "recovery_tokens",
    "other_tokens",
    "evidence_ref",
}
_WORK_FIELDS = {
    "work_ref",
    "completion_receipt_ref",
    "completion_receipt_sha256",
    "accepted_at",
}
_COMPACTION_FIELDS = {"compaction_id", "occurred_at", "pre", "post", "recovery_reads"}
_PRE_FIELDS = {
    "project_revision",
    "active_work_ref",
    "claim_ref",
    "expected_first_action_sha256",
    "acknowledged_input_ids",
}
_POST_FIELDS = {
    "project_revision",
    "active_work_ref",
    "claim_ref",
    "actual_first_action_sha256",
    "responded_input_ids",
}
_READ_FIELDS = {"source_kind", "source_ref", "content_sha256", "bytes_read"}
_RESPONSE_FIELDS = {
    "response_id",
    "input_id",
    "input_kind",
    "input_sha256",
    "response_sha256",
    "direct_answer",
    "table_present",
    "table_requested",
    "recovery_narration",
    "assessment_kind",
    "assessment_ref",
}
_REPORT_FIELDS = {
    "schema_version",
    "report_id",
    "observed_at",
    "segment_count",
    "segments_sha256",
    "matched_groups",
    "thresholds",
    "summary",
    "portability",
    "veto_failures",
    "gate_failures",
    "evidence_gaps",
    "verdict",
    "state_write_authority",
    "completion_authority",
    "report_sha256",
}
_STUDY_FIELDS = {
    "schema_version",
    "study_id",
    "created_at",
    "schedule_kind",
    "minimum_samples_per_arm",
    "baseline_feature_manifest_sha256",
    "candidate_feature_manifest_sha256",
    "planned_segments",
    "thresholds",
    "state_write_authority",
    "completion_authority",
    "study_sha256",
}
_PLANNED_SEGMENT_FIELDS = {
    "segment_id",
    "pair_id",
    "order_index",
    "arm",
    "match",
    "project_revision",
    "state_schema_version",
    "work_plan_refs",
}
_SOURCE_KINDS = {"provider-export", "host-hook"}
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
_DISALLOWED_RECOVERY_REREADS = {"skill", "document", "code", "memory"}
_ASSESSMENT_KINDS = {"deterministic-contract", "human", "independent-model"}


class LiveContinuityProbeError(ValueError):
    """Raised when live evidence is incomplete, inconsistent, or overstated."""


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
        raise LiveContinuityProbeError("value is not canonical JSON") from exc


def _digest(document: dict[str, Any], digest_field: str) -> str:
    body = copy.deepcopy(document)
    body.pop(digest_field, None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def _object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise LiveContinuityProbeError(f"{field} fields are invalid")
    return value


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise LiveContinuityProbeError(f"{field} is invalid")
    return value


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise LiveContinuityProbeError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LiveContinuityProbeError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveContinuityProbeError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LiveContinuityProbeError(f"{field} requires a timezone")
    return value


def _uint(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        raise LiveContinuityProbeError(f"{field} is invalid")
    return value


def _finite(value: Any, field: str, *, positive: bool = False) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < (1 if positive else 0)
    ):
        raise LiveContinuityProbeError(f"{field} is invalid")
    return float(value)


def _id_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise LiveContinuityProbeError(f"{field} must be a list")
    normalized = [_identifier(item, field) for item in value]
    if len(normalized) != len(set(normalized)):
        raise LiveContinuityProbeError(f"{field} must contain unique values")
    return sorted(normalized)


def _validate_match(value: Any) -> None:
    match = _object(value, _MATCH_FIELDS, "match")
    for field in (
        "provider_contract_id",
        "model_id",
        "model_revision",
        "reasoning_effort",
        "cache_policy",
        "task_class",
        "project_class",
        "repository_topology",
        "collaboration_mode",
        "repository_revision",
        "state_schema_version",
    ):
        _identifier(match[field], f"match.{field}")
    for field in (
        "tool_policy_sha256",
        "sandbox_policy_sha256",
        "adapter_sha256",
        "project_profile_sha256",
        "starting_state_sha256",
        "verification_profile_sha256",
    ):
        _sha(match[field], f"match.{field}")
    window = _uint(match["context_window_tokens"], "match.context_window_tokens", positive=True)
    compact = _uint(
        match["auto_compact_token_limit"],
        "match.auto_compact_token_limit",
        positive=True,
    )
    if compact > window:
        raise LiveContinuityProbeError("auto compact limit exceeds context window")


def _validate_trace(value: Any) -> None:
    trace = _object(value, _TRACE_FIELDS, "trace")
    if trace["source_kind"] not in _SOURCE_KINDS:
        raise LiveContinuityProbeError("trace source_kind is invalid")
    for field in ("source_ref", "adapter_id"):
        _identifier(trace[field], f"trace.{field}")
    for field in ("source_sha256", "adapter_sha256"):
        _sha(trace[field], f"trace.{field}")
    _timestamp(trace["observed_at"], "trace.observed_at")


def _validate_usage(value: Any) -> None:
    usage = _object(value, _USAGE_FIELDS, "provider_usage")
    status = usage["status"]
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
    )
    if status == "unavailable":
        if any(usage[field] is not None for field in (*token_fields, "evidence_ref")):
            raise LiveContinuityProbeError("unavailable provider usage carries a value")
        return
    if status != "measured":
        raise LiveContinuityProbeError("provider usage status is invalid")
    for field in token_fields:
        _uint(usage[field], f"provider_usage.{field}", positive=field == "input_tokens")
    _identifier(usage["evidence_ref"], "provider_usage.evidence_ref")


def _validate_attribution(value: Any, usage: dict[str, Any]) -> None:
    attribution = _object(value, _ATTRIBUTION_FIELDS, "token_attribution")
    token_fields = (
        "authoritative_current_tokens",
        "task_relevant_tokens",
        "skill_tokens",
        "repeated_history_tokens",
        "recovery_tokens",
        "other_tokens",
    )
    if attribution["status"] == "unavailable":
        if any(
            attribution[field] is not None
            for field in ("tokenizer_id", *token_fields, "evidence_ref")
        ):
            raise LiveContinuityProbeError("unavailable attribution carries a value")
        return
    if attribution["status"] != "measured":
        raise LiveContinuityProbeError("token attribution status is invalid")
    _identifier(attribution["tokenizer_id"], "token_attribution.tokenizer_id")
    _identifier(attribution["evidence_ref"], "token_attribution.evidence_ref")
    for field in token_fields:
        _uint(attribution[field], f"token_attribution.{field}")
    if usage["status"] != "measured":
        raise LiveContinuityProbeError("measured attribution requires measured provider usage")
    if sum(attribution[field] for field in token_fields) != usage["input_tokens"]:
        raise LiveContinuityProbeError("token attribution does not equal provider input")


def _validate_accepted_work(value: Any) -> None:
    if not isinstance(value, list):
        raise LiveContinuityProbeError("accepted_work must be a list")
    identities: list[str] = []
    receipts: list[str] = []
    for index, item in enumerate(value):
        work = _object(item, _WORK_FIELDS, f"accepted_work[{index}]")
        identities.append(_identifier(work["work_ref"], "accepted_work.work_ref"))
        receipts.append(
            _identifier(work["completion_receipt_ref"], "accepted_work.completion_receipt_ref")
        )
        _sha(work["completion_receipt_sha256"], "accepted_work.completion_receipt_sha256")
        _timestamp(work["accepted_at"], "accepted_work.accepted_at")
    if len(identities) != len(set(identities)) or len(receipts) != len(set(receipts)):
        raise LiveContinuityProbeError("accepted Work and receipts must be unique")


def _validate_compactions(value: Any) -> None:
    if not isinstance(value, list):
        raise LiveContinuityProbeError("compactions must be a list")
    identities: list[str] = []
    for index, item in enumerate(value):
        compact = _object(item, _COMPACTION_FIELDS, f"compactions[{index}]")
        identities.append(_identifier(compact["compaction_id"], "compaction_id"))
        _timestamp(compact["occurred_at"], "compaction.occurred_at")
        pre = _object(compact["pre"], _PRE_FIELDS, "compaction.pre")
        post = _object(compact["post"], _POST_FIELDS, "compaction.post")
        for state, fields in ((pre, _PRE_FIELDS), (post, _POST_FIELDS)):
            _uint(state["project_revision"], "compaction.project_revision", positive=True)
            for field in fields & {"active_work_ref", "claim_ref"}:
                _identifier(state[field], f"compaction.{field}")
        _sha(pre["expected_first_action_sha256"], "expected_first_action_sha256")
        _sha(post["actual_first_action_sha256"], "actual_first_action_sha256")
        _id_list(pre["acknowledged_input_ids"], "acknowledged_input_ids")
        _id_list(post["responded_input_ids"], "responded_input_ids")
        reads = compact["recovery_reads"]
        if not isinstance(reads, list):
            raise LiveContinuityProbeError("recovery_reads must be a list")
        read_refs: list[str] = []
        for read_index, item in enumerate(reads):
            read = _object(item, _READ_FIELDS, f"recovery_reads[{read_index}]")
            if read["source_kind"] not in _READ_KINDS:
                raise LiveContinuityProbeError("recovery read source_kind is invalid")
            read_refs.append(_identifier(read["source_ref"], "recovery_read.source_ref"))
            _sha(read["content_sha256"], "recovery_read.content_sha256")
            _uint(read["bytes_read"], "recovery_read.bytes_read", positive=True)
        if len(read_refs) != len(set(read_refs)):
            raise LiveContinuityProbeError("recovery read refs must be unique per boundary")
    if len(identities) != len(set(identities)):
        raise LiveContinuityProbeError("compaction IDs must be unique")


def _validate_responses(value: Any) -> None:
    if not isinstance(value, list):
        raise LiveContinuityProbeError("responses must be a list")
    identities: list[str] = []
    for index, item in enumerate(value):
        response = _object(item, _RESPONSE_FIELDS, f"responses[{index}]")
        for field in ("response_id", "input_id", "input_kind", "assessment_ref"):
            _identifier(response[field], f"response.{field}")
        identities.append(response["response_id"])
        _sha(response["input_sha256"], "response.input_sha256")
        _sha(response["response_sha256"], "response.response_sha256")
        for field in (
            "direct_answer",
            "table_present",
            "table_requested",
            "recovery_narration",
        ):
            if type(response[field]) is not bool:
                raise LiveContinuityProbeError(f"response.{field} is invalid")
        if response["assessment_kind"] not in _ASSESSMENT_KINDS:
            raise LiveContinuityProbeError("response assessment is not independent")
    if len(identities) != len(set(identities)):
        raise LiveContinuityProbeError("response IDs must be unique")


def validate_live_continuity_segment(value: Any, *, verify_digest: bool = True) -> None:
    """Validate one sanitized live segment without accepting raw conversation text."""
    segment = _object(value, _SEGMENT_FIELDS, "live continuity segment")
    if segment["schema_version"] != SEGMENT_SCHEMA_VERSION:
        raise LiveContinuityProbeError("segment schema_version is unsupported")
    _identifier(segment["segment_id"], "segment_id")
    if segment["arm"] not in {"baseline", "candidate"}:
        raise LiveContinuityProbeError("segment arm is invalid")
    _validate_match(segment["match"])
    _validate_trace(segment["trace"])
    _validate_usage(segment["provider_usage"])
    _validate_attribution(segment["token_attribution"], segment["provider_usage"])
    _validate_accepted_work(segment["accepted_work"])
    _validate_compactions(segment["compactions"])
    _validate_responses(segment["responses"])
    _finite(segment["duration_ms"], "duration_ms", positive=True)
    if segment["state_write_authority"] is not False or segment["completion_authority"] is not False:
        raise LiveContinuityProbeError("probe segments have no authority")
    _sha(segment["segment_sha256"], "segment_sha256")
    if verify_digest and segment["segment_sha256"] != _digest(segment, "segment_sha256"):
        raise LiveContinuityProbeError("segment_sha256 does not match segment")


def validate_live_continuity_study(value: Any, *, verify_digest: bool = True) -> None:
    """Validate the pre-registered study denominator and balanced execution order."""
    study = _object(value, _STUDY_FIELDS, "live continuity study")
    if study["schema_version"] != STUDY_SCHEMA_VERSION:
        raise LiveContinuityProbeError("study schema_version is unsupported")
    _identifier(study["study_id"], "study_id")
    _timestamp(study["created_at"], "created_at")
    if study["schedule_kind"] != "abba-balanced":
        raise LiveContinuityProbeError("study schedule_kind is unsupported")
    minimum = _uint(
        study["minimum_samples_per_arm"],
        "minimum_samples_per_arm",
        positive=True,
    )
    if minimum < MIN_SAMPLES_PER_ARM:
        raise LiveContinuityProbeError("minimum samples per arm is too small")
    baseline_digest = _sha(
        study["baseline_feature_manifest_sha256"],
        "baseline_feature_manifest_sha256",
    )
    candidate_digest = _sha(
        study["candidate_feature_manifest_sha256"],
        "candidate_feature_manifest_sha256",
    )
    if baseline_digest == candidate_digest:
        raise LiveContinuityProbeError("study arms must differ by feature manifest")
    planned = study["planned_segments"]
    if not isinstance(planned, list) or len(planned) < minimum * 2:
        raise LiveContinuityProbeError("planned segments do not satisfy the denominator")
    segment_ids: list[str] = []
    pairs: dict[str, list[dict[str, Any]]] = {}
    arms: list[str] = []
    for index, item in enumerate(planned):
        segment = _object(item, _PLANNED_SEGMENT_FIELDS, f"planned_segments[{index}]")
        segment_ids.append(_identifier(segment["segment_id"], "planned segment_id"))
        pair_id = _identifier(segment["pair_id"], "planned pair_id")
        if segment["order_index"] != index:
            raise LiveContinuityProbeError("planned segment order_index is not contiguous")
        if segment["arm"] not in {"baseline", "candidate"}:
            raise LiveContinuityProbeError("planned segment arm is invalid")
        arms.append(segment["arm"])
        pairs.setdefault(pair_id, []).append(segment)
        _validate_match(segment["match"])
        _identifier(segment["project_revision"], "planned project_revision")
        _identifier(segment["state_schema_version"], "planned state_schema_version")
        work_refs = _id_list(segment["work_plan_refs"], "planned work_plan_refs")
        if len(work_refs) < 1:
            raise LiveContinuityProbeError("planned segment requires Work units")
    if len(segment_ids) != len(set(segment_ids)):
        raise LiveContinuityProbeError("planned segment IDs must be unique")
    for pair in pairs.values():
        if len(pair) != 2 or {item["arm"] for item in pair} != {"baseline", "candidate"}:
            raise LiveContinuityProbeError("each planned pair requires both arms")
        if pair[0]["match"] != pair[1]["match"]:
            raise LiveContinuityProbeError("planned pair match keys differ")
        if pair[0]["work_plan_refs"] != pair[1]["work_plan_refs"]:
            raise LiveContinuityProbeError("planned pair Work denominators differ")
    if arms.count("baseline") < minimum or arms.count("candidate") < minimum:
        raise LiveContinuityProbeError("planned arms do not satisfy minimum samples")
    arm_symbols = "".join("A" if arm == "baseline" else "B" for arm in arms)
    for offset in range(0, len(arm_symbols) - len(arm_symbols) % 4, 4):
        if arm_symbols[offset : offset + 4] not in {"ABBA", "BAAB"}:
            raise LiveContinuityProbeError("planned schedule is not ABBA/BAAB balanced")
    remainder = arm_symbols[len(arm_symbols) - len(arm_symbols) % 4 :]
    if remainder and remainder not in {"AB", "BA"}:
        raise LiveContinuityProbeError("balanced schedule has an invalid trailing pair")
    expected_thresholds = {
        "input_tokens_per_work_reduction_percent": 30.0,
        "output_tokens_per_work_reduction_percent": 10.0,
        "total_tokens_per_work_reduction_percent": 30.0,
        "accepted_work_per_compaction_improvement_percent": 30.0,
        "useful_context_ratio_improvement_percent": 30.0,
        "maximum_recovery_read_bytes": MAX_RECOVERY_READ_BYTES,
        "direct_answer_rate": 1.0,
    }
    if study["thresholds"] != expected_thresholds:
        raise LiveContinuityProbeError("study thresholds are not canonical")
    if study["state_write_authority"] is not False or study["completion_authority"] is not False:
        raise LiveContinuityProbeError("study plans have no authority")
    _sha(study["study_sha256"], "study_sha256")
    if verify_digest and study["study_sha256"] != _digest(study, "study_sha256"):
        raise LiveContinuityProbeError("study digest does not match the plan")


def compose_live_continuity_study(
    *,
    study_id: str,
    created_at: str,
    planned_segments: list[dict[str, Any]],
    baseline_feature_manifest_sha256: str,
    candidate_feature_manifest_sha256: str,
    schedule_kind: str,
) -> dict[str, Any]:
    """Freeze a balanced live study before any segment evidence is observed."""
    study = {
        "schema_version": STUDY_SCHEMA_VERSION,
        "study_id": study_id,
        "created_at": created_at,
        "schedule_kind": schedule_kind,
        "minimum_samples_per_arm": MIN_SAMPLES_PER_ARM,
        "baseline_feature_manifest_sha256": baseline_feature_manifest_sha256,
        "candidate_feature_manifest_sha256": candidate_feature_manifest_sha256,
        "planned_segments": copy.deepcopy(planned_segments),
        "thresholds": {
            "input_tokens_per_work_reduction_percent": 30.0,
            "output_tokens_per_work_reduction_percent": 10.0,
            "total_tokens_per_work_reduction_percent": 30.0,
            "accepted_work_per_compaction_improvement_percent": 30.0,
            "useful_context_ratio_improvement_percent": 30.0,
            "maximum_recovery_read_bytes": MAX_RECOVERY_READ_BYTES,
            "direct_answer_rate": 1.0,
        },
        "state_write_authority": False,
        "completion_authority": False,
        "study_sha256": "",
    }
    study["study_sha256"] = _digest(study, "study_sha256")
    validate_live_continuity_study(study)
    return study


def compose_live_continuity_segment(
    *,
    segment_id: str,
    arm: str,
    match: dict[str, Any],
    trace: dict[str, Any],
    provider_usage: dict[str, Any],
    token_attribution: dict[str, Any],
    accepted_work: list[dict[str, Any]],
    compactions: list[dict[str, Any]],
    responses: list[dict[str, Any]],
    duration_ms: int | float,
) -> dict[str, Any]:
    """Compose a strict sanitized segment from adapter and State receipts."""
    segment = {
        "schema_version": SEGMENT_SCHEMA_VERSION,
        "segment_id": segment_id,
        "arm": arm,
        "match": copy.deepcopy(match),
        "trace": copy.deepcopy(trace),
        "provider_usage": copy.deepcopy(provider_usage),
        "token_attribution": copy.deepcopy(token_attribution),
        "accepted_work": copy.deepcopy(accepted_work),
        "compactions": copy.deepcopy(compactions),
        "responses": copy.deepcopy(responses),
        "duration_ms": duration_ms,
        "state_write_authority": False,
        "completion_authority": False,
        "segment_sha256": "",
    }
    segment["segment_sha256"] = _digest(segment, "segment_sha256")
    validate_live_continuity_segment(segment)
    return segment


def _match_key(segment: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(segment["match"])).hexdigest()


def _percent_reduction(baseline: float, candidate: float) -> float:
    if baseline <= 0:
        return 0.0
    return round((baseline - candidate) / baseline * 100, 6)


def _percent_improvement(baseline: float, candidate: float) -> float:
    if baseline <= 0:
        return 0.0
    return round((candidate - baseline) / baseline * 100, 6)


def _arm_metrics(segments: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = sum(len(item["accepted_work"]) for item in segments)
    compactions = sum(len(item["compactions"]) for item in segments)
    input_tokens = sum(item["provider_usage"]["input_tokens"] for item in segments)
    output_tokens = sum(item["provider_usage"]["output_tokens"] for item in segments)
    useful_tokens = sum(
        item["token_attribution"]["authoritative_current_tokens"]
        + item["token_attribution"]["task_relevant_tokens"]
        for item in segments
    )
    questions = [
        response
        for item in segments
        for response in item["responses"]
        if response["input_kind"] == "question"
    ]
    return {
        "segments": len(segments),
        "accepted_work": accepted,
        "compactions": compactions,
        "input_tokens_per_work": round(input_tokens / accepted, 6),
        "output_tokens_per_work": round(output_tokens / accepted, 6),
        "total_tokens_per_work": round((input_tokens + output_tokens) / accepted, 6),
        "accepted_work_per_compaction": round(accepted / compactions, 6),
        "compaction_interval_minutes": round(
            sum(item["duration_ms"] for item in segments) / compactions / 60_000,
            6,
        ),
        "useful_context_ratio": round(useful_tokens / input_tokens, 9),
        "direct_answer_rate": round(
            sum(response["direct_answer"] for response in questions) / len(questions),
            9,
        ),
        "unwanted_table_count": sum(
            response["table_present"] and not response["table_requested"]
            for response in questions
        ),
        "recovery_narration_count": sum(
            response["recovery_narration"]
            for item in segments
            for response in item["responses"]
        ),
    }


def _candidate_vetoes(segments: list[dict[str, Any]]) -> set[str]:
    failures: set[str] = set()
    for segment in segments:
        for response in segment["responses"]:
            if response["input_kind"] == "question" and not response["direct_answer"]:
                failures.add("direct-answer")
            if response["table_present"] and not response["table_requested"]:
                failures.add("unrequested-table")
            if response["recovery_narration"]:
                failures.add("recovery-narration")
        for compact in segment["compactions"]:
            pre = compact["pre"]
            post = compact["post"]
            if pre["active_work_ref"] != post["active_work_ref"]:
                failures.add("active-work-mismatch")
            if pre["claim_ref"] != post["claim_ref"]:
                failures.add("claim-mismatch")
            if post["project_revision"] < pre["project_revision"]:
                failures.add("revision-regression")
            if pre["expected_first_action_sha256"] != post["actual_first_action_sha256"]:
                failures.add("first-action-mismatch")
            if set(pre["acknowledged_input_ids"]) & set(post["responded_input_ids"]):
                failures.add("acknowledged-input-replay")
            if sum(item["bytes_read"] for item in compact["recovery_reads"]) > MAX_RECOVERY_READ_BYTES:
                failures.add("unbounded-recovery-read")
            if any(
                item["source_kind"] in _DISALLOWED_RECOVERY_REREADS
                for item in compact["recovery_reads"]
            ):
                failures.add("skill-reread-after-compaction")
    return failures


def evaluate_live_continuity_comparison(
    segments: list[dict[str, Any]],
    *,
    study: dict[str, Any],
    report_id: str,
    observed_at: str,
    required_project_profiles: int = 2,
    required_provider_contracts: int = 2,
    required_collaboration_modes: int = 2,
) -> dict[str, Any]:
    """Derive a matched A/B verdict; observations cannot directly set the result."""
    if not isinstance(segments, list) or not segments:
        raise LiveContinuityProbeError("segments are required")
    validate_live_continuity_study(study)
    for segment in segments:
        validate_live_continuity_segment(segment)
    segment_ids = [item["segment_id"] for item in segments]
    if len(segment_ids) != len(set(segment_ids)):
        raise LiveContinuityProbeError("segment IDs must be unique")
    planned = {item["segment_id"]: item for item in study["planned_segments"]}
    if set(segment_ids) != set(planned):
        raise LiveContinuityProbeError("observed segments do not equal the frozen denominator")
    study_time = datetime.fromisoformat(study["created_at"].replace("Z", "+00:00"))
    for segment in segments:
        plan = planned[segment["segment_id"]]
        if segment["arm"] != plan["arm"] or segment["match"] != plan["match"]:
            raise LiveContinuityProbeError("observed segment differs from its frozen match")
        if segment["trace"]["adapter_sha256"] != segment["match"]["adapter_sha256"]:
            raise LiveContinuityProbeError("segment adapter differs from its match key")
        if plan["project_revision"] != segment["match"]["repository_revision"]:
            raise LiveContinuityProbeError("planned repository revision differs from match key")
        if plan["state_schema_version"] != segment["match"]["state_schema_version"]:
            raise LiveContinuityProbeError("planned State schema differs from match key")
        observed_time = datetime.fromisoformat(
            segment["trace"]["observed_at"].replace("Z", "+00:00")
        )
        if observed_time < study_time:
            raise LiveContinuityProbeError("segment predates the frozen study")
        work_refs = {item["work_ref"] for item in segment["accepted_work"]}
        if not work_refs <= set(plan["work_plan_refs"]):
            raise LiveContinuityProbeError("accepted Work was not pre-registered")
    _identifier(report_id, "report_id")
    _timestamp(observed_at, "observed_at")
    for value, field in (
        (required_project_profiles, "required_project_profiles"),
        (required_provider_contracts, "required_provider_contracts"),
        (required_collaboration_modes, "required_collaboration_modes"),
    ):
        _uint(value, field, positive=True)

    gaps: set[str] = set()
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for segment in segments:
        groups.setdefault(_match_key(segment), {"baseline": [], "candidate": []})[
            segment["arm"]
        ].append(segment)
        if segment["provider_usage"]["status"] != "measured":
            gaps.add("provider-usage")
        if segment["token_attribution"]["status"] != "measured":
            gaps.add("token-attribution")
        if not segment["accepted_work"]:
            gaps.add("accepted-work")
        if not segment["compactions"]:
            gaps.add("compaction-boundary")
        if not any(response["input_kind"] == "question" for response in segment["responses"]):
            gaps.add("question-response")
    matched_groups = []
    for key, arms in sorted(groups.items()):
        counts = {arm: len(items) for arm, items in arms.items()}
        if any(count < MIN_SAMPLES_PER_ARM for count in counts.values()):
            gaps.add("matched-sample-count")
        matched_groups.append({"match_sha256": key, **counts})

    projects = {item["match"]["project_profile_sha256"] for item in segments}
    providers = {item["match"]["provider_contract_id"] for item in segments}
    collaborations = {item["match"]["collaboration_mode"] for item in segments}
    if len(projects) < required_project_profiles:
        gaps.add("project-profile-coverage")
    if len(providers) < required_provider_contracts:
        gaps.add("provider-contract-coverage")
    if len(collaborations) < required_collaboration_modes:
        gaps.add("collaboration-mode-coverage")

    measured = not gaps
    summary: dict[str, Any] | None = None
    gate_failures: set[str] = set()
    vetoes = _candidate_vetoes(
        [item for item in segments if item["arm"] == "candidate"]
    )
    if measured:
        baseline = _arm_metrics([item for item in segments if item["arm"] == "baseline"])
        candidate = _arm_metrics([item for item in segments if item["arm"] == "candidate"])
        summary = {
            "baseline": baseline,
            "candidate": candidate,
            "input_tokens_per_work_reduction_percent": _percent_reduction(
                baseline["input_tokens_per_work"], candidate["input_tokens_per_work"]
            ),
            "output_tokens_per_work_reduction_percent": _percent_reduction(
                baseline["output_tokens_per_work"], candidate["output_tokens_per_work"]
            ),
            "total_tokens_per_work_reduction_percent": _percent_reduction(
                baseline["total_tokens_per_work"], candidate["total_tokens_per_work"]
            ),
            "accepted_work_per_compaction_improvement_percent": _percent_improvement(
                baseline["accepted_work_per_compaction"],
                candidate["accepted_work_per_compaction"],
            ),
            "compaction_interval_improvement_percent": _percent_improvement(
                baseline["compaction_interval_minutes"],
                candidate["compaction_interval_minutes"],
            ),
            "useful_context_ratio_improvement_percent": _percent_improvement(
                baseline["useful_context_ratio"], candidate["useful_context_ratio"]
            ),
        }
        thresholds = {
            "minimum_samples_per_arm": MIN_SAMPLES_PER_ARM,
            "input_tokens_per_work_reduction_percent": 30.0,
            "output_tokens_per_work_reduction_percent": 10.0,
            "total_tokens_per_work_reduction_percent": 30.0,
            "accepted_work_per_compaction_improvement_percent": 30.0,
            "useful_context_ratio_improvement_percent": 30.0,
            "maximum_recovery_read_bytes": MAX_RECOVERY_READ_BYTES,
            "direct_answer_rate": 1.0,
        }
        for metric in (
            "input_tokens_per_work_reduction_percent",
            "output_tokens_per_work_reduction_percent",
            "total_tokens_per_work_reduction_percent",
            "accepted_work_per_compaction_improvement_percent",
            "useful_context_ratio_improvement_percent",
        ):
            if summary[metric] < thresholds[metric]:
                gate_failures.add(metric.replace("_", "-"))
        if candidate["direct_answer_rate"] != 1.0:
            vetoes.add("direct-answer")
        if candidate["unwanted_table_count"]:
            vetoes.add("unrequested-table")
        if candidate["recovery_narration_count"]:
            vetoes.add("recovery-narration")
    else:
        thresholds = {
            "minimum_samples_per_arm": MIN_SAMPLES_PER_ARM,
            "input_tokens_per_work_reduction_percent": 30.0,
            "output_tokens_per_work_reduction_percent": 10.0,
            "total_tokens_per_work_reduction_percent": 30.0,
            "accepted_work_per_compaction_improvement_percent": 30.0,
            "useful_context_ratio_improvement_percent": 30.0,
            "maximum_recovery_read_bytes": MAX_RECOVERY_READ_BYTES,
            "direct_answer_rate": 1.0,
        }

    verdict = (
        "insufficient-evidence"
        if gaps
        else "failed"
        if vetoes or gate_failures
        else "qualified"
    )
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_id": report_id,
        "observed_at": observed_at,
        "segment_count": len(segments),
        "segments_sha256": hashlib.sha256(
            _canonical([item["segment_sha256"] for item in sorted(segments, key=lambda x: x["segment_id"])])
        ).hexdigest(),
        "matched_groups": matched_groups,
        "thresholds": thresholds,
        "summary": summary,
        "portability": {
            "project_profiles": len(projects),
            "provider_contracts": len(providers),
            "collaboration_modes": len(collaborations),
            "required_project_profiles": required_project_profiles,
            "required_provider_contracts": required_provider_contracts,
            "required_collaboration_modes": required_collaboration_modes,
        },
        "veto_failures": sorted(vetoes),
        "gate_failures": sorted(gate_failures),
        "evidence_gaps": sorted(gaps),
        "verdict": verdict,
        "state_write_authority": False,
        "completion_authority": False,
        "report_sha256": "",
    }
    report["report_sha256"] = _digest(report, "report_sha256")
    validate_live_continuity_report(report)
    return report


def validate_live_continuity_report(value: Any, *, verify_digest: bool = True) -> None:
    """Validate a derived report and its authority boundary."""
    report = _object(value, _REPORT_FIELDS, "live continuity report")
    if report["schema_version"] != REPORT_SCHEMA_VERSION:
        raise LiveContinuityProbeError("report schema_version is unsupported")
    _identifier(report["report_id"], "report_id")
    _timestamp(report["observed_at"], "observed_at")
    _uint(report["segment_count"], "segment_count", positive=True)
    _sha(report["segments_sha256"], "segments_sha256")
    if not isinstance(report["matched_groups"], list) or not report["matched_groups"]:
        raise LiveContinuityProbeError("matched_groups are required")
    if not isinstance(report["thresholds"], dict):
        raise LiveContinuityProbeError("thresholds are invalid")
    if report["summary"] is not None and not isinstance(report["summary"], dict):
        raise LiveContinuityProbeError("summary is invalid")
    if not isinstance(report["portability"], dict):
        raise LiveContinuityProbeError("portability is invalid")
    for field in ("veto_failures", "gate_failures", "evidence_gaps"):
        _id_list(report[field], field)
    if report["verdict"] not in {"qualified", "failed", "insufficient-evidence"}:
        raise LiveContinuityProbeError("report verdict is invalid")
    if report["verdict"] == "qualified" and (
        report["veto_failures"] or report["gate_failures"] or report["evidence_gaps"]
    ):
        raise LiveContinuityProbeError("qualified report carries failures")
    if report["verdict"] == "insufficient-evidence" and not report["evidence_gaps"]:
        raise LiveContinuityProbeError("insufficient report lacks evidence gaps")
    if report["state_write_authority"] is not False or report["completion_authority"] is not False:
        raise LiveContinuityProbeError("probe reports have no authority")
    _sha(report["report_sha256"], "report_sha256")
    if verify_digest and report["report_sha256"] != _digest(report, "report_sha256"):
        raise LiveContinuityProbeError("report_sha256 does not match report")


__all__ = [
    "LiveContinuityProbeError",
    "MAX_RECOVERY_READ_BYTES",
    "REPORT_SCHEMA_VERSION",
    "SEGMENT_SCHEMA_VERSION",
    "STUDY_SCHEMA_VERSION",
    "compose_live_continuity_study",
    "compose_live_continuity_segment",
    "evaluate_live_continuity_comparison",
    "validate_live_continuity_report",
    "validate_live_continuity_segment",
    "validate_live_continuity_study",
]
