#!/usr/bin/env python3
"""Generate the deterministic M3-04 effect-scope benchmark receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

_ROOT = Path(__file__).parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context_control_plane.effect_scope_benchmark import benchmark_effect_scope_gate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=10_000)
    parser.add_argument("--pending-effect-counts", default="1,100,1000")
    parser.add_argument("--conflict-samples", type=int, default=1_000)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        pending_effect_counts = tuple(
            int(value) for value in arguments.pending_effect_counts.split(",")
        )
    except ValueError:
        parser.error("--pending-effect-counts must be comma-separated integers")
    root = _ROOT
    fixtures = yaml.safe_load(
        (root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
            encoding="utf-8"
        )
    )
    snapshot = next(
        case["document"]
        for case in fixtures["cases"]
        if case["case_id"] == "solo-active-work"
    )
    receipt = benchmark_effect_scope_gate(
        snapshot,
        samples=arguments.samples,
        pending_effect_counts=pending_effect_counts,
        conflict_samples=arguments.conflict_samples,
        observed_at=arguments.observed_at,
        root=root,
    )
    arguments.output.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
