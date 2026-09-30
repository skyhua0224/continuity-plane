#!/usr/bin/env python3
"""Run and publish the M5-03 PostCompact canary benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.postcompact_canary_benchmark import (
    benchmark_postcompact_canary,
    validate_postcompact_canary_benchmark_receipt,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--generated-at", default="2026-08-15T00:30:00Z")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/routing/m5-03-postcompact-canary-results.json"),
    )
    args = parser.parse_args()
    receipt = benchmark_postcompact_canary(
        root=ROOT,
        samples=args.samples,
        generated_at=args.generated_at,
    )
    validate_postcompact_canary_benchmark_receipt(receipt, root=ROOT)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "M5-03 PostCompact benchmark: "
        f"success={receipt['successful_samples']}/{receipt['samples']} "
        f"faults={receipt['fault_rejections']}/{receipt['fault_samples']} "
        f"p95={receipt['p95_ms']}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
