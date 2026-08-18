"""M9-05 human governance measured receipt tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from context_control_plane.human_governance_benchmark import (
    HumanGovernanceBenchmarkError,
    benchmark_human_governance,
    validate_human_governance_benchmark,
)


def _reseal(receipt: dict) -> None:
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            {
                key: value
                for key, value in receipt.items()
                if key != "receipt_sha256"
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class M905HumanGovernanceBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.receipt = benchmark_human_governance(
            root=cls.root,
            iterations=5,
            generated_at="2026-08-17T09:00:00+08:00",
        )

    def test_veto_matrix_and_latency_samples_are_complete(self) -> None:
        receipt = validate_human_governance_benchmark(
            self.receipt, root=self.root
        )

        self.assertEqual(receipt["results"]["four_action_successes"], 20)
        for field in (
            "four_action_success_rate",
            "generic_bypass_rejection_rate",
            "session_denial_rate",
            "duplicate_replay_rate",
            "duplicate_drift_conflict_rate",
            "event_tamper_rejection_rate",
            "audit_binding_rejection_rate",
            "project_scope_rate",
            "invalid_input_rejection_rate",
        ):
            self.assertEqual(receipt["results"][field], 1.0)
        self.assertEqual(len(receipt["latency_samples_ms"]), 5)
        self.assertLess(receipt["latency_ms"]["p95"], 50.0)
        self.assertEqual(receipt["gate"], {"status": "passed", "failed_gates": []})

    def test_resealed_veto_or_latency_tamper_is_rejected(self) -> None:
        authority = copy.deepcopy(self.receipt)
        authority["results"]["authority_violations"] = 1
        _reseal(authority)
        with self.assertRaisesRegex(HumanGovernanceBenchmarkError, "gates"):
            validate_human_governance_benchmark(authority, root=self.root)

        latency = copy.deepcopy(self.receipt)
        latency["latency_samples_ms"][0] *= 2
        _reseal(latency)
        with self.assertRaisesRegex(HumanGovernanceBenchmarkError, "latency"):
            validate_human_governance_benchmark(latency, root=self.root)

    def test_invalid_benchmark_parameters_are_rejected(self) -> None:
        for iterations in (True, 0, 1001):
            with self.subTest(iterations=iterations), self.assertRaises(ValueError):
                benchmark_human_governance(root=self.root, iterations=iterations)

    def test_provenance_binds_the_governance_audit_event_schema(self) -> None:
        expected = hashlib.sha256(
            (
                self.root
                / "schemas/m9-05/governance-authorization-audit-event.schema.json"
            ).read_bytes()
        ).hexdigest()
        self.assertEqual(
            self.receipt["provenance"]["authorization_audit_schema_sha256"],
            expected,
        )

    def test_committed_receipt_matches_current_implementation(self) -> None:
        path = (
            self.root
            / "experiments/evidence/m9-05-human-governance-results.json"
        )
        receipt = json.loads(path.read_text(encoding="utf-8"))

        validate_human_governance_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["parameters"]["iterations"], 1000)
        self.assertEqual(receipt["results"]["four_action_successes"], 4000)


if __name__ == "__main__":
    unittest.main()
