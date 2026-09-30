#!/usr/bin/env python3
"""Run and publish the M5-08 durable continuation benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.durable_continuation_benchmark import (
    benchmark_durable_continuation,
    validate_durable_continuation_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--generated-at", default="2026-08-15T04:30:00Z")
    parser.add_argument(
        "--output", type=Path,
        default=Path("experiments/routing/m5-08-durable-continuation-results.json"),
    )
    args = parser.parse_args()
    receipt = benchmark_durable_continuation(samples=args.samples, generated_at=args.generated_at)
    validate_durable_continuation_benchmark(receipt)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"M5-08 durable continuation benchmark: success={receipt['successful_samples']}/{receipt['samples']} faults={receipt['fault_rejections']}/{receipt['fault_samples']} p95={receipt['p95_ms']}ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
