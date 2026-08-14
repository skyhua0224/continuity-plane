#!/usr/bin/env python3
"""Generate the local zero-service M3-06 Idea continuity benchmark receipt."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.idea_continuity_benchmark import (
    run_idea_continuity_benchmark,
)

parser = argparse.ArgumentParser()
parser.add_argument("--samples", type=int, default=40)
parser.add_argument("--observed-at", required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
receipt = run_idea_continuity_benchmark(
    root=ROOT, samples=args.samples, observed_at=args.observed_at
)
args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
