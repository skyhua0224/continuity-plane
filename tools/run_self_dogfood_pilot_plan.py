#!/usr/bin/env python3
"""Build the repository-bound M10-00 self-dogfood pilot plan."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.self_dogfood_pilot import (
    build_self_dogfood_pilot_plan,
    validate_self_dogfood_pilot_plan,
)


def _plan(root: Path, *, state_revision: int, created_at: str) -> dict:
    evidence_paths = {
        "E0": ["docs/migrations/m5-05-context-accounting-acceptance-2026-08-15.md"],
        "E1": ["docs/migrations/m5-03-postcompact-canary-acceptance-2026-08-15.md"],
        "E2": ["docs/migrations/m3-08-continuation-dispatch-acceptance-2026-08-14.md"],
        "E3": ["docs/migrations/m4-04-layered-skill-loading-acceptance-2026-08-11.md"],
        "E4": ["docs/migrations/m4-03-skill-drift-quarantine-acceptance-2026-08-11.md"],
        "E5": ["docs/migrations/m6-retrieval-recall-acceptance-2026-08-15.md"],
        "E6": ["docs/migrations/m7-02-claim-evidence-gate-acceptance-2026-08-16.md"],
        "E7": [
            "docs/migrations/m7-05-affected-test-selection-acceptance-2026-08-16.md"
        ],
        "E8": ["docs/migrations/m8-02-shared-work-acceptance-2026-08-16.md"],
        "E9": ["docs/migrations/m8-01-durable-operation-acceptance-2026-08-16.md"],
    }
    return build_self_dogfood_pilot_plan(
        root=root,
        plan_id="pilot-plan-m10-00",
        campaign_id="campaign-m10-00",
        project_id="context-control-plane",
        state_revision=state_revision,
        created_at=created_at,
        required_leaves=[
            {
                "work_id": "M10-00-evidence-matrix",
                "dependency_ids": [],
                "worker_ref": "worker-analysis",
                "verifier_ref": "verifier-independent",
                "completion_gate_id": "gate-e0-e9",
            },
            {
                "work_id": "M10-00-fault-drill",
                "dependency_ids": ["M10-00-evidence-matrix"],
                "worker_ref": "worker-execution",
                "verifier_ref": "verifier-independent",
                "completion_gate_id": "gate-fault-coverage",
            },
            {
                "work_id": "M10-00-release-verification",
                "dependency_ids": ["M10-00-fault-drill"],
                "worker_ref": "worker-analysis",
                "verifier_ref": "verifier-independent",
                "completion_gate_id": "gate-repository-verification",
            },
        ],
        workers=[
            {
                "worker_ref": "worker-analysis",
                "role": "thinker",
                "provider_contract_version": "provider-neutral/v1",
            },
            {
                "worker_ref": "worker-execution",
                "role": "executor",
                "provider_contract_version": "provider-neutral/v1",
            },
        ],
        verifier_ref="verifier-independent",
        fault_kinds=["compaction", "idea", "interrupt", "worker-loss"],
        evidence_paths=evidence_paths,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build M10-00 pilot plan")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--state-revision", type=int, default=74)
    parser.add_argument(
        "--created-at",
        default="2026-08-17T23:59:00+08:00",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("experiments/evidence/m10-00-self-dogfood-pilot-plan.json"),
    )
    arguments = parser.parse_args(argv)
    root = arguments.root.resolve()
    output = arguments.output
    if not output.is_absolute():
        output = root / output
    plan = _plan(
        root,
        state_revision=arguments.state_revision,
        created_at=arguments.created_at,
    )
    validate_self_dogfood_pilot_plan(plan, root=root)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    print(
        "self-dogfood pilot plan: "
        f"leaves={len(plan['required_leaves'])} "
        f"workers={len(plan['workers'])} "
        f"faults={len(plan['fault_injections'])} "
        f"matrix={len(plan['evidence_matrix'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
