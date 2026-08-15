#!/usr/bin/env python3
"""Generate fixed M7-05 fixture and measured acceptance receipt."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.affected_test_selection_benchmark import (
    build_affected_test_selection_fixture,
    build_affected_test_selection_golden_matrix,
    run_affected_test_selection_benchmark,
)


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    arguments = parser.parse_args()
    fixture_path = (
        ROOT / "experiments/fixtures/m7-05-affected-test-selection-fixture.json"
    )
    golden_path = (
        ROOT / "experiments/fixtures/m7-05-affected-test-selection-golden.json"
    )
    result_path = (
        ROOT / "experiments/evidence/m7-05-affected-test-selection-results.json"
    )
    golden = build_affected_test_selection_golden_matrix()
    _atomic_json(golden_path, golden)
    _atomic_json(fixture_path, build_affected_test_selection_fixture(golden_matrix=golden))
    _atomic_json(
        result_path,
        run_affected_test_selection_benchmark(
            fixture_path=fixture_path,
            golden_path=golden_path,
            repository_root=ROOT,
            iterations=arguments.iterations,
            command_samples=1,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
