"""M8-05 authorization and isolation fault benchmark."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from context_control_plane.authorization_audit_benchmark import (
    AuthorizationAuditBenchmarkError,
    benchmark_authorization_isolation,
    validate_authorization_isolation_benchmark,
)


class M805AuthorizationBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_fault_matrix_rejects_every_unauthorized_attempt(self) -> None:
        receipt = benchmark_authorization_isolation(
            root=self.root,
            samples=10,
            generated_at="2026-08-16T12:30:00+00:00",
        )

        self.assertEqual(receipt["samples"], 10)
        self.assertEqual(receipt["allowed_attempts"], 10)
        self.assertEqual(receipt["allowed_decisions"], 10)
        self.assertEqual(receipt["unauthorized_attempts"], 100)
        self.assertEqual(receipt["unauthorized_rejections"], 100)
        self.assertIn("policy-not-active", receipt["case_ids"])
        self.assertEqual(receipt["audit_failure_attempts"], 10)
        self.assertEqual(receipt["audit_failure_rejections"], 10)
        self.assertTrue(all(value == 0 for value in receipt["metrics"].values()))
        self.assertEqual(receipt["verdict"]["decision"], "pass")
        validate_authorization_isolation_benchmark(receipt, root=self.root)

    def test_validator_rejects_forged_success_and_stale_provenance(self) -> None:
        receipt = benchmark_authorization_isolation(
            root=self.root,
            samples=2,
            generated_at="2026-08-16T12:30:00+00:00",
        )
        forged = copy.deepcopy(receipt)
        forged["metrics"]["unauthorized_accepts"] = 1
        with self.assertRaisesRegex(
            AuthorizationAuditBenchmarkError, "veto metric"
        ):
            validate_authorization_isolation_benchmark(forged, root=self.root)

        stale = copy.deepcopy(receipt)
        stale["provenance"]["implementation_sha256"] = "0" * 64
        with self.assertRaisesRegex(
            AuthorizationAuditBenchmarkError, "provenance"
        ):
            validate_authorization_isolation_benchmark(stale, root=self.root)

    def test_published_receipt_matches_current_implementation(self) -> None:
        receipt = json.loads(
            (
                self.root
                / "experiments"
                / "evidence"
                / "m8-05-authorization-isolation-results.json"
            ).read_text(encoding="utf-8")
        )

        validate_authorization_isolation_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["samples"], 1_000)
        self.assertEqual(receipt["unauthorized_rejections"], 10_000)
        self.assertEqual(receipt["metrics"]["unauthorized_accepts"], 0)
        self.assertEqual(receipt["metrics"]["cross_tenant_accepts"], 0)
        self.assertEqual(receipt["metrics"]["missing_audit_events"], 0)
        self.assertEqual(receipt["metrics"]["audit_chain_failures"], 0)

    def test_cli_atomically_writes_a_current_receipt(self) -> None:
        from tools.run_authorization_isolation_benchmark import main

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            argv = [
                "run_authorization_isolation_benchmark.py",
                "--root",
                str(self.root),
                "--samples",
                "3",
                "--generated-at",
                "2026-08-16T12:30:00+00:00",
                "--output",
                str(output),
            ]
            with mock.patch("sys.argv", argv):
                self.assertEqual(main(), 0)
            receipt = json.loads(output.read_text(encoding="utf-8"))

        validate_authorization_isolation_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["samples"], 3)


if __name__ == "__main__":
    unittest.main()
