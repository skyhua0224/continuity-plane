#!/usr/bin/env python3
"""Run E0/E1 context composition against admitted M1-04 replay fixtures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from context_control_plane.context_benchmark import (
    replay_fixture_scenarios,
    run_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=Path("replay/fixtures"),
    )
    parser.add_argument(
        "--budgets-chars",
        nargs="+",
        type=int,
        default=[512, 768, 1024, 1536, 2048, 4096],
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    manifest = json.loads(
        (args.fixture_dir / "validation-receipts.json").read_text(encoding="utf-8")
    )
    fixtures = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(args.fixture_dir.glob("fx_*.json"))
    ]
    if len(fixtures) != manifest["fixture_count"]:
        raise ValueError("fixture count does not match the validation manifest")
    report = run_benchmark(
        replay_fixture_scenarios(fixtures),
        budgets_chars=args.budgets_chars,
    )
    report.update(
        {
            "corpus_kind": "admitted-real-replay-fixture",
            "corpus_sha256": manifest["corpus_sha256"],
            "fixture_count": manifest["fixture_count"],
            "source_count": manifest["source_count"],
        }
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
