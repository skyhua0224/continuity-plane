"""Checkpoint-bound local completion-and-return transition tests."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.checkpoint import CheckpointIntegrityError
from context_control_plane.cli import main
from context_control_plane.sqlite_state_store import SQLiteStateBusy, SQLiteStateStore
from context_control_plane.state_mcp import (
    LOCAL_WORK_TRANSITION_REQUEST_SCHEMA_VERSION,
    LOCAL_WORK_TRANSITION_TOOL,
    state_mcp_tool_definitions,
)


class WorkTransitionCliTests(unittest.TestCase):
    def test_transition_request_has_a_registered_strict_schema(self) -> None:
        root = Path(__file__).parents[1]
        schema_path = root / "schemas/m10-11/local-work-transition-request.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.local-work-transition-request"
        )
        definition = next(
            item
            for item in state_mcp_tool_definitions()
            if item["name"] == LOCAL_WORK_TRANSITION_TOOL
        )

        Draft202012Validator.check_schema(schema)
        self.assertEqual(
            LOCAL_WORK_TRANSITION_REQUEST_SCHEMA_VERSION,
            "context.local-work-transition-request/v1alpha1",
        )
        self.assertEqual(entry["artifact_path"], schema_path.relative_to(root).as_posix())
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(definition["inputSchema"]["additionalProperties"])

    def _repository(self, directory: str) -> tuple[Path, Path, SQLiteStateStore, dict]:
        root = Path(directory) / "repo"
        root.mkdir()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Continuity Test"],
            check=True,
        )
        (root / ".git/info/exclude").write_text(".continuity/\n", encoding="utf-8")
        (root / "MASTER.md").write_text("# Existing Master\n", encoding="utf-8")
        (root / "STATUS.md").write_text("# Existing Status\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "MASTER.md", "STATUS.md"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-qm", "test: initialize fixture"],
            check=True,
        )
        with redirect_stdout(StringIO()):
            main(["init", "--root", str(root), "--project-id", "sample-app"])
            main(
                [
                    "attach", "plan", "--root", str(root),
                    "--master", "MASTER.md", "--status", "STATUS.md",
                    "--work-id", "N-69-06",
                    "--work-title", "Finish the trusted throughput gate",
                    "--owner-ref", "agent-main",
                    "--scope", "capability:network-cc-reliable",
                ]
            )
            main(
                [
                    "attach", "approve", "--root", str(root),
                    "--actor-ref", "agent-main", "--claim-id", "claim-network",
                ]
            )
            main(["checkpoint", "create", "--root", str(root)])
            transition = StringIO()
            with redirect_stdout(transition):
                main(
                    [
                        "work", "suspend-dependency", "--root", str(root),
                        "--work-id", "N-69-06", "--claim-id", "claim-network",
                        "--actor-ref", "agent-main",
                        "--dependency-work-id", "N-69-09-IO",
                        "--dependency-work-title", "Remove the durable I/O bottleneck",
                        "--dependency-scope", "capability:filetransfer-transfer",
                        "--reason", "The final network gate depends on durable file I/O",
                    ]
                )
        receipt = Path(directory) / "n69-09-io-evidence.json"
        receipt.write_text(
            json.dumps(
                {
                    "commit": "e" * 40,
                    "linux": "6/6",
                    "macos": "3/3",
                    "windows": "passed",
                    "windows_physical_strong_path_gate": "not-passed",
                }
            ),
            encoding="utf-8",
        )
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        store = SQLiteStateStore(root / ".continuity/state.sqlite3")
        return root, receipt, store, {
            "dependency_claim_id": json.loads(transition.getvalue())["claim_id"],
            "head": head,
        }

    @staticmethod
    def _transition_argv(root: Path, receipt: Path, fixture: dict) -> list[str]:
        return [
            "work", "transition", "--root", str(root),
            "--work-id", "N-69-09-IO",
            "--claim-id", fixture["dependency_claim_id"],
            "--actor-ref", "agent-main",
            "--return-work-id", "N-69-06",
            "--successor-claim-id", "claim-network-returned",
            "--successor-scope", "capability:network-cc-reliable",
            "--resolved-blocker-id", "blocker-dependency-",
            "--remaining-blocker-id", "blocker-external-windows-path",
            "--remaining-blocker-reason",
            "Windows physical path receives no protocol packets; keep the gate open",
            "--workspace-root", str(root),
            "--expected-head", fixture["head"],
            "--expected-ref", "HEAD",
            "--evidence-file", str(receipt),
        ]

    def _resolved_blocker_id(self, store: SQLiteStateStore) -> str:
        state = store.read_project("sample-app")
        return next(
            item["blocker_id"]
            for item in state["blockers"]
            if item["blocker_id"].startswith("blocker-dependency-")
        )

    def test_transition_is_one_event_and_returns_a_verified_resume_packet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, receipt, store, fixture = self._repository(directory)
            argv = self._transition_argv(root, receipt, fixture)
            argv[argv.index("blocker-dependency-")] = self._resolved_blocker_id(store)
            before_events = store.read_events("sample-app")
            output = StringIO()

            with redirect_stdout(output):
                result = main(argv)

            response = json.loads(output.getvalue())
            state = store.read_project("sample-app")
            events = store.read_events("sample-app")
            child = next(item for item in state["works"] if item["work_id"] == "N-69-09-IO")
            parent = next(item for item in state["works"] if item["work_id"] == "N-69-06")
            active_claims = [item for item in state["claims"] if item["status"] == "active"]

            self.assertEqual(result, 0)
            self.assertEqual(response["status"], "transitioned")
            self.assertEqual(response["revision"], 5)
            self.assertEqual(len(events), len(before_events) + 1)
            self.assertEqual(events[-1]["revision_before"], 4)
            self.assertEqual(events[-1]["revision_after"], 5)
            self.assertEqual(child["status"], "completed")
            self.assertEqual(parent["status"], "active")
            self.assertEqual(state["project"]["active_work_ids"], ["N-69-06"])
            self.assertEqual(len(active_claims), 1)
            self.assertEqual(active_claims[0]["claim_id"], "claim-network-returned")
            self.assertEqual(
                state["project"]["open_blocker_ids"],
                ["blocker-external-windows-path"],
            )
            self.assertTrue(response["checkpoint_verified"])
            self.assertTrue(response["source_fresh"])
            self.assertTrue(response["lease_valid"])
            self.assertFalse(response["read_only"])
            self.assertEqual(response["resume_packet"]["active_work"]["work_id"], "N-69-06")
            self.assertEqual(
                response["resume_packet"]["claim"]["claim_id"],
                "claim-network-returned",
            )

            replay_output = StringIO()
            with redirect_stdout(replay_output):
                replay_result = main(argv)
            replay = json.loads(replay_output.getvalue())
            self.assertEqual(replay_result, 0)
            self.assertEqual(replay["status"], "already-transitioned")
            self.assertEqual(len(store.read_events("sample-app")), len(events))
            self.assertEqual(replay["revision"], 5)

    def test_checkpoint_publication_failure_rolls_back_the_whole_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, receipt, store, fixture = self._repository(directory)
            argv = self._transition_argv(root, receipt, fixture)
            argv[argv.index("blocker-dependency-")] = self._resolved_blocker_id(store)
            before = store.read_project("sample-app")
            before_events = store.read_events("sample-app")
            checkpoint_before = (root / ".continuity/checkpoint-ref.json").read_bytes()
            output = StringIO()

            with patch(
                "context_control_plane.cli.publish_checkpoint",
                side_effect=CheckpointIntegrityError("injected publication failure"),
            ), redirect_stdout(output):
                result = main(argv)

            response = json.loads(output.getvalue())
            self.assertEqual(result, 2)
            self.assertEqual(response["status"], "denied")
            self.assertEqual(response["failed_gate"], "checkpoint_publication")
            self.assertTrue(response["rollback_verified"])
            self.assertEqual(store.read_project("sample-app"), before)
            self.assertEqual(store.read_events("sample-app"), before_events)
            self.assertEqual(
                (root / ".continuity/checkpoint-ref.json").read_bytes(),
                checkpoint_before,
            )

    def test_dirty_workspace_is_a_structured_gate_failure_without_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, receipt, store, fixture = self._repository(directory)
            argv = self._transition_argv(root, receipt, fixture)
            argv[argv.index("blocker-dependency-")] = self._resolved_blocker_id(store)
            before = store.read_project("sample-app")
            before_events = store.read_events("sample-app")
            (root / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
            output = StringIO()

            with redirect_stdout(output):
                result = main(argv)

            response = json.loads(output.getvalue())
            self.assertEqual(result, 2)
            self.assertEqual(response["status"], "denied")
            self.assertEqual(response["failed_gate"], "workspace_clean")
            self.assertTrue(response["rollback_verified"])
            self.assertEqual(store.read_project("sample-app"), before)
            self.assertEqual(store.read_events("sample-app"), before_events)

    def test_cas_storage_failure_rolls_back_event_and_snapshot_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root, receipt, store, fixture = self._repository(directory)
            argv = self._transition_argv(root, receipt, fixture)
            argv[argv.index("blocker-dependency-")] = self._resolved_blocker_id(store)
            before = store.read_project("sample-app")
            before_events = store.read_events("sample-app")

            def fail_after_event_insert(stage: str) -> None:
                if stage == "after_event_insert":
                    raise SQLiteStateBusy("injected transaction failure")

            fault_store = SQLiteStateStore(
                root / ".continuity/state.sqlite3",
                fault_hook=fail_after_event_insert,
            )
            output = StringIO()
            with patch(
                "context_control_plane.cli._open_state_store",
                return_value=fault_store,
            ), redirect_stdout(output):
                result = main(argv)

            response = json.loads(output.getvalue())
            self.assertEqual(result, 2)
            self.assertEqual(response["failed_gate"], "state_store_busy")
            self.assertTrue(response["rollback_verified"])
            self.assertEqual(store.read_project("sample-app"), before)
            self.assertEqual(store.read_events("sample-app"), before_events)
            self.assertFalse(
                (root / ".continuity/checkpoint-ref.transition-pending.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
