#!/usr/bin/env python3
"""Run the M2-06 restore benchmark and emit a provenance-bound receipt."""

from __future__ import annotations

import argparse
import copy
import datetime
import hashlib
import os
import sys
import tempfile
from pathlib import Path

import yaml

_ROOT = Path(__file__).parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context_control_plane.checkpoint_benchmark import (
    build_checkpoint_benchmark_receipt,
    run_checkpoint_benchmark,
)
from context_control_plane.schema_governance import registry_digest
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_store import capability_manifest_to_document


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _load_read_result() -> dict:
    fixtures = yaml.safe_load(
        (_ROOT / "experiments/state/m2-01-core-fixtures.yaml").read_text(
            encoding="utf-8"
        )
    )
    snapshot = copy.deepcopy(
        next(
            case["document"]
            for case in fixtures["cases"]
            if case["case_id"] == "solo-active-work"
        )
    )
    registry = yaml.safe_load(
        (_ROOT / "schemas/registry.yaml").read_text(encoding="utf-8")
    )
    return {
        "snapshot": snapshot,
        "revision": snapshot["project"]["revision"],
        "event_head": None,
        "registry_digest": registry_digest(registry),
        "capabilities": capability_manifest_to_document(
            SQLiteStateStore.capability_manifest
        ),
    }


def main(argv: list[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="benchmark immutable checkpoint restore")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--observed-at")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(raw_arguments)
    plan_digest = hashlib.sha256((_ROOT / "MASTER.md").read_bytes()).hexdigest()

    with tempfile.TemporaryDirectory(prefix="context-checkpoint-benchmark-") as directory:
        read_result = _load_read_result()
        benchmark = run_checkpoint_benchmark(
            Path(directory),
            read_result["snapshot"],
            canonical_plan_sha256=plan_digest,
            registry_digest=read_result["registry_digest"],
            samples=args.samples,
        )
    observed_at = args.observed_at or datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    receipt = build_checkpoint_benchmark_receipt(
        root=_ROOT,
        benchmark=benchmark,
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
