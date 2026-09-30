#!/usr/bin/env python3
"""Measure real Codex input and quality for thin-to-full harness prompts."""

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

from context_control_plane.codex_usage_benchmark import parse_codex_jsonl_result

_MODEL = "gpt-5.6-sol"
_PROMPT_PREFIX = (
    "You are a verifier. Do not call tools. Return exactly one JSON object "
    "with keys verdict and next_action. verdict must be ok and next_action "
    "must be M10-00. "
)


def _prompts() -> dict[str, str]:
    state = (
        "TYPED STATE: project=context-control-plane; revision=74; "
        "active_leaf=M10-00; latest_decision=use bounded packet; "
        "constraint=do not repeat completed work; blocker=none. "
    )
    full = (
        state + "SKILL LOCK: resolver=v1; manifest_sha256=aaaaaaaa; "
        "rule_ids=[state-recovery, no-replay, evidence-gate]; applicability=M10-00. "
        "RETRIEVAL RECEIPT: source=current; repository_revision=74; "
        "bearing_refs=[STATUS.md,MASTER.md]; expansion=bounded. "
        "CONTINUATION CURSOR: last_action=validate-release-receipt; "
        "in_flight=none; confirmed_input_refs=[M9-07]; replay_policy=resume. "
    )
    history = (
        "HISTORICAL NOTES: "
        + ("STALE NOTE: previously completed work must be repeated. " * 1200)
        + state
    )
    return {
        "bare": _PROMPT_PREFIX,
        "state-packet": _PROMPT_PREFIX + state,
        "full-packet": _PROMPT_PREFIX + full,
        "history": _PROMPT_PREFIX + history,
    }


def _run(root: Path, case: str, index: int, prompt: str) -> dict[str, Any]:
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
    result = subprocess.run(
        command,
        cwd=root,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Codex ablation case {case}-{index} failed")
    parsed = parse_codex_jsonl_result(result.stdout)
    return {
        "sample_id": f"{case}-{index}",
        "case": case,
        "prompt_chars": len(prompt),
        "usage": parsed["usage"],
        "quality": parsed["quality"],
        "warning_count": len(parsed["warnings"]),
        "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def _summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(sample["case"], []).append(sample)
    result: dict[str, Any] = {}
    for case, items in grouped.items():
        values = [item["usage"]["input_tokens"] for item in items]
        result[case] = {
            "count": len(items),
            "input_tokens_mean": round(sum(values) / len(values), 3),
            "input_tokens_min": min(values),
            "input_tokens_max": max(values),
            "quality_rate": sum(item["quality"]["correct"] for item in items)
            / len(items),
            "warning_count_total": sum(item["warning_count"] for item in items),
        }
    bare = result["bare"]["input_tokens_mean"]
    history = result["history"]["input_tokens_mean"]
    for case in ("state-packet", "full-packet"):
        result[case]["reduction_vs_bare_percent"] = round(
            (bare - result[case]["input_tokens_mean"]) / bare * 100,
            4,
        )
        result[case]["reduction_vs_history_percent"] = round(
            (history - result[case]["input_tokens_mean"]) / history * 100,
            4,
        )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run Codex harness ablation")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-codex-harness-ablation-results.json"),
    )
    args = parser.parse_args(argv)
    if not 1 <= args.iterations <= 10:
        raise SystemExit("iterations must be between 1 and 10")
    root = args.root.resolve()
    samples: list[dict[str, Any]] = []
    for case, prompt in _prompts().items():
        for index in range(args.iterations):
            samples.append(_run(root, case, index, prompt))
    receipt: dict[str, Any] = {
        "schema_version": "context.codex-harness-ablation/v1alpha1",
        "benchmark_id": "m10-00-codex-harness-ablation",
        "generated_at": "2026-08-18T01:00:00+08:00",
        "configuration": {
            "model": _MODEL,
            "context_window_tokens": 1_000_000,
            "auto_compact_token_limit": 900_000,
            "sandbox": "read-only",
            "tools_allowed": False,
        },
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
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    output = args.output if args.output.is_absolute() else root / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(receipt["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
