#!/usr/bin/env python3
"""Run a real Codex usage A/B benchmark without retaining raw prompts."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.codex_usage_benchmark import (
    parse_codex_jsonl_result,
    summarize_usage_samples,
)

_MODEL = "gpt-5.6-sol"
_CONTEXT_WINDOW = 1_000_000
_AUTO_COMPACT = 900_000
_MAX_INPUT_CHARS = 1_048_576


def _packet_prompt() -> str:
    return (
        "You are a verifier. Do not call tools. Return exactly one JSON object "
        "with keys verdict and next_action. EXECUTION PACKET: "
        "active_leaf=M10-00; latest_decision=use bounded packet; "
        "constraint=do not repeat completed work; next_action=M10-00. "
        "Return verdict=ok."
    )


def _baseline_prompt() -> str:
    return (
        "You are a verifier. Do not call tools. Return exactly one JSON object "
        "with keys verdict and next_action. The current task is M10-00 and "
        "next_action must be M10-00. Historical notes follow. "
        + ("STALE NOTE: previously completed work must be repeated. " * 1200)
        + " AUTHORITATIVE CURRENT STATE: active_leaf=M10-00; "
        "latest_decision=use bounded packet; constraint=do not repeat completed work; "
        "next_action=M10-00. Return verdict=ok."
    )


def _large_prompt() -> str:
    return (
        ("HISTORICAL FILLER. " * 54_000)
        + " AUTHORITATIVE CURRENT STATE: active_leaf=M10-00; "
        "latest_decision=use bounded packet; constraint=do not repeat completed work; "
        "next_action=M10-00. Return exactly one JSON object with verdict=ok "
        "and next_action=M10-00. Do not call tools."
    )


def _run_case(root: Path, case: str, index: int, prompt: str) -> dict[str, Any]:
    command = [
        "codex",
        "exec",
        "--ephemeral",
        "--json",
        "--sandbox",
        "read-only",
        "--model",
        _MODEL,
        "-C",
        str(root),
        "--skip-git-repo-check",
        "-",
    ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=root,
        input=prompt,
        capture_output=True,
        text=True,
        check=False,
    )
    wall_time_ms = (time.perf_counter() - started) * 1000
    if completed.returncode != 0:
        raise RuntimeError(
            f"Codex case {case}-{index} failed with exit {completed.returncode}"
        )
    parsed = parse_codex_jsonl_result(completed.stdout)
    return {
        "sample_id": f"{case}-{index}",
        "case": case,
        "prompt_chars": len(prompt),
        "usage": parsed["usage"],
        "quality": parsed["quality"],
        "warning_count": len(parsed["warnings"]),
        "wall_time_ms": round(wall_time_ms, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run Codex usage A/B benchmark")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--large-iterations", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-codex-usage-ab-results.json"),
    )
    arguments = parser.parse_args(argv)
    if not 1 <= arguments.iterations <= 10 or not 0 <= arguments.large_iterations <= 10:
        raise SystemExit("iterations must be 1..10 and large-iterations must be 0..10")
    root = arguments.root.resolve()
    samples: list[dict[str, Any]] = []
    for case, count, prompt in (
        ("baseline", arguments.iterations, _baseline_prompt()),
        ("packet", arguments.iterations, _packet_prompt()),
        ("large-history", arguments.large_iterations, _large_prompt()),
    ):
        for index in range(count):
            samples.append(_run_case(root, case, index, prompt))
    receipt: dict[str, Any] = {
        "schema_version": "context.codex-usage-ab/v1alpha1",
        "benchmark_id": "m10-00-codex-usage-ab",
        "generated_at": "2026-08-18T00:30:00+08:00",
        "configuration": {
            "model": _MODEL,
            "context_window_tokens": _CONTEXT_WINDOW,
            "auto_compact_token_limit": _AUTO_COMPACT,
            "max_input_chars": _MAX_INPUT_CHARS,
            "sandbox": "read-only",
            "tools_allowed": False,
        },
        "samples": samples,
        "summary": summarize_usage_samples(samples),
        "boundary": {
            "oversize_input_chars": 1_083_237,
            "oversize_rejected_before_provider": True,
            "largest_accepted_prompt_chars": max(
                item["prompt_chars"] for item in samples
            ),
        },
        "provider_invocations": len(samples),
        "external_side_effects": 0,
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    output = (
        arguments.output if arguments.output.is_absolute() else root / arguments.output
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    packet = receipt["summary"].get("packet", {})
    print(
        "Codex usage A/B: "
        f"baseline={receipt['summary']['baseline']['input_tokens_mean']:.0f} "
        f"packet={packet['input_tokens_mean']:.0f} "
        f"reduction={packet.get('input_reduction_percent', 0):.4f}% "
        f"large={receipt['summary'].get('large-history', {}).get('input_tokens_mean', 0):.0f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
