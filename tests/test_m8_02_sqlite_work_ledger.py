"""M8-02 crash-safe local coordinator persistence."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from context_control_plane.sqlite_work_ledger import (
    SQLiteWorkLedgerConflict,
    SQLiteWorkLedgerIntegrityError,
    SQLiteWorkLedgerStore,
)


def _scope(ref: str = "capability/a") -> dict[str, str]:
    return {"scope_kind": "capability", "scope_ref": ref}


def _works() -> list[dict]:
    return [
        {
            "work_id": "work-a",
            "status": "ready",
            "identity_key": "identity-a",
            "scope_refs": [_scope()],
        }
    ]


def _acquire_arguments(
    *, claim_id: str = "claim-a", actor_ref: str = "actor-a"
) -> dict:
    return {
        "work_id": "work-a",
        "actor_ref": actor_ref,
        "expected_project_revision": 7,
        "observed_at": "2026-08-16T10:00:00+00:00",
        "requested_ttl_ms": 500,
        "claim_id": claim_id,
        "scope_owners": [_scope()],
    }


class M802SQLiteWorkLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "work-ledger.sqlite3"

    def _store(self, *, fault_hook=None) -> SQLiteWorkLedgerStore:
        store = SQLiteWorkLedgerStore(self.path, fault_hook=fault_hook)
        store.initialize(
            project_id="project-m8-02",
            project_revision=7,
            works=_works(),
            max_ttl_ms=1_000,
        )
        return store

    def test_committed_claim_survives_process_restart(self) -> None:
        first = self._store()
        accepted = first.execute(
            project_id="project-m8-02",
            operation="acquire_claim",
            request_id="request-acquire-a",
            arguments=_acquire_arguments(),
        )

        reopened = SQLiteWorkLedgerStore(self.path)
        snapshot = reopened.read_snapshot("project-m8-02")

        self.assertEqual(snapshot["project_revision"], 8)
        self.assertEqual(snapshot["claims"], [accepted["claim"]])
        self.assertEqual(snapshot["claims"][0]["status"], "active")

    def test_response_loss_replays_same_receipt_after_restart(self) -> None:
        first = self._store()
        arguments = _acquire_arguments()
        accepted = first.execute(
            project_id="project-m8-02",
            operation="acquire_claim",
            request_id="request-replay-a",
            arguments=arguments,
        )
        snapshot_after = first.read_snapshot("project-m8-02")

        reopened = SQLiteWorkLedgerStore(self.path)
        replay = reopened.execute(
            project_id="project-m8-02",
            operation="acquire_claim",
            request_id="request-replay-a",
            arguments=arguments,
        )

        self.assertEqual(replay, accepted)
        self.assertEqual(reopened.read_snapshot("project-m8-02"), snapshot_after)

    def test_changed_payload_replay_is_rejected_without_mutation(self) -> None:
        store = self._store()
        store.execute(
            project_id="project-m8-02",
            operation="acquire_claim",
            request_id="request-replay-conflict",
            arguments=_acquire_arguments(),
        )
        before = store.read_snapshot("project-m8-02")

        changed = _acquire_arguments(actor_ref="actor-b")
        with self.assertRaisesRegex(SQLiteWorkLedgerConflict, "request"):
            store.execute(
                project_id="project-m8-02",
                operation="acquire_claim",
                request_id="request-replay-conflict",
                arguments=changed,
            )

        self.assertEqual(store.read_snapshot("project-m8-02"), before)

    def test_concurrent_same_work_claim_has_one_winner_and_no_silent_overwrite(
        self,
    ) -> None:
        self._store()
        barrier = Barrier(2)

        def claim(index: int) -> str:
            store = SQLiteWorkLedgerStore(self.path)
            barrier.wait(timeout=5)
            try:
                store.execute(
                    project_id="project-m8-02",
                    operation="acquire_claim",
                    request_id=f"request-race-{index}",
                    arguments=_acquire_arguments(
                        claim_id=f"claim-{index}", actor_ref=f"actor-{index}"
                    ),
                )
                return "accepted"
            except SQLiteWorkLedgerConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(claim, range(2)))

        snapshot = SQLiteWorkLedgerStore(self.path).read_snapshot("project-m8-02")
        self.assertEqual(sorted(outcomes), ["accepted", "conflict"])
        self.assertEqual(len(snapshot["claims"]), 1)
        self.assertEqual(snapshot["project_revision"], 8)
        self.assertEqual(len(snapshot["transitions"]), 1)

    def test_fault_before_commit_rolls_back_snapshot_event_and_request_receipt(
        self,
    ) -> None:
        def fail(point: str) -> None:
            if point == "before_commit":
                raise RuntimeError("injected crash")

        store = self._store(fault_hook=fail)
        before = store.read_snapshot("project-m8-02")
        with self.assertRaisesRegex(RuntimeError, "injected crash"):
            store.execute(
                project_id="project-m8-02",
                operation="acquire_claim",
                request_id="request-crash-before-commit",
                arguments=_acquire_arguments(),
            )

        reopened = SQLiteWorkLedgerStore(self.path)
        self.assertEqual(reopened.read_snapshot("project-m8-02"), before)
        self.assertIsNone(
            reopened.read_request_receipt(
                "project-m8-02", "acquire_claim", "request-crash-before-commit"
            )
        )

    def test_fault_after_commit_is_recoverable_by_exact_request_replay(self) -> None:
        calls = 0

        def fail_once(point: str) -> None:
            nonlocal calls
            if point == "after_commit" and calls == 0:
                calls += 1
                raise RuntimeError("response lost")

        store = self._store(fault_hook=fail_once)
        arguments = _acquire_arguments()
        with self.assertRaisesRegex(RuntimeError, "response lost"):
            store.execute(
                project_id="project-m8-02",
                operation="acquire_claim",
                request_id="request-after-commit",
                arguments=arguments,
            )

        replay = SQLiteWorkLedgerStore(self.path).execute(
            project_id="project-m8-02",
            operation="acquire_claim",
            request_id="request-after-commit",
            arguments=arguments,
        )
        self.assertEqual(replay["status"], "accepted")
        self.assertEqual(
            SQLiteWorkLedgerStore(self.path).read_snapshot("project-m8-02")[
                "project_revision"
            ],
            8,
        )

    def test_snapshot_digest_tampering_is_detected(self) -> None:
        store = self._store()
        store.execute(
            project_id="project-m8-02",
            operation="acquire_claim",
            request_id="request-tamper",
            arguments=_acquire_arguments(),
        )
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE work_ledgers SET snapshot_sha256 = ? WHERE project_id = ?",
                ("0" * 64, "project-m8-02"),
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaisesRegex(SQLiteWorkLedgerIntegrityError, "digest"):
            store.read_snapshot("project-m8-02")


if __name__ == "__main__":
    unittest.main()
