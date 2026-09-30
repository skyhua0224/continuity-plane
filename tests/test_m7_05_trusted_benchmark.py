from __future__ import annotations

import copy
import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane import affected_test_selection_benchmark as benchmark


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reseal(document: dict, field: str) -> None:
    document[field] = hashlib.sha256(
        _canonical({key: value for key, value in document.items() if key != field})
    ).hexdigest()


class M705TrustedBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.fixture_path = (
            self.root
            / "experiments/fixtures/m7-05-affected-test-selection-fixture.json"
        )
        self.golden_path = (
            self.root
            / "experiments/fixtures/m7-05-affected-test-selection-golden.json"
        )

    def test_generator_and_independent_verifier_apis_exist(self) -> None:
        module = importlib.import_module(
            "context_control_plane.affected_test_selection_benchmark"
        )

        for name in (
            "build_affected_test_selection_golden_matrix",
            "run_affected_test_selection_benchmark",
            "verify_affected_test_selection_benchmark",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(module, name, None)))

    def test_independent_verifier_replays_oracle_counts_and_workload(self) -> None:
        try:
            receipt, fixture_path, golden_path = self.run_benchmark(iterations=80)
        except NotImplementedError:
            self.fail("independent M7-05 benchmark is not implemented")

        forged = copy.deepcopy(receipt)
        forged["scenario_counts"] = {f"fake-{index}": 10 for index in range(8)}
        forged["selection_mode_counts"] = {"selected": 80}
        forged["micro_workload_outputs_sha256"] = "0" * 64
        _reseal(forged, "benchmark_sha256")
        with self.assertRaises(benchmark.AffectedTestSelectionBenchmarkError):
            benchmark.verify_affected_test_selection_benchmark(
                forged,
                fixture_path=fixture_path,
                golden_path=golden_path,
                repository_root=self.root,
                rerun_real_commands=False,
            )

    def test_current_repository_commands_clear_real_gate(self) -> None:
        try:
            receipt, fixture_path, golden_path = self.run_benchmark(iterations=80)
        except NotImplementedError:
            self.fail("real repository command benchmark is not implemented")

        self.assertEqual(receipt["real_command_missed_count"], 0)
        self.assertGreaterEqual(
            receipt["real_wall_time_reduction_basis_points"], 3000
        )
        self.assertTrue(receipt["real_command_receipts"])
        self.assertTrue(receipt["environment"]["repository_tree_sha256"])
        benchmark.verify_affected_test_selection_benchmark(
            receipt,
            fixture_path=fixture_path,
            golden_path=golden_path,
            repository_root=self.root,
            rerun_real_commands=True,
        )

    def test_environment_timing_samples_and_current_tree_are_bound(self) -> None:
        try:
            receipt, fixture_path, golden_path = self.run_benchmark(iterations=80)
        except NotImplementedError:
            self.fail("raw timing and environment binding is not implemented")

        forged = copy.deepcopy(receipt)
        forged["environment"]["repository_tree_sha256"] = "f" * 64
        _reseal(forged, "benchmark_sha256")
        with self.assertRaises(benchmark.AffectedTestSelectionBenchmarkError):
            benchmark.verify_affected_test_selection_benchmark(
                forged,
                fixture_path=fixture_path,
                golden_path=golden_path,
                repository_root=self.root,
                rerun_real_commands=False,
            )

    def run_benchmark(self, *, iterations: int) -> tuple[dict, Path, Path]:
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


if __name__ == "__main__":
    unittest.main()
