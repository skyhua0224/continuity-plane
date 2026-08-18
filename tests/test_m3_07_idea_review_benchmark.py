"""M3-07 local-embedded Idea review benchmark acceptance."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.idea_review_benchmark import (
    run_idea_review_benchmark,
    validate_idea_review_benchmark_receipt,
)


class M307IdeaReviewBenchmarkTests(unittest.TestCase):
    def test_benchmark_is_repeatable_strict_and_current(self):
        root = Path(__file__).parents[1]
        generated = run_idea_review_benchmark(
            root=root,
            samples=2,
            observed_at="2026-08-14T10:00:00+08:00",
        )
        self.assertEqual(generated["measurement"]["successful_runs"], 2)
        self.assertEqual(generated["measurement"]["dedupe_convergence_rate_millionths"], 1_000_000)
        self.assertEqual(generated["measurement"]["unauthorized_write_count"], 0)
        validate_idea_review_benchmark_receipt(generated, root=root)

        receipt = json.loads(
            (root / "experiments/routing/m3-07-idea-review-results.json").read_text()
        )
        validate_idea_review_benchmark_receipt(receipt, root=root)
        self.assertEqual(receipt["measurement"]["samples"], 1000)

        registry = yaml.safe_load((root / "schemas/registry.yaml").read_text())
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.idea-review-benchmark"
        )
        path = root / entry["artifact_path"]
        self.assertEqual(
            entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        schema = json.loads(path.read_text())
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(receipt)

    def test_validator_rejects_a_false_acceptance_claim(self):
        root = Path(__file__).parents[1]
        receipt = run_idea_review_benchmark(
            root=root,
            samples=1,
            observed_at="2026-08-14T10:00:00+08:00",
        )
        receipt["measurement"]["unauthorized_write_count"] = 1
        with self.assertRaisesRegex(ValueError, "acceptance"):
            validate_idea_review_benchmark_receipt(receipt, root=root)


if __name__ == "__main__":
    unittest.main()
