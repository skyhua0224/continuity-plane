"""M7-06 deterministic offline ReferenceWatcher benchmark."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.reference_watcher_benchmark import (
    benchmark_reference_watcher,
    validate_reference_watcher_benchmark,
)


class M706ReferenceWatcherBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema = json.loads(
            (
                cls.root / "schemas/m7-06/reference-watcher-benchmark.schema.json"
            ).read_text(encoding="utf-8")
        )

    def test_benchmark_detects_every_change_and_blocks_unreviewed_assertions(
        self,
    ) -> None:
        receipt = benchmark_reference_watcher(samples=1000, root=self.root)

        self.assertEqual(receipt["successful_samples"], 1000)
        self.assertEqual(receipt["changed_fixture_count"], 400)
        self.assertEqual(receipt["changed_fixture_stale_or_quarantined_count"], 400)
        self.assertEqual(receipt["unsafe_fixture_count"], 800)
        self.assertEqual(receipt["unsafe_fixture_stale_or_quarantined_count"], 800)
        self.assertEqual(receipt["unreviewed_completion_attempts"], 800)
        self.assertEqual(receipt["unreviewed_completion_allows"], 0)
        self.assertEqual(receipt["reverified_release_attempts"], 400)
        self.assertEqual(receipt["reverified_release_allows"], 400)
        self.assertEqual(receipt["replay_mismatch_count"], 0)
        self.assertEqual(receipt["authority_violation_count"], 0)
        self.assertEqual(receipt["external_service_calls"], 0)
        validate_reference_watcher_benchmark(receipt, root=self.root)
        Draft202012Validator(self.schema).validate(receipt)

    def test_tracked_receipt_replays_exactly(self) -> None:
        tracked = json.loads(
            (
                self.root / "experiments/evidence/m7-06-reference-watcher-results.json"
            ).read_text(encoding="utf-8")
        )
        replay = benchmark_reference_watcher(samples=tracked["samples"], root=self.root)

        self.assertEqual(replay, tracked)
        validate_reference_watcher_benchmark(tracked, root=self.root)

    def test_receipt_rejects_false_allow_or_incomplete_change_coverage(self) -> None:
        receipt = benchmark_reference_watcher(samples=1000, root=self.root)
        mutations = (
            ("unreviewed_completion_allows", 1),
            ("changed_fixture_stale_or_quarantined_count", 399),
            ("replay_mismatch_count", 1),
            ("authority_violation_count", 1),
            ("external_service_calls", 1),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                invalid = copy.deepcopy(receipt)
                invalid[field] = value
                with self.assertRaises(ValueError):
                    validate_reference_watcher_benchmark(invalid, root=self.root)

    def test_benchmark_rejects_every_adversarial_binding_attack(self) -> None:
        receipt = benchmark_reference_watcher(samples=1000, root=self.root)

        self.assertEqual(
            receipt.get("adversarial_case_counts"),
            {
                "detached-decision": 400,
                "detached-observation": 400,
                "forged-state-matrix": 400,
                "unresolved-replacement": 400,
                "unverified-trusted-time": 1000,
            },
        )
        self.assertEqual(
            receipt.get("adversarial_allow_counts"),
            {
                "detached-decision": 0,
                "detached-observation": 0,
                "forged-state-matrix": 0,
                "unresolved-replacement": 0,
                "unverified-trusted-time": 0,
            },
        )


if __name__ == "__main__":
    unittest.main()
