import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


class M209SQLiteBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture_path = (
            Path(__file__).parents[1]
            / "experiments"
            / "state"
            / "m2-01-core-fixtures.yaml"
        )
        fixture_set = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
        cases = {case["case_id"]: case["document"] for case in fixture_set["cases"]}
        cls.initial = cases["completed-work-overlap-blocked"]

    def test_benchmark_records_full_operation_latency_and_zero_service_footprint(self):
        from context_control_plane.sqlite_benchmark import run_sqlite_benchmark

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = run_sqlite_benchmark(
                Path(temporary_directory) / "benchmark.sqlite3",
                copy.deepcopy(self.initial),
                samples=5,
            )

        self.assertEqual(
            result["schema_version"],
            "context.sqlite-state-store-results/v1alpha1",
        )
        self.assertEqual(result["environment"]["fixture"], "completed-work-overlap-blocked")
        self.assertEqual(result["environment"]["external_services"], 0)
        self.assertEqual(result["latency_ms"]["samples"], 5)
        for operation in ("commit", "read"):
            self.assertGreaterEqual(result["latency_ms"][f"{operation}_p50"], 0)
            self.assertGreaterEqual(
                result["latency_ms"][f"{operation}_p95"],
                result["latency_ms"][f"{operation}_p50"],
            )
            self.assertGreaterEqual(
                result["latency_ms"][f"{operation}_max"],
                result["latency_ms"][f"{operation}_p95"],
            )
        self.assertLess(result["latency_ms"]["state_only_restore_p95"], 2_000)
        self.assertGreater(result["footprint_bytes"]["database"], 0)
        self.assertEqual(result["authority_boundary"]["vector_index_authority"], False)

    def test_benchmark_rejects_non_positive_sample_counts(self):
        from context_control_plane.sqlite_benchmark import run_sqlite_benchmark

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "samples"):
                run_sqlite_benchmark(
                    Path(temporary_directory) / "benchmark.sqlite3",
                    copy.deepcopy(self.initial),
                    samples=0,
                )

    def test_stream_benchmark_records_growth_and_final_restore(self):
        from context_control_plane.sqlite_benchmark import run_sqlite_stream_benchmark

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = run_sqlite_stream_benchmark(
                Path(temporary_directory) / "stream.sqlite3",
                copy.deepcopy(self.initial),
                events=10,
            )

        self.assertEqual(
            result["schema_version"],
            "context.sqlite-stream-results/v1alpha1",
        )
        self.assertEqual(result["events"], 10)
        self.assertEqual(
            result["final_revision"],
            self.initial["project"]["revision"] + 10,
        )
        self.assertGreaterEqual(
            result["latency_ms"]["commit_p95"],
            result["latency_ms"]["commit_p50"],
        )
        self.assertGreaterEqual(result["latency_ms"]["final_restore"], 0)
        self.assertGreaterEqual(
            result["latency_ms"]["total_wall"],
            result["latency_ms"]["total_commit"],
        )

    def test_forty_sample_replay_keeps_the_restore_gate(self):
        from context_control_plane.sqlite_benchmark import run_sqlite_benchmark

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = run_sqlite_benchmark(
                Path(temporary_directory) / "benchmark-40.sqlite3",
                copy.deepcopy(self.initial),
                samples=40,
            )

        self.assertEqual(result["latency_ms"]["samples"], 40)
        self.assertLess(result["latency_ms"]["state_only_restore_p95"], 2_000)

    def test_cli_generates_a_comparable_yaml_receipt(self):
        root = Path(__file__).parents[1]
        result = subprocess.run(
            [
                sys.executable,
                str(root / "tools" / "run_sqlite_benchmark.py"),
                "--samples",
                "2",
                "--stream-events",
                "10",
            ],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = yaml.safe_load(result.stdout)
        self.assertEqual(receipt["latency_ms"]["samples"], 2)
        self.assertTrue(receipt["comparison_to_postgresql"]["same_fixture"])
        self.assertEqual(
            receipt["comparison_to_postgresql"]["postgres_evidence"],
            "experiments/state/m2-03-postgres-cas-results.yaml",
        )
        self.assertEqual(receipt["stream_scaling"]["events"], 10)
        committed = yaml.safe_load(
            (
                root
                / "experiments"
                / "state"
                / "m2-09-sqlite-state-store-results.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(set(receipt), set(committed))
        for field in (
            "provenance",
            "live_platform_gates",
            "authority_boundary",
            "limitations",
        ):
            self.assertEqual(receipt[field], committed[field])
        self.assertEqual(
            receipt["generation"]["arguments"],
            ["--samples", "2", "--stream-events", "10"],
        )
        self.assertIn("--samples 2", receipt["generation"]["command"])
        self.assertIn("--stream-events 10", receipt["generation"]["command"])

    def test_committed_receipt_has_current_provenance_and_valid_schema(self):
        import context_control_plane.sqlite_benchmark as benchmark_contracts

        root = Path(__file__).parents[1]
        validator = getattr(
            benchmark_contracts,
            "validate_sqlite_benchmark_receipt",
            None,
        )
        self.assertIsNotNone(validator)
        receipt = yaml.safe_load(
            (
                root
                / "experiments"
                / "state"
                / "m2-09-sqlite-state-store-results.yaml"
            ).read_text(encoding="utf-8")
        )
        validator(receipt, root=root)

    def test_linux_acceptance_receipt_is_comparable_and_keeps_platform_gaps_open(self):
        root = Path(__file__).parents[1]
        receipt = yaml.safe_load(
            (
                root
                / "experiments"
                / "state"
                / "m2-09-sqlite-state-store-results.yaml"
            ).read_text(encoding="utf-8")
        )
        postgres = yaml.safe_load(
            (
                root
                / "experiments"
                / "state"
                / "m2-03-postgres-cas-results.yaml"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(receipt["latency_ms"]["samples"], 40)
        self.assertEqual(receipt["environment"]["external_services"], 0)
        self.assertLess(receipt["latency_ms"]["state_only_restore_p95"], 2_000)
        comparison = receipt["comparison_to_postgresql"]
        self.assertTrue(comparison["same_fixture"])
        self.assertTrue(comparison["same_samples"])
        self.assertEqual(
            comparison["commit_p95_reduction_percent"],
            round(
                (
                    postgres["latency_ms"]["commit_p95"]
                    - receipt["latency_ms"]["commit_p95"]
                )
                / postgres["latency_ms"]["commit_p95"]
                * 100,
                4,
            ),
        )
        self.assertEqual(receipt["live_platform_gates"]["linux"], "passed")
        self.assertEqual(receipt["live_platform_gates"]["windows"], "blocked")
        self.assertEqual(receipt["live_platform_gates"]["macos"], "blocked")
        self.assertFalse(receipt["authority_boundary"]["shared_authority"])
        self.assertFalse(receipt["authority_boundary"]["vector_index_authority"])


if __name__ == "__main__":
    unittest.main()
