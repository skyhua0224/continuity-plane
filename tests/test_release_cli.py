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
    def test_attach_plan_binds_existing_master_and_status_without_writing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MASTER.md").write_text("# Existing Master\n", encoding="utf-8")
            (root / "STATUS.md").write_text("# Existing Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            output = StringIO()

            with redirect_stdout(output):
                result = main(
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

            proposal_path = root / ".continuity/attach-proposal.json"
            proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
            state = SQLiteStateStore(root / ".continuity/state.sqlite3").read_project(
                "sample-app"
            )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "planned")
            self.assertEqual(proposal["work"]["work_id"], "M10-09")
            self.assertEqual(proposal["sources"][0]["kind"], "master")
            self.assertEqual(proposal["state_write_authority"], False)
            self.assertEqual(state["project"]["revision"], 0)
            self.assertEqual(state["project"]["active_work_ids"], [])

    def test_attach_approve_commits_ready_work_then_claims_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
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
            output = StringIO()

            with redirect_stdout(output):
                result = main(
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

            store = SQLiteStateStore(root / ".continuity/state.sqlite3")
            state = store.read_project("sample-app")
            events = store.read_events("sample-app")
            work = next(item for item in state["works"] if item["work_id"] == "M10-09")
            claim = next(item for item in state["claims"] if item["claim_id"] == "claim-current")
            initial = next(item for item in state["works"] if item["work_id"] == "work-initial")
            response = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(response["status"], "attached")
            self.assertEqual(response["revision"], 2)
            self.assertEqual(state["project"]["active_work_ids"], ["M10-09"])
            self.assertEqual(state["project"]["primary_work_id"], "M10-09")
            self.assertEqual(work["status"], "active")
            self.assertEqual(claim["actor_ref"], "agent-main")
            self.assertEqual(initial["status"], "rejected")
            self.assertEqual(len(events), 2)

            replay_output = StringIO()
            with redirect_stdout(replay_output):
                replay_result = main(
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
            self.assertEqual(replay_result, 0)
            self.assertEqual(
                json.loads(replay_output.getvalue())["status"], "already-attached"
            )
            self.assertEqual(len(store.read_events("sample-app")), 2)

    def test_attach_approve_rejects_stale_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "MASTER.md"
            master.write_text("# Existing Master\n", encoding="utf-8")
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
            master.write_text("# Changed Master\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "source.*changed"):
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

            state = SQLiteStateStore(root / ".continuity/state.sqlite3").read_project(
                "sample-app"
            )
            self.assertEqual(state["project"]["revision"], 0)

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
