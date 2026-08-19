"""Read-only adapter for the stable subset of Codex rollout metadata."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class CodexRolloutAdapterError(ValueError):
    """Raised when a Codex rollout cannot provide trustworthy usage evidence."""


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _usage(info: dict[str, Any]) -> dict[str, int]:
    total = info.get("total_token_usage")
    if not isinstance(total, dict):
        raise CodexRolloutAdapterError("token_count total usage is missing")
    fields = {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    }
    if set(total) != fields or any(
        type(total[field]) is not int or total[field] < 0 for field in fields
    ):
        raise CodexRolloutAdapterError("token_count total usage is invalid")
    return {field: total[field] for field in fields}


def inspect_codex_rollout(path: str | Path) -> dict[str, Any]:
    """Derive hashed interaction counts and cumulative usage from a rollout JSONL."""
    source = Path(path)
    if not source.is_file() or source.is_symlink():
        raise CodexRolloutAdapterError("rollout path is unavailable")
    source_hasher = hashlib.sha256()
    interaction_hasher = hashlib.sha256()
    token_samples: list[dict[str, int]] = []
    windows: set[int] = set()
    missing_window = False
    compactions: list[dict[str, Any]] = []
    user_messages = 0
    assistant_messages = 0
    event_count = 0
    try:
        with source.open("rb") as stream:
            for line_no, line in enumerate(stream, start=1):
                source_hasher.update(line)
                if not line.strip():
                    continue
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CodexRolloutAdapterError(
                        f"rollout line {line_no} is not JSON"
                    ) from exc
                if not isinstance(value, dict):
                    raise CodexRolloutAdapterError("rollout event must be an object")
                event_count += 1
                payload = value.get("payload")
                if value.get("type") == "event_msg" and isinstance(payload, dict):
                    if payload.get("type") == "token_count":
                        info = payload.get("info")
                        if not isinstance(info, dict):
                            raise CodexRolloutAdapterError("token_count info is missing")
                        token_samples.append(_usage(info))
                        window = info.get("model_context_window")
                        if window is None:
                            missing_window = True
                        elif type(window) is not int or window <= 0:
                            raise CodexRolloutAdapterError("model context window is invalid")
                        else:
                            windows.add(window)
                    elif payload.get("type") == "context_compacted":
                        timestamp = value.get("timestamp")
                        compactions.append(
                            {
                                "event_type": "context_compacted",
                                "timestamp": timestamp if isinstance(timestamp, str) else None,
                                "event_sha256": _sha(
                                    json.dumps(value, sort_keys=True, separators=(",", ":"))
                                ),
                            }
                        )
                if value.get("type") != "response_item" or not isinstance(payload, dict):
                    continue
                if payload.get("type") != "message":
                    continue
                role = payload.get("role")
                content = payload.get("content")
                if not isinstance(content, list):
                    continue
                text = "\n".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and isinstance(item.get("text"), str)
                )
                if not text:
                    continue
                if role == "user":
                    user_messages += 1
                    interaction_hasher.update(_sha(text).encode("ascii") + b"\n")
                elif role == "assistant":
                    assistant_messages += 1
    except OSError as exc:
        raise CodexRolloutAdapterError("rollout cannot be read") from exc
    if event_count == 0:
        raise CodexRolloutAdapterError("rollout is empty")

    if not token_samples:
        raise CodexRolloutAdapterError("rollout has no token_count evidence")
    if len(windows) > 1 or (windows and missing_window):
        raise CodexRolloutAdapterError("rollout context window changed")
    for previous, current in zip(token_samples, token_samples[1:]):
        if any(current[field] < previous[field] for field in previous):
            raise CodexRolloutAdapterError("cumulative token counter reset")
    first = token_samples[0]
    last = token_samples[-1]
    usage = {
        field: last[field] - first[field]
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        )
    }
    if len(token_samples) == 1:
        usage = {
            field: first[field]
            for field in (
                "input_tokens",
                "cached_input_tokens",
                "cache_write_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            )
        }
    total_delta = last["total_tokens"] - first["total_tokens"]
    if total_delta > 0 and usage["input_tokens"] + usage["output_tokens"] == 0:
        provider_usage: dict[str, Any] = {
            "status": "unavailable",
            "input_tokens": None,
            "cached_input_tokens": None,
            "cache_write_input_tokens": None,
            "output_tokens": None,
            "reasoning_output_tokens": None,
            "unavailable_reason": "legacy total-only token_count telemetry",
        }
    else:
        provider_usage = {"status": "measured", **usage}
    return {
        "schema_version": "context.codex-rollout-observation/v1alpha1",
        "source_sha256": source_hasher.hexdigest(),
        "provider_usage": provider_usage,
        "context_window_tokens": next(iter(windows)) if windows else None,
        "compaction_count": len(compactions),
        "compactions": compactions,
        "user_message_count": user_messages,
        "assistant_message_count": assistant_messages,
        "interaction_refs_sha256": interaction_hasher.hexdigest(),
        "raw_transcript_admission": False,
        "state_write_authority": False,
        "completion_authority": False,
    }


__all__ = ["CodexRolloutAdapterError", "inspect_codex_rollout"]
