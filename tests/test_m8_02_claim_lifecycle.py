import copy
import hashlib
import json
import unittest
from datetime import datetime, timedelta

from context_control_plane.shared_work_ledger import (
    ClaimLifecycleError,
    WorkLedger,
)

BASE_TIME = "2026-08-16T10:00:00+00:00"


def _scope(ref: str) -> dict[str, str]:
    return {"scope_kind": "capability", "scope_ref": ref}


def _time_after(milliseconds: int) -> str:
    instant = datetime.fromisoformat(BASE_TIME) + timedelta(milliseconds=milliseconds)
    return instant.isoformat()


def _ledger(*works: dict, project_revision: int = 7) -> WorkLedger:
    return WorkLedger(
        project_id="project-m8-02",
        project_revision=project_revision,
        works=list(works)
        or [
            {
                "work_id": "work-a",
                "status": "ready",
                "identity_key": "identity-a",
                "scope_refs": [_scope("capability/a")],
            }
        ],
        max_ttl_ms=1_000,
    )


def _claim_args(
    *,
    work_id: str = "work-a",
    actor_ref: str = "actor-a",
    expected_project_revision: int = 7,
    observed_at: str = BASE_TIME,
    requested_ttl_ms: int = 500,
    claim_id: str = "claim-a",
    scope_ref: str = "capability/a",
) -> dict:
    return {
        "work_id": work_id,
        "actor_ref": actor_ref,
        "expected_project_revision": expected_project_revision,
        "observed_at": observed_at,
        "requested_ttl_ms": requested_ttl_ms,
        "claim_id": claim_id,
        "scope_owners": [_scope(scope_ref)],
    }


