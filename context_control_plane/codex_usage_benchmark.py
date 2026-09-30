"""Provider usage and quality receipts for real Codex A/B probes."""

from __future__ import annotations

import json
import statistics
from typing import Any

USAGE_FIELDS = {
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
}


class CodexUsageBenchmarkError(ValueError):
    """Raised when a Codex JSONL run lacks usage or quality evidence."""


def parse_codex_jsonl_result(output: str) -> dict[str, Any]:
    """Extract one turn usage and the required JSON quality result."""
    if not isinstance(output, str) or not output:
        raise CodexUsageBenchmarkError("Codex output is empty")
    usage: dict[str, int] | None = None
    final_message: str | None = None
    warnings: list[str] = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            candidate = event.get("usage")
            if not isinstance(candidate, dict) or set(candidate) != USAGE_FIELDS:
                raise CodexUsageBenchmarkError("turn.completed usage is incomplete")
            if (
                any(type(value) is not int or value < 0 for value in candidate.values())
                or candidate["input_tokens"] <= 0
            ):
                raise CodexUsageBenchmarkError("turn.completed usage is invalid")
            usage = candidate
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            final_message = item.get("text")
        if isinstance(item, dict) and item.get("type") == "error":
            warnings.append(str(item.get("message", "error")))
    if usage is None:
        raise CodexUsageBenchmarkError("Codex output has no turn.completed usage")
    if not isinstance(final_message, str):
        raise CodexUsageBenchmarkError("Codex output has no final agent message")
    try:
        quality = json.loads(final_message)
    except json.JSONDecodeError as exc:
        raise CodexUsageBenchmarkError("final agent message is not JSON") from exc
    if quality != {"verdict": "ok", "next_action": "M10-00"}:
        raise CodexUsageBenchmarkError("quality result does not match the fixture")
    return {
        "usage": usage,
        "quality": {"correct": True, "verdict": "ok", "next_action": "M10-00"},
        "warnings": warnings,
    }


def summarize_usage_samples(samples: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Summarize same-fixture samples by case and compute packet reduction."""
    if not isinstance(samples, list) or not samples:
        raise CodexUsageBenchmarkError("usage samples are empty")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        if not isinstance(sample, dict) or sample.get("case") not in {
            "baseline",
            "packet",
            "large-history",
        }:
            raise CodexUsageBenchmarkError("usage sample case is invalid")
        usage = sample.get("usage")
        quality = sample.get("quality")
        if not isinstance(usage, dict) or not isinstance(quality, dict):
            raise CodexUsageBenchmarkError("usage sample is incomplete")
        grouped.setdefault(sample["case"], []).append(sample)
    result: dict[str, dict[str, Any]] = {}
    for case, items in grouped.items():
        inputs = [item["usage"]["input_tokens"] for item in items]
        result[case] = {
            "count": len(items),
            "input_tokens_mean": round(statistics.mean(inputs), 3),
            "input_tokens_min": min(inputs),
            "input_tokens_max": max(inputs),
            "cached_input_tokens_mean": round(
                statistics.mean(
                    item["usage"].get("cached_input_tokens", 0) for item in items
                ),
                3,
            ),
            "output_tokens_mean": round(
                statistics.mean(
                    item["usage"].get("output_tokens", 0) for item in items
                ),
                3,
            ),
            "quality_rate": round(
                sum(item["quality"].get("correct") is True for item in items)
                / len(items),
                6,
            ),
        }
    baseline = result.get("baseline")
    packet = result.get("packet")
    if baseline is not None and packet is not None:
        packet["input_reduction_percent"] = round(
            (baseline["input_tokens_mean"] - packet["input_tokens_mean"])
            / baseline["input_tokens_mean"]
            * 100,
            4,
        )
    return result


__all__ = [
    "CodexUsageBenchmarkError",
    "parse_codex_jsonl_result",
    "summarize_usage_samples",
]
