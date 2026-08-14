"""M5-04 bounded artifact expansion benchmark acceptance."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from context_control_plane.bounded_artifact_expansion_benchmark import (
    benchmark_bounded_artifact_expansion,
    validate_bounded_artifact_expansion_benchmark,
)


class M504BoundedArtifactExpansionBenchmarkTests(unittest.TestCase):
    def test_benchmark_keeps_prompt_return_bounded_and_rejects_faults(self):
        receipt = benchmark_bounded_artifact_expansion(samples=100)
        validate_bounded_artifact_expansion_benchmark(receipt)
        self.assertEqual(receipt["successful_samples"], 100)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["budget_rejections"], receipt["budget_fault_samples"])
        self.assertEqual(receipt["digest_rejections"], receipt["digest_fault_samples"])
        self.assertLessEqual(receipt["returned_bytes_max"], receipt["returned_byte_budget"])
        self.assertGreater(receipt["scanned_bytes_max"], receipt["returned_bytes_max"])
        self.assertGreater(receipt["prompt_reduction_millionths"], 0)
        self.assertEqual(receipt["external_services"], 0)

    def test_committed_receipt_is_current(self):
        root = Path(__file__).parents[1]
        import json

        receipt = json.loads(
            (root / "experiments/routing/m5-04-bounded-expansion-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_bounded_artifact_expansion_benchmark(receipt, root=root)

    def test_validator_rejects_false_veto(self):
        receipt = benchmark_bounded_artifact_expansion(samples=8)
        changed = copy.deepcopy(receipt)
        changed["replay_mismatch"] = 1
        with self.assertRaises(ValueError):
            validate_bounded_artifact_expansion_benchmark(changed)


if __name__ == "__main__":
    unittest.main()
