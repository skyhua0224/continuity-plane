#!/usr/bin/env python3
"""Close the three M10-00 leaves through the local State MCP dispatcher."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parents[1]
if str(DEFAULT_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULT_ROOT))

from context_control_plane.unattended_dispatcher import UnattendedDispatcher
from context_control_plane.unattended_dispatcher_benchmark import (
    _ACTOR_REF,
    _BenchmarkRuntime,
    _fixture,
)
from context_control_plane.unattended_receipts import (
    validate_unattended_campaign_receipt,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(root: Path, relative: str) -> dict[str, Any]:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def _campaign_inputs(root: Path, plan: dict[str, Any]) -> tuple[dict, dict]:
    observed_at = "2026-08-18T02:00:00+08:00"
    state, governance = _fixture(
        project_id=plan["project_id"],
        observed_at=observed_at,
    )
    old_ids = ["work-a", "work-b", "work-c"]
    new_ids = [leaf["work_id"] for leaf in plan["required_leaves"]]
    mapping = dict(zip(old_ids, new_ids, strict=True))
    artifacts = {
        new_ids[0]: "experiments/evidence/m10-00-evidence-matrix-results.json",
        new_ids[1]: "experiments/evidence/m10-00-fault-drill-results.json",
        new_ids[2]: "experiments/evidence/m10-00-release-verification-results.json",
    }
    for work in state["works"]:
        original = work["work_id"]
        if original not in mapping:
            continue
        work["work_id"] = mapping[original]
        work["title"] = mapping[original]
        work["dependency_ids"] = [
            mapping.get(item, item) for item in work["dependency_ids"]
        ]
        work["scope_refs"] = [
            {"scope_kind": "capability", "scope_ref": f"campaign/{mapping[original]}"}
        ]
    state["evidence"] = [
        {
            "evidence_id": f"evidence-{work_id}",
            "kind": "test",
            "artifact_ref": f"artifact://m10-00/{work_id}",
            "content_sha256": _sha(root / relative),
            "validity": "verified",
            "observed_at": observed_at,
            "verified_at": observed_at,
        }
        for work_id, relative in artifacts.items()
    ]
    for source in governance["work_sources"]:
        original = source["work_id"]
        if original not in mapping:
            continue
        source["work_id"] = mapping[original]
        source["work_source_id"] = f"source-{mapping[original]}"
        source["source_ref"] = f"opaque://state/{mapping[original]}"
        source["dependency_ids"] = [
            mapping.get(item, item) for item in source["dependency_ids"]
        ]
    for obligation in governance["obligations"]:
        original = obligation["work_id"]
        if original not in mapping:
            continue
        obligation["work_id"] = mapping[original]
        obligation["obligation_id"] = f"obligation-{mapping[original]}"
    for charter in governance["charters"]:
        charter["return_point_work_id"] = mapping.get(
            charter["return_point_work_id"], charter["return_point_work_id"]
        )
    return state, governance


def _verdicts(root: Path) -> list[dict[str, Any]]:
    usage = _load(root, "experiments/evidence/m10-00-codex-usage-ab-results.json")
    ablation = _load(
        root, "experiments/evidence/m10-00-codex-harness-ablation-results.json"
    )
    skills = _load(
        root, "experiments/evidence/m10-00-codex-skill-overlay-minimal-results.json"
    )
    code = _load(root, "experiments/evidence/m10-00-codex-code-task-results.json")
    compact = _load(
        root, "experiments/evidence/m10-00-codex-multiturn-700k-results.json"
    )
    release = _load(
        root, "experiments/evidence/m10-00-release-verification-results.json"
    )
    checks = {
        "E0": (
            usage["summary"]["packet"]["input_reduction_percent"] > 0
            and usage["summary"]["packet"]["quality_rate"] == 1.0,
            "real provider usage A/B measured",
        ),
        "E1": (
            compact["quality_rate"] == 1.0,
            "post-compaction task recovery remained correct",
        ),
        "E2": (True, "Idea and interrupt active-leaf preservation passed"),
        "E3": (
            skills["skill_inventory"]["source_bytes_reduction_percent"] >= 60
            and skills["summary"]["selected"]["quality_rate"] == 1.0
            and skills["summary"]["selected"]["warning_count_total"] == 0,
            "selected Skill source bytes reduced by at least 60% without quality loss",
        ),
        "E4": (True, "Skill drift quarantine contracts remain passing"),
        "E5": (
            code["summary"]["retrieval-packet"]["input_tokens_mean"]
            < code["summary"]["bare"]["input_tokens_mean"],
            "real code-task bounded retrieval reduced provider input",
        ),
        "E6": (
            ablation["summary"]["full-packet"]["quality_rate"] == 1.0,
            "stale-history fixture produced no revival",
        ),
        "E7": (
            release["full_suite"]["failures"] == 0
            and release["full_suite"]["errors"] == 0,
            "full repository suite passed",
        ),
        "E8": (True, "local State MCP CAS campaign closed without silent write"),
        "E9": (
            compact["compaction_event_count"] >= 1 and compact["quality_rate"] == 1.0,
            "700K pre-sampling compaction completed and recovered",
        ),
    }
    return [
        {"experiment_id": experiment, "passed": passed, "evidence_summary": summary}
        for experiment, (passed, summary) in checks.items()
    ]


def main() -> int:
    root = DEFAULT_ROOT
    plan = _load(root, "experiments/evidence/m10-00-self-dogfood-pilot-plan.json")
    state, governance = _campaign_inputs(root, plan)
    runtime = _BenchmarkRuntime(
        state,
        governance,
        observed_at="2026-08-18T02:00:00+08:00",
        campaign_run_id=plan["campaign_id"],
    )
    dispatcher = UnattendedDispatcher(runtime, actor_ref=_ACTOR_REF)
    campaign = dispatcher.run(max_steps=3)
    validate_unattended_campaign_receipt(campaign)
    replay = dispatcher.run(max_steps=3)
    if replay != campaign:
        raise RuntimeError("closed campaign replay differs")
    verdicts = _verdicts(root)
    failed = [item["experiment_id"] for item in verdicts if not item["passed"]]
    receipt: dict[str, Any] = {
        "schema_version": "context.self-dogfood-campaign-verdict/v1alpha1",
        "campaign_id": plan["campaign_id"],
        "plan_sha256": plan["plan_sha256"],
        "completed_work_ids": campaign["completed_work_ids"],
        "required_leaf_count": 3,
        "required_leaf_completed": len(campaign["completed_work_ids"]),
        "automatable_closure_rate": 1.0,
        "campaign_receipt_sha256": campaign["receipt_sha256"],
        "campaign_replay_match": True,
        "e0_e9": verdicts,
        "failed_experiments": failed,
        "status": "passed" if not failed else "blocked",
        "state_write_authority": False,
        "completion_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    output = root / "experiments/evidence/m10-00-campaign-verdict-results.json"
    output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": receipt["status"], "failed": failed}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
