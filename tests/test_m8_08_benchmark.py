"""M8-08 forge collaboration quantitative acceptance tests."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_control_plane.forge_collaboration_benchmark import (
    benchmark_forge_collaboration,
    validate_forge_collaboration_benchmark,
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


class M808ForgeCollaborationBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_dual_adapter_benchmark_meets_mapping_replay_and_conflict_gates(self) -> None:
        receipt = benchmark_forge_collaboration(
            root=self.root,
            samples=20,
            generated_at="2026-08-17T00:00:00Z",
        )

        validate_forge_collaboration_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["verdict"]["decision"], "pass")
        self.assertEqual(receipt["rates"]["visible_record_mapping_rate"], 1.0)
        self.assertEqual(receipt["rates"]["deterministic_replay_rate"], 1.0)
        self.assertEqual(receipt["rates"]["stale_ref_rejection_rate"], 1.0)
        self.assertEqual(receipt["rates"]["unpublished_downgrade_rate"], 1.0)
        self.assertEqual(receipt["authority_escalations"], 0)
        self.assertEqual(receipt["provider_invocations"], 0)
        self.assertEqual(receipt["external_services"], 0)

    def test_benchmark_validator_rejects_forged_success(self) -> None:
        receipt = benchmark_forge_collaboration(
            root=self.root,
            samples=2,
            generated_at="2026-08-17T00:00:00Z",
        )
        forged = copy.deepcopy(receipt)
        forged["complete_visible_record_mappings"] -= 1
        forged["rates"]["visible_record_mapping_rate"] = 1.0
        _resign(forged)

        with self.assertRaisesRegex(ValueError, "receipt_sha256|mapping"):
            validate_forge_collaboration_benchmark(forged, root=self.root)

    def test_benchmark_validator_recomputes_latency_gate_after_resigning(self) -> None:
        receipt = benchmark_forge_collaboration(
            root=self.root,
            samples=2,
            generated_at="2026-08-17T00:00:00Z",
        )
        forged = copy.deepcopy(receipt)
        forged["latency_ms"]["p95_ms"] = 5000.0
        forged["latency_ms"]["max_ms"] = 5000.0
        _resign(forged)

        with self.assertRaisesRegex(ValueError, "latency"):
            validate_forge_collaboration_benchmark(forged, root=self.root)

    def test_cli_writes_a_schema_valid_reproducible_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "receipt.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self.root / "tools/run_forge_collaboration_benchmark.py"),
                    "--root",
                    str(self.root),
                    "--samples",
                    "2",
                    "--generated-at",
                    "2026-08-17T00:00:00Z",
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
            validate_forge_collaboration_benchmark(receipt, root=self.root)


if __name__ == "__main__":
    unittest.main()
