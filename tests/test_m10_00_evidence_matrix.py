"""M10-00 evidence admission receipt tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.self_dogfood_pilot import (
    SelfDogfoodPilotError,
    build_evidence_matrix_receipt,
    validate_evidence_matrix_receipt,
)
from tests.test_m10_00_self_dogfood_pilot import M1000SelfDogfoodPilotTests
from tools import run_self_dogfood_evidence_matrix


class M1000EvidenceMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def plan(self) -> dict:
        fixture = M1000SelfDogfoodPilotTests(
            "test_plan_binds_three_leaves_workers_faults_and_e0_e9"
        )
        fixture.root = self.root
        return fixture.plan()

    def test_admission_receipt_preserves_candidate_limitations(self) -> None:
        receipt = build_evidence_matrix_receipt(
            self.plan(),
            root=self.root,
            observed_at="2026-08-18T00:00:00+08:00",
        )

        self.assertEqual(receipt["completion_status"], "ready-for-runtime")
        self.assertEqual(len(receipt["entries"]), 10)
        self.assertEqual(
            {entry["runtime_status"] for entry in receipt["entries"]},
            {"pending"},
        )
        self.assertEqual(receipt["authority"]["state_write_authority"], False)
        self.assertEqual(receipt["admitted_count"], 10)

    def test_admission_rejects_plan_digest_or_current_evidence_drift(self) -> None:
        plan = self.plan()
        plan["plan_sha256"] = "f" * 64
        with self.assertRaises(SelfDogfoodPilotError):
            build_evidence_matrix_receipt(
                plan,
                root=self.root,
                observed_at="2026-08-18T00:00:00+08:00",
            )

        stale = self.plan()
        stale["evidence_matrix"][0]["evidence_refs"][0]["content_sha256"] = "f" * 64
        with self.assertRaises(SelfDogfoodPilotError):
            build_evidence_matrix_receipt(
                stale,
                root=self.root,
                observed_at="2026-08-18T00:00:00+08:00",
            )

    def test_runner_writes_a_current_admission_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "matrix.json"
            result = run_self_dogfood_evidence_matrix.main(
                [
                    "--root",
                    str(self.root),
                    "--plan",
                    str(
                        self.root
                        / "experiments/evidence/m10-00-self-dogfood-pilot-plan.json"
                    ),
                    "--output",
                    str(output),
                    "--observed-at",
                    "2026-08-18T00:00:00+08:00",
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        plan = json.loads(
            (
                self.root / "experiments/evidence/m10-00-self-dogfood-pilot-plan.json"
            ).read_text(encoding="utf-8")
        )
        validate_evidence_matrix_receipt(receipt, plan=plan, root=self.root)


if __name__ == "__main__":
    unittest.main()
