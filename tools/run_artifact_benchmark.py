#!/usr/bin/env python3
"""Run the M2-04 local artifact benchmark and emit a YAML receipt."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import shlex
import sys
import tempfile
from pathlib import Path

import yaml

_ROOT = Path(__file__).parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context_control_plane.artifact_benchmark import run_artifact_benchmark


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
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
    parser = argparse.ArgumentParser(description="benchmark the local artifact store")
    parser.add_argument("--payload-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--range-bytes", type=int, default=8192)
    parser.add_argument("--observed-at")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(raw_arguments)

    with tempfile.TemporaryDirectory(prefix="context-artifact-benchmark-") as directory:
        result = run_artifact_benchmark(
            Path(directory) / "artifact-store",
            payload_bytes=args.payload_bytes,
            range_bytes=args.range_bytes,
        )

    observed_at = args.observed_at or datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    receipt = {
        "schema_version": result.pop("schema_version"),
        "observed_at": observed_at,
        "provenance": {
            "implementation_sha256": _sha256(
                _ROOT / "context_control_plane/artifact_store.py"
            ),
            "benchmark_sha256": _sha256(
                _ROOT / "context_control_plane/artifact_benchmark.py"
            ),
            "runner_sha256": _sha256(Path(__file__)),
        },
        "environment": result.pop("environment"),
        "measurement": result,
        "acceptance": {
            "checksum_verified": result["integrity"] == "passed",
            "bounded_range_read": result["range_bytes"] <= args.range_bytes,
            "external_services": result.get("external_services", 0),
        },
        "generation": {
            "command": shlex.join(
                [
                    ".venv/bin/python",
                    "tools/run_artifact_benchmark.py",
                    *raw_arguments,
                ]
            ),
            "arguments": raw_arguments,
            "writes_runtime_state_to_repository": False,
        },
    }
    payload = yaml.safe_dump(receipt, sort_keys=False)
    if args.output is None:
        print(payload, end="")
    else:
        _atomic_write(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
