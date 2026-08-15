import base64
import copy
import gzip
import hashlib
import json
import unittest
from pathlib import Path

from context_control_plane.memory_ablation import (
    MemoryAblationError,
    benchmark_memory_ablation,
    validate_memory_ablation,
)


class M604MemoryAblationTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = benchmark_memory_ablation(samples=1000)

    def test_candidate_recall_has_significant_safe_gain(self) -> None:
        validate_memory_ablation(self.result, root=self.root)
        self.assertEqual(self.result["baseline_accuracy"], 0.6)
        self.assertEqual(self.result["candidate_accuracy"], 0.9)
        self.assertEqual(self.result["absolute_gain"], 0.3)
        self.assertGreater(self.result["absolute_gain"], 0)
        self.assertLess(self.result["paired_p_value"], 0.05)
        self.assertEqual(self.result["stale_decision_revivals"], 0)
        self.assertEqual(self.result["authority_violations"], 0)
        self.assertEqual(self.result["provider_503_state_failures"], 0)
        self.assertEqual(
            self.result["provider_admission_status"],
            "reference_fixture_conformance_only",
        )

    def test_all_samples_are_unique_persisted_paired_outcomes(self) -> None:
        outcomes = self.result["paired_outcomes"]
        self.assertEqual(len(outcomes), 1000)
        self.assertEqual(self.result["unique_case_count"], 1000)
        self.assertEqual(len({item["case_id"] for item in outcomes}), 1000)
        for item in outcomes:
            expected = item["expected_answer_sha256"]
            self.assertEqual(
                item["baseline_correct"], item["baseline_answer_sha256"] == expected
            )
            self.assertEqual(
                item["candidate_correct"],
                item["candidate_freshness"] == "current"
                and item["candidate_answer_sha256"] == expected,
            )
            if item["candidate_freshness"] == "stale":
                self.assertFalse(item["candidate_correct"])

    def test_receipt_artifact_binds_each_outcome_and_503_safety(self) -> None:
        artifact = self.result["recall_receipt_artifact"]
        compressed = base64.b64decode(artifact["payload_base64"], validate=True)
        receipts = json.loads(gzip.decompress(compressed))
        self.assertEqual(artifact["receipt_count"], 1001)
        self.assertEqual(len(receipts), 1001)
        self.assertEqual(
            {item["recall_receipt_sha256"] for item in self.result["paired_outcomes"]},
            {receipt["receipt_sha256"] for receipt in receipts[:-1]},
        )
        self.assertEqual(
            self.result["provider_503_receipt_sha256"],
            receipts[-1]["receipt_sha256"],
        )
        self.assertLess(len(compressed), artifact["uncompressed_bytes"])
        self.assertLess(len(compressed), 512_000)

    def test_resealed_receipt_digest_substitution_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.result)
        invalid["paired_outcomes"][0]["recall_receipt_sha256"] = "f" * 64
        invalid["outcomes_sha256"] = hashlib.sha256(
            json.dumps(
                invalid["paired_outcomes"],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(MemoryAblationError, "receipt"):
            validate_memory_ablation(invalid, root=self.root)

    def test_missing_503_receipt_is_rejected_after_artifact_reseal(self) -> None:
        invalid = copy.deepcopy(self.result)
        artifact = invalid["recall_receipt_artifact"]
        receipts = json.loads(
            gzip.decompress(base64.b64decode(artifact["payload_base64"], validate=True))
        )
        receipts = [
            receipt
            for receipt in receipts
            if receipt["request_id"] != "recall/m6-04/503"
        ]
        payload = json.dumps(
            receipts, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        artifact.update(
            {
                "artifact_ref": "artifact://sha256/"
                + hashlib.sha256(payload).hexdigest(),
                "receipt_count": len(receipts),
                "uncompressed_bytes": len(payload),
                "payload_base64": base64.b64encode(
                    gzip.compress(payload, compresslevel=9, mtime=0)
                ).decode("ascii"),
            }
        )
        with self.assertRaisesRegex(
            MemoryAblationError, "receipt artifact count|503"
        ):
            validate_memory_ablation(invalid, root=self.root)

    def test_provenance_hashes_are_recomputed_from_root(self) -> None:
        self.assertEqual(
            self.result["benchmark_sha256"],
            hashlib.sha256(
                (self.root / "context_control_plane/memory_ablation.py").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            self.result["subject_sha256"],
            hashlib.sha256(
                (self.root / "context_control_plane/recall_provider.py").read_bytes()
            ).hexdigest(),
        )
        invalid = copy.deepcopy(self.result)
        invalid["benchmark_sha256"] = "f" * 64
        with self.assertRaisesRegex(MemoryAblationError, "benchmark_sha256"):
            validate_memory_ablation(invalid, root=self.root)

    def test_safety_regression_vetoes_admission(self) -> None:
        for field in (
            "stale_decision_revivals",
            "authority_violations",
            "provider_503_state_failures",
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(self.result)
                invalid[field] = 1
                with self.assertRaises(MemoryAblationError):
                    validate_memory_ablation(invalid)

    def test_non_significant_gain_is_rejected(self) -> None:
        invalid = copy.deepcopy(self.result)
        invalid["paired_p_value"] = 0.2
        with self.assertRaises(MemoryAblationError):
            validate_memory_ablation(invalid)

    def test_paired_statistics_are_recomputed(self) -> None:
        invalid = copy.deepcopy(self.result)
        invalid["paired_improvements"] -= 1
        with self.assertRaisesRegex(MemoryAblationError, "paired"):
            validate_memory_ablation(invalid)

    def test_resealed_duplicate_or_false_candidate_outcome_is_rejected(self) -> None:
        for mutate in (
            lambda rows: rows[1].__setitem__("case_id", rows[0]["case_id"]),
            lambda rows: rows[-1].__setitem__("candidate_correct", True),
        ):
            invalid = copy.deepcopy(self.result)
            mutate(invalid["paired_outcomes"])
            invalid["outcomes_sha256"] = hashlib.sha256(
                json.dumps(
                    invalid["paired_outcomes"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            with self.assertRaises(MemoryAblationError):
                validate_memory_ablation(invalid, root=self.root)

    def test_unhashable_case_id_fails_closed_as_domain_error(self) -> None:
        invalid = copy.deepcopy(self.result)
        invalid["paired_outcomes"][0]["case_id"] = ["not", "a", "string"]
        with self.assertRaisesRegex(MemoryAblationError, "case_id"):
            validate_memory_ablation(invalid, root=self.root)


if __name__ == "__main__":
    unittest.main()
