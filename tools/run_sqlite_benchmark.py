#!/usr/bin/env python3
"""Run the M2-09 SQLite benchmark and print a comparable YAML receipt."""

from __future__ import annotations

import argparse
import copy
import datetime
import os
import sys
import tempfile
from pathlib import Path

import yaml

_ROOT = Path(__file__).parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context_control_plane.sqlite_benchmark import (
    build_sqlite_benchmark_receipt,
    run_sqlite_benchmark,
    run_sqlite_stream_benchmark,
)


def _load_inputs(root: Path) -> tuple[dict, dict]:
    fixtures = yaml.safe_load(
        (root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
            encoding="utf-8"
        )
    )
    initial = next(
        case["document"]
        for case in fixtures["cases"]
        if case["case_id"] == "completed-work-overlap-blocked"
    )
    postgres = yaml.safe_load(
        (root / "experiments/state/m2-03-postgres-cas-results.yaml").read_text(
            encoding="utf-8"
        )
    )
    return initial, postgres


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="benchmark the embedded SQLite StateStore"
    )
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--stream-events", type=int, default=1000)
    parser.add_argument("--observed-at")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(raw_arguments)
    initial, postgres = _load_inputs(_ROOT)

    with tempfile.TemporaryDirectory(prefix="context-sqlite-benchmark-") as directory:
        result = run_sqlite_benchmark(
            Path(directory) / "state.sqlite3",
            copy.deepcopy(initial),
            samples=args.samples,
        )
        stream = run_sqlite_stream_benchmark(
            Path(directory) / "stream.sqlite3",
            copy.deepcopy(initial),
            events=args.stream_events,
        )

    observed_at = args.observed_at or datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    receipt = build_sqlite_benchmark_receipt(
        root=_ROOT,
        benchmark=result,
        stream=stream,
        postgres=postgres,
        observed_at=observed_at,
        arguments=raw_arguments,
    )
    payload = yaml.safe_dump(receipt, sort_keys=False)
    if args.output is None:
        print(payload, end="")
    else:
        _atomic_write(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
