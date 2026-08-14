#!/usr/bin/env python3
"""Run and publish the M5-06 Idea return packet benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.idea_return_packet_benchmark import (
    benchmark_idea_return_packet,
    validate_idea_return_packet_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--generated-at", default="2026-08-15T03:30:00Z")
    parser.add_argument(
        "--output", type=Path,
        default=Path("experiments/routing/m5-06-idea-return-packet-results.json"),
    )
    args = parser.parse_args()
    receipt = benchmark_idea_return_packet(root=ROOT, samples=args.samples)
    receipt["generated_at"] = args.generated_at
    validate_idea_return_packet_benchmark(receipt, root=ROOT)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"M5-06 Idea return benchmark: success={receipt['successful_samples']}/{receipt['samples']} p95={receipt['p95_ms']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
