#!/usr/bin/env python3
"""Run and publish the offline M8-06 Harness Run benchmark."""

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

from context_control_plane.harness_run_benchmark import benchmark_harness_run


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--samples", type=int, default=1_000)
    parser.add_argument("--generated-at", default="2026-08-16T20:00:00+08:00")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m8-06-harness-benchmark-results.json"),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    receipt = benchmark_harness_run(
        root=root,
        samples=args.samples,
        generated_at=args.generated_at,
    )
    if receipt["verdict"]["decision"] != "pass":
        raise SystemExit(f"M8-06 benchmark failed: {receipt['verdict']['failed_gates']}")
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
        "M8-06 Harness benchmark: "
        f"drift={receipt['metrics']['provider_drift_rejections']}/{receipt['provider_drift_attempts']} "
        f"loss={receipt['metrics']['worker_loss_authority_unchanged']}/{receipt['worker_loss_attempts']} "
        f"effect={receipt['metrics']['effect_scope_rejections']}/{receipt['effect_attempts']} "
        f"handoff={receipt['metrics']['stale_handoff_rejections']}/{receipt['stale_handoff_attempts']} "
        f"p95={receipt['latency_ms']['p95_ms']:.6f}ms"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
