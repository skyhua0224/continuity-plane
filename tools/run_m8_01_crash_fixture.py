#!/usr/bin/env python3
"""Run one M8-01 local process-crash fixture."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.testing.durable_operation_fixture import (
    canonical_receipt,
    run_fixture,
    run_real_fixture,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--crash-point")
    parser.add_argument("--real-authority", action="store_true")
    parser.add_argument("--status-unavailable", action="store_true")
    arguments = parser.parse_args()
    if arguments.real_authority:
        receipt = run_real_fixture(
            arguments.root,
            repository_root=ROOT,
            crash_point=arguments.crash_point,
            status_lookup=("none" if arguments.status_unavailable else "supported"),
        )
    else:
        receipt = run_fixture(arguments.root, crash_point=arguments.crash_point)
    print(canonical_receipt(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
