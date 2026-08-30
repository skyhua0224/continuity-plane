"""Sanitized provider-token and compaction receipts for Codex exec streams."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import statistics
from datetime import datetime
from typing import Any, TextIO


OBSERVATION_SCHEMA_VERSION = "context.codex-exec-observation/v1alpha1"
COMPARISON_SCHEMA_VERSION = "context.codex-exec-comparison/v1alpha1"
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_RECOVERY_NARRATION_RE = re.compile(
    r"(?:after|following) (?:the )?(?:context )?(?:compaction|recovery)|"
    r"(?:重新加载|恢复后|压缩后).{0,24}(?:继续|恢复|读取)",
    re.IGNORECASE,
)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _receipt_digest(document: dict[str, Any]) -> str:
    body = copy.deepcopy(document)
    body.pop("receipt_sha256", None)
    return _sha(_canonical(body))


def _timestamp(value: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValueError("timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp requires a timezone")


def _read_kind(command: str, name: str) -> bool:
    return bool(
        re.search(
            rf"(?:^|[/\\\s'\"]){re.escape(name)}(?:$|[/\\\s'\"])",
            command,
            re.IGNORECASE,
        )
    )


def _hook_compaction(hook_events: list[dict[str, Any]]) -> dict[str, int]:
    successful = {"precompact": 0, "postcompact": 0, "compact-start": 0}
    failed = 0
    for event in hook_events:
        if not isinstance(event, dict):
            raise ValueError("hook event is invalid")
        event_type = event.get("event_type")
        success = event.get("success")
        trigger = event.get("trigger")
        if type(success) is not bool:
            raise ValueError("hook event success is invalid")
        if not success:
            failed += 1
            continue
        if event_type == "precompact" and trigger == "auto":
            successful["precompact"] += 1
        elif event_type == "postcompact" and trigger == "auto":
            successful["postcompact"] += 1
        elif event_type == "session-start" and trigger == "compact":
            successful["compact-start"] += 1
    return {
        "precompact_events": successful["precompact"],
        "postcompact_events": successful["postcompact"],
        "compact_start_events": successful["compact-start"],
        "complete_chains": min(successful.values()),
        "failed_events": failed,
    }


def observe_codex_exec_stream(
    stream: TextIO,
    *,
    arm: str,
    segment_id: str,
    observed_at: str,
    hook_events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Consume one Codex --json stream without retaining messages or tool output."""
    if arm not in {"baseline", "candidate"}:
        raise ValueError("arm is invalid")
    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("segment_id is invalid")
    _timestamp(observed_at)
    source = hashlib.sha256()
    usage: dict[str, int] | None = None
    tool_calls = 0
    tool_output_bytes = 0
    status_reads = 0
    master_reads = 0
    skill_reads = 0
    bounded_search_calls = 0
    assistant_messages = 0
    assistant_hashes: list[str] = []
    recovery_narration = 0
    error_items = 0
    trust_warnings = 0
    event_count = 0
    for line_no, line in enumerate(stream, start=1):
        encoded = line.encode("utf-8")
        source.update(encoded)
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Codex exec line {line_no} is invalid") from exc
        if not isinstance(event, dict):
            raise ValueError("Codex exec event is invalid")
        event_count += 1
        if event.get("type") == "turn.completed":
            candidate = event.get("usage")
            expected = {
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            }
            if not isinstance(candidate, dict) or set(candidate) != expected:
                raise ValueError("turn usage is invalid")
            if any(type(candidate[field]) is not int or candidate[field] < 0 for field in expected):
                raise ValueError("turn usage is invalid")
            usage = {field: candidate[field] for field in sorted(expected)}
            continue
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "command_execution":
            tool_calls += 1
            command = item.get("command")
            output = item.get("aggregated_output")
            if isinstance(command, str):
                status_reads += int(_read_kind(command, "STATUS.md"))
                master_reads += int(_read_kind(command, "MASTER.md"))
                skill_reads += int(_read_kind(command, "SKILL.md"))
                bounded_search_calls += int(
                    re.search(r"\bcontinuity\s+context\s+search\b", command)
                    is not None
                )
            if isinstance(output, str):
                tool_output_bytes += len(output.encode("utf-8"))
        elif item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str):
                assistant_messages += 1
                assistant_hashes.append(_sha(text.encode("utf-8")))
                recovery_narration += int(_RECOVERY_NARRATION_RE.search(text) is not None)
        elif item_type == "error":
            message = item.get("message")
            if isinstance(message, str) and "bypass-hook-trust" in message:
                trust_warnings += 1
            else:
                error_items += 1
    if event_count == 0 or usage is None:
        raise ValueError("Codex exec stream is incomplete")
    receipt = {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "segment_id": segment_id,
        "arm": arm,
        "observed_at": observed_at,
        "source_sha256": source.hexdigest(),
        "provider_usage": usage,
        "context_efficiency": {
            "tool_calls": tool_calls,
            "tool_output_bytes": tool_output_bytes,
            "status_read_calls": status_reads,
            "master_read_calls": master_reads,
            "skill_read_calls": skill_reads,
            "bounded_search_calls": bounded_search_calls,
            "assistant_messages": assistant_messages,
            "assistant_messages_sha256": _sha(
                _canonical(sorted(assistant_hashes))
            ),
            "recovery_narration_count": recovery_narration,
            "error_item_count": error_items,
            "hook_trust_warning_count": trust_warnings,
        },
        "compaction": _hook_compaction(hook_events),
        "raw_transcript_admission": False,
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    validate_codex_exec_observation(receipt)
    return receipt


def validate_codex_exec_observation(value: Any) -> None:
    fields = {
        "schema_version",
        "segment_id",
        "arm",
        "observed_at",
        "source_sha256",
        "provider_usage",
        "context_efficiency",
        "compaction",
        "raw_transcript_admission",
        "state_write_authority",
        "completion_authority",
        "receipt_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("observation fields are invalid")
    if value["schema_version"] != OBSERVATION_SCHEMA_VERSION:
        raise ValueError("observation schema is unsupported")
    if value["arm"] not in {"baseline", "candidate"}:
        raise ValueError("observation arm is invalid")
    _timestamp(value["observed_at"])
    for field in ("source_sha256", "receipt_sha256"):
        if not isinstance(value[field], str) or _SHA_RE.fullmatch(value[field]) is None:
            raise ValueError(f"{field} is invalid")
    if value["receipt_sha256"] != _receipt_digest(value):
        raise ValueError("observation digest mismatch")
    if (
        value["raw_transcript_admission"] is not False
        or value["state_write_authority"] is not False
        or value["completion_authority"] is not False
    ):
        raise ValueError("observation authority boundary is invalid")
    if not isinstance(value["provider_usage"], dict) or not isinstance(
        value["context_efficiency"], dict
    ) or not isinstance(value["compaction"], dict):
        raise ValueError("observation metrics are invalid")


def _median(items: list[dict[str, Any]], section: str, field: str) -> float:
    return float(statistics.median(item[section][field] for item in items))


def _reduction(baseline: float, candidate: float) -> float:
    return round((baseline - candidate) / baseline * 100, 6) if baseline > 0 else 0.0


def compare_codex_exec_observations(
    baseline: list[dict[str, Any]],
    candidate: list[dict[str, Any]],
    *,
    report_id: str,
    observed_at: str,
) -> dict[str, Any]:
    if len(baseline) != len(candidate) or len(baseline) < 3:
        raise ValueError("comparison requires equal arms with at least three samples")
    _timestamp(observed_at)
    for item in baseline + candidate:
        validate_codex_exec_observation(item)
    if any(item["arm"] != "baseline" for item in baseline) or any(
        item["arm"] != "candidate" for item in candidate
    ):
        raise ValueError("comparison arms are invalid")
    baseline_metrics = {
        "input_tokens_median": _median(baseline, "provider_usage", "input_tokens"),
        "output_tokens_median": _median(baseline, "provider_usage", "output_tokens"),
        "tool_output_bytes_median": _median(
            baseline, "context_efficiency", "tool_output_bytes"
        ),
        "status_read_calls": sum(
            item["context_efficiency"]["status_read_calls"] for item in baseline
        ),
    }
    candidate_metrics = {
        "input_tokens_median": _median(candidate, "provider_usage", "input_tokens"),
        "output_tokens_median": _median(candidate, "provider_usage", "output_tokens"),
        "tool_output_bytes_median": _median(
            candidate, "context_efficiency", "tool_output_bytes"
        ),
        "status_read_calls": sum(
            item["context_efficiency"]["status_read_calls"] for item in candidate
        ),
    }
    improvements = {
        "input_median_percent": _reduction(
            baseline_metrics["input_tokens_median"],
            candidate_metrics["input_tokens_median"],
        ),
        "output_median_percent": _reduction(
            baseline_metrics["output_tokens_median"],
            candidate_metrics["output_tokens_median"],
        ),
        "tool_output_median_percent": _reduction(
            baseline_metrics["tool_output_bytes_median"],
            candidate_metrics["tool_output_bytes_median"],
        ),
    }
    vetoes: set[str] = set()
    if any(item["context_efficiency"]["error_item_count"] for item in candidate):
        vetoes.add("provider-error")
    if any(item["context_efficiency"]["recovery_narration_count"] for item in candidate):
        vetoes.add("recovery-narration")
    if any(item["compaction"]["failed_events"] for item in candidate):
        vetoes.add("compaction-hook-failure")
    gates = []
    if improvements["input_median_percent"] < 30:
        gates.append("input-token-reduction")
    if improvements["output_median_percent"] < 10:
        gates.append("output-token-reduction")
    report = {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "report_id": report_id,
        "observed_at": observed_at,
        "sample_count_per_arm": len(baseline),
        "baseline": baseline_metrics,
        "candidate": candidate_metrics,
        "improvements": improvements,
        "veto_failures": sorted(vetoes),
        "gate_failures": sorted(gates),
        "verdict": "qualified" if not vetoes and not gates else "failed",
        "observations_sha256": _sha(
            _canonical(
                sorted(item["receipt_sha256"] for item in baseline + candidate)
            )
        ),
        "raw_transcript_admission": False,
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "",
    }
    report["receipt_sha256"] = _receipt_digest(report)
    validate_codex_exec_comparison(report)
    return report


def validate_codex_exec_comparison(value: Any) -> None:
    fields = {
        "schema_version",
        "report_id",
        "observed_at",
        "sample_count_per_arm",
        "baseline",
        "candidate",
        "improvements",
        "veto_failures",
        "gate_failures",
        "verdict",
        "observations_sha256",
        "raw_transcript_admission",
        "state_write_authority",
        "completion_authority",
        "receipt_sha256",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("comparison fields are invalid")
    if value["schema_version"] != COMPARISON_SCHEMA_VERSION:
        raise ValueError("comparison schema is unsupported")
    _timestamp(value["observed_at"])
    if type(value["sample_count_per_arm"]) is not int or value[
        "sample_count_per_arm"
    ] < 3:
        raise ValueError("comparison sample count is invalid")
    if value["verdict"] not in {"qualified", "failed"}:
        raise ValueError("comparison verdict is invalid")
    if value["verdict"] == "qualified" and (
        value["veto_failures"] or value["gate_failures"]
    ):
        raise ValueError("qualified comparison carries failures")
    for field in ("observations_sha256", "receipt_sha256"):
        if not isinstance(value[field], str) or _SHA_RE.fullmatch(value[field]) is None:
            raise ValueError(f"{field} is invalid")
    if value["receipt_sha256"] != _receipt_digest(value):
        raise ValueError("comparison digest mismatch")
    if (
        value["raw_transcript_admission"] is not False
        or value["state_write_authority"] is not False
        or value["completion_authority"] is not False
    ):
        raise ValueError("comparison authority boundary is invalid")
    for field in (
        "baseline",
        "candidate",
        "improvements",
        "veto_failures",
        "gate_failures",
    ):
        if not isinstance(value[field], dict if field in {"baseline", "candidate", "improvements"} else list):
            raise ValueError("comparison metrics are invalid")


__all__ = [
    "compare_codex_exec_observations",
    "observe_codex_exec_stream",
    "validate_codex_exec_comparison",
    "validate_codex_exec_observation",
]
