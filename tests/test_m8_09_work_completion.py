"""M8-09 atomic Work completion and claim release contract."""

from __future__ import annotations

import copy
import unittest

from context_control_plane.shared_work_ledger import (
    ClaimLifecycleError,
    WorkLedger,
)


NOW = "2026-08-17T10:00:00+00:00"


def _scope() -> dict[str, str]:
    return {"scope_kind": "capability", "scope_ref": "campaign/work-a"}


def _ledger() -> WorkLedger:
    return WorkLedger(
        project_id="project-m8-09",
        project_revision=10,
        works=[
            {
                "work_id": "work-a",
                "status": "ready",
                "identity_key": "identity-a",
                "scope_refs": [_scope()],
                "revision": 3,
                "evidence_ids": [],
            }
        ],
        max_ttl_ms=60_000,
    )


def _acquire(ledger: WorkLedger) -> dict:
    return ledger.acquire_claim(
        work_id="work-a",
        actor_ref="actor-executor",
        expected_project_revision=10,
        observed_at=NOW,
        requested_ttl_ms=30_000,
        claim_id="claim-work-a",
        scope_owners=[_scope()],
    )


def _completion_arguments(acquire: dict) -> dict:
    claim = acquire["claim"]
    return {
        "work_id": "work-a",
        "claim_id": claim["claim_id"],
        "actor_ref": "actor-executor",
        "expected_project_revision": acquire["project_revision"],
        "expected_work_revision": 3,
        "expected_claim_revision": claim["claim_revision"],
        "lease_epoch": claim["lease_epoch"],
        "fence": claim["lease_epoch"],
        "observed_at": "2026-08-17T10:00:01+00:00",
        "evidence_ids": ["evidence-work-a"],
        "verification_decision_sha256": "a" * 64,
        "claim_evidence_verdict_sha256": "b" * 64,
    }


class M809WorkCompletionTests(unittest.TestCase):
    def test_completion_atomically_closes_work_and_claim(self) -> None:
        ledger = _ledger()
        acquired = _acquire(ledger)

        completed = ledger.complete_work(**_completion_arguments(acquired))

        snapshot = ledger.snapshot()
        work = snapshot["works"][0]
        claim = snapshot["claims"][0]
        self.assertEqual(completed["operation"], "complete_work")
        self.assertEqual(completed["project_revision"], 12)
        self.assertEqual(work["status"], "completed")
        self.assertEqual(work["revision"], 4)
        self.assertEqual(work["evidence_ids"], ["evidence-work-a"])
        self.assertEqual(claim["status"], "released")
        self.assertEqual(claim["close_reason"], "worker_release")
        self.assertEqual(
            completed["verification_decision_sha256"], "a" * 64
        )
        self.assertEqual(
            completed["claim_evidence_verdict_sha256"], "b" * 64
        )

    def test_stale_revision_and_fence_reject_without_partial_completion(self) -> None:
        for field, value, reason in (
            ("expected_project_revision", 10, "stale_revision"),
            ("expected_work_revision", 2, "work_revision_mismatch"),
            ("expected_claim_revision", 2, "claim_revision_mismatch"),
            ("lease_epoch", 2, "lease_epoch_mismatch"),
            ("fence", 2, "fence_mismatch"),
        ):
            with self.subTest(field=field):
                ledger = _ledger()
                acquired = _acquire(ledger)
                arguments = _completion_arguments(acquired)
                arguments[field] = value
                before = copy.deepcopy(ledger.snapshot())

                with self.assertRaises(ClaimLifecycleError) as caught:
                    ledger.complete_work(**arguments)

                self.assertEqual(caught.exception.code, reason)
                self.assertEqual(ledger.snapshot(), before)

    def test_completion_requires_current_verification_and_evidence_bindings(self) -> None:
        for field, value, reason in (
            ("evidence_ids", [], "missing_completion_evidence"),
            (
                "verification_decision_sha256",
                "not-a-digest",
                "invalid_sha256",
            ),
            (
                "claim_evidence_verdict_sha256",
                "not-a-digest",
                "invalid_sha256",
            ),
        ):
            with self.subTest(field=field):
                ledger = _ledger()
                acquired = _acquire(ledger)
                arguments = _completion_arguments(acquired)
                arguments[field] = value
                before = copy.deepcopy(ledger.snapshot())

                with self.assertRaises(ClaimLifecycleError) as caught:
                    ledger.complete_work(**arguments)

                self.assertEqual(caught.exception.code, reason)
                self.assertEqual(ledger.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
