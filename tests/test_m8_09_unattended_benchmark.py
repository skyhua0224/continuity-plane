"""M8-09 unattended dispatcher quantitative acceptance benchmark."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_control_plane.unattended_dispatcher_benchmark import (
    benchmark_unattended_dispatcher,
    validate_unattended_dispatcher_benchmark,
)


def _resign(receipt: dict) -> None:
    receipt.pop("receipt_sha256", None)
    payload = json.dumps(
        receipt,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    receipt["receipt_sha256"] = hashlib.sha256(payload).hexdigest()


class M809UnattendedDispatcherBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_required_closure_replay_and_duplicate_gates_pass(self) -> None:
        receipt = benchmark_unattended_dispatcher(
            root=self.root,
            samples=3,
            generated_at="2026-08-17T12:00:00+00:00",
        )

        validate_unattended_dispatcher_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["verdict"]["decision"], "pass")
        self.assertEqual(receipt["required_work_per_sample"], 3)
        self.assertEqual(receipt["optional_work_per_sample"], 1)
        self.assertEqual(receipt["required_completion_attempts"], 9)
        self.assertEqual(receipt["required_completions"], 9)
        self.assertEqual(receipt["campaign_closure_attempts"], 3)
        self.assertEqual(receipt["campaign_closures"], 3)
        self.assertEqual(receipt["replay_attempts"], 3)
        self.assertEqual(receipt["replay_matches"], 3)
        self.assertEqual(receipt["duplicate_attempts"], 9)
        self.assertEqual(receipt["duplicate_claim_execute_complete_rejections"], 9)
        self.assertEqual(receipt["authority_violations"], 0)
        self.assertEqual(receipt["provider_invocations"], 0)
        self.assertEqual(receipt["external_services"], 0)
        self.assertLessEqual(receipt["latency_ms"]["p95_ms"], 50.0)

    def test_validator_rejects_forged_success_after_receipt_resign(self) -> None:
        receipt = benchmark_unattended_dispatcher(
            root=self.root,
            samples=1,
            generated_at="2026-08-17T12:00:00+00:00",
        )
        forged = copy.deepcopy(receipt)
        forged["required_completions"] -= 1
        _resign(forged)

        with self.assertRaisesRegex(ValueError, "completion|coverage|receipt"):
            validate_unattended_dispatcher_benchmark(forged, root=self.root)

    def test_validator_rejects_resigned_unknown_top_level_field(self) -> None:
        receipt = benchmark_unattended_dispatcher(
            root=self.root,
            samples=1,
            generated_at="2026-08-17T12:00:00+00:00",
        )
        forged = copy.deepcopy(receipt)
        forged["unexpected"] = True
        _resign(forged)

        with self.assertRaisesRegex(ValueError, "fields"):
            validate_unattended_dispatcher_benchmark(forged, root=self.root)

    def test_cli_writes_and_validates_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "receipt.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self.root / "tools/run_unattended_dispatcher_benchmark.py"),
                    "--root",
                    str(self.root),
                    "--samples",
                    "1",
                    "--generated-at",
                    "2026-08-17T12:00:00+00:00",
                    "--output",
                    str(output),
                ],
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            validate_unattended_dispatcher_benchmark(receipt, root=self.root)


if __name__ == "__main__":
    unittest.main()
