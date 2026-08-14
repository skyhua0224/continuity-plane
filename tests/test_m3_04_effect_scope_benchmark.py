import json
import tempfile
import unittest
from pathlib import Path

import yaml

from context_control_plane.effect_scope_benchmark import (
    benchmark_effect_scope_gate,
    validate_effect_scope_benchmark_receipt,
)


class M304EffectScopeBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixtures = yaml.safe_load(
            (cls.root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.snapshot = next(
            case["document"]
            for case in fixtures["cases"]
            if case["case_id"] == "solo-active-work"
        )

    def test_benchmark_measures_allow_deny_and_zero_mutation(self):
        receipt = benchmark_effect_scope_gate(
            self.snapshot,
            samples=200,
            pending_effect_counts=(1, 100),
            conflict_samples=20,
            observed_at="2026-08-13T08:00:00+08:00",
            root=self.root,
        )
        measurement = receipt["measurement"]["baseline"]
        self.assertEqual(receipt["schema_version"], "context.effect-scope-benchmark/v1alpha1")
        self.assertEqual(measurement["samples"], 200)
        self.assertEqual(measurement["allowed"], 100)
        self.assertEqual(measurement["read_only_denied"], 100)
        self.assertEqual(measurement["state_mutations"], 0)
        self.assertLess(measurement["p95_gate_latency_ms"], 1.0)
        self.assertEqual(
            [item["pending_effects"] for item in receipt["measurement"]["conflict_loads"]],
            [1, 100],
        )
        self.assertTrue(
            all(
                item["read_only_denied"] == 20
                and item["allowed"] == 0
                and item["reason"] == "effect_scope_conflict"
                for item in receipt["measurement"]["conflict_loads"]
            )
        )
        validate_effect_scope_benchmark_receipt(receipt, root=self.root)

    def test_committed_receipt_is_current_and_validated(self):
        receipt = json.loads(
            (
                self.root
                / "experiments/routing/m3-04-effect-scope-verification.json"
            ).read_text(encoding="utf-8")
        )
        validate_effect_scope_benchmark_receipt(receipt, root=self.root)
        self.assertEqual(receipt["measurement"]["baseline"]["samples"], 10_000)
        self.assertEqual(
            [item["pending_effects"] for item in receipt["measurement"]["conflict_loads"]],
            [1, 100, 1_000],
        )

    def test_invalid_sample_count_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "samples"):
            benchmark_effect_scope_gate(
                self.snapshot,
                samples=0,
                observed_at="2026-08-13T08:00:00+08:00",
                root=self.root,
            )


if __name__ == "__main__":
    unittest.main()
