#!/usr/bin/env python3
"""Run and publish the M5-02 PreCompact delta benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.compaction_checkpoint_benchmark import (
    benchmark_compaction_checkpoint,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--generated-at", default="2026-08-14T20:30:00Z")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/routing/m5-02-compaction-checkpoint-results.json"),
    )
    args = parser.parse_args()
    receipt = benchmark_compaction_checkpoint(root=ROOT, samples=args.samples, generated_at=args.generated_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "M5-02 PreCompact benchmark: "
        f"success={receipt['successful_samples']}/{receipt['samples']} "
        f"delta={receipt['min_delta_bytes']}-{receipt['max_delta_bytes']}B "
        f"p95={receipt['p95_ms']}ms "
        f"watermark_mismatch={receipt['watermark_mismatch']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
