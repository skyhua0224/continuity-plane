import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.skill_resolver_benchmark import (
    benchmark_replay_fixture,
    benchmark_skill_resolver,
    validate_skill_resolution_benchmark_receipt,
)


class M409SkillResolverBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.schema_path = (
            cls.root / "schemas/m4-09/skill-resolution-benchmark.schema.json"
        )
        cls.receipt_path = (
            cls.root / "experiments/routing/m4-09-skill-resolution-results.json"
        )
        cls.fixture_path = (
            cls.root
            / "experiments/skills/m4-09-skill-resolution-replay-v1alpha1.json"
        )

    def test_benchmark_quantifies_replay_isolation_quarantine_and_reduction(self):
        receipt = benchmark_skill_resolver(
            samples=1000,
            negative_samples=125,
            generated_at="2026-08-14T18:00:00Z",
        )

        self.assertEqual(receipt["successful_samples"], 1000)
        self.assertEqual(receipt["negative_quarantine"], 125)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["role_leakage"], 0)
        self.assertEqual(receipt["provider_leakage"], 0)
        self.assertEqual(receipt["state_write_authority_true"], 0)
        self.assertGreaterEqual(receipt["selection_reduction_percent"], 30)
        self.assertLess(receipt["p95_ms"], 100)
        self.assertEqual(receipt["external_services"], 0)

    def test_strict_schema_registry_and_committed_receipt_are_current(self):
        schema = json.loads(self.schema_path.read_text(encoding="utf-8"))
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(receipt)), [])
        validate_skill_resolution_benchmark_receipt(receipt)
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.skill-resolution-benchmark"
        )
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(self.schema_path.read_bytes()).hexdigest(),
        )
        self.assertEqual(
            receipt["implementation_sha256"],
            hashlib.sha256(
                (self.root / "context_control_plane/skill_resolver.py").read_bytes()
            ).hexdigest(),
        )

    def test_committed_fixture_replays_to_the_same_decision_and_packet(self):
        fixture = json.loads(self.fixture_path.read_text(encoding="utf-8"))
        expected = benchmark_replay_fixture()
        self.assertEqual(fixture, expected)
        canonical_fixture = json.dumps(
            fixture,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(
            receipt["fixture_sha256"],
            hashlib.sha256(canonical_fixture).hexdigest(),
        )

    def test_receipt_validator_rejects_correctness_and_capacity_regressions(self):
        receipt = benchmark_skill_resolver(
            samples=1000,
            negative_samples=125,
            generated_at="2026-08-14T18:00:00Z",
        )
        for field, value in (
            ("replay_mismatch", 1),
            ("role_leakage", 1),
            ("provider_leakage", 1),
            ("state_write_authority_true", 1),
            ("negative_quarantine", 124),
            ("selection_reduction_percent", 29.9999),
            ("p95_ms", 100),
            ("external_services", 1),
        ):
            with self.subTest(field=field):
                changed = dict(receipt)
                changed[field] = value
                with self.assertRaises(ValueError):
                    validate_skill_resolution_benchmark_receipt(changed)

    def test_benchmark_rejects_insufficient_samples(self):
        for samples, negatives in ((999, 125), (1000, 124)):
            with self.subTest(samples=samples, negatives=negatives), self.assertRaisesRegex(
                ValueError, "1000/125"
            ):
                benchmark_skill_resolver(
                    samples=samples,
                    negative_samples=negatives,
                )


if __name__ == "__main__":
    unittest.main()
