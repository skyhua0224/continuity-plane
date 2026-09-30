#!/usr/bin/env python3
"""Generate committed M7-03 provider-neutral Verification Profile fixtures."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.verification_profile import build_verification_profile


def _gate(
    gate_id: str,
    gate_kind: str,
    mode: str,
    capability: str,
    *,
    condition: str | None = None,
    dependencies: list[str] | None = None,
    thresholds: list[dict] | None = None,
) -> dict:
    return {
        "gate_id": gate_id,
        "gate_kind": gate_kind,
        "mode": mode,
        "condition_ref": condition,
        "capability_refs": [capability],
        "depends_on_gate_ids": dependencies or [],
        "evidence_requirements": ["invocation", "exit-status", "artifact-digest"],
        "thresholds": thresholds or [],
    }


def _profiles() -> dict[str, dict]:
    alkaidlab = build_verification_profile(
        profile_id="verification/alkaidlab/default",
        project_id="alkaidlab",
        profile_version="1.0.0-alpha.1",
        revision=1,
        valid_from="2026-08-16T00:00:00+08:00",
        valid_until=None,
        gates=[
            _gate("static", "static", "required", "capability/alkaidlab/static"),
            _gate(
                "tdd",
                "tdd",
                "required",
                "capability/alkaidlab/test",
                dependencies=["static"],
            ),
            _gate(
                "build",
                "build",
                "required",
                "capability/alkaidlab/build",
                dependencies=["tdd"],
            ),
            _gate(
                "contract",
                "contract",
                "conditional",
                "capability/alkaidlab/contract",
                condition="condition/protocol-or-api-changed",
                dependencies=["build"],
            ),
            _gate(
                "golden",
                "golden",
                "conditional",
                "capability/alkaidlab/golden",
                condition="condition/serialized-output-changed",
                dependencies=["build"],
            ),
            _gate(
                "mutation",
                "mutation",
                "conditional",
                "capability/alkaidlab/mutation",
                condition="condition/logic-or-parser-changed",
                dependencies=["tdd"],
                thresholds=[
                    {
                        "metric": "mutation_score",
                        "operator": "gte",
                        "value": 8000,
                        "unit": "basis-points",
                    }
                ],
            ),
            _gate(
                "loopback",
                "loopback",
                "conditional",
                "capability/alkaidlab/loopback",
                condition="condition/stream-or-network-path-changed",
                dependencies=["build"],
            ),
            _gate(
                "weak-network",
                "weak-network",
                "conditional",
                "capability/alkaidlab/weak-network",
                condition="condition/transport-or-congestion-changed",
                dependencies=["loopback"],
            ),
            _gate(
                "live",
                "live",
                "conditional",
                "capability/alkaidlab/live-device",
                condition="condition/os-or-device-path-changed",
                dependencies=["build"],
            ),
            _gate(
                "performance",
                "performance",
                "conditional",
                "capability/alkaidlab/performance",
                condition="condition/hot-path-changed",
                dependencies=["build"],
                thresholds=[
                    {
                        "metric": "regression",
                        "operator": "lte",
                        "value": 0,
                        "unit": "basis-points",
                    }
                ],
            ),
            _gate(
                "fault-recovery",
                "fault-recovery",
                "conditional",
                "capability/alkaidlab/fault-recovery",
                condition="condition/durable-workflow-changed",
                dependencies=["build"],
            ),
        ],
    )
    portable = build_verification_profile(
        profile_id="verification/portable-python-library/default",
        project_id="portable-python-library",
        profile_version="1.0.0-alpha.1",
        revision=1,
        valid_from="2026-08-16T00:00:00+08:00",
        valid_until=None,
        gates=[
            _gate("static", "static", "required", "capability/python"),
            _gate(
                "tdd",
                "tdd",
                "required",
                "capability/python",
                dependencies=["static"],
            ),
            _gate(
                "build",
                "build",
                "required",
                "capability/python",
                dependencies=["tdd"],
            ),
            _gate(
                "contract",
                "contract",
                "conditional",
                "capability/python",
                condition="condition/public-api-changed",
                dependencies=["build"],
            ),
            _gate(
                "golden",
                "golden",
                "conditional",
                "capability/python",
                condition="condition/serialized-output-changed",
                dependencies=["build"],
            ),
            _gate(
                "mutation",
                "mutation",
                "conditional",
                "capability/mutation-runner",
                condition="condition/logic-changed",
                dependencies=["tdd"],
                thresholds=[
                    {
                        "metric": "mutation_score",
                        "operator": "gte",
                        "value": 8000,
                        "unit": "basis-points",
                    }
                ],
            ),
            _gate(
                "performance",
                "performance",
                "conditional",
                "capability/local-benchmark",
                condition="condition/hot-path-changed",
                dependencies=["build"],
            ),
            _gate(
                "live",
                "live",
                "optional",
                "capability/live-environment",
            ),
        ],
    )
    return {
        "alkaidlab.example.json": alkaidlab,
        "portable-python-library.example.json": portable,
    }


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
    output = ROOT / "profiles" / "verification"
    for name, profile in _profiles().items():
        _atomic_json(output / name, profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
