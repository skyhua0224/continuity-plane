"""M8-01 DeepSeek runnable checkpoint evidence contract."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.deepseek_checkpoint_receipt import (
    DeepSeekCheckpointReceiptError,
    compose_deepseek_checkpoint_receipt,
    validate_deepseek_checkpoint_receipt,
)


class M801DeepSeekCheckpointReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        schema_path = (
            cls.root / "schemas/m8-01/deepseek-checkpoint-receipt.schema.json"
        )
        cls.schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(
            cls.schema, format_checker=FormatChecker()
        )
        cls.report = {
            "numTotalTestSuites": 1,
            "numPassedTestSuites": 1,
            "numFailedTestSuites": 0,
            "numPendingTestSuites": 0,
            "numTotalTests": 2,
            "numPassedTests": 2,
            "numFailedTests": 0,
            "numPendingTests": 0,
            "numTodoTests": 0,
            "success": True,
            "testResults": [
                {
                    "name": (
                        "/source/"
                        "packages/session/session-checkpoint-policy/tests/"
                        "crash-recovery.e2e.ts"
                    ),
                    "status": "passed",
                    "assertionResults": [
                        {
                            "title": (
                                "persists the complete request before model dispatch"
                            ),
                            "status": "passed",
                        },
                        {
                            "title": (
                                "persists tool intent before a side effect and repairs "
                                "its missing result as unknown"
                            ),
                            "status": "passed",
                        },
                    ],
                }
            ],
        }

    def compose(self) -> dict:
        return compose_deepseek_checkpoint_receipt(
            self.report,
            generated_at="2026-08-16T15:00:00+08:00",
            source_revision="47f943859bef60e4160492346772ded9b24f765a",
            source_tree="f904efab9ef435201d6ba4da88a34d6366568272",
            source_test_blob_sha256=(
                "f26537de8fe8905b3fc57bec221c1428145a844f90812fe427ece64abaa248de"
            ),
            report_sha256="1" * 64,
            node_version="22.23.2",
            package_manager="pnpm@11.7.0",
            platform="linux",
        )

    def test_composer_records_real_crash_recovery_without_provider_authority(
        self,
    ) -> None:
        receipt = self.compose()

        validate_deepseek_checkpoint_receipt(receipt)
        self.validator.validate(receipt)
        self.assertEqual(receipt["checkpoint_fixture_runs"], 2)
        self.assertEqual(receipt["sigkill_count"], 2)
        self.assertEqual(receipt["recovery_passes"], 2)
        self.assertEqual(receipt["signals"], [
            "request-interrupted",
            "TOOL_OUTCOME_UNKNOWN",
        ])
        self.assertEqual(receipt["external_provider_invocations"], 0)
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["provider_native_authority"])

    def test_validator_rejects_failed_counts_authority_and_digest_forgery(self) -> None:
        receipt = self.compose()
        changes = (
            {"recovery_passes": 1},
            {"external_provider_invocations": 1},
            {"state_write_authority": True},
            {"receipt_sha256": "2" * 64},
        )
        for change in changes:
            with self.subTest(change=change):
                forged = copy.deepcopy(receipt)
                forged.update(change)
                with self.assertRaises(DeepSeekCheckpointReceiptError):
                    validate_deepseek_checkpoint_receipt(forged)

    def test_composer_rejects_an_unrelated_two_test_report(self) -> None:
        report = copy.deepcopy(self.report)
        report["testResults"][0]["assertionResults"][1]["title"] = (
            "an unrelated passing test"
        )

        with self.assertRaises(DeepSeekCheckpointReceiptError):
            compose_deepseek_checkpoint_receipt(
                report,
                generated_at="2026-08-16T15:00:00+08:00",
                source_revision="47f943859bef60e4160492346772ded9b24f765a",
                source_tree="f904efab9ef435201d6ba4da88a34d6366568272",
                source_test_blob_sha256=(
                    "f26537de8fe8905b3fc57bec221c1428145a844f90812fe427ece64abaa248de"
                ),
                report_sha256="1" * 64,
                node_version="22.23.2",
                package_manager="pnpm@11.7.0",
                platform="linux",
            )

    def test_committed_receipt_is_strict_and_bound_to_the_provider_oracle(
        self,
    ) -> None:
        receipt_path = (
            self.root
            / "experiments/evidence/m8-01-deepseek-checkpoint-receipt.json"
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        oracle_fixture = json.loads(
            (
                self.root / "experiments/fixtures/m8-01-provider-oracles.json"
            ).read_text(encoding="utf-8")
        )
        oracle = next(
            item
            for item in oracle_fixture["oracles"]
            if item["oracle_id"] == "deepseek-tool-outcome-unknown"
        )

        validate_deepseek_checkpoint_receipt(receipt)
        self.validator.validate(receipt)
        self.assertFalse(self.schema["additionalProperties"])
        self.assertEqual(
            set(self.schema["required"]), set(self.schema["properties"])
        )
        self.assertEqual(receipt["source_revision"], oracle["source_revision"])
        self.assertEqual(receipt["source_tree"], oracle["source_tree"])
        unsigned = dict(receipt)
        digest = unsigned.pop("receipt_sha256")
        canonical = json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())

        forged = copy.deepcopy(receipt)
        forged["unexpected"] = True
        with self.assertRaises(ValidationError):
            self.validator.validate(forged)


if __name__ == "__main__":
    unittest.main()
