"""M10-00 local fault-drill behavior tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.self_dogfood_pilot import (
    SelfDogfoodPilotError,
    run_self_dogfood_fault_drill,
    validate_self_dogfood_fault_drill,
    validate_self_dogfood_pilot_plan,
)
from tools import run_self_dogfood_fault_drill as fault_drill_runner


class M1000FaultDrillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def plan_and_matrix(self) -> tuple[dict, dict]:
        plan = json.loads(
            (
                self.root / "experiments/evidence/m10-00-self-dogfood-pilot-plan.json"
            ).read_text(encoding="utf-8")
        )
        matrix = json.loads(
            (
                self.root / "experiments/evidence/m10-00-evidence-matrix-results.json"
            ).read_text(encoding="utf-8")
        )
        validate_self_dogfood_pilot_plan(plan, root=self.root)
        return plan, matrix

    def test_fault_drill_covers_four_faults_and_two_workers(self) -> None:
        plan, matrix = self.plan_and_matrix()
        receipt = run_self_dogfood_fault_drill(
            plan,
            matrix,
            root=self.root,
            observed_at="2026-08-18T00:05:00+08:00",
        )

        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(
            {fault["fault_kind"] for fault in receipt["faults"]},
            {"compaction", "idea", "interrupt", "worker-loss"},
        )
        self.assertEqual(receipt["worker_count"], 2)
        self.assertEqual(receipt["verifier_ref"], "verifier-independent")
        self.assertEqual(receipt["coverage"]["status"], "pass")
        self.assertEqual(receipt["harness"]["worker_loss_authority_unchanged"], True)
        self.assertEqual(receipt["authority"]["external_effect_authority"], 0)
        validate_self_dogfood_fault_drill(
            receipt,
            plan=plan,
            matrix_receipt=matrix,
            root=self.root,
        )

    def test_fault_drill_rejects_non_admitted_matrix(self) -> None:
        plan, matrix = self.plan_and_matrix()
        matrix["completion_status"] = "passed"
        with self.assertRaises(SelfDogfoodPilotError):
            run_self_dogfood_fault_drill(
                plan,
                matrix,
                root=self.root,
                observed_at="2026-08-18T00:05:00+08:00",
            )

    def test_runner_writes_a_current_fault_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "faults.json"
            result = fault_drill_runner.main(
                [
                    "--root",
                    str(self.root),
                    "--plan",
                    str(
                        self.root
                        / "experiments/evidence/m10-00-self-dogfood-pilot-plan.json"
                    ),
                    "--matrix",
                    str(
                        self.root
                        / "experiments/evidence/m10-00-evidence-matrix-results.json"
                    ),
                    "--output",
                    str(output),
                    "--observed-at",
                    "2026-08-18T00:05:00+08:00",
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        plan, matrix = self.plan_and_matrix()
        validate_self_dogfood_fault_drill(
            receipt,
            plan=plan,
            matrix_receipt=matrix,
            root=self.root,
        )


if __name__ == "__main__":
    unittest.main()
