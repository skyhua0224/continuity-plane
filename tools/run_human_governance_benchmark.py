"""Run and publish the M9-05 human governance benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.human_governance_benchmark import (
    benchmark_human_governance,
    validate_human_governance_benchmark,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run the human governance benchmark")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument(
        "--generated-at",
        default="2026-08-17T09:00:00+08:00",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m9-05-human-governance-results.json"),
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    output = arguments.output
    if not output.is_absolute():
        output = root / output
    receipt = benchmark_human_governance(
        root=root,
        iterations=arguments.iterations,
        generated_at=arguments.generated_at,
    )
    validate_human_governance_benchmark(receipt, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        "human governance benchmark: "
        f"four_action={receipt['results']['four_action_successes']}/"
        f"{receipt['parameters']['iterations'] * 4} "
        f"tamper_rejected={receipt['results']['event_tamper_rejections']}/"
        f"{receipt['parameters']['iterations']} "
        f"p95={receipt['latency_ms']['p95']:.6f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
