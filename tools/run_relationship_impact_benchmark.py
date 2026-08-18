#!/usr/bin/env python3
"""Run and publish the M9-07 Relationship and Impact benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.relationship_impact_benchmark import (
    benchmark_relationship_impact_projection,
    validate_relationship_impact_benchmark,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="run the Relationship and Impact projection benchmark"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--scale-iterations", type=int, default=25)
    parser.add_argument(
        "--generated-at",
        default="2026-08-17T23:30:00+08:00",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m9-07-relationship-impact-results.json"),
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    output = arguments.output
    if not output.is_absolute():
        output = root / output
    receipt = benchmark_relationship_impact_projection(
        root=root,
        iterations=arguments.iterations,
        scale_iterations=arguments.scale_iterations,
        generated_at=arguments.generated_at,
    )
    validate_relationship_impact_benchmark(receipt, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        "Relationship and Impact benchmark: "
        f"complete={receipt['results']['complete_projection_matches']}/"
        f"{receipt['parameters']['iterations']} "
        f"scale={receipt['results']['scale_complete_matches']}/"
        f"{receipt['parameters']['scale_iterations']} "
        f"p95={receipt['latency_ms']['p95']:.6f}ms "
        f"scale_p95={receipt['scale_latency_ms']['p95']:.6f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
