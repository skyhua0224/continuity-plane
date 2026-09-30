"""Streaming Claude Code archive and stream-json metadata adapter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class ClaudeRolloutAdapterError(ValueError):
    """Raised when Claude telemetry is inconsistent or cannot be normalized."""


_USAGE_FIELDS = {
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
}


def _usage(value: Any, field: str) -> dict[str, int]:
    if not isinstance(value, dict) or not _USAGE_FIELDS <= set(value):
        raise ClaudeRolloutAdapterError(f"{field} usage is incomplete")
    result = {key: value[key] for key in _USAGE_FIELDS}
    if any(type(item) is not int or item < 0 for item in result.values()):
        raise ClaudeRolloutAdapterError(f"{field} usage is invalid")
    return result


def _usage_fingerprint(value: dict[str, int]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def inspect_claude_rollout(path: str | Path) -> dict[str, Any]:
    """Read one Claude JSONL stream without retaining message or summary text."""
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise ClaudeRolloutAdapterError("Claude rollout path is unavailable")
    source_hasher = hashlib.sha256()
    message_usage: dict[str, dict[str, int]] = {}
    result_usage: dict[str, int] | None = None
    api_error_status: int | None = None
    compactions: list[dict[str, Any]] = []
    event_count = 0
    try:
        with source.open("rb") as stream:
            for line_number, line in enumerate(stream, start=1):
                source_hasher.update(line)
                if not line.strip():
                    continue
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ClaudeRolloutAdapterError(
                        f"Claude rollout line {line_number} is invalid"
                    ) from exc
                if not isinstance(event, dict):
                    raise ClaudeRolloutAdapterError("Claude rollout event must be an object")
                event_count += 1
                if event.get("type") == "assistant":
                    message = event.get("message")
                    if not isinstance(message, dict):
                        raise ClaudeRolloutAdapterError("assistant message is invalid")
                    message_id = message.get("id")
                    if not isinstance(message_id, str) or not message_id:
                        raise ClaudeRolloutAdapterError("assistant message ID is invalid")
                    current = _usage(message.get("usage"), "assistant")
                    previous = message_usage.get(message_id)
                    if previous is not None and previous != current:
                        raise ClaudeRolloutAdapterError(
                            "duplicate Claude message has changed usage"
                        )
                    message_usage[message_id] = current
                elif event.get("type") == "result":
                    result_usage = _usage(event.get("usage"), "result")
                    status = event.get("api_error_status")
                    if event.get("is_error") is True:
                        if type(status) is not int or status < 100 or status > 599:
                            raise ClaudeRolloutAdapterError("Claude API error status is invalid")
                        api_error_status = status
                elif event.get("type") == "system" and event.get("subtype") == "compact_boundary":
                    metadata = event.get("compactMetadata")
                    required = {
                        "trigger",
                        "preTokens",
                        "postTokens",
                        "cumulativeDroppedTokens",
                        "durationMs",
                    }
                    if not isinstance(metadata, dict) or not required <= set(metadata):
                        raise ClaudeRolloutAdapterError("Claude compact metadata is incomplete")
                    if metadata["trigger"] not in {"auto", "manual"}:
                        raise ClaudeRolloutAdapterError("Claude compact trigger is invalid")
                    for field in required - {"trigger"}:
                        if type(metadata[field]) is not int or metadata[field] < 0:
                            raise ClaudeRolloutAdapterError(
                                f"Claude compact {field} is invalid"
                            )
                    if metadata["postTokens"] > metadata["preTokens"]:
                        raise ClaudeRolloutAdapterError("Claude compact token counts are invalid")
                    compactions.append(
                        {
                            "trigger": metadata["trigger"],
                            "pre_tokens": metadata["preTokens"],
                            "post_tokens": metadata["postTokens"],
                            "cumulative_dropped_tokens": metadata[
                                "cumulativeDroppedTokens"
                            ],
                            "duration_ms": metadata["durationMs"],
                            "event_sha256": hashlib.sha256(line).hexdigest(),
                            "observed_at": event.get("timestamp")
                            if isinstance(event.get("timestamp"), str)
                            else None,
                        }
                    )
    except OSError as exc:
        raise ClaudeRolloutAdapterError("Claude rollout cannot be read") from exc
    if event_count == 0:
        raise ClaudeRolloutAdapterError("Claude rollout is empty")

    if api_error_status is not None:
        provider_usage: dict[str, Any] = {
            "status": "unavailable",
            "input_tokens": None,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "unavailable_reason": f"Claude API error {api_error_status}",
        }
        provider_status = f"api-error-{api_error_status}"
    else:
        unique = list(message_usage.values())
        if unique:
            totals = {
                field: sum(item[field] for item in unique) for field in _USAGE_FIELDS
            }
        elif result_usage is not None:
            totals = result_usage
        else:
            totals = None
        if totals is None:
            provider_usage = {
                "status": "unavailable",
                "input_tokens": None,
                "cached_input_tokens": None,
                "cache_write_input_tokens": None,
                "output_tokens": None,
                "reasoning_output_tokens": None,
                "unavailable_reason": "Claude usage telemetry is absent",
            }
            provider_status = "usage-unavailable"
        else:
            provider_usage = {
                "status": "measured",
                "input_tokens": totals["input_tokens"]
                + totals["cache_creation_input_tokens"]
                + totals["cache_read_input_tokens"],
                "cached_input_tokens": totals["cache_read_input_tokens"],
                "cache_write_input_tokens": totals["cache_creation_input_tokens"],
                "output_tokens": totals["output_tokens"],
                "reasoning_output_tokens": None,
            }
            provider_status = "measured"
    return {
        "schema_version": "context.claude-rollout-observation/v1alpha1",
        "source_sha256": source_hasher.hexdigest(),
        "provider_status": provider_status,
        "provider_usage": provider_usage,
        "unique_message_count": len(message_usage),
        "compaction_count": len(compactions),
        "compactions": compactions,
        "raw_transcript_admission": False,
        "state_write_authority": False,
        "completion_authority": False,
    }


__all__ = ["ClaudeRolloutAdapterError", "inspect_claude_rollout"]
