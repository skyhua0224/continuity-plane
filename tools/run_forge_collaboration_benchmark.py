#!/usr/bin/env python3
"""Run and publish the offline M8-08 forge collaboration benchmark."""

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

from context_control_plane.forge_collaboration_benchmark import (
    benchmark_forge_collaboration,
    validate_forge_collaboration_benchmark,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--samples", type=int, default=1_000)
    parser.add_argument("--generated-at", default="2026-08-17T08:00:00+08:00")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m8-08-forge-collaboration-results.json"),
    )
    args = parser.parse_args()
    root = args.root.resolve()
    receipt = benchmark_forge_collaboration(
        root=root,
        samples=args.samples,
        generated_at=args.generated_at,
    )
    validate_forge_collaboration_benchmark(receipt, root=root)
    schema = json.loads(
        (root / "schemas/m8-08/forge-collaboration-benchmark.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(receipt)
    if receipt["verdict"]["decision"] != "pass":
        raise SystemExit(
            f"M8-08 forge collaboration benchmark failed: "
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

    print(
        "M8-08 forge collaboration benchmark: "
        f"mapping={receipt['complete_visible_record_mappings']}/"
        f"{receipt['projection_attempts']} "
        f"replay={receipt['deterministic_replays']}/{receipt['replay_attempts']} "
        f"stale_ref={receipt['stale_ref_rejections']}/"
        f"{receipt['stale_ref_attempts']} "
        f"unpublished={receipt['explicit_unpublished_downgrades']}/"
        f"{receipt['unpublished_work_attempts']} "
        f"p95={receipt['latency_ms']['p95_ms']:.6f}ms "
        f"authority_escalations={receipt['authority_escalations']} "
        f"decision={receipt['verdict']['decision']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
