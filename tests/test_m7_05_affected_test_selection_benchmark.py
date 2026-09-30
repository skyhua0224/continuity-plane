from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane import affected_test_selection_benchmark as benchmark


class M705AffectedTestSelectionBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def run_benchmark(self, iterations: int = 80) -> tuple[dict, Path, Path]:
        golden = benchmark.build_affected_test_selection_golden_matrix()
        fixture = benchmark.build_affected_test_selection_fixture(
            golden_matrix=golden
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        fixture_path = root / "fixture.json"
        golden_path = root / "golden.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        golden_path.write_text(json.dumps(golden), encoding="utf-8")
        receipt = benchmark.run_affected_test_selection_benchmark(
            fixture_path=fixture_path,
            golden_path=golden_path,
            repository_root=self.root,
            iterations=iterations,
            command_samples=1,
        )
        return receipt, fixture_path, golden_path

    def test_independent_benchmark_replays_fault_matrix(self) -> None:
        receipt, fixture_path, golden_path = self.run_benchmark()
        self.assertEqual(receipt["iterations"], 80)
        self.assertEqual(sum(receipt["scenario_counts"].values()), 80)
        self.assertEqual(receipt["missed_test_count"], 0)
        self.assertEqual(receipt["unsafe_partial_selection_count"], 0)
        self.assertEqual(receipt["fallback_mismatch_count"], 0)
        self.assertEqual(receipt["replay_mismatch_count"], 0)
        benchmark.verify_affected_test_selection_benchmark(
            receipt,
            fixture_path=fixture_path,
            golden_path=golden_path,
            repository_root=self.root,
            rerun_real_commands=False,
        )

    def test_real_repository_gate_is_distinct_from_microbenchmark(self) -> None:
        receipt, _, _ = self.run_benchmark()
        self.assertGreaterEqual(receipt["real_wall_time_reduction_basis_points"], 3000)
        self.assertGreaterEqual(receipt["micro_wall_time_reduction_basis_points"], 0)
        self.assertEqual(receipt["real_command_missed_count"], 0)
        self.assertNotEqual(
            receipt["real_baseline_wall_time_ns"],
            receipt["micro_baseline_wall_time_ns"],
        )

    def test_committed_fixture_and_receipt_are_independently_verifiable(self) -> None:
        fixture_path = (
            self.root
            / "experiments/fixtures/m7-05-affected-test-selection-fixture.json"
        )
        golden_path = (
            self.root
            / "experiments/fixtures/m7-05-affected-test-selection-golden.json"
        )
        receipt_path = (
            self.root / "experiments/evidence/m7-05-affected-test-selection-results.json"
        )
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            self.fail(f"committed M7-05 receipt is missing: {exc.filename}")
        try:
            benchmark.verify_affected_test_selection_benchmark(
                receipt,
                fixture_path=fixture_path,
                golden_path=golden_path,
                repository_root=self.root,
                rerun_real_commands=False,
            )
        except (FileNotFoundError, benchmark.AffectedTestSelectionBenchmarkError) as exc:
            self.fail(f"committed M7-05 receipt is not independently verifiable: {exc}")


if __name__ == "__main__":
    unittest.main()
