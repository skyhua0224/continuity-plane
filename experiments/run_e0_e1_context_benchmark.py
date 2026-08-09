#!/usr/bin/env python3
"""Run the synthetic E0/E1 context compression benchmark."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from context_control_plane.context_benchmark import run_benchmark, synthetic_scenarios


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--budgets-chars",
        nargs="+",
        type=int,
        default=[256, 512, 768, 1024],
        help="tail-retention compaction budgets in characters",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_benchmark(synthetic_scenarios(), budgets_chars=args.budgets_chars)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
