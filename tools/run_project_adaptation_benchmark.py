#!/usr/bin/env python3
"""Run and publish the offline M8-07 ProjectAdaptation benchmark."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.project_adaptation_benchmark import (
    benchmark_project_adaptation,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--samples", type=int, default=1_000)
    parser.add_argument("--generated-at", default="2026-08-16T20:00:00+08:00")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m8-07-project-adaptation-results.json"),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    receipt = benchmark_project_adaptation(
        root=root,
        samples=args.samples,
        generated_at=args.generated_at,
    )
    schema_path = root / "schemas/m8-07/project-adaptation-benchmark.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)
    if receipt["verdict"]["decision"] != "pass":
        raise SystemExit(
            f"M8-07 ProjectAdaptation benchmark failed: "
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

    rates = receipt["rates"]
    print(
        "M8-07 ProjectAdaptation benchmark: "
        f"replay={receipt['deterministic_replays']}/{receipt['replay_attempts']} "
        f"unapproved={receipt['unauthorized_activation_rejections']}/"
        f"{receipt['unauthorized_activation_attempts']} "
        f"rollback={receipt['rollback_veto_recoveries']}/"
        f"{receipt['rollback_attempts']} "
        f"reset={receipt['resets']}/{receipt['reset_attempts']} "
        f"p95={receipt['latency_ms']['p95_ms']:.6f}ms "
        f"authority_mutations={receipt['authority_mutations']} "
        f"decision={receipt['verdict']['decision']}"
    )
    assert rates["deterministic_replay_rate"] == 1.0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
