#!/usr/bin/env python3
"""Run the deterministic M4-04 layered Skill loading benchmark."""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import tempfile
from pathlib import Path

import yaml


_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context_control_plane.layered_skill_benchmark import (  # noqa: E402
    run_layered_skill_benchmark,
    validate_layered_skill_benchmark_receipt,
)


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(
        description="benchmark bounded in-memory Skill content composition"
    )
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--observed-at")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(raw_arguments)
    observed_at = args.observed_at or datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    generation_arguments = [
        "--samples",
        str(args.samples),
        "--observed-at",
        observed_at,
        "--output",
        str(args.output),
    ]
    receipt = run_layered_skill_benchmark(
        _ROOT,
        samples=args.samples,
        observed_at=observed_at,
        arguments=generation_arguments,
    )
    validate_layered_skill_benchmark_receipt(receipt, root=_ROOT)
    _atomic_write(args.output, yaml.safe_dump(receipt, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
