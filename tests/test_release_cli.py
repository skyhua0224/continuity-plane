"""Release CLI initialization and public-surface tests."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import yaml

from context_control_plane.cli import main
from context_control_plane.sqlite_state_store import SQLiteStateStore


class ReleaseCliTests(unittest.TestCase):
    def test_init_creates_neutral_local_embedded_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    ["init", "--root", str(root), "--project-id", "sample-app"]
                )
            project = yaml.safe_load(
                (root / ".continuity/project.yaml").read_text()
            )

            self.assertEqual(result, 0)
            self.assertEqual(project["project_id"], "sample-app")
            self.assertEqual(project["runtime_profile"], "local-embedded")
            self.assertEqual(project["state_store"]["adapter"], "sqlite")
            self.assertTrue((root / ".continuity/MASTER.md").is_file())
            self.assertTrue((root / ".continuity/STATUS.md").is_file())
            self.assertTrue((root / ".continuity/MASTER.en.md").is_file())
            self.assertTrue((root / ".continuity/STATUS.en.md").is_file())
            self.assertTrue((root / ".continuity/state.sqlite3").is_file())
            state = SQLiteStateStore(
                root / ".continuity/state.sqlite3"
            ).read_project("sample-app")
            self.assertEqual(state["project"]["revision"], 0)
            self.assertEqual(state["project"]["primary_work_id"], None)
            self.assertEqual(state["works"][0]["work_id"], "work-initial")
            self.assertEqual(json.loads(output.getvalue())["status"], "initialized")

    def test_init_never_overwrites_existing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".continuity"
            target.mkdir()
            project = target / "project.yaml"
            project.write_text("owned: true\n")

            with self.assertRaises(FileExistsError):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            self.assertEqual(project.read_text(), "owned: true\n")

    def test_verify_accepts_initialized_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
                result = main(["verify", "--root", str(root)])
            self.assertEqual(result, 0)

    def test_verify_rejects_missing_state_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            (root / ".continuity/state.sqlite3").unlink()

            with self.assertRaises(FileNotFoundError):
                main(["verify", "--root", str(root)])

    def test_state_show_reads_the_authoritative_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            output = StringIO()

            with redirect_stdout(output):
                result = main(["state", "show", "--root", str(root)])

            response = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(response["status"], "ok")
            self.assertEqual(response["project_id"], "sample-app")
            self.assertEqual(response["revision"], 0)
            self.assertEqual(response["event_head"], None)
            self.assertEqual(response["state"]["works"][0]["work_id"], "work-initial")

    def test_public_templates_have_no_internal_markers(self) -> None:
        root = Path(__file__).parents[1]
        text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (root / "context_control_plane/templates").glob("*")
            if path.is_file()
        ).lower()
        for marker in (
            "alkaidlab",
            "projectcompute",
            "deepseek",
            "moonlight",
            "sunshine",
            "/home/",
            "/users/",
        ):
            self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
