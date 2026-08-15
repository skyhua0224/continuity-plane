#!/usr/bin/env python3
"""Generate independently schema-valid M6 acceptance receipts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.m6_acceptance_fixtures import build_m6_acceptance_fixtures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("experiments/retrieval"),
    )
    args = parser.parse_args()
    output_dir = args.output_dir if args.output_dir.is_absolute() else ROOT / args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    fixtures = build_m6_acceptance_fixtures(samples=args.samples)
    for filename, document in fixtures.items():
        (output_dir / filename).write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    retrieval = fixtures["m6-01-retrieval-results.json"]
    ablation = fixtures["m6-04-memory-ablation.json"]
    print(
        "M6 acceptance fixtures: "
        f"files={len(fixtures)} retrieval={retrieval['successful_samples']}/{retrieval['samples']} "
        f"read_reduction={retrieval['duplicate_read_reduction_percent']}% "
        f"memory={ablation['baseline_accuracy']}->{ablation['candidate_accuracy']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
