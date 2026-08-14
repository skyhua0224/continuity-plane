"""M5-07 dogfood emitter benchmark acceptance."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.dogfood_emitter_benchmark import (
    benchmark_dogfood_emitter,
    validate_dogfood_emitter_benchmark,
)


class M507DogfoodEmitterBenchmarkTests(unittest.TestCase):
    def test_benchmark_has_full_event_coverage_and_detects_vetoes(self):
        receipt = benchmark_dogfood_emitter(samples=32)
        validate_dogfood_emitter_benchmark(receipt)
        self.assertEqual(receipt["successful_samples"], 32)
        self.assertEqual(receipt["emitted_events"], 32 * 8)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["overall_coverage_millionths"], 1_000_000)
        self.assertTrue(
            all(value == 1_000_000 for value in receipt["coverage_millionths"].values())
        )
        self.assertEqual(receipt["veto_detection_samples"], 32 * 4)
        self.assertEqual(receipt["veto_detection_samples"], receipt["veto_detections"])
        self.assertEqual(
            receipt["veto_detections_by_kind"],
            {
                "acknowledged-input-replay": 32,
                "first-action-mismatch": 32,
                "late-canary": 32,
                "missing-handoff": 32,
            },
        )
        self.assertEqual(receipt["authority_violations"], 0)
        self.assertEqual(receipt["external_services"], 0)

    def test_committed_receipt_is_current(self):
        root = Path(__file__).parents[1]
        receipt = json.loads(
            (root / "experiments/dogfood/m5-07-emitter-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_dogfood_emitter_benchmark(receipt, root=root)
        for relative in (
            "schemas/m5-07/dogfood-event.schema.json",
            "schemas/m5-07/dogfood-coverage.schema.json",
            "schemas/m5-07/dogfood-emitter-benchmark.schema.json",
        ):
            Draft202012Validator.check_schema(
                json.loads((root / relative).read_text(encoding="utf-8"))
            )

    def test_validator_rejects_false_success(self):
        receipt = benchmark_dogfood_emitter(samples=4)
        changed = copy.deepcopy(receipt)
        changed["overall_coverage_millionths"] = 999_999
        with self.assertRaises(ValueError):
            validate_dogfood_emitter_benchmark(changed)

    def test_runner_publishes_a_valid_receipt(self):
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "tools/run_dogfood_emitter_benchmark.py"),
                    "--samples",
                    "4",
                    "--output",
                    str(output),
                ],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            validate_dogfood_emitter_benchmark(receipt, root=root)


if __name__ == "__main__":
    unittest.main()
