#!/usr/bin/env python3
"""Run and persist the local multi-session Work/claim/lease fault benchmark."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from context_control_plane.shared_work_benchmark import (
    benchmark_shared_work,
    validate_shared_work_benchmark,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=1_000)
    parser.add_argument("--generated-at", default="2026-08-21T15:00:00+08:00")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m8-02-shared-work-results.json"),
    )
    args = parser.parse_args(argv)
    receipt = benchmark_shared_work(
        samples=args.samples,
        generated_at=args.generated_at,
        latency_p95_threshold_ms=50.0,
    )
    validate_shared_work_benchmark(receipt)
    if receipt["verdict"]["decision"] != "pass":
        raise SystemExit(f"shared Work benchmark failed: {receipt['verdict']['failed_gates']}")
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(receipt, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    print(
        "M8-02 shared Work benchmark: "
        f"scenarios={receipt['sample_count']}/{receipt['sample_count']} "
        f"silent_overwrites={receipt['metrics']['silent_overwrites']} "
        f"duplicate_effects={receipt['metrics']['duplicate_effects']} "
        f"orphan_reclaims={receipt['metrics']['orphan_reclaims']} "
        f"p95={receipt['latency_ms']['p95']:.6f}ms "
        f"decision={receipt['verdict']['decision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
