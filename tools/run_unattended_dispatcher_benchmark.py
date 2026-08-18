#!/usr/bin/env python3
"""Run and publish the offline M8-09 unattended dispatcher benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.unattended_dispatcher_benchmark import (
    benchmark_unattended_dispatcher,
    validate_unattended_dispatcher_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--samples", type=int, default=1_000)
    parser.add_argument("--generated-at", default="2026-08-17T12:00:00+00:00")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m8-09-unattended-dispatcher-results.json"),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    receipt = benchmark_unattended_dispatcher(
        root=root,
        samples=args.samples,
        generated_at=args.generated_at,
    )
    validate_unattended_dispatcher_benchmark(receipt, root=root)
    if receipt["verdict"]["decision"] != "pass":
        raise SystemExit(
            "M8-09 unattended dispatcher benchmark failed: "
            f"{receipt['verdict']['failed_gates']}"
        )

    output = args.output if args.output.is_absolute() else root / args.output
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
        "M8-09 unattended dispatcher benchmark: "
        f"required={receipt['required_completions']}/"
        f"{receipt['required_completion_attempts']} "
        f"closure={receipt['campaign_closures']}/"
        f"{receipt['campaign_closure_attempts']} "
        f"replay={receipt['replay_matches']}/{receipt['replay_attempts']} "
        f"duplicate={receipt['duplicate_claim_execute_complete_rejections']}/"
        f"{receipt['duplicate_attempts']} "
        f"p95={receipt['latency_ms']['p95_ms']:.6f}ms "
        f"authority_violations={receipt['authority_violations']} "
        f"decision={receipt['verdict']['decision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
