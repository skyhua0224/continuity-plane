"""M8-01 crash recovery measurement receipt."""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class M801DurableOperationBenchmarkTests(unittest.TestCase):
    def test_cli_atomically_writes_a_valid_measured_receipt(self) -> None:
        if not hasattr(signal, "SIGKILL"):
            self.skipTest("platform has no SIGKILL")
        from context_control_plane.durable_operation_benchmark import (
            validate_durable_operation_benchmark,
        )

        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "tools/generate_m8_01_acceptance.py"),
                    "--samples",
                    "1",
                    "--crash-point",
                    "after-prepared",
                    "--generated-at",
                    "2026-08-16T13:30:00+08:00",
                    "--output",
                    str(output),
                ],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            validate_durable_operation_benchmark(receipt)
            self.assertEqual(receipt["samples"], 1)
            self.assertEqual(receipt["crash_points"], ["after-prepared"])
            self.assertFalse(list(output.parent.glob(f".{output.name}.*")))

    def test_benchmark_separates_invocation_effect_and_latency_metrics(self) -> None:
        if not hasattr(signal, "SIGKILL"):
            self.skipTest("platform has no SIGKILL")
        from context_control_plane.durable_operation_benchmark import (
            benchmark_durable_operation,
            validate_durable_operation_benchmark,
        )

        receipt = benchmark_durable_operation(
            root=Path(__file__).parents[1],
            samples=1,
            crash_points=("after-external-effect",),
            generated_at="2026-08-16T13:30:00+08:00",
        )
        validate_durable_operation_benchmark(receipt)

        self.assertEqual(receipt["scenario_count"], 1)
        self.assertEqual(receipt["terminal_recoveries"], 1)
        self.assertEqual(receipt["authority_backend"], "state-mcp-sqlite")
        self.assertEqual(
            receipt["checkpoint_backend"], "content-addressed-local"
        )
        self.assertEqual(receipt["provider_invocations"], 0)
        self.assertEqual(receipt["deduplicated_provider_invocations"], 0)
        self.assertEqual(receipt["effect_adapter_invocations"], 2)
        self.assertEqual(receipt["deduplicated_effect_invocations"], 1)
        self.assertEqual(receipt["applied_effects"], 1)
        self.assertEqual(receipt["duplicate_semantic_effects"], 0)
        self.assertGreater(receipt["restore_latency_ms"]["p95"], 0)
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["provider_native_authority"])

    def test_benchmark_rejects_conflated_or_forged_metrics(self) -> None:
        from context_control_plane.durable_operation_benchmark import (
            DurableOperationBenchmarkError,
            benchmark_durable_operation,
            validate_durable_operation_benchmark,
        )

        receipt = benchmark_durable_operation(
            root=Path(__file__).parents[1],
            samples=1,
            crash_points=("after-prepared",),
            generated_at="2026-08-16T13:30:00+08:00",
        )
        receipt["duplicate_semantic_effects"] = receipt["applied_effects"]
        with self.assertRaises(DurableOperationBenchmarkError):
            validate_durable_operation_benchmark(receipt)


if __name__ == "__main__":
    unittest.main()
