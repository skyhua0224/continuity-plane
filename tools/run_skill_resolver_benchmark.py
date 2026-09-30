#!/usr/bin/env python3
"""Run and publish the M4-09 Skill resolver benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.skill_resolver_benchmark import (
    benchmark_replay_fixture,
    benchmark_skill_resolver,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--negative-samples", type=int, default=125)
    parser.add_argument("--generated-at", default="2026-08-14T18:00:00Z")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/routing/m4-09-skill-resolution-results.json"),
    )
    parser.add_argument(
        "--fixture-output",
        type=Path,
        default=Path(
            "experiments/skills/m4-09-skill-resolution-replay-v1alpha1.json"
        ),
    )
    args = parser.parse_args()
    receipt = benchmark_skill_resolver(
        samples=args.samples,
        negative_samples=args.negative_samples,
        generated_at=args.generated_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.fixture_output.parent.mkdir(parents=True, exist_ok=True)
    args.fixture_output.write_text(
        json.dumps(benchmark_replay_fixture(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        "skill resolver benchmark: "
        f"success={receipt['successful_samples']}/{receipt['samples']} "
        f"negative={receipt['negative_quarantine']}/{receipt['negative_samples']} "
        f"replay={receipt['replay_mismatch']} "
        f"p95={receipt['p95_ms']}ms "
        f"reduction={receipt['selection_reduction_percent']}%"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
