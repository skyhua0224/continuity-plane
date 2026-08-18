#!/usr/bin/env python3
"""Admit current E0-E9 evidence for the M10-00 pilot."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.self_dogfood_pilot import (
    build_evidence_matrix_receipt,
    validate_evidence_matrix_receipt,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="admit M10-00 E0-E9 evidence")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("experiments/evidence/m10-00-self-dogfood-pilot-plan.json"),
    )
    parser.add_argument(
        "--observed-at",
        default="2026-08-18T00:00:00+08:00",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-evidence-matrix-results.json"),
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    plan_path = arguments.plan
    if not plan_path.is_absolute():
        plan_path = root / plan_path
    output = arguments.output
    if not output.is_absolute():
        output = root / output
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    receipt = build_evidence_matrix_receipt(
        plan,
        root=root,
        observed_at=arguments.observed_at,
    )
    validate_evidence_matrix_receipt(receipt, plan=plan, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        "self-dogfood evidence matrix: "
        f"admitted={receipt['admitted_count']} "
        f"pending={receipt['pending_count']} "
        f"status={receipt['completion_status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
