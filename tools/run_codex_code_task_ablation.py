#!/usr/bin/env python3
"""Measure packet and retrieval effects on a real read-only code task."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
_EXPECTED = {
    "function": "build_evidence_matrix_receipt",
    "completion_status": "ready-for-runtime",
}
_BASE = (
    "Inspect the current repository with read-only tools. Find the function that "
    "builds the M10 evidence admission receipt and the exact completion_status "
    "string it emits. Return exactly one JSON object with keys function and "
    "completion_status."
)


def _prompts() -> dict[str, str]:
    return {
        "bare": _BASE,
        "state-packet": (
            _BASE + " EXECUTION PACKET: active_leaf=M10-00; task=evidence-matrix; "
            "current_code_ref=context_control_plane/self_dogfood_pilot.py."
        ),
        "retrieval-packet": (
            _BASE + " RETRIEVAL RECEIPT: current_code path="
            "context_control_plane/self_dogfood_pilot.py; function line=534; "
            "completion_status line=572; provenance=current-worktree."
        ),
    }


def _parse(output: str) -> tuple[dict[str, int], bool, int, list[str]]:
    usage = None
    message = None
    tool_types: list[str] = []
    warnings = 0
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            usage = event.get("usage")
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "agent_message":
            message = item.get("text")
        elif item_type == "error":
            warnings += 1
        elif item_type not in {"reasoning"}:
            tool_types.append(str(item_type))
    if not isinstance(usage, dict) or not isinstance(message, str):
        raise TypeError("Codex code-task output is incomplete")
    try:
        quality = json.loads(message) == _EXPECTED
    except json.JSONDecodeError:
        quality = False
    return usage, quality, warnings, tool_types


def _run(root: Path, case: str, index: int, prompt: str) -> dict[str, Any]:
    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "--model",
        "gpt-5.6-sol",
        "-C",
        str(root),
        "--skip-git-repo-check",
        prompt,
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Codex code task failed: {case}-{index}")
    usage, correct, warnings, tools = _parse(completed.stdout)
    return {
        "sample_id": f"{case}-{index}",
        "case": case,
        "usage": usage,
        "quality": {"correct": correct},
        "warning_count": warnings,
        "tool_call_count": len(tools),
        "tool_types": tools,
        "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(sample["case"], []).append(sample)
    result = {}
    for case, items in grouped.items():
        result[case] = {
            "count": len(items),
            "input_tokens_mean": sum(item["usage"]["input_tokens"] for item in items)
            / len(items),
            "tool_calls_mean": sum(item["tool_call_count"] for item in items)
            / len(items),
            "wall_time_ms_mean": sum(item["wall_time_ms"] for item in items)
            / len(items),
            "quality_rate": sum(item["quality"]["correct"] for item in items)
            / len(items),
            "warning_count_total": sum(item["warning_count"] for item in items),
        }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run real Codex code task ablation")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-codex-code-task-results.json"),
    )
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 10:
        raise SystemExit("iterations must be 1..10")
    root = args.root.resolve()
    samples = [
        _run(root, case, index, prompt)
        for case, prompt in _prompts().items()
        for index in range(args.iterations)
    ]
    receipt: dict[str, Any] = {
        "schema_version": "context.codex-code-task-ablation/v1alpha1",
        "benchmark_id": "m10-00-codex-code-task-ablation",
        "generated_at": "2026-08-18T01:40:00+08:00",
        "model": "gpt-5.6-sol",
        "samples": samples,
        "summary": _summary(samples),
        "provider_invocations": len(samples),
        "external_side_effects": 0,
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt["summary"], sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
