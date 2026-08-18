"""Local completion adapter tests for attached Work and claims."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.cli import main
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import (
    LOCAL_WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
    LOCAL_WORK_COMPLETION_TOOL,
    state_mcp_tool_definitions,
)


class WorkCompletionCliTests(unittest.TestCase):
    def test_completion_request_has_a_registered_strict_schema(self) -> None:
        root = Path(__file__).parents[1]
        schema_path = root / "schemas/m10-09/local-work-completion-request.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.local-work-completion-request"
        )
        definition = next(
            item
            for item in state_mcp_tool_definitions()
            if item["name"] == LOCAL_WORK_COMPLETION_TOOL
        )

        Draft202012Validator.check_schema(schema)
        self.assertEqual(
            LOCAL_WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
            "context.local-work-completion-request/v1alpha1",
        )
        self.assertEqual(entry["artifact_path"], schema_path.relative_to(root).as_posix())
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(definition["inputSchema"]["additionalProperties"])

    def _attached_project(self, root: Path) -> SQLiteStateStore:
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
            main(["checkpoint", "create", "--root", str(root)])
        return SQLiteStateStore(root / ".continuity/state.sqlite3")

    def test_completion_atomically_adds_evidence_and_closes_work_and_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached_project(root)
            receipt = root / "verification.json"
            receipt.write_text('{"focused":"4/4","status":"passed"}\n', encoding="utf-8")
            output = StringIO()

            with redirect_stdout(output):
                result = main(
                    [
                        "work",
                        "complete",
                        "--root",
                        str(root),
                        "--work-id",
                        "M10-09",
                        "--claim-id",
                        "claim-current",
                        "--actor-ref",
                        "agent-main",
                        "--evidence-file",
                        str(receipt),
                    ]
                )

            response = json.loads(output.getvalue())
            state = store.read_project("sample-app")
            events = store.read_events("sample-app")
            work = next(item for item in state["works"] if item["work_id"] == "M10-09")
            claim = next(
                item for item in state["claims"] if item["claim_id"] == "claim-current"
            )

            self.assertEqual(result, 0)
            self.assertEqual(response["status"], "completed")
            self.assertEqual(response["revision"], 3)
            self.assertEqual(work["status"], "completed")
            self.assertEqual(work["revision"], 2)
            self.assertEqual(claim["status"], "released")
            self.assertIsNotNone(claim["released_at"])
            self.assertEqual(state["project"]["active_work_ids"], [])
            self.assertIsNone(state["project"]["primary_work_id"])
            self.assertEqual(len(events), 3)
            self.assertEqual(len(response["evidence_ids"]), 2)
            self.assertTrue(set(response["evidence_ids"]).issubset(work["evidence_ids"]))

            replay = StringIO()
            with redirect_stdout(replay):
                replay_result = main(
                    [
                        "work",
                        "complete",
                        "--root",
                        str(root),
                        "--work-id",
                        "M10-09",
                        "--claim-id",
                        "claim-current",
                        "--actor-ref",
                        "agent-main",
                        "--evidence-file",
                        str(receipt),
                    ]
                )
            self.assertEqual(replay_result, 0)
            self.assertEqual(json.loads(replay.getvalue())["status"], "already-completed")
            self.assertEqual(len(store.read_events("sample-app")), 3)

    def test_completion_rejects_missing_evidence_without_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached_project(root)
            before = store.read_project("sample-app")

            with self.assertRaisesRegex(ValueError, "evidence file"):
                main(
                    [
                        "work",
                        "complete",
                        "--root",
                        str(root),
                        "--work-id",
                        "M10-09",
                        "--claim-id",
                        "claim-current",
                        "--actor-ref",
                        "agent-main",
                        "--evidence-file",
                        str(root / "missing.json"),
                    ]
                )

            self.assertEqual(store.read_project("sample-app"), before)
            self.assertEqual(len(store.read_events("sample-app")), 2)


if __name__ == "__main__":
    unittest.main()
