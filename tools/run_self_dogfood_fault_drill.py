#!/usr/bin/env python3
"""Run the M10-00 local compaction/Idea/interrupt/worker-loss drill."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.self_dogfood_pilot import (
    run_self_dogfood_fault_drill,
    validate_evidence_matrix_receipt,
    validate_self_dogfood_fault_drill,
    validate_self_dogfood_pilot_plan,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run M10-00 local fault drill")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("experiments/evidence/m10-00-self-dogfood-pilot-plan.json"),
    )
    parser.add_argument(
        "--matrix",
        type=Path,
        default=Path("experiments/evidence/m10-00-evidence-matrix-results.json"),
    )
    parser.add_argument("--observed-at", default="2026-08-18T00:05:00+08:00")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-fault-drill-results.json"),
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    plan_path = (
        arguments.plan if arguments.plan.is_absolute() else root / arguments.plan
    )
    matrix_path = (
        arguments.matrix if arguments.matrix.is_absolute() else root / arguments.matrix
    )
    output = (
        arguments.output if arguments.output.is_absolute() else root / arguments.output
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    validate_self_dogfood_pilot_plan(plan, root=root)
    validate_evidence_matrix_receipt(matrix, plan=plan, root=root)
    receipt = run_self_dogfood_fault_drill(
        plan,
        matrix,
        root=root,
        observed_at=arguments.observed_at,
    )
    validate_self_dogfood_fault_drill(
        receipt,
        plan=plan,
        matrix_receipt=matrix,
        root=root,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        "self-dogfood fault drill: "
        f"faults={len(receipt['faults'])} "
        f"workers={receipt['worker_count']} "
        f"coverage={receipt['coverage']['overall_coverage_millionths']}/1000000 "
        f"status={receipt['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
