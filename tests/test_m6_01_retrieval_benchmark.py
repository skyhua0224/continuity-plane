import copy
import hashlib
import inspect
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from context_control_plane.retrieval_benchmark import (
    RetrievalBenchmarkError,
    _fixture,
    benchmark_retrieval,
    validate_retrieval_benchmark,
)
from context_control_plane.retrieval_routing import compose_retrieval_receipt


class M601RetrievalBenchmarkTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = benchmark_retrieval(samples=100)

    @staticmethod
    def reseal(result: dict) -> None:
        unsigned = copy.deepcopy(result)
        unsigned.pop("receipt_sha256")
        result["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def test_meets_e5_quality_and_reduction_gates(self) -> None:
        self.assertIn("root", inspect.signature(validate_retrieval_benchmark).parameters)
        validate_retrieval_benchmark(self.result, root=self.root)
        self.assertEqual(self.result["route_accuracy"], 1.0)
        self.assertEqual(self.result["precision"], 1.0)
        self.assertEqual(self.result["recall"], 1.0)
        self.assertEqual(self.result["freshness_pass_rate"], 1.0)
        self.assertEqual(self.result["provenance_coverage"], 1.0)
        self.assertGreaterEqual(self.result["duplicate_read_reduction_percent"], 30.0)
        self.assertEqual(self.result["external_services"], 0)

    def test_fixture_drives_quality_and_receipt_byte_accounting(self) -> None:
        fixture = _fixture()
        for case in fixture:
            self.assertIn("expected_evidence_ids", case)
            self.assertIn("evidence", case)
            self.assertIn("step_results", case)

        baseline = benchmark_retrieval(samples=2)
        modified = copy.deepcopy(fixture)
        modified[0]["step_results"][0]["returned_bytes"] += 31
        with patch(
            "context_control_plane.retrieval_benchmark._fixture",
            return_value=modified,
        ):
            changed = benchmark_retrieval(samples=2)

        self.assertEqual(
            changed["baseline_duplicate_read_bytes"]
            - baseline["baseline_duplicate_read_bytes"],
            124,
        )
        self.assertEqual(
            changed["bounded_unique_read_bytes"]
            - baseline["bounded_unique_read_bytes"],
            62,
        )

    def test_gold_labels_reject_precision_and_recall_regressions(self) -> None:
        precision_fixture = copy.deepcopy(_fixture())
        self.assertIn("evidence", precision_fixture[0])
        self.assertIn("expected_evidence_ids", precision_fixture[0])
        irrelevant = copy.deepcopy(precision_fixture[0]["evidence"][0])
        irrelevant["evidence_id"] = "evidence/m6-01/irrelevant"
        irrelevant["source_ref"] = "repo://context-control-plane/irrelevant.py#L1"
        precision_fixture[0]["evidence"].append(irrelevant)
        with patch(
            "context_control_plane.retrieval_benchmark._fixture",
            return_value=precision_fixture,
        ), self.assertRaisesRegex(RetrievalBenchmarkError, "precision"):
            benchmark_retrieval(samples=1)

        recall_fixture = copy.deepcopy(_fixture())
        recall_fixture[0]["expected_evidence_ids"].append(
            "evidence/m6-01/missing"
        )
        with patch(
            "context_control_plane.retrieval_benchmark._fixture",
            return_value=recall_fixture,
        ), self.assertRaisesRegex(RetrievalBenchmarkError, "recall"):
            benchmark_retrieval(samples=1)

    def test_benchmark_executes_receipt_and_fault_paths(self) -> None:
        samples = 2
        with patch(
            "context_control_plane.retrieval_benchmark.compose_retrieval_receipt",
            side_effect=compose_retrieval_receipt,
            create=True,
        ) as compose:
            result = benchmark_retrieval(samples=samples)

        self.assertGreaterEqual(compose.call_count, samples * (len(_fixture()) * 3 + 3))
        fault_calls = [
            call
            for call in compose.call_args_list
            if "/fault/" in call.kwargs["plan"]["question_id"]
        ]
        self.assertEqual(len(fault_calls), samples * 3)
        self.assertEqual(result["precision_faults_rejected"], samples)
        self.assertEqual(result["recall_faults_rejected"], samples)
        self.assertEqual(result["freshness_faults_rejected"], samples)

    def test_persists_one_replayable_outcome_per_iteration(self) -> None:
        self.assertIn("sample_outcomes", self.result)
        self.assertIn("outcomes_sha256", self.result)
        self.assertEqual(len(self.result["sample_outcomes"]), self.result["samples"])
        self.assertEqual(
            [item["sample_index"] for item in self.result["sample_outcomes"]],
            list(range(self.result["samples"])),
        )

    def test_validator_replays_persisted_outcome_semantics(self) -> None:
        invalid = copy.deepcopy(self.result)
        invalid["sample_outcomes"][0]["route_bundle_sha256"] = "f" * 64
        invalid["outcomes_sha256"] = hashlib.sha256(
            json.dumps(
                invalid["sample_outcomes"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        self.reseal(invalid)
        with self.assertRaisesRegex(RetrievalBenchmarkError, "outcome semantics"):
            validate_retrieval_benchmark(invalid, root=self.root)

    def test_benchmark_receipt_is_digest_bound_and_time_valid(self) -> None:
        self.assertIn("receipt_sha256", self.result)
        invalid = copy.deepcopy(self.result)
        invalid["generated_at"] = "not-a-time"
        self.reseal(invalid)
        with self.assertRaisesRegex(RetrievalBenchmarkError, "generated_at"):
            validate_retrieval_benchmark(invalid, root=self.root)

    def test_provenance_rechecks_fixture_benchmark_and_subject(self) -> None:
        self.assertIn("root", inspect.signature(validate_retrieval_benchmark).parameters)
        validate_retrieval_benchmark(self.result, root=self.root)
        for field in ("fixture_sha256", "benchmark_sha256", "subject_sha256"):
            with self.subTest(field=field):
                invalid = copy.deepcopy(self.result)
                invalid[field] = "0" * 64
                self.reseal(invalid)
                with self.assertRaisesRegex(RetrievalBenchmarkError, field):
                    validate_retrieval_benchmark(invalid, root=self.root)

    def test_negative_quality_gates_fail_closed(self) -> None:
        for field in ("precision", "recall", "freshness_pass_rate"):
            with self.subTest(field=field):
                invalid = copy.deepcopy(self.result)
                invalid[field] = 0.99
                with self.assertRaises(RetrievalBenchmarkError):
                    validate_retrieval_benchmark(invalid)

    def test_benchmark_is_replayable(self) -> None:
        replay = benchmark_retrieval(samples=100)
        left = copy.deepcopy(self.result)
        right = copy.deepcopy(replay)
        for result in (left, right):
            for field in (
                "p50_ms",
                "p95_ms",
                "max_ms",
                "outcomes_sha256",
                "receipt_sha256",
            ):
                result.pop(field)
            for outcome in result["sample_outcomes"]:
                outcome.pop("duration_ms")
        self.assertEqual(left, right)


if __name__ == "__main__":
    unittest.main()
