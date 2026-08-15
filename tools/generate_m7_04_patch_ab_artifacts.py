#!/usr/bin/env python3
"""Generate deterministic M7-04 fixture and benchmark artifacts."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.patch_ab_evaluation import (
    build_fixed_patch_ab_fixture,
    build_patch_ab_benchmark,
)


def _write_json(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    evidence_root = ROOT / "experiments" / "evidence"
    benchmark, outcomes = build_patch_ab_benchmark(samples=1000)
    _write_json(
        evidence_root / "m7-04-patch-ab-fixture.json",
        build_fixed_patch_ab_fixture(),
    )
    _write_json(
        evidence_root / "m7-04-patch-ab-benchmark-results.json",
        benchmark,
    )
    (evidence_root / "m7-04-patch-ab-benchmark-outcomes.json").write_bytes(
        outcomes
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
