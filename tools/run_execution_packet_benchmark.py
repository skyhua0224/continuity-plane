#!/usr/bin/env python3
"""Run and publish the M5-01 Execution Packet benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.execution_packet_benchmark import (
    benchmark_execution_packet,
    benchmark_fixture,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--generated-at", default="2026-08-14T20:30:00Z")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/routing/m5-01-execution-packet-results.json"),
    )
    parser.add_argument(
        "--fixture-output",
        type=Path,
        default=Path("experiments/skills/m5-01-execution-packet-replay-v1alpha1.json"),
    )
    args = parser.parse_args()
    root = ROOT
    receipt = benchmark_execution_packet(
        root=root,
        samples=args.samples,
        generated_at=args.generated_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.fixture_output.parent.mkdir(parents=True, exist_ok=True)
    args.fixture_output.write_text(
        json.dumps(benchmark_fixture(root), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "execution packet benchmark: "
        f"success={receipt['successful_samples']}/{receipt['samples']} "
        f"replay={receipt['replay_mismatch']} "
        f"size={receipt['min_packet_bytes']}-{receipt['max_packet_bytes']}B "
        f"p95={receipt['p95_ms']}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
