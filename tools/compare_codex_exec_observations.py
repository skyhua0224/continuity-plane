#!/usr/bin/env python3
"""Compare three or more sanitized Codex exec observations per arm."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.codex_exec_observation import (  # noqa: E402
    compare_codex_exec_observations,
    validate_codex_exec_comparison,
    validate_codex_exec_observation,
)


def _load(paths: list[Path]) -> list[dict]:
    receipts = []
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError("observation path is unavailable")
        value = json.loads(path.read_text(encoding="utf-8"))
        validate_codex_exec_observation(value)
        receipts.append(value)
    return receipts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="compare sanitized Codex exec observations"
    )
    parser.add_argument("--baseline", type=Path, action="append", required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument("--report-id", required=True)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)
    report = compare_codex_exec_observations(
        _load(arguments.baseline),
        _load(arguments.candidate),
        report_id=arguments.report_id,
        observed_at=arguments.observed_at,
    )
    validate_codex_exec_comparison(report)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = arguments.output.with_suffix(f"{arguments.output.suffix}.tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(arguments.output)
    print(
        "Codex exec comparison: "
        f"samples={report['sample_count_per_arm']} "
        f"input={report['improvements']['input_median_percent']}% "
        f"verdict={report['verdict']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
