from __future__ import annotations

import subprocess
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.codex_hook_efficiency_benchmark import (
    benchmark_codex_hook_efficiency,
    validate_codex_hook_efficiency_receipt,
)


class M1100CodexHookEfficiencyBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.baseline_ref = subprocess.run(
            ["git", "-C", str(cls.root), "rev-parse", "ffcdbfb^{commit}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_candidate_removes_redundant_autorun_and_context(self) -> None:
        receipt = benchmark_codex_hook_efficiency(
            self.root,
            baseline_git_ref=self.baseline_ref,
            samples=3,
            observed_at="2026-08-30T18:00:00+08:00",
        )

        validate_codex_hook_efficiency_receipt(receipt, root=self.root)
        baseline = receipt["arms"]["baseline"]
        candidate = receipt["arms"]["candidate"]
        self.assertEqual(candidate["deny_count"], 0)
        self.assertEqual(candidate["stop_count"], 0)
        self.assertEqual(candidate["continuity_calls_per_sample"], 4)
        self.assertEqual(baseline["continuity_calls_per_sample"], 6)
        self.assertNotIn(
            "PostToolUse:Bash", candidate["registered_event_sequence"]
        )
        self.assertIn("PostToolUse:Bash", baseline["registered_event_sequence"])
        self.assertRegex(
            receipt["provenance"]["candidate_hook_contract_sha256"],
            r"^[0-9a-f]{64}$",
        )
        self.assertGreaterEqual(
            receipt["improvements"]["continuity_call_reduction_percent"],
            30,
        )
        self.assertGreaterEqual(
            receipt["improvements"]["model_context_byte_reduction_percent"],
            30,
        )
        self.assertIn("wall_ms_p50_reduction_percent", receipt["improvements"])
        self.assertIn("wall_ms_p95_reduction_percent", receipt["improvements"])
        self.assertTrue(receipt["acceptance"]["passed"])

    def test_receipt_rejects_tampered_measurements(self) -> None:
        receipt = benchmark_codex_hook_efficiency(
            self.root,
            baseline_git_ref=self.baseline_ref,
            samples=2,
            observed_at="2026-08-30T18:00:00+08:00",
        )
        receipt["arms"]["candidate"]["deny_count"] = 1

        with self.assertRaisesRegex(ValueError, "digest"):
            validate_codex_hook_efficiency_receipt(receipt, root=self.root)

    def test_runner_writes_a_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            completed = subprocess.run(
                [
                    str(self.root / ".venv/bin/python"),
                    str(self.root / "tools/run_codex_hook_efficiency_benchmark.py"),
                    "--root",
                    str(self.root),
                    "--baseline-git-ref",
                    self.baseline_ref,
                    "--samples",
                    "2",
                    "--observed-at",
                    "2026-08-30T18:00:00+08:00",
                    "--output",
                    str(output),
                ],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            validate_codex_hook_efficiency_receipt(receipt, root=self.root)


if __name__ == "__main__":
    unittest.main()
