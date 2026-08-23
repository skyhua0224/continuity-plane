"""Successor Work activation tests for the local lifecycle adapter."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from context_control_plane.cli import main
from context_control_plane.sqlite_state_store import SQLiteStateStore


class WorkActivationCliTests(unittest.TestCase):
    def _completed_project(self, root: Path) -> SQLiteStateStore:
        (root / "MASTER.md").write_text("# Existing Master\n", encoding="utf-8")
        (root / "STATUS.md").write_text("# Existing Status\n", encoding="utf-8")
        receipt = root / "verification.json"
        receipt.write_text('{"status":"passed"}\n', encoding="utf-8")
        with redirect_stdout(StringIO()):
            main(["init", "--root", str(root), "--project-id", "sample-app"])
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
                    "M10-09",
                    "--work-title",
                    "Complete migration",
                    "--owner-ref",
                    "agent-main",
                    "--scope",
                    "capability:migration",
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
                    "claim-m10-09",
                ]
            )
            main(["checkpoint", "create", "--root", str(root)])
            main(
                [
                    "work",
                    "complete",
                    "--root",
                    str(root),
                    "--work-id",
                    "M10-09",
                    "--claim-id",
                    "claim-m10-09",
                    "--actor-ref",
                    "agent-main",
                    "--evidence-file",
                    str(receipt),
                ]
            )
        return SQLiteStateStore(root / ".continuity/state.sqlite3")

    def test_successor_activation_creates_work_claim_and_checkpoint_in_one_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._completed_project(root)

            with redirect_stdout(StringIO()) as output:
                result = main(
                    [
                        "work",
                        "activate",
                        "--root",
                        str(root),
                        "--work-id",
                        "M10-01",
                        "--work-title",
                        "Run the external pilot",
                        "--owner-ref",
                        "agent-main",
                        "--claim-id",
                        "claim-m10-01",
                        "--scope",
                        "capability:external-pilot",
                    ]
                )

            state = store.read_project("sample-app")
            events = store.read_events("sample-app")
            work = next(item for item in state["works"] if item["work_id"] == "M10-01")
            claim = next(
                item for item in state["claims"] if item["claim_id"] == "claim-m10-01"
            )
            self.assertEqual(result, 0)
            self.assertIn('"status": "activated"', output.getvalue())
            self.assertEqual(state["project"]["revision"], 4)
            self.assertEqual(state["project"]["active_work_ids"], ["M10-01"])
            self.assertEqual(work["status"], "active")
            self.assertEqual(len(work["evidence_ids"]), 1)
            self.assertTrue(work["evidence_ids"][0].startswith("evidence-attach-"))
            self.assertEqual(claim["status"], "active")
            self.assertEqual(len(events), 4)

            with redirect_stdout(StringIO()) as replay:
                replay_result = main(
                    [
                        "work",
                        "activate",
                        "--root",
                        str(root),
                        "--work-id",
                        "M10-01",
                        "--work-title",
                        "Run the external pilot",
                        "--owner-ref",
                        "agent-main",
                        "--claim-id",
                        "claim-m10-01",
                        "--scope",
                        "capability:external-pilot",
                    ]
                )
            self.assertEqual(replay_result, 0)
            self.assertIn('"status": "already-active"', replay.getvalue())
            self.assertEqual(len(store.read_events("sample-app")), 4)


if __name__ == "__main__":
    unittest.main()