class M802ClaimLifecycleTests(unittest.TestCase):
    def test_acquire_assigns_fenced_claim_and_hash_bound_receipt(self):
        ledger = _ledger()

        result = ledger.acquire_claim(**_claim_args())
        claim = result["claim"]

        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["operation"], "acquire")
        self.assertEqual(claim["status"], "active")
        self.assertEqual(claim["claim_revision"], 1)
        self.assertEqual(claim["lease_epoch"], 1)
        self.assertEqual(claim["expected_project_revision"], 8)
        self.assertEqual(claim["lease_expires_at"], "2026-08-16T10:00:00.500000+00:00")
        self.assertEqual(claim["last_heartbeat_at"], BASE_TIME)
        self.assertIsNone(claim["closed_at"])
        self.assertIsNone(claim["closed_by_ref"])
        self.assertIsNone(claim["close_reason"])
        self.assertIsNone(claim["reclaimed_from_claim_id"])
        self.assertEqual(
            set(claim),
            {
                "claim_id",
                "work_id",
                "actor_ref",
                "status",
                "expected_project_revision",
                "claimed_at",
                "lease_expires_at",
                "released_at",
                "scope_owners",
                "claim_revision",
                "lease_epoch",
                "last_heartbeat_at",
                "closed_at",
                "closed_by_ref",
                "close_reason",
                "reclaimed_from_claim_id",
            },
        )
        receipt = copy.deepcopy(result["receipt"])
        self.assertEqual(receipt["fence"], claim["lease_epoch"])
        digest = receipt.pop("receipt_sha256")
        canonical = json.dumps(
            receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())

    def test_same_input_has_same_transition_and_receipt(self):
        first = _ledger().acquire_claim(**_claim_args())
        second = _ledger().acquire_claim(**_claim_args())

        self.assertEqual(first, second)

    def test_invalid_ttl_is_rejected_without_state_change(self):
        ledger = _ledger()
        before = ledger.snapshot()

        with self.assertRaisesRegex(ClaimLifecycleError, "ttl_out_of_bounds") as caught:
            ledger.acquire_claim(**_claim_args(requested_ttl_ms=1_001))

        self.assertEqual(caught.exception.code, "ttl_out_of_bounds")
        self.assertEqual(ledger.snapshot(), before)

    def test_second_active_claim_for_same_work_is_rejected_without_mutation(self):
        ledger = _ledger()
        first = ledger.acquire_claim(**_claim_args())
        before = ledger.snapshot()

        with self.assertRaisesRegex(ClaimLifecycleError, "work_already_claimed"):
            ledger.acquire_claim(
                **_claim_args(
                    actor_ref="actor-b",
                    expected_project_revision=first["claim"][
                        "expected_project_revision"
                    ],
                    claim_id="claim-b",
                )
            )

        self.assertEqual(ledger.snapshot(), before)

    def test_terminal_work_is_blocked_before_claim(self):
        ledger = _ledger(
            {
                "work_id": "work-terminal",
                "status": "completed",
                "identity_key": "identity-terminal",
                "scope_refs": [_scope("capability/terminal")],
            }
        )
        before = ledger.snapshot()

        with self.assertRaisesRegex(ClaimLifecycleError, "terminal_work"):
            ledger.acquire_claim(
                **_claim_args(
                    work_id="work-terminal",
                    claim_id="claim-terminal",
                    scope_ref="capability/terminal",
                )
            )

        self.assertEqual(ledger.snapshot(), before)

    def test_terminal_duplicate_identity_is_blocked_before_claim(self):
        ledger = _ledger(
            {
                "work_id": "work-done",
                "status": "completed",
                "identity_key": "same-identity",
                "scope_refs": [_scope("capability/done")],
            },
            {
                "work_id": "work-copy",
                "status": "ready",
                "identity_key": "same-identity",
                "scope_refs": [_scope("capability/copy")],
            },
        )
        before = ledger.snapshot()

        with self.assertRaisesRegex(ClaimLifecycleError, "duplicate_terminal_work"):
            ledger.acquire_claim(
                **_claim_args(
                    work_id="work-copy",
                    claim_id="claim-copy",
                    scope_ref="capability/copy",
                )
            )

        self.assertEqual(ledger.snapshot(), before)

    def test_active_duplicate_identity_is_blocked_after_first_claim(self):
        ledger = _ledger(
            {
                "work_id": "work-one",
                "status": "ready",
                "identity_key": "same-identity",
                "scope_refs": [_scope("capability/one")],
            },
            {
                "work_id": "work-two",
                "status": "ready",
                "identity_key": "same-identity",
                "scope_refs": [_scope("capability/two")],
            },
        )
        first = ledger.acquire_claim(
            **_claim_args(
                work_id="work-one",
                claim_id="claim-one",
                scope_ref="capability/one",
            )
        )
        before = ledger.snapshot()

        with self.assertRaisesRegex(ClaimLifecycleError, "duplicate_work_identity"):
            ledger.acquire_claim(
                **_claim_args(
                    work_id="work-two",
                    actor_ref="actor-b",
                    expected_project_revision=first["claim"][
                        "expected_project_revision"
                    ],
                    claim_id="claim-two",
                    scope_ref="capability/two",
                )
            )

        self.assertEqual(ledger.snapshot(), before)

    def test_overlapping_scope_is_rejected_for_distinct_work(self):
        ledger = _ledger(
            {
                "work_id": "work-one",
                "status": "ready",
                "identity_key": "identity-one",
                "scope_refs": [_scope("capability/shared")],
            },
            {
                "work_id": "work-two",
                "status": "ready",
                "identity_key": "identity-two",
                "scope_refs": [_scope("capability/shared")],
            },
        )
        first = ledger.acquire_claim(
            **_claim_args(
                work_id="work-one",
                claim_id="claim-one",
                scope_ref="capability/shared",
            )
        )
        before = ledger.snapshot()

        with self.assertRaisesRegex(ClaimLifecycleError, "scope_overlap"):
            ledger.acquire_claim(
                **_claim_args(
                    work_id="work-two",
                    actor_ref="actor-b",
                    expected_project_revision=first["claim"][
                        "expected_project_revision"
                    ],
                    claim_id="claim-two",
                    scope_ref="capability/shared",
                )
            )

        self.assertEqual(ledger.snapshot(), before)

    def test_disjoint_claims_rebase_authority_cursor_without_changing_fence(self):
        ledger = _ledger(
            {
                "work_id": "work-one",
                "status": "ready",
                "identity_key": "identity-one",
                "scope_refs": [_scope("capability/one")],
            },
            {
                "work_id": "work-two",
                "status": "ready",
                "identity_key": "identity-two",
                "scope_refs": [_scope("capability/two")],
            },
        )
        first = ledger.acquire_claim(
            **_claim_args(
                work_id="work-one",
                claim_id="claim-one",
                scope_ref="capability/one",
            )
        )["claim"]
        ledger.acquire_claim(
            **_claim_args(
                work_id="work-two",
                actor_ref="actor-b",
                expected_project_revision=first["expected_project_revision"],
                claim_id="claim-two",
                scope_ref="capability/two",
            )
        )

        first_after = next(
            claim
            for claim in ledger.snapshot()["claims"]
            if claim["claim_id"] == "claim-one"
        )
        self.assertEqual(first_after["expected_project_revision"], 9)
        self.assertEqual(first_after["claim_revision"], first["claim_revision"])
        self.assertEqual(first_after["lease_epoch"], first["lease_epoch"])
        verdict = ledger.dispatch_gate(
            claim_id=first_after["claim_id"],
            work_id=first_after["work_id"],
            actor_ref=first_after["actor_ref"],
            expected_project_revision=first_after["expected_project_revision"],
            expected_claim_revision=first_after["claim_revision"],
            lease_epoch=first_after["lease_epoch"],
            fence=first_after["lease_epoch"],
            observed_at=_time_after(100),
            requested_scope=_scope("capability/one"),
        )
        self.assertEqual(verdict["decision"], "allow")

    def test_heartbeat_renews_claim_and_increments_claim_revision(self):
        ledger = _ledger()
        acquired = ledger.acquire_claim(**_claim_args())
        claim = acquired["claim"]

        renewed = ledger.heartbeat_claim(
            claim_id=claim["claim_id"],
            actor_ref=claim["actor_ref"],
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at=_time_after(100),
            requested_ttl_ms=500,
        )

        self.assertEqual(renewed["claim"]["claim_revision"], 2)
        self.assertEqual(renewed["claim"]["lease_epoch"], 1)
        self.assertEqual(
            renewed["claim"]["lease_expires_at"], "2026-08-16T10:00:00.600000+00:00"
        )
        self.assertEqual(renewed["claim"]["expected_project_revision"], 9)
        self.assertEqual(renewed["claim"]["last_heartbeat_at"], _time_after(100))

    def test_stale_revision_claim_revision_epoch_and_fence_are_rejected(self):
        ledger = _ledger()
        acquired = ledger.acquire_claim(**_claim_args())
        claim = acquired["claim"]

        cases = (
            ("stale_revision", {"expected_project_revision": 7}),
            ("claim_revision_mismatch", {"expected_claim_revision": 0}),
            ("claim_revision_mismatch", {"expected_claim_revision": True}),
            ("lease_epoch_mismatch", {"lease_epoch": 0}),
            ("lease_epoch_mismatch", {"lease_epoch": True}),
            ("fence_mismatch", {"fence": 0}),
            ("fence_mismatch", {"fence": True}),
        )
        for code, changed in cases:
            with self.subTest(code=code):
                before = ledger.snapshot()
                arguments = {
                    "claim_id": claim["claim_id"],
                    "actor_ref": claim["actor_ref"],
                    "expected_project_revision": claim["expected_project_revision"],
                    "expected_claim_revision": claim["claim_revision"],
                    "lease_epoch": claim["lease_epoch"],
                    "fence": claim["lease_epoch"],
                    "observed_at": _time_after(100),
                    "requested_ttl_ms": 500,
                }
                arguments.update(changed)
                with self.assertRaisesRegex(ClaimLifecycleError, code):
                    ledger.heartbeat_claim(**arguments)
                self.assertEqual(ledger.snapshot(), before)

    def test_exact_expiry_is_expired_and_cannot_heartbeat(self):
        ledger = _ledger()
        acquired = ledger.acquire_claim(**_claim_args(requested_ttl_ms=100))
        claim = acquired["claim"]
        before = ledger.snapshot()

        with self.assertRaisesRegex(ClaimLifecycleError, "claim_expired"):
            ledger.heartbeat_claim(
                claim_id=claim["claim_id"],
                actor_ref=claim["actor_ref"],
                expected_project_revision=claim["expected_project_revision"],
                expected_claim_revision=claim["claim_revision"],
                lease_epoch=claim["lease_epoch"],
                fence=claim["lease_epoch"],
                observed_at=_time_after(100),
                requested_ttl_ms=100,
            )

        self.assertEqual(ledger.snapshot(), before)
        expired = ledger.expire_claim(
            claim_id=claim["claim_id"],
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at=_time_after(100),
        )
        self.assertEqual(expired["claim"]["status"], "expired")
        self.assertEqual(expired["claim"]["close_reason"], "lease_expired")

    def test_release_and_revoke_write_terminal_provenance(self):
        released_ledger = _ledger()
        acquired = released_ledger.acquire_claim(**_claim_args())
        claim = acquired["claim"]
        released = released_ledger.release_claim(
            claim_id=claim["claim_id"],
            actor_ref=claim["actor_ref"],
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at=_time_after(100),
        )
        self.assertEqual(released["claim"]["status"], "released")
        self.assertEqual(released["claim"]["closed_at"], _time_after(100))
        self.assertEqual(released["claim"]["closed_by_ref"], "actor-a")
        self.assertEqual(released["claim"]["close_reason"], "worker_release")

        revoked_ledger = _ledger()
        acquired = revoked_ledger.acquire_claim(**_claim_args())
        claim = acquired["claim"]
        revoked = revoked_ledger.revoke_claim(
            claim_id=claim["claim_id"],
            revoker_ref="admin-a",
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at=_time_after(100),
            reason="scope changed",
        )
        self.assertEqual(revoked["claim"]["status"], "revoked")
        self.assertEqual(revoked["claim"]["closed_by_ref"], "admin-a")
        self.assertEqual(revoked["claim"]["close_reason"], "administrative_revoke")

    def test_reclaim_atomically_closes_expired_claim_and_fences_new_worker(self):
        ledger = _ledger()
        acquired = ledger.acquire_claim(**_claim_args(requested_ttl_ms=100))
        old = acquired["claim"]

        reclaimed = ledger.reclaim_claim(
            old_claim_id=old["claim_id"],
            new_claim_id="claim-b",
            new_actor_ref="actor-b",
            expected_project_revision=old["expected_project_revision"],
            expected_claim_revision=old["claim_revision"],
            lease_epoch=old["lease_epoch"],
            fence=old["lease_epoch"],
            observed_at=_time_after(100),
            requested_ttl_ms=500,
            scope_owners=[_scope("capability/a")],
        )

        self.assertEqual(reclaimed["operation"], "reclaim")
        self.assertEqual(reclaimed["claim"]["status"], "active")
        self.assertEqual(reclaimed["claim"]["claim_id"], "claim-b")
        self.assertEqual(reclaimed["claim"]["lease_epoch"], 2)
        self.assertEqual(reclaimed["claim"]["reclaimed_from_claim_id"], "claim-a")
        old_after = next(
            item
            for item in ledger.snapshot()["claims"]
            if item["claim_id"] == "claim-a"
        )
        self.assertEqual(old_after["status"], "expired")
        self.assertEqual(old_after["close_reason"], "lease_expired")
        self.assertEqual(len(ledger.snapshot()["transitions"]), 2)

    def test_old_worker_cannot_write_after_reclaim(self):
        ledger = _ledger()
        acquired = ledger.acquire_claim(**_claim_args(requested_ttl_ms=100))
        old = acquired["claim"]
        reclaimed = ledger.reclaim_claim(
            old_claim_id=old["claim_id"],
            new_claim_id="claim-b",
            new_actor_ref="actor-b",
            expected_project_revision=old["expected_project_revision"],
            expected_claim_revision=old["claim_revision"],
            lease_epoch=old["lease_epoch"],
            fence=old["lease_epoch"],
            observed_at=_time_after(100),
            requested_ttl_ms=500,
            scope_owners=[_scope("capability/a")],
        )
        before = ledger.snapshot()

        with self.assertRaisesRegex(ClaimLifecycleError, "claim_not_active"):
            ledger.release_claim(
                claim_id=old["claim_id"],
                actor_ref=old["actor_ref"],
                expected_project_revision=reclaimed["claim"][
                    "expected_project_revision"
                ],
                expected_claim_revision=old["claim_revision"],
                lease_epoch=old["lease_epoch"],
                fence=old["lease_epoch"],
                observed_at=_time_after(150),
            )
        self.assertEqual(ledger.snapshot(), before)

    def test_dispatch_gate_requires_current_lease_revision_fence_and_scope(self):
        ledger = _ledger()
        acquired = ledger.acquire_claim(**_claim_args())
        claim = acquired["claim"]
        allowed = ledger.dispatch_gate(
            claim_id=claim["claim_id"],
            work_id=claim["work_id"],
            actor_ref=claim["actor_ref"],
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at=_time_after(100),
            requested_scope=_scope("capability/a"),
        )
        self.assertEqual(allowed["decision"], "allow")
        self.assertEqual(allowed["reason"], "authorized")
        self.assertEqual(allowed["fence"], claim["lease_epoch"])

        boolean_fence = ledger.dispatch_gate(
            claim_id=claim["claim_id"],
            work_id=claim["work_id"],
            actor_ref=claim["actor_ref"],
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=True,
            observed_at=_time_after(100),
            requested_scope=_scope("capability/a"),
        )
        self.assertEqual(boolean_fence["decision"], "deny")
        self.assertEqual(boolean_fence["reason"], "fence_mismatch")

        denied = ledger.dispatch_gate(
            claim_id=claim["claim_id"],
            work_id=claim["work_id"],
            actor_ref=claim["actor_ref"],
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at=claim["lease_expires_at"],
            requested_scope=_scope("capability/a"),
        )
        self.assertEqual(denied["decision"], "deny")
        self.assertEqual(denied["reason"], "claim_expired")

        denied_scope = ledger.dispatch_gate(
            claim_id=claim["claim_id"],
            work_id=claim["work_id"],
            actor_ref=claim["actor_ref"],
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at=_time_after(100),
            requested_scope=_scope("capability/other"),
        )
        self.assertEqual(denied_scope["reason"], "scope_not_owned")

    def test_rejected_dispatch_is_read_only_and_has_stable_verdict_digest(self):
        ledger = _ledger()
        acquired = ledger.acquire_claim(**_claim_args())
        claim = acquired["claim"]
        before = ledger.snapshot()
        result = ledger.dispatch_gate(
            claim_id=claim["claim_id"],
            work_id=claim["work_id"],
            actor_ref="old-worker",
            expected_project_revision=claim["expected_project_revision"],
            expected_claim_revision=claim["claim_revision"],
            lease_epoch=claim["lease_epoch"],
            fence=claim["lease_epoch"],
            observed_at=_time_after(100),
            requested_scope=_scope("capability/a"),
        )
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(result["reason"], "actor_mismatch")
        self.assertEqual(ledger.snapshot(), before)
        unsigned = copy.deepcopy(result)
        digest = unsigned.pop("verdict_sha256")
        canonical = json.dumps(
            unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self.assertEqual(digest, hashlib.sha256(canonical).hexdigest())


if __name__ == "__main__":
    unittest.main()
