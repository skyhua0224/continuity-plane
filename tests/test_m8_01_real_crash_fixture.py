"""M8-01 combined content-addressed checkpoint and State MCP crash fixture."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class M801RealCrashFixtureTests(unittest.TestCase):
    @staticmethod
    def _command(root: Path, directory: str) -> list[str]:
        return [
            sys.executable,
            str(root / "tools" / "run_m8_01_crash_fixture.py"),
            "--root",
            directory,
            "--real-authority",
        ]

    def test_every_boundary_reconciles_real_checkpoint_state_and_effect(self) -> None:
        if not hasattr(signal, "SIGKILL"):
            self.skipTest("platform has no SIGKILL")
        root = Path(__file__).parents[1]
        crash_points = (
            "after-prepared",
            "after-intent-commit",
            "after-intent-record",
            "after-effect-start",
            "after-external-effect",
            "after-effect-settlement",
            "after-state-commit",
            "after-response-record",
            "after-terminal",
        )
        for crash_point in crash_points:
            with self.subTest(crash_point=crash_point), tempfile.TemporaryDirectory() as directory:
                command = self._command(root, directory)
                killed = subprocess.run(
                    [*command, "--crash-point", crash_point],
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)
                recovered = subprocess.run(
                    command,
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(recovered.returncode, os.EX_OK, recovered.stderr)
                receipt = json.loads(recovered.stdout)
                self.assertEqual(
                    receipt["schema_version"],
                    "context.durable-operation-real-crash-fixture/v1alpha1",
                )
                self.assertEqual(receipt["phase"], "terminal")
                self.assertEqual(receipt["authority_backend"], "state-mcp-sqlite")
                self.assertEqual(
                    receipt["checkpoint_backend"], "content-addressed-local"
                )
                self.assertEqual(receipt["state_revision"], 10)
                self.assertEqual(receipt["state_event_count"], 3)
                self.assertEqual(receipt["effect_state"], "succeeded")
                self.assertEqual(receipt["physical_effects"], 1)
                self.assertEqual(receipt["semantic_effects"], 1)
                self.assertEqual(receipt["duplicate_effects"], 0)


if __name__ == "__main__":
    unittest.main()
