"""M5-03 PostCompact canary benchmark acceptance."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.postcompact_canary_benchmark import (
    benchmark_postcompact_canary,
    validate_postcompact_canary_benchmark_receipt,
)


class M503PostCompactCanaryBenchmarkTests(unittest.TestCase):
    def test_benchmark_restores_all_critical_fields_and_rejects_all_faults(self):
        root = Path(__file__).parents[1]
        receipt = benchmark_postcompact_canary(root=root, samples=100)
        validate_postcompact_canary_benchmark_receipt(receipt, root=root)
        self.assertEqual(receipt["successful_samples"], 100)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["critical_field_mismatch"], 0)
        self.assertEqual(receipt["authority_violations"], 0)
        self.assertEqual(receipt["fault_rejections"], receipt["fault_samples"])
        self.assertEqual(set(receipt["faults"]), {
            "active_work", "constraint", "decision", "deepseek_checkpoint",
            "delta_binding", "effect_watermark", "pi_cut_point", "pi_split_turn",
        })
        self.assertTrue(all(value == 0 for value in receipt["faults"].values()))
        self.assertEqual(receipt["decision_recovery_millionths"], 1_000_000)
        self.assertEqual(receipt["constraint_recovery_millionths"], 1_000_000)
        self.assertEqual(receipt["work_recovery_millionths"], 1_000_000)
        self.assertEqual(receipt["external_services"], 0)
        self.assertLess(receipt["p95_ms"], 2000)

    def test_committed_receipt_and_schemas_are_current(self):
        root = Path(__file__).parents[1]
        receipt = json.loads(
            (root / "experiments/routing/m5-03-postcompact-canary-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_postcompact_canary_benchmark_receipt(receipt, root=root)
        for relative in (
            "schemas/m5-03/postcompact-canary.schema.json",
            "schemas/m5-03/postcompact-canary-benchmark.schema.json",
        ):
            Draft202012Validator.check_schema(
                json.loads((root / relative).read_text(encoding="utf-8"))
            )

    def test_validator_rejects_false_veto_and_stale_provenance(self):
        root = Path(__file__).parents[1]
        receipt = benchmark_postcompact_canary(root=root, samples=16)
        for field, value in (
            ("critical_field_mismatch", 1),
            ("authority_violations", 1),
            ("fault_rejections", receipt["fault_rejections"] - 1),
        ):
            candidate = copy.deepcopy(receipt)
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_postcompact_canary_benchmark_receipt(candidate, root=root)
        stale = copy.deepcopy(receipt)
        stale["provenance"]["implementation_sha256"] = "a" * 64
        with self.assertRaises(ValueError):
            validate_postcompact_canary_benchmark_receipt(stale, root=root)


if __name__ == "__main__":
    unittest.main()
