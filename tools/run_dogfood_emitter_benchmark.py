#!/usr/bin/env python3
"""Run and publish the M5-07 dogfood emitter benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.dogfood_emitter_benchmark import (
    benchmark_dogfood_emitter,
    validate_dogfood_emitter_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--generated-at", default="2026-08-15T05:30:00Z")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/dogfood/m5-07-emitter-results.json"),
    )
    args = parser.parse_args()
    receipt = benchmark_dogfood_emitter(
        samples=args.samples, generated_at=args.generated_at
    )
    validate_dogfood_emitter_benchmark(receipt)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "M5-07 dogfood emitter benchmark: "
        f"success={receipt['successful_samples']}/{receipt['samples']} "
        f"events={receipt['emitted_events']} "
        f"veto={receipt['veto_detections']}/{receipt['veto_detection_samples']} "
        f"p95={receipt['p95_ms']}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
