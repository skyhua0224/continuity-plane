"""M10-00 repository verification receipt tests."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.self_dogfood_pilot import (
    SelfDogfoodPilotError,
    build_repository_verification_receipt,
    validate_repository_verification_receipt,
)
from tests.test_m10_00_fault_drill import M1000FaultDrillTests
from tools import run_self_dogfood_release_verification


class M1000ReleaseVerificationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def inputs(self) -> tuple[dict, dict, str]:
        fault_test = M1000FaultDrillTests(
            "test_fault_drill_covers_four_faults_and_two_workers"
        )
        fault_test.root = self.root
        plan, matrix = fault_test.plan_and_matrix()
        fault = json.loads(
            (
                self.root / "experiments/evidence/m10-00-fault-drill-results.json"
            ).read_text(encoding="utf-8")
        )
        validate_self_dogfood_fault_drill = __import__(
            "context_control_plane.self_dogfood_pilot",
            fromlist=["validate_self_dogfood_fault_drill"],
        ).validate_self_dogfood_fault_drill
        validate_self_dogfood_fault_drill(
            fault,
            plan=plan,
            matrix_receipt=matrix,
            root=self.root,
        )
        return plan, fault, "unittest full suite output\nOK\n"

    def test_receipt_binds_full_suite_and_end_repository(self) -> None:
        plan, fault, output = self.inputs()
        receipt = build_repository_verification_receipt(
            plan,
            fault,
            root=self.root,
            command=["python", "-m", "unittest", "discover"],
            passed=1778,
            skipped=31,
            wall_time_seconds=214.140,
            output=output,
            observed_at="2026-08-18T00:10:00+08:00",
        )

        validate_repository_verification_receipt(
            receipt,
            plan=plan,
            fault_receipt=fault,
            root=self.root,
        )
        self.assertEqual(receipt["full_suite"]["passed"], 1778)
        self.assertEqual(receipt["full_suite"]["skipped"], 31)
        self.assertEqual(
            receipt["full_suite"]["output_sha256"],
            hashlib.sha256(output.encode()).hexdigest(),
        )

    def test_receipt_rejects_failed_or_forged_suite(self) -> None:
        plan, fault, output = self.inputs()
        kwargs = {
            "root": self.root,
            "command": ["python", "-m", "unittest", "discover"],
            "passed": 1778,
            "skipped": 31,
            "wall_time_seconds": 214.140,
            "output": output,
            "observed_at": "2026-08-18T00:10:00+08:00",
        }
        receipt = build_repository_verification_receipt(plan, fault, **kwargs)
        receipt["full_suite"]["passed"] = 1777
        with self.assertRaises(SelfDogfoodPilotError):
            validate_repository_verification_receipt(
                receipt, plan=plan, fault_receipt=fault, root=self.root
            )

    def test_runner_executes_and_records_a_real_unittest_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "release.json"
            result = run_self_dogfood_release_verification.main(
                [
                    "--root",
                    str(self.root),
                    "--output",
                    str(output),
                    "--suite-command-json",
                    json.dumps(
                        [
                            str(self.root / ".venv/bin/python"),
                            "-m",
                            "unittest",
                            "tests.test_m10_00_fault_drill",
                            "-q",
                        ]
                    ),
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertGreaterEqual(receipt["full_suite"]["tests_run"], 3)


if __name__ == "__main__":
    unittest.main()
