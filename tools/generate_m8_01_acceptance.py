#!/usr/bin/env python3
"""Generate the measured M8-01 durable-operation acceptance receipt."""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.durable_operation_benchmark import (
    benchmark_durable_operation,
)

CRASH_POINTS = (
    "after-prepared",
    "after-intent-commit",
    "after-intent-record",
    "after-effect-start",
    "after-external-effect",
    "after-effect-settlement",
    "after-state-commit",
    "after-response-record",
    "after-terminal",
)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="measure M8-01 SIGKILL recovery and semantic-effect idempotency"
    )
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument(
        "--crash-point",
        action="append",
        choices=CRASH_POINTS,
        dest="crash_points",
    )
    parser.add_argument("--generated-at")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "experiments/evidence/m8-01-durable-operation-results.json",
    )
    arguments = parser.parse_args(argv)
    generated_at = arguments.generated_at or datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    receipt = benchmark_durable_operation(
        root=ROOT,
        samples=arguments.samples,
        crash_points=tuple(arguments.crash_points or CRASH_POINTS),
        generated_at=generated_at,
    )
    _atomic_json(arguments.output, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
