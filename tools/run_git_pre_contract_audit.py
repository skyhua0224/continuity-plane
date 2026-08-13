#!/usr/bin/env python3
"""Audit the repository root and publish its immutable admission receipt."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from context_control_plane.git_admission import audit_first_commit


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="audit a repository root commit against its current Git contract"
    )
    parser.add_argument("--root", type=Path, default=_ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    receipt = audit_first_commit(args.root.resolve())
    content = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(content)
    else:
        _write_atomic(args.output.resolve(), content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
