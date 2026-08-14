"""M3-08 local-embedded continuation benchmark acceptance."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.input_progression_benchmark import (
    run_input_progression_benchmark,
    validate_input_progression_benchmark_receipt,
)


class M308InputProgressionBenchmarkTests(unittest.TestCase):
    def test_benchmark_is_repeatable_strict_and_current(self):
        root = Path(__file__).parents[1]
        generated = run_input_progression_benchmark(
            root=root,
            samples=16,
            observed_at="2026-08-14T16:00:00+08:00",
        )
        self.assertEqual(generated["measurement"]["successful_samples"], 16)
        self.assertEqual(generated["measurement"]["ready_required_missed_count"], 0)
        self.assertEqual(generated["measurement"]["premature_stop_count"], 0)
        self.assertEqual(generated["measurement"]["untyped_ask_count"], 0)
        self.assertEqual(generated["measurement"]["incomplete_escalation_count"], 0)
        validate_input_progression_benchmark_receipt(generated, root=root)

        receipt = json.loads(
            (
                root
                / "experiments/routing/m3-08-input-progression-results.json"
            ).read_text(encoding="utf-8")
        )
        validate_input_progression_benchmark_receipt(receipt, root=root)
        self.assertEqual(receipt["measurement"]["samples"], 1000)

        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.continuation-dispatch-benchmark"
        )
        path = root / entry["artifact_path"]
        self.assertEqual(
            entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(receipt)

    def test_validator_rejects_false_veto_claims(self):
        root = Path(__file__).parents[1]
        receipt = run_input_progression_benchmark(
            root=root,
            samples=8,
            observed_at="2026-08-14T16:00:00+08:00",
        )
        receipt["measurement"]["premature_stop_count"] = 1
        with self.assertRaisesRegex(ValueError, "acceptance"):
            validate_input_progression_benchmark_receipt(receipt, root=root)

    def test_validator_rejects_stale_provenance(self):
        root = Path(__file__).parents[1]
        receipt = run_input_progression_benchmark(
            root=root,
            samples=8,
            observed_at="2026-08-14T16:00:00+08:00",
        )
        receipt["provenance"]["implementation_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "provenance"):
            validate_input_progression_benchmark_receipt(receipt, root=root)


if __name__ == "__main__":
    unittest.main()
