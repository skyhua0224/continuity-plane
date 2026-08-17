from __future__ import annotations

import unittest

from benchmarks.run_local_state import run_benchmark


class PublicBenchmarkTests(unittest.TestCase):
    def test_local_state_benchmark_is_reproducible(self) -> None:
        result = run_benchmark(iterations=25)

        self.assertEqual(result["schema_version"], "context.public-benchmark/v1")
        self.assertEqual(result["benchmark"], "local-authority-read")
        self.assertEqual(result["iterations"], 25)
        self.assertEqual(result["successful_reads"], 25)
        self.assertEqual(result["quality_rate"], 1.0)
        self.assertGreater(result["p95_ms"], 0)
        self.assertEqual(result["external_services_required"], 0)
