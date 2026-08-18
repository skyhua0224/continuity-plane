#!/usr/bin/env python3
"""Run the M9-04 Context Health measured acceptance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.context_health_benchmark import (
    benchmark_context_health_projection,
    validate_context_health_benchmark,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    receipt = benchmark_context_health_projection(
        root=arguments.root,
        iterations=arguments.iterations,
        generated_at=arguments.generated_at,
    )
    validate_context_health_benchmark(receipt, root=arguments.root)
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
