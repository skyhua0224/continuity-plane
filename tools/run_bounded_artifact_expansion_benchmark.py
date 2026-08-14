#!/usr/bin/env python3
"""Run and publish the M5-04 bounded artifact expansion benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.bounded_artifact_expansion_benchmark import (
    benchmark_bounded_artifact_expansion,
    validate_bounded_artifact_expansion_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--generated-at", default="2026-08-15T03:00:00Z")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/routing/m5-04-bounded-expansion-results.json"),
    )
    args = parser.parse_args()
    receipt = benchmark_bounded_artifact_expansion(
        samples=args.samples, generated_at=args.generated_at
    )
    validate_bounded_artifact_expansion_benchmark(receipt, root=ROOT)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "M5-04 bounded expansion benchmark: "
        f"success={receipt['successful_samples']}/{receipt['samples']} "
        f"budget={receipt['budget_rejections']}/{receipt['budget_fault_samples']} "
        f"digest={receipt['digest_rejections']}/{receipt['digest_fault_samples']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
