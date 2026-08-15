import copy
import hashlib
import json
import unittest

from context_control_plane.reviewer_adapter import (
    DeferredReviewerAdapter,
    LocalReviewerAdapter,
    ReviewContractError,
    ReviewCoordinator,
    TimeoutReviewerAdapter,
    validate_review_receipt,
)


def _reseal(receipt: dict) -> dict:
    unsigned = copy.deepcopy(receipt)
    unsigned.pop("receipt_sha256", None)
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return receipt


class M605ReviewerAdapterTests(unittest.TestCase):
    def request(self) -> dict:
        return {
            "request_id": "review/m6-05/retrieval-contract",
            "task_id": "M6-05",
            "execution_packet_ref": "artifact://sha256/" + "1" * 64,
            "artifact_refs": ["artifact://sha256/" + "2" * 64],
            "max_output_bytes": 2048,
            "deadline_at": "2026-08-15T06:05:00Z",
        }

    def findings(self) -> list[dict]:
        return [
            {
                "finding_id": "finding/retrieval-budget",
                "severity": "medium",
                "summary": "Verify the scan budget before accepting the receipt.",
                "evidence_refs": ["artifact://sha256/" + "2" * 64],
                "candidate_only": True,
            }
        ]

    def test_local_reviewer_returns_candidate_findings(self) -> None:
        receipt = ReviewCoordinator(LocalReviewerAdapter(self.findings())).review(
            self.request(), observed_at="2026-08-15T06:00:00Z"
        )
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(len(receipt["findings"]), 1)
        self.assertTrue(receipt["findings"][0]["candidate_only"])
        self.assertEqual(
            receipt["execution_packet_ref"], self.request()["execution_packet_ref"]
        )
        self.assertEqual(receipt["artifact_refs"], self.request()["artifact_refs"])
        self.assertEqual(receipt["max_output_bytes"], self.request()["max_output_bytes"])
        self.assertRegex(receipt["request_sha256"], r"^[0-9a-f]{64}$")
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["completion_authority"])
        validate_review_receipt(receipt, expected_request=self.request())

    def test_external_submission_is_non_blocking_and_pending(self) -> None:
        receipt = ReviewCoordinator(DeferredReviewerAdapter()).review(
            self.request(), observed_at="2026-08-15T06:00:00Z"
        )
        self.assertEqual(receipt["status"], "pending")
        self.assertTrue(receipt["async_degraded"])
        self.assertEqual(receipt["findings"], [])
        self.assertIsNotNone(receipt["operation_ref"])

    def test_timeout_degrades_to_async_operation(self) -> None:
        receipt = ReviewCoordinator(TimeoutReviewerAdapter()).review(
            self.request(), observed_at="2026-08-15T06:06:00Z"
        )
        self.assertEqual(receipt["status"], "timed_out")
        self.assertTrue(receipt["async_degraded"])
        self.assertFalse(receipt["state_write_authority"])
        validate_review_receipt(receipt)

    def test_rejects_finding_that_claims_authority(self) -> None:
        finding = self.findings()[0]
        finding["candidate_only"] = False
        with self.assertRaises(ReviewContractError):
            LocalReviewerAdapter([finding])

    def test_finding_evidence_must_be_requested(self) -> None:
        finding = self.findings()[0]
        finding["evidence_refs"] = ["artifact://sha256/" + "3" * 64]
        with self.assertRaisesRegex(ReviewContractError, "requested artifact"):
            ReviewCoordinator(LocalReviewerAdapter([finding])).review(
                self.request(), observed_at="2026-08-15T06:00:00Z"
            )

    def test_output_budget_counts_the_complete_finding_payload(self) -> None:
        request = self.request()
        request["max_output_bytes"] = 1
        finding = self.findings()[0]
        finding["summary"] = "x"
        with self.assertRaisesRegex(ReviewContractError, "budget"):
            ReviewCoordinator(LocalReviewerAdapter([finding])).review(
                request, observed_at="2026-08-15T06:00:00Z"
            )

    def test_status_observation_obeys_deadline(self) -> None:
        for adapter in (
            LocalReviewerAdapter(self.findings()),
            DeferredReviewerAdapter(),
        ):
            with self.subTest(adapter=adapter.provider_id), self.assertRaisesRegex(
                ReviewContractError, "deadline"
            ):
                ReviewCoordinator(adapter).review(
                    self.request(), observed_at="2026-08-15T06:05:01Z"
                )

        with self.assertRaisesRegex(ReviewContractError, "deadline"):
            ReviewCoordinator(TimeoutReviewerAdapter()).review(
                self.request(), observed_at="2026-08-15T06:04:59Z"
            )

        completed = ReviewCoordinator(
            LocalReviewerAdapter(self.findings())
        ).review(self.request(), observed_at="2026-08-15T06:05:00Z")
        timed_out = ReviewCoordinator(TimeoutReviewerAdapter()).review(
            self.request(), observed_at="2026-08-15T06:05:00Z"
        )
        validate_review_receipt(completed)
        validate_review_receipt(timed_out)

    def test_resealed_request_budget_and_deadline_errors_fail_closed(self) -> None:
        receipt = ReviewCoordinator(LocalReviewerAdapter(self.findings())).review(
            self.request(), observed_at="2026-08-15T06:00:00Z"
        )
        mutations = (
            lambda value: value.update(
                execution_packet_ref="artifact://sha256/" + "4" * 64
            ),
            lambda value: value.update(
                artifact_refs=["artifact://sha256/" + "3" * 64]
            ),
            lambda value: value.update(max_output_bytes=value["returned_bytes"] - 1),
            lambda value: value.update(observed_at="2026-08-15T06:05:01Z"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(receipt)
                mutation(invalid)
                with self.assertRaises(ReviewContractError):
                    validate_review_receipt(_reseal(invalid))

    def test_expected_request_rejects_self_consistent_replacement(self) -> None:
        request = self.request()
        receipt = ReviewCoordinator(LocalReviewerAdapter(self.findings())).review(
            request, observed_at="2026-08-15T06:00:00Z"
        )
        replacement = copy.deepcopy(receipt)
        replacement["execution_packet_ref"] = "artifact://sha256/" + "4" * 64
        replacement_request = {
            **request,
            "execution_packet_ref": replacement["execution_packet_ref"],
        }
        payload = json.dumps(
            replacement_request,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        replacement["request_sha256"] = hashlib.sha256(payload).hexdigest()
        _reseal(replacement)
        validate_review_receipt(replacement)
        with self.assertRaisesRegex(ReviewContractError, "expected request"):
            validate_review_receipt(replacement, expected_request=request)

    def test_receipt_is_strict(self) -> None:
        receipt = ReviewCoordinator(LocalReviewerAdapter(self.findings())).review(
            self.request(), observed_at="2026-08-15T06:00:00Z"
        )
        invalid = copy.deepcopy(receipt)
        invalid["accepted"] = True
        with self.assertRaises(ReviewContractError):
            validate_review_receipt(invalid)


if __name__ == "__main__":
    unittest.main()
