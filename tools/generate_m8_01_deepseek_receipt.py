#!/usr/bin/env python3
"""Generate M8-01 evidence from the pinned DeepSeek Vitest report."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.deepseek_checkpoint_receipt import (
    SOURCE_REVISION,
    SOURCE_TEST_BLOB_SHA256,
    SOURCE_TEST_PATH,
    SOURCE_TREE,
    compose_deepseek_checkpoint_receipt,
)


def _command(*arguments: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        arguments,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="seal a pinned DeepSeek checkpoint crash-fixture receipt"
    )
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--generated-at")
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT / "experiments/evidence/m8-01-deepseek-checkpoint-receipt.json"
        ),
    )
    arguments = parser.parse_args(argv)
    source_root = arguments.source_root.resolve()
    report_path = arguments.report.resolve()
    source_revision = _command("git", "rev-parse", "HEAD", cwd=source_root)
    source_tree = _command("git", "rev-parse", "HEAD^{tree}", cwd=source_root)
    if source_revision != SOURCE_REVISION or source_tree != SOURCE_TREE:
        parser.error("DeepSeek source revision or tree is not pinned")
    source_payload = (source_root / SOURCE_TEST_PATH).read_bytes()
    source_test_blob_sha256 = hashlib.sha256(source_payload).hexdigest()
    if source_test_blob_sha256 != SOURCE_TEST_BLOB_SHA256:
        parser.error("DeepSeek crash fixture source digest is invalid")
    report_payload = report_path.read_bytes()
    report = json.loads(report_payload)
    node_version = _command("node", "--version").removeprefix("v")
    package_manager_version = _command(
        "corepack", "pnpm@11.7.0", "--version"
    )
    generated_at = arguments.generated_at or datetime.datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    receipt = compose_deepseek_checkpoint_receipt(
        report,
        generated_at=generated_at,
        source_revision=source_revision,
        source_tree=source_tree,
        source_test_blob_sha256=source_test_blob_sha256,
        report_sha256=hashlib.sha256(report_payload).hexdigest(),
        node_version=node_version,
        package_manager=f"pnpm@{package_manager_version}",
        platform=platform.system().lower(),
    )
    _atomic_json(arguments.output, receipt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
