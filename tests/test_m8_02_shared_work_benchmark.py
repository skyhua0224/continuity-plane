"""M8-02 deterministic shared Work/claim/lease fault benchmark."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.shared_work_benchmark import (
    SHARED_WORK_SCENARIOS,
    SharedWorkBenchmarkError,
    benchmark_shared_work,
    validate_shared_work_benchmark,
)
from tools import run_shared_work_benchmark


class M802SharedWorkBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1]
        cls.schema = json.loads(
            (root / "schemas/m8-02/shared-work-benchmark.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator.check_schema(cls.schema)
        cls.schema_validator = Draft202012Validator(
            cls.schema,
            format_checker=FormatChecker(),
        )

    def receipt(self, *, samples: int = 2) -> dict:
        return benchmark_shared_work(
            samples=samples,
            generated_at="2026-08-16T17:00:00+08:00",
            latency_p95_threshold_ms=50.0,
        )

    @staticmethod
    def resign(receipt: dict) -> None:
        body = copy.deepcopy(receipt)
        body.pop("receipt_sha256", None)
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        receipt["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()

    def test_every_fault_family_passes_and_zero_loss_gates_are_measured(self) -> None:
        receipt = self.receipt()

        validate_shared_work_benchmark(receipt)
        self.schema_validator.validate(receipt)
        self.assertEqual(receipt["samples_per_scenario"], 2)
        self.assertEqual(receipt["sample_count"], 2 * len(SHARED_WORK_SCENARIOS))
        self.assertEqual(
            [item["scenario"] for item in receipt["scenarios"]],
            list(SHARED_WORK_SCENARIOS),
        )
        self.assertTrue(
            all(
                item["attempted"] == 2 and item["passed"] == 2
                for item in receipt["scenarios"]
            )
        )
        for field in (
            "silent_overwrites",
            "duplicate_claims",
            "duplicate_effects",
            "post_revoke_effects",
            "old_worker_admissions",
            "event_mismatches",
            "revision_mismatches",
            "hash_mismatches",
        ):
            self.assertEqual(receipt["metrics"][field], 0, field)
        self.assertEqual(receipt["metrics"]["orphan_reclaims"], 2)
        self.assertEqual(receipt["metrics"]["expected_orphan_reclaims"], 2)
        self.assertGreater(receipt["latency_ms"]["p50"], 0)
        self.assertGreaterEqual(
            receipt["latency_ms"]["p95"], receipt["latency_ms"]["p50"]
        )
        self.assertEqual(receipt["verdict"]["decision"], "pass")
        self.assertEqual(receipt["verdict"]["failed_gates"], [])

    def test_committed_receipt_revalidates_without_live_execution(self) -> None:
        root = Path(__file__).parents[1]
        receipt = json.loads(
            (root / "experiments/evidence/m8-02-shared-work-results.json").read_text(
                encoding="utf-8"
            )
        )

        validate_shared_work_benchmark(receipt)
        self.schema_validator.validate(receipt)
        self.assertEqual(receipt["sample_count"], 10_000)
        self.assertEqual(receipt["verdict"]["decision"], "pass")

    def test_receipt_replay_is_logically_deterministic(self) -> None:
        first = self.receipt(samples=1)
        second = self.receipt(samples=1)

        self.assertEqual(first["benchmark_id"], second["benchmark_id"])
        self.assertEqual(first["scenarios"], second["scenarios"])
        self.assertEqual(first["metrics"], second["metrics"])
        self.assertEqual(first["thresholds"], second["thresholds"])
        self.assertEqual(first["verdict"]["scenario_pass_rate"], 1.0)
        self.assertEqual(second["verdict"]["scenario_pass_rate"], 1.0)

    def test_validator_fails_closed_on_false_success_or_unknown_fields(self) -> None:
        receipt = self.receipt(samples=1)
        forged = copy.deepcopy(receipt)
        forged["metrics"]["post_revoke_effects"] = 1
        self.resign(forged)
        with self.assertRaises(SharedWorkBenchmarkError):
            validate_shared_work_benchmark(forged)

        forged = copy.deepcopy(receipt)
        forged["verdict"]["failed_gates"] = ["latency_p95_ms"]
        self.resign(forged)
        with self.assertRaises(SharedWorkBenchmarkError):
            validate_shared_work_benchmark(forged)

        forged = copy.deepcopy(receipt)
        forged["unknown"] = True
        self.resign(forged)
        with self.assertRaises(SharedWorkBenchmarkError):
            validate_shared_work_benchmark(forged)
        with self.assertRaises(ValidationError):
            self.schema_validator.validate(forged)

    def test_samples_and_latency_threshold_are_bounded(self) -> None:
        for samples in (0, 1001, True):
            with (
                self.subTest(samples=samples),
                self.assertRaises(SharedWorkBenchmarkError),
            ):
                benchmark_shared_work(
                    samples=samples,
                    generated_at="2026-08-16T17:00:00+08:00",
                )

    def test_runner_persists_and_prints_a_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = run_shared_work_benchmark.main(
                ["--samples", "1", "--output", str(output)]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result, 0)
        self.assertEqual(receipt["verdict"]["decision"], "pass")
        for threshold in (0, -1, True):
            with (
                self.subTest(threshold=threshold),
                self.assertRaises(SharedWorkBenchmarkError),
            ):
                benchmark_shared_work(
                    samples=1,
                    generated_at="2026-08-16T17:00:00+08:00",
                    latency_p95_threshold_ms=threshold,
                )


if __name__ == "__main__":
    unittest.main()
