#!/usr/bin/env python3
"""Measure validated reads from a temporary local authority database."""

from __future__ import annotations

import argparse
import json
import math
import platform
import tempfile
import time
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

from continuity_plane.cli import main as cli_main
from continuity_plane.sqlite_state_store import SQLiteStateStore


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[index]


def run_benchmark(*, iterations: int = 1_000) -> dict[str, Any]:
    if type(iterations) is not int or iterations < 1:
        raise ValueError("iterations must be a positive integer")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with redirect_stdout(StringIO()):
            cli_main(["init", "--root", str(root), "--project-id", "benchmark-project"])
        store = SQLiteStateStore(root / ".continuity/state.sqlite3")
        durations: list[float] = []
        successful_reads = 0
        for _ in range(iterations):
            started = time.perf_counter_ns()
            snapshot = store.read_project("benchmark-project")
            durations.append((time.perf_counter_ns() - started) / 1_000_000)
            if (
                snapshot["project"]["project_id"] == "benchmark-project"
                and snapshot["project"]["revision"] == 0
            ):
                successful_reads += 1
    return {
        "schema_version": "context.public-benchmark/v1",
        "benchmark": "local-authority-read",
        "iterations": iterations,
        "successful_reads": successful_reads,
        "quality_rate": successful_reads / iterations,
        "median_ms": round(_percentile(durations, 0.5), 6),
        "p95_ms": round(_percentile(durations, 0.95), 6),
        "external_services_required": 0,
        "environment": {
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1_000)
    args = parser.parse_args()
    print(json.dumps(run_benchmark(iterations=args.iterations), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
