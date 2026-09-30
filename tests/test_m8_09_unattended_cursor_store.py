"""M8-09 durable unattended cursor store contract."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from context_control_plane.unattended_cursor_store import (
    InMemoryUnattendedCursorStore,
    SQLiteUnattendedCursorStore,
    UnattendedCursorConflict,
    UnattendedCursorError,
    UnattendedCursorIntegrityError,
    build_campaign_cursor,
    evolve_campaign_cursor,
)


def _cursor() -> dict:
    return build_campaign_cursor(
        campaign_run_id="campaign-run-m8-09",
        project_id="project-m8-09",
        profile_id="profile-m8-09",
        governance_revision=1,
        start_project_revision=7,
    )


class M809UnattendedCursorStoreTests(unittest.TestCase):
    def test_memory_store_enforces_revision_cas_and_exact_replay(self) -> None:
        store = InMemoryUnattendedCursorStore()

        created = store.compare_and_set(
            "campaign-run-m8-09", expected_cursor_revision=None, cursor=_cursor()
        )
        replayed = store.compare_and_set(
            "campaign-run-m8-09", expected_cursor_revision=None, cursor=_cursor()
        )

        self.assertEqual(created, replayed)
        self.assertEqual(created["cursor_revision"], 1)
        self.assertEqual(store.read("campaign-run-m8-09"), created)

        changed = evolve_campaign_cursor(created, next_action="select")
        advanced = store.compare_and_set(
            "campaign-run-m8-09", expected_cursor_revision=1, cursor=changed
        )
        self.assertEqual(advanced["cursor_revision"], 2)

        with self.assertRaises(UnattendedCursorConflict):
            store.compare_and_set(
                "campaign-run-m8-09",
                expected_cursor_revision=1,
                cursor=evolve_campaign_cursor(advanced, next_action="select"),
            )

    def test_sqlite_store_survives_restart_and_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unattended.sqlite3"
            first = SQLiteUnattendedCursorStore(path)
            created = first.compare_and_set(
                "campaign-run-m8-09", expected_cursor_revision=None, cursor=_cursor()
            )
            first.close()

            second = SQLiteUnattendedCursorStore(path)
            self.assertEqual(second.read("campaign-run-m8-09"), created)
            second.close()

            with closing(sqlite3.connect(path)) as connection:
                with connection:
                    row = connection.execute(
                        "SELECT cursor_json FROM unattended_campaign_cursors "
                        "WHERE campaign_run_id = ?",
                        ("campaign-run-m8-09",),
                    ).fetchone()
                    payload = json.loads(row[0])
                    payload["next_action"] = "execute"
                    connection.execute(
                        "UPDATE unattended_campaign_cursors SET cursor_json = ? "
                        "WHERE campaign_run_id = ?",
                        (
                            json.dumps(
                                payload, sort_keys=True, separators=(",", ":")
                            ),
                            "campaign-run-m8-09",
                        ),
                    )

            third = SQLiteUnattendedCursorStore(path)
            with self.assertRaises(UnattendedCursorIntegrityError):
                third.read("campaign-run-m8-09")
            third.close()

    def test_closed_cursor_rejects_malformed_terminal_receipt(self) -> None:
        store = InMemoryUnattendedCursorStore()
        initial = store.compare_and_set(
            "campaign-run-m8-09", expected_cursor_revision=None, cursor=_cursor()
        )

        with self.assertRaisesRegex(UnattendedCursorError, "terminal receipt"):
            evolve_campaign_cursor(
                initial,
                phase="closed",
                next_action="terminal",
                terminal_receipt={"malformed": True},
            )

    def test_cursor_phase_and_next_action_are_a_closed_matrix(self) -> None:
        store = InMemoryUnattendedCursorStore()
        initial = store.compare_and_set(
            "campaign-run-m8-09", expected_cursor_revision=None, cursor=_cursor()
        )

        with self.assertRaisesRegex(UnattendedCursorError, "phase"):
            evolve_campaign_cursor(initial, phase="prepared", next_action="terminal")

        with self.assertRaisesRegex(UnattendedCursorError, "selection"):
            evolve_campaign_cursor(initial, phase="claimed", next_action="compose")

    def test_closed_cursor_terminal_receipt_binds_outer_identity_and_steps(self) -> None:
        store = InMemoryUnattendedCursorStore()
        initial = store.compare_and_set(
            "campaign-run-m8-09", expected_cursor_revision=None, cursor=_cursor()
        )
        terminal = {
            "schema_version": "context.unattended-campaign-receipt/v1alpha1",
            "status": "completed",
            "campaign_run_id": "other-campaign",
            "project_id": initial["project_id"],
            "profile_id": initial["profile_id"],
            "governance_revision": initial["governance_revision"],
            "start_project_revision": initial["start_project_revision"],
            "end_project_revision": initial["current_project_revision"],
            "completed_work_ids": [],
            "conditional_not_applicable_work_ids": [],
            "remaining_optional_work_ids": [],
            "steps": [],
            "condition_decisions": [],
            "blocker_id": None,
            "evidence_ids": [],
            "resume_condition": None,
            "state_write_authority": False,
            "completion_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        }
        terminal["receipt_sha256"] = __import__("hashlib").sha256(
            json.dumps(terminal, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        with self.assertRaisesRegex(UnattendedCursorError, "binding"):
            evolve_campaign_cursor(
                initial,
                phase="closed",
                next_action="terminal",
                terminal_receipt=terminal,
            )


if __name__ == "__main__":
    unittest.main()
