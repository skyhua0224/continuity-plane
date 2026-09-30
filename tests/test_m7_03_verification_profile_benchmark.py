import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.verification_profile_benchmark import (
    run_verification_profile_benchmark,
    validate_verification_profile_benchmark,
)


class M703VerificationProfileBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]

    def test_fixed_matrix_has_no_false_decisions_or_authority(self) -> None:
        receipt = run_verification_profile_benchmark(iterations=1000)

        self.assertEqual(receipt["iterations"], 1000)
        self.assertEqual(
            set(receipt["fixture_profile_sha256s"]),
            {
                "verification/alkaidlab/default",
                "verification/portable-python-library/default",
            },
        )
        self.assertTrue(
            all(len(digest) == 64 for digest in receipt["fixture_profile_sha256s"].values())
        )
        self.assertEqual(receipt["scenario_counts"], {
            "conditional-not-met": 250,
            "optional-failed": 250,
            "required-capability-unavailable": 250,
            "required-passed": 250,
        })
        self.assertEqual(receipt["overall_counts"], {"blocked": 250, "satisfied": 750})
        self.assertEqual(receipt["optional_failed_gate_count"], 250)
        self.assertEqual(receipt["false_allow_count"], 0)
        self.assertEqual(receipt["false_deny_count"], 0)
        self.assertEqual(receipt["replay_mismatch_count"], 0)
        self.assertEqual(receipt["external_service_calls"], 0)
        self.assertEqual(receipt["state_write_authority_count"], 0)
        self.assertEqual(receipt["completion_authority_count"], 0)
        validate_verification_profile_benchmark(receipt)

    def test_committed_receipt_and_schema_match_fresh_replay(self) -> None:
        committed = json.loads(
            (self.root / "experiments/evidence/m7-03-verification-profile-results.json").read_text()
        )
        schema = json.loads(
            (self.root / "schemas/m7-03/verification-profile-benchmark.schema.json").read_text()
        )

        self.assertEqual(committed, run_verification_profile_benchmark(iterations=1000))
        Draft202012Validator(schema).validate(committed)
        validate_verification_profile_benchmark(committed)


if __name__ == "__main__":
    unittest.main()
