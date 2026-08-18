"""M8-07 quantitative replay and safety benchmark tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from context_control_plane.project_adaptation_benchmark import benchmark_project_adaptation


class M807BenchmarkTests(unittest.TestCase):
    def test_benchmark_meets_safety_and_replay_gates(self) -> None:
        receipt = benchmark_project_adaptation(
            root=Path(__file__).resolve().parents[1],
            samples=20,
            generated_at="2026-08-16T12:00:00Z",
        )

        self.assertEqual(receipt["verdict"]["decision"], "pass")
        self.assertEqual(receipt["rates"]["deterministic_replay_rate"], 1.0)
        self.assertEqual(receipt["rates"]["unauthorized_activation_rejection_rate"], 1.0)
        self.assertEqual(receipt["rates"]["rollback_veto_recovery_rate"], 1.0)
        self.assertEqual(receipt["authority_mutations"], 0)
        self.assertEqual(receipt["provider_invocations"], 0)
        self.assertEqual(receipt["external_services"], 0)


if __name__ == "__main__":
    unittest.main()
