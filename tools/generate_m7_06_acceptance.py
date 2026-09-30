#!/usr/bin/env python3
"""Generate the replayable M7-06 ReferenceWatcher benchmark receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.reference_watcher_benchmark import (
    benchmark_reference_watcher,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m7-06-reference-watcher-results.json"),
    )
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    receipt = benchmark_reference_watcher(samples=args.samples, root=ROOT)
    output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    adversarial_attempts = sum(receipt["adversarial_case_counts"].values())
    adversarial_allows = sum(receipt["adversarial_allow_counts"].values())
    print(
        "M7-06 ReferenceWatcher benchmark: "
        f"{receipt['successful_samples']}/{receipt['samples']} "
        f"changed={receipt['changed_fixture_stale_or_quarantined_count']}/"
        f"{receipt['changed_fixture_count']} "
        f"unreviewed_allow={receipt['unreviewed_completion_allows']} "
        f"reverified={receipt['reverified_release_allows']}/"
        f"{receipt['reverified_release_attempts']} "
        f"adversarial_rejected={adversarial_attempts - adversarial_allows}/"
        f"{adversarial_attempts}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
