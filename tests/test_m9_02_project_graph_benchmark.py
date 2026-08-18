"""M9-02 Project Graph projection measured acceptance tests."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.project_graph_projection_benchmark import (
    ProjectGraphProjectionBenchmarkError,
    benchmark_project_graph_projection,
    validate_project_graph_projection_benchmark,
)
from tools import run_project_graph_projection_benchmark


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


class M902ProjectGraphProjectionBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_benchmark_proves_projection_completeness_and_health_visibility(self) -> None:
        receipt = benchmark_project_graph_projection(
            root=self.root,
            iterations=20,
            generated_at="2026-08-17T20:00:00+08:00",
        )

        validate_project_graph_projection_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["gate"]["status"], "passed")
        self.assertEqual(receipt["gate"]["failed_gates"], [])
        for field in (
            "same_revision_matches",
            "active_work_complete_matches",
            "cycle_visibility_matches",
            "orphan_visibility_matches",
            "expired_branch_visibility_matches",
            "lease_expiry_visibility_matches",
            "work_scope_overlap_visibility_matches",
            "bounded_overlap_complete_matches",
            "large_graph_complete_matches",
            "tamper_rejections",
        ):
            self.assertEqual(receipt["results"][field], 20)
        self.assertEqual(receipt["parameters"]["large_graph_work_count"], 128)
        self.assertEqual(receipt["parameters"]["max_nested_items"], 50_000)
        self.assertEqual(receipt["parameters"]["max_scope_comparisons"], 50_000)
        self.assertEqual(receipt["parameters"]["stress_scope_count"], 223)
        self.assertTrue(receipt["results"]["claim_conflict_rejected"])
        self.assertEqual(receipt["results"]["authority_violations"], 0)
        self.assertEqual(receipt["results"]["provider_invocations"], 0)
        self.assertEqual(receipt["results"]["external_services"], 0)

    def test_validator_rejects_forged_results_latency_and_provenance(self) -> None:
        receipt = benchmark_project_graph_projection(
            root=self.root,
            iterations=5,
            generated_at="2026-08-17T20:00:00+08:00",
        )
        mutations = [
            lambda item: item["results"].__setitem__("same_revision_matches", 4),
            lambda item: item["results"].__setitem__("tamper_rejections", 6),
            lambda item: item["results"].__setitem__("authority_violations", 1),
            lambda item: item["results"].__setitem__("provider_invocations", 1),
            lambda item: item["results"].__setitem__("external_services", 1),
            lambda item: item["results"].__setitem__(
                "claim_conflict_rejected", False
            ),
            lambda item: item["results"].__setitem__(
                "active_work_complete_rate", True
            ),
            lambda item: item["latency_ms"].__setitem__("p95", -1.0),
            lambda item: item["latency_ms"].__setitem__("max", float("inf")),
            lambda item: item["thresholds"].__setitem__(
                "authority_violations_max", False
            ),
            lambda item: item.__setitem__(
                "generated_at", "2026-99-99T20:00:00+08:00"
            ),
            lambda item: item["provenance"].__setitem__(
                "implementation_sha256", "0" * 64
            ),
        ]
        for mutate in mutations:
            forged = copy.deepcopy(receipt)
            mutate(forged)
            _resign(forged)
            with self.assertRaises(ProjectGraphProjectionBenchmarkError):
                validate_project_graph_projection_benchmark(forged, root=self.root)

    def test_validator_rejects_a_consistently_derived_failed_gate(self) -> None:
        receipt = benchmark_project_graph_projection(
            root=self.root,
            iterations=2,
            generated_at="2026-08-17T20:00:00+08:00",
        )
        receipt["results"]["same_revision_matches"] = 1
        receipt["results"]["same_revision_rate"] = 0.5
        receipt["gate"] = {
            "status": "failed",
            "failed_gates": ["same-revision"],
        }
        _resign(receipt)

        with self.assertRaises(ProjectGraphProjectionBenchmarkError):
            validate_project_graph_projection_benchmark(receipt, root=self.root)

    def test_committed_receipt_is_independently_valid(self) -> None:
        path = (
            self.root
            / "experiments/evidence/m9-02-project-graph-projection-results.json"
        )
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.skipTest("M9-02 measured receipt has not been generated")
        validate_project_graph_projection_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["parameters"]["iterations"], 1000)
        self.assertEqual(receipt["gate"]["status"], "passed")

    def test_invalid_iteration_count_is_rejected(self) -> None:
        for value in (True, 0, 1001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                benchmark_project_graph_projection(
                    root=self.root,
                    iterations=value,
                    generated_at="2026-08-17T20:00:00+08:00",
                )

    def test_runner_writes_a_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = run_project_graph_projection_benchmark.main(
                [
                    "--root",
                    str(self.root),
                    "--iterations",
                    "3",
                    "--generated-at",
                    "2026-08-17T20:00:00+08:00",
                    "--output",
                    str(output),
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        validate_project_graph_projection_benchmark(receipt, root=self.root)


if __name__ == "__main__":
    unittest.main()
