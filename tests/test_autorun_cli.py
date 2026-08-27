"""Checkpoint-bound same-session autorun tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest import mock

from context_control_plane.cli import main
from context_control_plane.sqlite_state_store import SQLiteStateStore


class AutorunCliTests(unittest.TestCase):
    def _active_project(self, root: Path) -> SQLiteStateStore:
        (root / "MASTER.md").write_text(
            "# Master\n\n版本：revision 1\n\n## Plan\n\n| M10-01 | 🟡 | active |\n",
            encoding="utf-8",
        )
        (root / "STATUS.md").write_text(
            "# Status\n\n版本：revision 1\n\n"
            "| active work | M10-01：active（🟡） |\n"
            "| hard blocker | none |\n"
            "| next action | continue active Work |\n\n"
            "## 恢复入口\n\n1. Read [MASTER](MASTER.md).\n",
            encoding="utf-8",
        )
        with redirect_stdout(StringIO()):
            main(["init", "--root", str(root), "--project-id", "autorun-project"])
            main(
                [
                    "attach",
                    "plan",
                    "--root",
                    str(root),
                    "--master",
                    "MASTER.md",
                    "--status",
                    "STATUS.md",
                    "--work-id",
                    "M10-01",
                    "--work-title",
                    "Run active work",
                    "--owner-ref",
                    "agent-main",
                    "--scope",
                    "capability:autorun",
                ]
            )
            main(
                [
                    "attach",
                    "approve",
                    "--root",
                    str(root),
                    "--actor-ref",
                    "agent-main",
                    "--claim-id",
                    "claim-autorun",
                ]
            )
            main(["checkpoint", "create", "--root", str(root)])
        return SQLiteStateStore(root / ".continuity/state.sqlite3")

    def test_same_checkpoint_is_continued_once_without_a_duplicate_state_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._active_project(root)
            initial_events = len(store.read_events("autorun-project"))

            with redirect_stdout(StringIO()) as first_output:
                first = main(
                    [
                        "autorun",
                        "--root",
                        str(root),
                        "--session-id",
                        "session-autorun",
                    ]
                )
            first_document = json.loads(first_output.getvalue())
            self.assertEqual(first, 0)
            self.assertEqual(first_document["status"], "continued")
            self.assertEqual(first_document["state_event_created"], False)
            self.assertEqual(first_document["resume_packet"]["active_work"]["work_id"], "M10-01")

            with redirect_stdout(StringIO()) as second_output:
                second = main(
                    [
                        "autorun",
                        "--root",
                        str(root),
                        "--session-id",
                        "session-autorun",
                    ]
                )
            second_document = json.loads(second_output.getvalue())
            self.assertEqual(second, 0)
            self.assertEqual(second_document["status"], "already-continued")
            self.assertEqual(len(store.read_events("autorun-project")), initial_events)

    def test_stale_source_is_rebound_before_same_session_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._active_project(root)
            before = len(store.read_events("autorun-project"))
            (root / "MASTER.md").write_text(
                (root / "MASTER.md").read_text(encoding="utf-8") + "\nChanged.\n",
                encoding="utf-8",
            )

            with redirect_stdout(StringIO()) as output:
                result = main(
                    [
                        "autorun",
                        "--root",
                        str(root),
                        "--session-id",
                        "session-stale",
                    ]
                )
            document = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(document["status"], "continued")
            self.assertEqual(document["state_event_created"], False)
            self.assertEqual(document["recovery_receipts"][0]["status"], "heartbeat")
            self.assertEqual(len(store.read_events("autorun-project")), before + 1)

    def test_lease_near_expiry_heartbeats_before_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".continuity").mkdir()
            now = datetime.now(UTC)
            packet = {
                "project_id": "n69-08",
                "active_work": {"work_id": "N-69-08"},
                "claim": {
                    "claim_id": "claim-n-69-08",
                    "actor_ref": "platform-session",
                    "lease_expires_at": (now + timedelta(seconds=10)).isoformat(),
                },
                "checkpoint_ref": {"digest": "a" * 64},
                "next_action": "continue-active-work",
                "source_fresh": True,
                "read_only": False,
            }
            refreshed = {
                **packet,
                "claim": {
                    **packet["claim"],
                    "lease_expires_at": (now + timedelta(hours=8)).isoformat(),
                },
                "checkpoint_ref": {"digest": "b" * 64},
            }
            recover_calls = []
            with mock.patch(
                "context_control_plane.cli._autorun_resume_packet",
                side_effect=[packet, refreshed],
            ), mock.patch(
                "context_control_plane.cli._capture_json_handler",
                side_effect=lambda _handler, args: recover_calls.append(args)
                or {"status": "heartbeat"},
            ):
                output = StringIO()
                with redirect_stdout(output):
                    result = main(
                        [
                            "autorun",
                            "--root",
                            str(root),
                            "--session-id",
                            "session-heartbeat",
                        ]
                    )
            document = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(document["status"], "continued")
            self.assertEqual(recover_calls[0].action, "heartbeat")

    def test_expired_lease_reclaims_with_a_deterministic_new_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".continuity").mkdir()
            packet = {
                "project_id": "n69-08",
                "active_work": {"work_id": "N-69-08"},
                "claim": {
                    "claim_id": "claim-n-69-08-expired",
                    "actor_ref": "platform-session",
                    "lease_expires_at": "2020-01-01T00:00:00+00:00",
                },
                "checkpoint_ref": {"digest": "c" * 64},
                "next_action": "continue-active-work",
                "source_fresh": True,
                "read_only": True,
            }
            refreshed = {
                **packet,
                "claim": {
                    "claim_id": "autorun-reclaimed-claim",
                    "actor_ref": "platform-session",
                    "lease_expires_at": (
                        datetime.now(UTC) + timedelta(hours=8)
                    ).isoformat(),
                },
                "checkpoint_ref": {"digest": "d" * 64},
                "read_only": False,
            }
            recover_calls = []
            with mock.patch(
                "context_control_plane.cli._autorun_resume_packet",
                side_effect=[packet, refreshed],
            ), mock.patch(
                "context_control_plane.cli._capture_json_handler",
                side_effect=lambda _handler, args: recover_calls.append(args)
                or {"status": "reclaim"},
            ):
                output = StringIO()
                with redirect_stdout(output):
                    result = main(
                        [
                            "autorun",
                            "--root",
                            str(root),
                            "--session-id",
                            "session-reclaim",
                        ]
                    )
            document = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(document["status"], "continued")
            self.assertEqual(recover_calls[0].action, "reclaim")
            self.assertTrue(recover_calls[0].new_claim_id.startswith("autorun-reclaim-"))


if __name__ == "__main__":
    unittest.main()
