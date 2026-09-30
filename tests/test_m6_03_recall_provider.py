import copy
import hashlib
import inspect
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.recall_provider import (
    InMemoryRecallProvider,
    RecallContractError,
    RecallCoordinator,
    UnavailableRecallProvider,
    validate_recall_receipt,
)


class M603RecallProviderTests(unittest.TestCase):
    root = Path(__file__).parents[1]

    @staticmethod
    def reseal(receipt: dict) -> None:
        unsigned = copy.deepcopy(receipt)
        unsigned.pop("receipt_sha256")
        receipt["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def request(self) -> dict:
        return {
            "request_id": "recall/m6-03/current-decision",
            "task_id": "M6-03",
            "query": "current retrieval decision",
            "max_candidates": 2,
            "max_returned_bytes": 512,
        }

    def records(self) -> list[dict]:
        return [
            {
                "record_id": "memory/current-retrieval-decision",
                "content": "Current retrieval decision uses bounded receipts.",
                "tags": ["current", "retrieval", "decision"],
                "observed_at": "2026-08-15T05:00:00Z",
                "valid_until": "2026-08-16T05:00:00Z",
                "source_ref": "event://decision/m6-01",
                "source_sha256": "1" * 64,
            },
            {
                "record_id": "memory/old-retrieval-decision",
                "content": "Old retrieval decision used unbounded scans.",
                "tags": ["old", "retrieval", "decision"],
                "observed_at": "2026-08-01T05:00:00Z",
                "valid_until": "2026-08-02T05:00:00Z",
                "source_ref": "event://decision/old",
                "source_sha256": "2" * 64,
            },
        ]

    def test_provider_returns_bounded_candidates_without_authority(self) -> None:
        request = self.request()
        coordinator = RecallCoordinator(InMemoryRecallProvider(self.records()))
        receipt = coordinator.retrieve(
            request, observed_at="2026-08-15T05:30:00Z"
        )
        self.assertIn(
            "expected_request",
            inspect.signature(validate_recall_receipt).parameters,
        )
        validate_recall_receipt(receipt, expected_request=request)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(
            receipt["query_sha256"],
            hashlib.sha256(request["query"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(receipt["max_candidates"], request["max_candidates"])
        self.assertEqual(
            receipt["max_returned_bytes"], request["max_returned_bytes"]
        )
        self.assertEqual(
            receipt["request_sha256"],
            hashlib.sha256(
                json.dumps(
                    request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
        self.assertLessEqual(len(receipt["candidates"]), 2)
        self.assertLessEqual(receipt["returned_bytes"], 512)
        self.assertTrue(all(not item["active_state_authority"] for item in receipt["candidates"]))
        self.assertFalse(receipt["state_write_authority"])

    def test_stale_candidate_is_explicit_and_never_promoted(self) -> None:
        request = self.request()
        request["query"] = "old retrieval decision"
        receipt = RecallCoordinator(InMemoryRecallProvider(self.records())).retrieve(
            request, observed_at="2026-08-15T05:30:00Z"
        )
        self.assertEqual(receipt["candidates"][0]["freshness"], "stale")
        self.assertFalse(receipt["candidates"][0]["active_state_authority"])

    def test_provider_503_degrades_without_state_impact(self) -> None:
        receipt = RecallCoordinator(UnavailableRecallProvider("503")).retrieve(
            self.request(), observed_at="2026-08-15T05:30:00Z"
        )
        self.assertEqual(receipt["status"], "degraded")
        self.assertEqual(receipt["provider_error_code"], "503")
        self.assertEqual(receipt["candidates"], [])
        self.assertFalse(receipt["state_write_authority"])
        validate_recall_receipt(receipt)

    def test_receipt_validation_is_strict(self) -> None:
        receipt = RecallCoordinator(InMemoryRecallProvider(self.records())).retrieve(
            self.request(), observed_at="2026-08-15T05:30:00Z"
        )
        invalid = copy.deepcopy(receipt)
        invalid["active_task"] = "M6-03"
        with self.assertRaises(RecallContractError):
            validate_recall_receipt(invalid)

    def test_receipt_validator_recomputes_candidate_freshness(self) -> None:
        receipt = RecallCoordinator(InMemoryRecallProvider(self.records())).retrieve(
            self.request(), observed_at="2026-08-15T05:30:00Z"
        )
        receipt["candidates"][0]["freshness"] = "stale"
        self.reseal(receipt)
        with self.assertRaisesRegex(RecallContractError, "freshness"):
            validate_recall_receipt(receipt)

        invalid = copy.deepcopy(receipt)
        invalid["candidates"][0]["active_state_authority"] = True
        with self.assertRaises(RecallContractError):
            validate_recall_receipt(invalid)

    def test_resealed_receipt_rejects_candidate_observed_in_the_future(self) -> None:
        receipt = RecallCoordinator(InMemoryRecallProvider(self.records())).retrieve(
            self.request(), observed_at="2026-08-15T05:30:00Z"
        )
        receipt["candidates"][0]["observed_at"] = "2026-08-15T06:00:00Z"
        self.reseal(receipt)

        with self.assertRaisesRegex(RecallContractError, "postdates"):
            validate_recall_receipt(receipt)

        future_records = self.records()
        future_records[0]["observed_at"] = "2026-08-15T06:00:00Z"
        with self.assertRaisesRegex(RecallContractError, "postdates"):
            RecallCoordinator(InMemoryRecallProvider(future_records)).retrieve(
                self.request(), observed_at="2026-08-15T05:30:00Z"
            )

    def test_resealed_receipt_rejects_inverted_candidate_validity(self) -> None:
        receipt = RecallCoordinator(InMemoryRecallProvider(self.records())).retrieve(
            self.request(), observed_at="2026-08-15T05:30:00Z"
        )
        receipt["candidates"][0]["valid_until"] = "2026-08-14T05:00:00Z"
        receipt["candidates"][0]["freshness"] = "stale"
        self.reseal(receipt)

        with self.assertRaisesRegex(RecallContractError, "precedes"):
            validate_recall_receipt(receipt)

    def test_expected_request_rejects_self_consistent_replacement(self) -> None:
        request = self.request()
        receipt = RecallCoordinator(InMemoryRecallProvider(self.records())).retrieve(
            request, observed_at="2026-08-15T05:30:00Z"
        )
        replacement_request = {
            **request,
            "query": "replacement decision",
            "max_candidates": 3,
            "max_returned_bytes": 1024,
        }
        replacement = copy.deepcopy(receipt)
        self.assertTrue(
            {
                "query_sha256",
                "max_candidates",
                "max_returned_bytes",
                "request_sha256",
            }
            <= set(replacement)
        )
        replacement.update(
            query_sha256=hashlib.sha256(
                replacement_request["query"].encode("utf-8")
            ).hexdigest(),
            max_candidates=replacement_request["max_candidates"],
            max_returned_bytes=replacement_request["max_returned_bytes"],
            request_sha256=hashlib.sha256(
                json.dumps(
                    replacement_request,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
        )
        self.reseal(replacement)

        validate_recall_receipt(replacement)
        with self.assertRaisesRegex(RecallContractError, "expected request"):
            validate_recall_receipt(replacement, expected_request=request)

    def test_resealed_receipt_rejects_duplicate_candidates_and_request_overruns(self) -> None:
        receipt = RecallCoordinator(InMemoryRecallProvider(self.records())).retrieve(
            self.request(), observed_at="2026-08-15T05:30:00Z"
        )

        duplicate = copy.deepcopy(receipt)
        duplicate["max_candidates"] = 3
        duplicate["candidates"].append(copy.deepcopy(duplicate["candidates"][0]))
        duplicate["returned_bytes"] += len(
            duplicate["candidates"][0]["content"].encode("utf-8")
        )
        self.reseal(duplicate)
        with self.assertRaisesRegex(RecallContractError, "unique"):
            validate_recall_receipt(duplicate)

        too_many = copy.deepcopy(receipt)
        too_many["max_candidates"] = 1
        self.reseal(too_many)
        with self.assertRaisesRegex(RecallContractError, "max_candidates"):
            validate_recall_receipt(too_many)

        too_large = copy.deepcopy(receipt)
        too_large["max_returned_bytes"] = receipt["returned_bytes"] - 1
        self.reseal(too_large)
        with self.assertRaisesRegex(RecallContractError, "max_returned_bytes"):
            validate_recall_receipt(too_large)

    def test_schema_requires_request_binding_and_unique_candidates(self) -> None:
        schema = json.loads(
            (self.root / "schemas/m6-03/recall-receipt.schema.json").read_text()
        )
        receipt = RecallCoordinator(InMemoryRecallProvider(self.records())).retrieve(
            self.request(), observed_at="2026-08-15T05:30:00Z"
        )
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        validator.validate(receipt)

        for field in (
            "query_sha256",
            "max_candidates",
            "max_returned_bytes",
            "request_sha256",
        ):
            with self.subTest(field=field):
                invalid = copy.deepcopy(receipt)
                invalid.pop(field, None)
                with self.assertRaises(ValidationError):
                    validator.validate(invalid)

        duplicate = copy.deepcopy(receipt)
        duplicate["candidates"].append(copy.deepcopy(duplicate["candidates"][0]))
        duplicate["returned_bytes"] += len(
            duplicate["candidates"][0]["content"].encode("utf-8")
        )
        with self.assertRaises(ValidationError):
            validator.validate(duplicate)


if __name__ == "__main__":
    unittest.main()
