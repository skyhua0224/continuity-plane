#!/usr/bin/env python3
"""Generate the local-embedded M3-08 continuation benchmark receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.input_progression_benchmark import (
    run_input_progression_benchmark,
)

parser = argparse.ArgumentParser()
parser.add_argument("--samples", type=int, default=1000)
parser.add_argument("--observed-at", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
receipt = run_input_progression_benchmark(
    root=ROOT,
    samples=args.samples,
    observed_at=args.observed_at,
)
args.output.write_text(
    json.dumps(receipt, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
