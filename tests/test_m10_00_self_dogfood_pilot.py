"""M10-00 self-dogfood pilot plan and evidence behavior tests."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from context_control_plane.self_dogfood_pilot import (
    SELF_DOGFOOD_PILOT_PLAN_SCHEMA_VERSION,
    SelfDogfoodPilotError,
    build_self_dogfood_pilot_plan,
    validate_self_dogfood_pilot_plan,
)
from tools import run_self_dogfood_pilot_plan


class M1000SelfDogfoodPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def plan(self) -> dict:
        evidence_paths = {
            "E0": ["docs/migrations/m5-05-context-accounting-acceptance-2026-08-15.md"],
            "E1": ["docs/migrations/m5-03-postcompact-canary-acceptance-2026-08-15.md"],
            "E2": [
                "docs/migrations/m3-08-continuation-dispatch-acceptance-2026-08-14.md"
            ],
            "E3": [
                "docs/migrations/m4-04-layered-skill-loading-acceptance-2026-08-11.md"
            ],
            "E4": [
                "docs/migrations/m4-03-skill-drift-quarantine-acceptance-2026-08-11.md"
            ],
            "E5": ["docs/migrations/m6-retrieval-recall-acceptance-2026-08-15.md"],
            "E6": [
                "docs/migrations/m7-02-claim-evidence-gate-acceptance-2026-08-16.md"
            ],
            "E7": [
                "docs/migrations/m7-05-affected-test-selection-acceptance-2026-08-16.md"
            ],
            "E8": ["docs/migrations/m8-02-shared-work-acceptance-2026-08-16.md"],
            "E9": ["docs/migrations/m8-01-durable-operation-acceptance-2026-08-16.md"],
        }
        return build_self_dogfood_pilot_plan(
            root=self.root,
            plan_id="pilot-plan-m10-00",
            campaign_id="campaign-m10-00",
            project_id="context-control-plane",
            state_revision=74,
            created_at="2026-08-17T23:59:00+08:00",
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

    def test_plan_binds_three_leaves_workers_faults_and_e0_e9(self) -> None:
        plan = self.plan()

        self.assertEqual(plan["schema_version"], SELF_DOGFOOD_PILOT_PLAN_SCHEMA_VERSION)
        self.assertEqual(len(plan["required_leaves"]), 3)
        self.assertEqual(len(plan["workers"]), 2)
        self.assertEqual(
            plan["repository_baseline"]["base_commit"],
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        )
        self.assertEqual(
            plan["repository_baseline"]["schema_registry_sha256"],
            hashlib.sha256(
                (self.root / "schemas/registry.yaml").read_bytes()
            ).hexdigest(),
        )
        self.assertTrue(plan["repository_baseline"]["dirty"])
        self.assertEqual(
            {item["fault_kind"] for item in plan["fault_injections"]},
            {"compaction", "idea", "interrupt", "worker-loss"},
        )
        self.assertEqual(
            [item["experiment_id"] for item in plan["evidence_matrix"]],
            [f"E{index}" for index in range(10)],
        )
        self.assertEqual(plan["authority"]["state_write_authority"], False)
        self.assertEqual(plan["authority"]["completion_authority"], False)
        validate_self_dogfood_pilot_plan(plan, root=self.root)

    def test_plan_is_deterministic_for_unordered_sets(self) -> None:
        first = self.plan()
        second = self.plan()
        self.assertEqual(first, second)

    def test_plan_rejects_missing_veto_fault_or_independent_verifier(self) -> None:
        plan = self.plan()
        mutations = []
        missing_experiment = copy.deepcopy(plan)
        missing_experiment["evidence_matrix"].pop()
        mutations.append(missing_experiment)
        missing_fault = copy.deepcopy(plan)
        missing_fault["fault_injections"].pop()
        mutations.append(missing_fault)
        shared_verifier = copy.deepcopy(plan)
        shared_verifier["verifier"]["verifier_ref"] = "worker-analysis"
        mutations.append(shared_verifier)
        stale_evidence = copy.deepcopy(plan)
        stale_evidence["evidence_matrix"][0]["evidence_refs"][0]["content_sha256"] = (
            "f" * 64
        )
        mutations.append(stale_evidence)
        authority = copy.deepcopy(plan)
        authority["authority"]["completion_authority"] = True
        mutations.append(authority)
        repository = copy.deepcopy(plan)
        repository["repository_baseline"]["base_commit"] = "f" * 40
        mutations.append(repository)
        for mutation in mutations:
            with (
                self.subTest(mutation=mutation),
                self.assertRaises(SelfDogfoodPilotError),
            ):
                validate_self_dogfood_pilot_plan(mutation, root=self.root)

    def test_runner_writes_a_current_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "plan.json"
            result = run_self_dogfood_pilot_plan.main(
                [
                    "--root",
                    str(self.root),
                    "--output",
                    str(output),
                    "--created-at",
                    "2026-08-17T23:59:00+08:00",
                ]
            )
            plan = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        validate_self_dogfood_pilot_plan(plan, root=self.root)


if __name__ == "__main__":
    unittest.main()
