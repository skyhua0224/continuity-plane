"""M9-07 measured Relationship and Impact projection acceptance tests."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.relationship_impact_benchmark import (
    benchmark_relationship_impact_projection,
    validate_relationship_impact_benchmark,
)
from tools import run_relationship_impact_benchmark


class M907RelationshipImpactBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_benchmark_proves_projection_focus_and_fail_closed_gates(self) -> None:
        receipt = benchmark_relationship_impact_projection(
            root=self.root,
            iterations=3,
            scale_iterations=2,
            generated_at="2026-08-17T23:30:00+08:00",
        )

        validate_relationship_impact_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["results"]["same_revision_matches"], 3)
        self.assertEqual(receipt["results"]["complete_projection_matches"], 3)
        self.assertEqual(receipt["results"]["focus_direction_matches"], 3)
        self.assertEqual(receipt["results"]["code_clock_separation_matches"], 3)
        self.assertEqual(receipt["results"]["tamper_rejections"], 3)
        self.assertEqual(receipt["results"]["filter_rejections"], 3)
        self.assertEqual(receipt["results"]["scale_complete_matches"], 2)
        self.assertEqual(receipt["results"]["authority_violations"], 0)
        self.assertEqual(receipt["results"]["provider_invocations"], 0)
        self.assertEqual(receipt["results"]["external_services"], 0)
        self.assertEqual(receipt["verdict"], "passed")

    def test_validator_rejects_forged_results_latency_and_provenance(self) -> None:
        receipt = benchmark_relationship_impact_projection(
            root=self.root,
            iterations=2,
            scale_iterations=1,
            generated_at="2026-08-17T23:30:00+08:00",
        )
        mutations = []
        forged_result = copy.deepcopy(receipt)
        forged_result["results"]["tamper_rejections"] = 1
        mutations.append(forged_result)
        forged_latency = copy.deepcopy(receipt)
        forged_latency["latency_ms"]["p95"] = 0.0
        mutations.append(forged_latency)
        forged_provenance = copy.deepcopy(receipt)
        forged_provenance["provenance"]["implementation_sha256"] = "f" * 64
        mutations.append(forged_provenance)
        for forged in mutations:
            forged["receipt_sha256"] = "0" * 64
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                validate_relationship_impact_benchmark(forged, root=self.root)

    def test_invalid_iteration_counts_are_rejected(self) -> None:
        for iterations, scale_iterations in ((0, 1), (1, 0), (10001, 1), (1, 101)):
            with (
                self.subTest(
                    iterations=iterations,
                    scale_iterations=scale_iterations,
                ),
                self.assertRaises(ValueError),
            ):
                benchmark_relationship_impact_projection(
                    root=self.root,
                    iterations=iterations,
                    scale_iterations=scale_iterations,
                    generated_at="2026-08-17T23:30:00+08:00",
                )

    def test_runner_writes_a_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = run_relationship_impact_benchmark.main(
                [
                    "--root",
                    str(self.root),
                    "--iterations",
                    "2",
                    "--scale-iterations",
                    "1",
                    "--output",
                    str(output),
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        validate_relationship_impact_benchmark(receipt, root=self.root)

    def test_committed_receipt_is_independently_valid(self) -> None:
        receipt = json.loads(
            (
                self.root
                / "experiments/evidence/m9-07-relationship-impact-results.json"
            ).read_text(encoding="utf-8")
        )
        validate_relationship_impact_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["parameters"]["iterations"], 1000)


if __name__ == "__main__":
    unittest.main()
