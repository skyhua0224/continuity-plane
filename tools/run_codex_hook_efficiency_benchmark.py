#!/usr/bin/env python3
"""Run the M11 Codex hook lifecycle efficiency benchmark."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.codex_hook_efficiency_benchmark import (  # noqa: E402
    benchmark_codex_hook_efficiency,
    validate_codex_hook_efficiency_receipt,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="measure Codex lifecycle hook calls and model-visible context"
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--baseline-git-ref", required=True)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m11-00-codex-hook-efficiency.json"),
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    output = arguments.output
    if not output.is_absolute():
        output = root / output
    receipt = benchmark_codex_hook_efficiency(
        root,
        baseline_git_ref=arguments.baseline_git_ref,
        samples=arguments.samples,
        observed_at=arguments.observed_at,
    )
    validate_codex_hook_efficiency_receipt(receipt, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        "Codex hook efficiency: "
        f"calls={receipt['improvements']['continuity_call_reduction_percent']}% "
        f"context={receipt['improvements']['model_context_byte_reduction_percent']}% "
        f"deny={receipt['arms']['candidate']['deny_count']} "
        f"stop={receipt['arms']['candidate']['stop_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
