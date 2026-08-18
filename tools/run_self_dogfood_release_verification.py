#!/usr/bin/env python3
"""Run and bind the M10-00 repository verification command."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.self_dogfood_pilot import (
    build_repository_verification_receipt,
    validate_repository_verification_receipt,
    validate_self_dogfood_fault_drill,
    validate_self_dogfood_pilot_plan,
)


def _parse_suite(output: str) -> tuple[int, int]:
    match = re.search(r"Ran\s+(\d+)\s+tests?\s+in\s+[0-9.]+s", output)
    if match is None:
        raise ValueError("unittest output has no test count")
    skipped_match = re.search(r"skipped=(\d+)", output)
    skipped = int(skipped_match.group(1)) if skipped_match else 0
    if re.search(r"\bFAILED\s*\(", output):
        raise ValueError("unittest suite failed")
    if "OK" not in output:
        raise ValueError("unittest output has no OK verdict")
    return int(match.group(1)), skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run M10-00 release verification")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--plan",
        type=Path,
        default=Path("experiments/evidence/m10-00-self-dogfood-pilot-plan.json"),
    )
    parser.add_argument(
        "--fault",
        type=Path,
        default=Path("experiments/evidence/m10-00-fault-drill-results.json"),
    )
    parser.add_argument(
        "--observed-at",
        default="2026-08-18T00:10:00+08:00",
    )
    parser.add_argument(
        "--suite-command-json",
        default=None,
        help="JSON array overriding the full CI unittest command",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-release-verification-results.json"),
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    plan_path = (
        arguments.plan if arguments.plan.is_absolute() else root / arguments.plan
    )
    fault_path = (
        arguments.fault if arguments.fault.is_absolute() else root / arguments.fault
    )
    output = (
        arguments.output if arguments.output.is_absolute() else root / arguments.output
    )
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    fault = json.loads(fault_path.read_text(encoding="utf-8"))
    validate_self_dogfood_pilot_plan(plan, root=root)
    validate_self_dogfood_fault_drill(
        fault,
        plan=plan,
        matrix_receipt=json.loads(
            (
                root / "experiments/evidence/m10-00-evidence-matrix-results.json"
            ).read_text()
        ),
        root=root,
    )
    if arguments.suite_command_json:
        command = json.loads(arguments.suite_command_json)
    else:
        command = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test_*.py",
            "-q",
        ]
    started = time.perf_counter()
    completed = subprocess.run(
        command,
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    wall_time = time.perf_counter() - started
    combined = completed.stdout + completed.stderr
    if completed.returncode != 0:
        sys.stderr.write(combined)
        return completed.returncode or 1
    try:
        passed, skipped = _parse_suite(combined)
    except ValueError as exc:
        print(f"release verification parse failed: {exc}", file=sys.stderr)
        sys.stderr.write(combined)
        return 1
    receipt = build_repository_verification_receipt(
        plan,
        fault,
        root=root,
        command=command,
        passed=passed,
        skipped=skipped,
        wall_time_seconds=wall_time,
        output=combined,
        observed_at=arguments.observed_at,
    )
    validate_repository_verification_receipt(
        receipt,
        plan=plan,
        fault_receipt=fault,
        root=root,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        "self-dogfood release verification: "
        f"tests={passed} skipped={skipped} wall={wall_time:.3f}s status={receipt['status']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
