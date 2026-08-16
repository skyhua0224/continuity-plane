#!/usr/bin/env python3
"""Run and publish the offline M8-03 workflow benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.workflow_orchestration_benchmark import (
    benchmark_workflow_orchestration,
    validate_workflow_orchestration_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--generated-at", default="2026-08-16T20:00:00+08:00")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m8-03-workflow-results.json"),
    )
    args = parser.parse_args()
    receipt = benchmark_workflow_orchestration(
        root=ROOT,
        samples=args.samples,
        generated_at=args.generated_at,
    )
    validate_workflow_orchestration_benchmark(receipt, root=ROOT)
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=output.parent,
            prefix=f".{output.name}.",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(receipt, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, output)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
    print(
        "M8-03 workflow benchmark: "
        f"chains={receipt['successful_chains']}/{receipt['samples']} "
        f"faults={receipt['fault_rejections']}/{receipt['fault_attempts']} "
        f"p95={receipt['latency_ms']['p95']}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
