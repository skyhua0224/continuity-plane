"""User-facing immutable checkpoint lifecycle tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from context_control_plane.cli import main


class CheckpointCliTests(unittest.TestCase):
    def _attached_project(self, root: Path) -> None:
        (root / "MASTER.md").write_text("# Existing Master\n", encoding="utf-8")
        (root / "STATUS.md").write_text("# Existing Status\n", encoding="utf-8")
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
                    "Continue the existing mainline",
                    "--owner-ref",
                    "agent-main",
                    "--scope",
                    "repo:repo://sample-app",
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
                    "claim-current",
                ]
            )

    def test_checkpoint_create_and_verify_are_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._attached_project(root)

            first_output = StringIO()
            with redirect_stdout(first_output):
                first_result = main(["checkpoint", "create", "--root", str(root)])
            first = json.loads(first_output.getvalue())
            checkpoint_file = root / ".continuity/checkpoint-ref.json"
            persisted_ref = json.loads(checkpoint_file.read_text(encoding="utf-8"))

            second_output = StringIO()
            with redirect_stdout(second_output):
                second_result = main(["checkpoint", "create", "--root", str(root)])
            second = json.loads(second_output.getvalue())

            verify_output = StringIO()
            with redirect_stdout(verify_output):
                verify_result = main(["checkpoint", "verify", "--root", str(root)])
            verified = json.loads(verify_output.getvalue())

            self.assertEqual(first_result, 0)
            self.assertEqual(second_result, 0)
            self.assertEqual(verify_result, 0)
            self.assertEqual(first["status"], "created")
            self.assertEqual(first["checkpoint_ref"], persisted_ref)
            self.assertEqual(second["checkpoint_ref"], first["checkpoint_ref"])
            self.assertEqual(verified["status"], "verified")
            self.assertEqual(verified["checkpoint_ref"], first["checkpoint_ref"])
            self.assertEqual(verified["project_id"], "sample-app")
            self.assertEqual(verified["revision"], 3)
            self.assertEqual(verified["active_work_ids"], ["M10-09"])
            self.assertEqual(verified["primary_work_id"], "M10-09")
            self.assertTrue(
                (root / ".continuity/artifacts/objects/sha256").is_dir()
            )

    def test_checkpoint_verify_rejects_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._attached_project(root)
            with redirect_stdout(StringIO()):
                main(["checkpoint", "create", "--root", str(root)])
            (root / "MASTER.md").write_text("# Changed Master\n", encoding="utf-8")

            output = StringIO()
            with redirect_stdout(output):
                result = main(["checkpoint", "verify", "--root", str(root)])
            denied = json.loads(output.getvalue())
            self.assertEqual(result, 2)
            self.assertEqual(denied["failed_gate"], "source_rebind_required")
            self.assertFalse(denied["state_changed"])
            self.assertEqual(
                denied["next_action"], "rebind-source-and-activate-next-work"
            )


if __name__ == "__main__":
    unittest.main()
