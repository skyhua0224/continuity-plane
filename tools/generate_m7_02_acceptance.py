#!/usr/bin/env python3
"""Generate the replayable M7-02 claim-evidence benchmark receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.claim_evidence_benchmark import benchmark_claim_evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m7-02-claim-evidence-results.json"),
    )
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = benchmark_claim_evidence(samples=args.samples, root=ROOT)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "M7-02 claim-evidence benchmark: "
        f"{receipt['successful_samples']}/{receipt['samples']} "
        f"false_allow={receipt['false_allow_count']} "
        f"false_deny={receipt['false_deny_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
