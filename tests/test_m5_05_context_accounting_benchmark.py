"""M5-05 offline context accounting replay benchmark."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.context_accounting_benchmark import (
    benchmark_context_accounting,
    benchmark_context_accounting_fixture,
    validate_context_accounting_benchmark,
)


class M505ContextAccountingBenchmarkTests(unittest.TestCase):
    def test_committed_receipt_and_schemas_are_current(self):
        root = Path(__file__).parents[1]
        receipt = json.loads(
            (root / "experiments/routing/m5-05-context-accounting-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_context_accounting_benchmark(receipt)
        for relative in (
            "schemas/m5-05/context-accounting.schema.json",
            "schemas/m5-05/context-accounting-benchmark.schema.json",
        ):
            Draft202012Validator.check_schema(
                json.loads((root / relative).read_text(encoding="utf-8"))
            )

    def test_same_corpus_budget_routes_are_replayable_without_provider_claims(self):
        receipt = benchmark_context_accounting(samples=32, generated_at="2026-08-15T02:30:00Z")
        validate_context_accounting_benchmark(receipt)
        self.assertEqual(receipt["successful_samples"], 32)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["same_corpus_budget_failures"], 0)
        self.assertEqual(receipt["route_configuration_failures"], 0)
        self.assertEqual(receipt["accounting_failures"], 0)
        self.assertEqual(receipt["provider_measured_routes"], 0)
        self.assertEqual(receipt["provider_unavailable_routes"], 2)
        self.assertEqual(receipt["false_provider_improvement_claims"], 0)
        self.assertEqual(receipt["locally_measured_metrics_per_route"], 5)
        self.assertEqual(receipt["external_services"], 0)

    def test_fixture_records_pi_and_deepseek_threshold_pruning_and_all_metric_families(self):
        fixture = benchmark_context_accounting_fixture()
        accounting = fixture["accounting"]
        self.assertEqual(accounting["budget_tokens"], 4096)
        self.assertEqual(accounting["corpus_sha256"], fixture["corpus_sha256"])
        routes = {route["provider_id"]: route for route in accounting["routes"]}
        self.assertEqual(set(routes), {"pi", "deepseek"})
        self.assertEqual(routes["pi"]["route_threshold_tokens"], 3072)
        self.assertTrue(routes["pi"]["model_free_pruning"])
        self.assertEqual(routes["deepseek"]["route_threshold_tokens"], 3584)
        self.assertFalse(routes["deepseek"]["model_free_pruning"])
        self.assertEqual(
            set(routes["pi"]["metrics"]),
            {
                "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                "billing_usd_micros", "retrieval_queries", "retrieval_read_bytes",
                "retrieval_output_bytes", "retrieval_latency_ms", "compaction_latency_ms",
                "cut_point_tokens", "cache_invalidated",
            },
        )

    def test_benchmark_validator_rejects_false_success_or_provider_claims(self):
        receipt = benchmark_context_accounting(samples=4)
        for field, value in (
            ("replay_mismatch", 1),
            ("same_corpus_budget_failures", 1),
            ("route_configuration_failures", 1),
            ("accounting_failures", 1),
            ("false_provider_improvement_claims", 1),
            ("external_services", 1),
        ):
            changed = copy.deepcopy(receipt)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_context_accounting_benchmark(changed)


if __name__ == "__main__":
    unittest.main()
