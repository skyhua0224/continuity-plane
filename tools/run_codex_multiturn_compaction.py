#!/usr/bin/env python3
"""Drive one Codex session across the configured auto-compaction threshold."""

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
_CATALOG = "/home/skyhua/.codex/model-catalog-1m.json"
_PREFIX = (
    "Do not call tools. Return exactly one JSON object with verdict=ok and "
    "next_action=M10-00. "
)


def _large_prompt(turn: int) -> str:
    return (
        _PREFIX
        + (f"TURN {turn} HISTORICAL FILLER. " * 38_000)
        + " AUTHORITATIVE CURRENT STATE: active_leaf=M10-00; "
        "constraint=do not repeat completed work; next_action=M10-00."
    )


def _packet_prompt() -> str:
    return (
        _PREFIX + "EXECUTION PACKET: active_leaf=M10-00; latest_decision=thin packet; "
        "constraint=do not repeat completed work; next_action=M10-00."
    )


def _invoke(
    root: Path, prompt: str, thread_id: str | None, compact_limit: int
) -> dict[str, Any]:
    if thread_id is None:
        command = [
            "codex",
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--model",
            _MODEL,
            "-c",
            f'model_catalog_json="{_CATALOG}"',
            "-c",
            "model_context_window=1000000",
            "-c",
            f"model_auto_compact_token_limit={compact_limit}",
            "-C",
            str(root),
            "--skip-git-repo-check",
            "-",
        ]
    else:
        command = [
            "codex",
            "exec",
            "resume",
            "--json",
            "--model",
            _MODEL,
            "-c",
            f'model_catalog_json="{_CATALOG}"',
            "-c",
            "model_context_window=1000000",
            "-c",
            f"model_auto_compact_token_limit={compact_limit}",
            "--skip-git-repo-check",
            thread_id,
            "-",
        ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=root,
        input=prompt,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Codex multi-turn invocation failed with exit {completed.returncode}"
        )
    parsed = parse_codex_jsonl_result(completed.stdout)
    events = []
    observed_thread = thread_id
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            observed_thread = event.get("thread_id")
        serialized = json.dumps(event, sort_keys=True)
        if "compact" in serialized.lower():
            events.append(
                {
                    "event_type": event.get("type"),
                    "item_type": event.get("item", {}).get("type")
                    if isinstance(event.get("item"), dict)
                    else None,
                    "event_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
                }
            )
    return {
        "thread_id": observed_thread,
        "prompt_chars": len(prompt),
        "usage": parsed["usage"],
        "quality": parsed["quality"],
        "warning_count": len(parsed["warnings"]),
        "compaction_events": events,
        "wall_time_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="run Codex multi-turn compaction probe"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--large-turns", type=int, default=3)
    parser.add_argument("--compact-limit", type=int, default=900_000)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-codex-multiturn-results.json"),
    )
    args = parser.parse_args(argv)
    if not 2 <= args.large_turns <= 4:
        raise SystemExit("large-turns must be 2..4")
    if not 300_000 <= args.compact_limit <= 900_000:
        raise SystemExit("compact-limit must be 300000..900000")
    root = args.root.resolve()
    turns: list[dict[str, Any]] = []
    thread_id: str | None = None
    prompts = [_large_prompt(index + 1) for index in range(args.large_turns)]
    prompts.append(_packet_prompt())
    for index, prompt in enumerate(prompts, start=1):
        result = _invoke(root, prompt, thread_id, args.compact_limit)
        thread_id = result["thread_id"]
        turns.append({"turn": index, **result})
    receipt: dict[str, Any] = {
        "schema_version": "context.codex-multiturn-compaction/v1alpha1",
        "benchmark_id": "m10-00-codex-multiturn-compaction",
        "generated_at": "2026-08-18T01:15:00+08:00",
        "configuration": {
            "model": _MODEL,
            "context_window_tokens": 1_000_000,
            "auto_compact_token_limit": args.compact_limit,
            "large_turns": args.large_turns,
        },
        "thread_id_sha256": hashlib.sha256(thread_id.encode()).hexdigest(),
        "turns": turns,
        "compaction_event_count": sum(len(turn["compaction_events"]) for turn in turns),
        "quality_rate": sum(turn["quality"]["correct"] for turn in turns) / len(turns),
        "provider_invocations": len(turns),
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
    print(
        json.dumps(
            {
                "input_tokens": [turn["usage"]["input_tokens"] for turn in turns],
                "compaction_events": receipt["compaction_event_count"],
                "quality_rate": receipt["quality_rate"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
