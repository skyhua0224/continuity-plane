"""M8-01 live process-kill recovery fixtures."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class M801DurableOperationCrashTests(unittest.TestCase):
    @staticmethod
    def _command(root: Path, directory: str) -> list[str]:
        return [
            sys.executable,
            str(root / "tools" / "run_m8_01_crash_fixture.py"),
            "--root",
            directory,
        ]

    def test_sigkill_after_external_effect_recovers_without_a_duplicate_effect(self) -> None:
        if not hasattr(signal, "SIGKILL"):
            self.skipTest("platform has no SIGKILL")
        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            command = self._command(root, directory)
            killed = subprocess.run(
                [*command, "--crash-point", "after-external-effect"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(killed.returncode, -signal.SIGKILL)

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

        self.assertEqual(receipt["phase"], "terminal")
        self.assertEqual(receipt["physical_effects"], 1)
        self.assertEqual(receipt["semantic_effects"], 1)
        self.assertEqual(receipt["intent_commits"], 1)
        self.assertEqual(receipt["state_commits"], 1)
        self.assertEqual(receipt["duplicate_effects"], 0)
        self.assertEqual(receipt["history_phases"].count("effect-in-flight"), 1)

    def test_every_durable_boundary_recovers_after_sigkill(self) -> None:
        if not hasattr(signal, "SIGKILL"):
            self.skipTest("platform has no SIGKILL")
        root = Path(__file__).parents[1]
        crash_points = (
            "after-prepared",
            "after-intent-commit",
            "after-intent-record",
            "after-effect-start",
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
                self.assertEqual(killed.returncode, -signal.SIGKILL)
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
                self.assertEqual(receipt["phase"], "terminal")
                self.assertEqual(receipt["physical_effects"], 1)
                self.assertEqual(receipt["semantic_effects"], 1)
                self.assertEqual(receipt["duplicate_effects"], 0)
                self.assertEqual(receipt["intent_commits"], 1)
                self.assertEqual(receipt["state_commits"], 1)


if __name__ == "__main__":
    unittest.main()
