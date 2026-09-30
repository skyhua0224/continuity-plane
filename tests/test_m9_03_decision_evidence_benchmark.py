"""M9-03 Decision and Evidence measured acceptance tests."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.decision_evidence_benchmark import (
    DecisionEvidenceBenchmarkError,
    benchmark_decision_evidence_projection,
    validate_decision_evidence_benchmark,
)
from tools import run_decision_evidence_benchmark


def _resign(receipt: dict) -> None:
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class M903DecisionEvidenceBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_benchmark_proves_history_provenance_and_matrix_integrity(self) -> None:
        receipt = benchmark_decision_evidence_projection(
            root=self.root,
            iterations=3,
            generated_at="2026-08-17T22:00:00+08:00",
        )

        validate_decision_evidence_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["gate"]["status"], "passed")
        self.assertEqual(receipt["gate"]["failed_gates"], [])
        for field in (
            "same_revision_matches",
            "decision_timeline_complete_matches",
            "constraint_matrix_complete_matches",
            "evidence_matrix_complete_matches",
            "current_decision_matches",
            "supersedes_chain_matches",
            "metadata_only_honesty_matches",
            "validated_provenance_matches",
            "evidence_classification_matches",
            "tamper_rejections",
        ):
            self.assertEqual(receipt["results"][field], 3)
        self.assertEqual(receipt["parameters"]["long_chain_items"], 128)
        self.assertGreaterEqual(receipt["parameters"]["stress_matrix_cells"], 4096)
        self.assertEqual(receipt["parameters"]["max_evidence_references"], 50_000)
        self.assertFalse(receipt["parameters"]["latency_gate_evaluated"])
        self.assertTrue(receipt["results"]["object_capacity_rejected"])
        self.assertTrue(receipt["results"]["reference_capacity_rejected"])
        self.assertEqual(len(receipt["latency_samples_ms"]), 3)
        self.assertTrue(all(value > 0 for value in receipt["latency_samples_ms"]))
        self.assertEqual(
            receipt["attestation"],
            {
                "mode": "local-unattested",
                "measurement_authenticity": False,
                "zero_count_source": "fixture-instrumentation",
            },
        )
        self.assertEqual(receipt["results"]["false_supports"], 0)
        self.assertEqual(receipt["results"]["old_decision_resurrections"], 0)
        self.assertEqual(receipt["results"]["authority_violations"], 0)
        self.assertEqual(receipt["results"]["provider_invocations"], 0)
        self.assertEqual(receipt["results"]["external_services"], 0)

    def test_validator_rejects_forged_results_latency_and_provenance(self) -> None:
        receipt = benchmark_decision_evidence_projection(
            root=self.root,
            iterations=2,
            generated_at="2026-08-17T22:00:00+08:00",
        )
        mutations = [
            lambda item: item["results"].__setitem__("same_revision_matches", 1),
            lambda item: item["results"].__setitem__("false_supports", 1),
            lambda item: item["results"].__setitem__(
                "object_capacity_rejected", False
            ),
            lambda item: item["results"].__setitem__(
                "reference_capacity_rejected", False
            ),
            lambda item: item["latency_ms"].__setitem__("p95", -1.0),
            lambda item: item["latency_ms"].__setitem__("max", float("inf")),
            lambda item: item["thresholds"].__setitem__(
                "authority_violations_max", False
            ),
            lambda item: item.__setitem__(
                "generated_at", "2026-99-99T22:00:00+08:00"
            ),
            lambda item: item["provenance"].__setitem__(
                "implementation_sha256", "0" * 64
            ),
            lambda item: item["parameters"].__setitem__(
                "latency_gate_evaluated", True
            ),
        ]
        for mutate in mutations:
            forged = copy.deepcopy(receipt)
            mutate(forged)
            _resign(forged)
            with self.assertRaises(DecisionEvidenceBenchmarkError):
                validate_decision_evidence_benchmark(forged, root=self.root)

    def test_validator_rejects_a_consistently_derived_failed_gate(self) -> None:
        receipt = benchmark_decision_evidence_projection(
            root=self.root,
            iterations=2,
            generated_at="2026-08-17T22:00:00+08:00",
        )
        receipt["results"]["same_revision_matches"] = 1
        receipt["results"]["same_revision_rate"] = 0.5
        receipt["gate"] = {
            "status": "failed",
            "failed_gates": ["same-revision"],
        }
        _resign(receipt)

        with self.assertRaises(DecisionEvidenceBenchmarkError):
            validate_decision_evidence_benchmark(receipt, root=self.root)

    def test_validator_rejects_rederived_latency_without_measurement_samples(
        self,
    ) -> None:
        receipt = benchmark_decision_evidence_projection(
            root=self.root,
            iterations=2,
            generated_at="2026-08-17T22:00:00+08:00",
        )
        receipt["latency_ms"] = {"min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
        _resign(receipt)

        with self.assertRaises(DecisionEvidenceBenchmarkError):
            validate_decision_evidence_benchmark(receipt, root=self.root)

    def test_committed_receipt_is_source_bound_and_internally_consistent(self) -> None:
        path = (
            self.root
            / "experiments/evidence/m9-03-decision-evidence-results.json"
        )
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.skipTest("M9-03 measured receipt has not been generated")
        validate_decision_evidence_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["parameters"]["iterations"], 1000)
        self.assertEqual(receipt["gate"]["status"], "passed")

    def test_invalid_iteration_count_is_rejected(self) -> None:
        for value in (True, 0, 1001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                benchmark_decision_evidence_projection(
                    root=self.root,
                    iterations=value,
                    generated_at="2026-08-17T22:00:00+08:00",
                )

    def test_runner_writes_a_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = run_decision_evidence_benchmark.main(
                [
                    "--root",
                    str(self.root),
                    "--iterations",
                    "2",
                    "--generated-at",
                    "2026-08-17T22:00:00+08:00",
                    "--output",
                    str(output),
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        validate_decision_evidence_benchmark(receipt, root=self.root)


if __name__ == "__main__":
    unittest.main()
