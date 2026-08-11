#!/usr/bin/env python3
"""Generate the versioned M4-06 Skill compatibility probe receipt."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context_control_plane.skill_compatibility_probe import (
    run_skill_compatibility_probe,
    validate_skill_compatibility_probe_receipt,
)


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--observed-at", required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/state/m4-06-skill-compatibility-results.yaml"),
    )
    args = parser.parse_args()
    root = _ROOT
    receipt = run_skill_compatibility_probe(
        root,
        samples=args.samples,
        observed_at=args.observed_at,
        arguments=[
            "--samples",
            str(args.samples),
            "--observed-at",
            args.observed_at,
            "--output",
            str(args.output),
        ],
    )
    validate_skill_compatibility_probe_receipt(receipt, root=root)
    _atomic_write(
        args.output,
        yaml.safe_dump(receipt, allow_unicode=True, sort_keys=False),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
