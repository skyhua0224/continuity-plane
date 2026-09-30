#!/usr/bin/env python3
"""Convert a Codex exec JSON stream into a sanitized M11 observation."""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.codex_exec_observation import (  # noqa: E402
    observe_codex_exec_stream,
    validate_codex_exec_observation,
)


def _hook_events(
    paths: list[Path], directories: list[Path]
) -> list[dict[str, object]]:
    expanded = list(paths)
    for directory in directories:
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError("hook event directory is unavailable")
        found = sorted(directory.glob("*.jsonl"))
        if not found:
            raise ValueError("hook event directory is empty")
        expanded.extend(found)
    events: list[dict[str, object]] = []
    for path in expanded:
        if not path.is_file() or path.is_symlink():
            raise ValueError("hook event path is unavailable")
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("hook event is invalid")
                events.append(
                    {
                        "event_type": value.get("event_type"),
                        "trigger": value.get("trigger"),
                        "success": value.get("success"),
                    }
                )
    return events


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="sanitize one Codex exec --json stream without retaining raw text"
    )
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--segment-id", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--hook-events", type=Path, action="append", default=[])
    parser.add_argument(
        "--hook-events-dir", type=Path, action="append", default=[]
    )
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    provider_stream = io.StringIO(sys.stdin.read())
    receipt = observe_codex_exec_stream(
        provider_stream,
        arm=arguments.arm,
        segment_id=arguments.segment_id,
        observed_at=arguments.observed_at,
        hook_events=_hook_events(
            arguments.hook_events, arguments.hook_events_dir
        ),
    )
    validate_codex_exec_observation(receipt)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_suffix(f"{arguments.output.suffix}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(arguments.output)
    print(
        "Codex exec observation: "
        f"arm={receipt['arm']} "
        f"input={receipt['provider_usage']['input_tokens']} "
        f"compactions={receipt['compaction']['complete_chains']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
