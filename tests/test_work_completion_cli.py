"""Local completion adapter tests for attached Work and claims."""

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

from context_control_plane.cli import main
from context_control_plane.checkpoint import CheckpointIntegrityError
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import (
    LOCAL_WORK_ACTIVATION_REQUEST_SCHEMA_VERSION,
    LOCAL_WORK_ACTIVATION_TOOL,
    LOCAL_WORK_COMPLETION_REQUEST_SCHEMA_VERSION,
    LOCAL_WORK_COMPLETION_TOOL,
    state_mcp_tool_definitions,
)


class WorkCompletionCliTests(unittest.TestCase):
    def test_atomic_activation_request_has_a_registered_strict_schema(self) -> None:
        root = Path(__file__).parents[1]
        schema_path = root / "schemas/m10-11/local-work-activation-request.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.local-work-activation-request"
        )
        definition = next(
            item
            for item in state_mcp_tool_definitions()
            if item["name"] == LOCAL_WORK_ACTIVATION_TOOL
        )

        Draft202012Validator.check_schema(schema)
        self.assertEqual(
            LOCAL_WORK_ACTIVATION_REQUEST_SCHEMA_VERSION,
            "context.local-work-activation-request/v1alpha1",
        )
        self.assertEqual(entry["artifact_path"], schema_path.relative_to(root).as_posix())
        self.assertFalse(schema["additionalProperties"])
        self.assertFalse(definition["inputSchema"]["additionalProperties"])

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

    def test_complete_idle_resume_and_activate_successor_without_external_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached_project(root)
            receipt = root / "verification.json"
            receipt.write_text('{"status":"passed"}\n', encoding="utf-8")
            completed_output = StringIO()

            with redirect_stdout(completed_output):
                self.assertEqual(
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
                            str(receipt),
                        ]
                    ),
                    0,
                )
            completed = json.loads(completed_output.getvalue())
            self.assertEqual(completed["revision"], 3)
            self.assertTrue(completed["checkpoint_verified"])
            completed_projection = json.loads(
                (root / ".continuity/status-projection.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(completed_projection["revision"], 3)
            self.assertIn(
                "active Work | none",
                (root / ".continuity/STATUS.current.md").read_text(encoding="utf-8"),
            )

            idle_output = StringIO()
            with redirect_stdout(idle_output):
                self.assertEqual(main(["resume", "--root", str(root)]), 0)
            idle = json.loads(idle_output.getvalue())
            self.assertIsNone(idle["active_work"])
            self.assertIsNone(idle["claim"])
            self.assertEqual(idle["next_action"], "activate-next-work")
            self.assertEqual(
                idle["first_permitted_action"]["target"], "activate-next-work"
            )
            self.assertTrue(idle["checkpoint_verified"])
            self.assertTrue(idle["source_fresh"])
            self.assertTrue(idle["lease_valid"])
            self.assertFalse(idle["read_only"])

            activated_output = StringIO()
            with redirect_stdout(activated_output):
                self.assertEqual(
                    main(
                        [
                            "work",
                            "activate",
                            "--root",
                            str(root),
                            "--work-id",
                            "N-69-07",
                            "--work-title",
                            "Validate ECN and BBR end to end",
                            "--owner-ref",
                            "agent-main",
                            "--claim-id",
                            "claim-n-69-07",
                            "--scope",
                            "capability:network-cc-reliable",
                            "--scope",
                            "capability:network-transport-diagnostics",
                        ]
                    ),
                    0,
                )
            activated = json.loads(activated_output.getvalue())
            state = store.read_project("sample-app")
            events = store.read_events("sample-app")
            active_claims = [item for item in state["claims"] if item["status"] == "active"]

            self.assertEqual(activated["status"], "activated")
            self.assertEqual(activated["revision"], 4)
            self.assertTrue(activated["checkpoint_verified"])
            activated_projection = json.loads(
                (root / ".continuity/status-projection.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(activated_projection["revision"], 4)
            self.assertIn(
                "N-69-07",
                (root / ".continuity/STATUS.current.md").read_text(encoding="utf-8"),
            )
            self.assertEqual(len(events), 4)
            self.assertEqual(events[-1]["revision_before"], 3)
            self.assertEqual(events[-1]["revision_after"], 4)
            self.assertEqual(state["project"]["active_work_ids"], ["N-69-07"])
            self.assertEqual(state["project"]["primary_work_id"], "N-69-07")
            self.assertEqual(len(active_claims), 1)
            self.assertEqual(active_claims[0]["claim_id"], "claim-n-69-07")

            resumed_output = StringIO()
            with redirect_stdout(resumed_output):
                self.assertEqual(main(["resume", "--root", str(root)]), 0)
            resumed = json.loads(resumed_output.getvalue())
            self.assertEqual(resumed["active_work"]["work_id"], "N-69-07")
            self.assertEqual(resumed["claim"]["claim_id"], "claim-n-69-07")
            self.assertTrue(resumed["checkpoint_verified"])
            self.assertFalse(resumed["read_only"])

    def test_idle_activation_checkpoint_failure_leaves_state_unclaimed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached_project(root)
            receipt = root / "verification.json"
            receipt.write_text('{"status":"passed"}\n', encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(
                    [
                        "work", "complete", "--root", str(root),
                        "--work-id", "M10-09", "--claim-id", "claim-current",
                        "--actor-ref", "agent-main", "--evidence-file", str(receipt),
                    ]
                )
            before = store.read_project("sample-app")
            before_events = store.read_events("sample-app")
            output = StringIO()

            with patch(
                "context_control_plane.cli.publish_checkpoint",
                side_effect=CheckpointIntegrityError("injected activation failure"),
            ), redirect_stdout(output):
                result = main(
                    [
                        "work", "activate", "--root", str(root),
                        "--work-id", "N-69-07",
                        "--work-title", "Validate ECN and BBR end to end",
                        "--owner-ref", "agent-main",
                        "--claim-id", "claim-n-69-07",
                        "--scope", "capability:network-cc-reliable",
                        "--scope", "capability:network-transport-diagnostics",
                    ]
                )

            denied = json.loads(output.getvalue())
            self.assertEqual(result, 2)
            self.assertEqual(denied["status"], "denied")
            self.assertEqual(denied["failed_gate"], "checkpoint_publication")
            self.assertFalse(denied["state_changed"])
            self.assertEqual(store.read_project("sample-app"), before)
            self.assertEqual(store.read_events("sample-app"), before_events)

    def test_delivery_activation_binds_source_predecessor_head_evidence_and_effects(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
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
            (root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "test: initialize"],
                check=True,
            )
            (root / ".git/info/exclude").write_text(".continuity/\n", encoding="utf-8")
            store = self._attached_project(root)
            subprocess.run(
                ["git", "-C", str(root), "add", "MASTER.md", "STATUS.md"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "test: bind governance"],
                check=True,
            )
            implementation = Path(directory) / "implementation.json"
            implementation.write_text('{"tests":"passed"}\n', encoding="utf-8")
            completed_output = StringIO()
            with redirect_stdout(completed_output):
                main(
                    [
                        "work", "complete", "--root", str(root),
                        "--work-id", "M10-09", "--claim-id", "claim-current",
                        "--actor-ref", "agent-main",
                        "--evidence-file", str(implementation),
                    ]
                )
            completed = json.loads(completed_output.getvalue())
            implementation_evidence_id = next(
                item
                for item in completed["evidence_ids"]
                if item.startswith("evidence-test-")
            )
            head = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            (root / "implementation-change.txt").write_text(
                "verified but not committed\n", encoding="utf-8"
            )
            output = StringIO()

            with redirect_stdout(output):
                result = main(
                    [
                        "work", "activate", "--root", str(root),
                        "--work-id", "M10-09-delivery",
                        "--work-title", "Deliver the verified M10-09 change",
                        "--owner-ref", "agent-main",
                        "--claim-id", "claim-m10-09-delivery",
                        "--scope", "capability:release-delivery",
                        "--execution-class", "delivery",
                        "--source-ref", "issue://sample-app/10",
                        "--predecessor-work-id", "M10-09",
                        "--implementation-evidence-id", implementation_evidence_id,
                        "--workspace-root", str(root),
                        "--expected-head", head,
                        "--expected-ref", "HEAD",
                        "--allow-effect", "source-control.push",
                        "--allow-effect", "source-control.pr",
                        "--allow-effect", "source-control.merge",
                        "--allow-effect", "deployment.deploy",
                        "--allow-effect", "remote-effect.install-verification",
                    ]
                )

            response = json.loads(output.getvalue())
            state = store.read_project("sample-app")
            delivery = next(
                item for item in state["works"] if item["work_id"] == "M10-09-delivery"
            )
            claim = next(
                item
                for item in state["claims"]
                if item["claim_id"] == "claim-m10-09-delivery"
            )

            self.assertEqual(result, 0)
            self.assertEqual(response["execution_class"], "delivery")
            self.assertRegex(response["delivery_contract_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(delivery["parent_work_id"], "M10-09")
            self.assertIn(implementation_evidence_id, delivery["evidence_ids"])
            self.assertTrue(
                any(item.startswith("evidence-delivery-") for item in delivery["evidence_ids"])
            )
            effect_refs = {
                item["scope_ref"]
                for item in claim["scope_owners"]
                if item["scope_kind"] == "effect"
            }
            self.assertEqual(
                effect_refs,
                {
                    "source-control.push",
                    "source-control.pr",
                    "source-control.merge",
                    "deployment.deploy",
                    "remote-effect.install-verification",
                },
            )

    def test_delivery_activation_missing_binding_fails_before_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._attached_project(root)
            receipt = root / "verification.json"
            receipt.write_text('{"status":"passed"}\n', encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(
                    [
                        "work", "complete", "--root", str(root),
                        "--work-id", "M10-09", "--claim-id", "claim-current",
                        "--actor-ref", "agent-main", "--evidence-file", str(receipt),
                    ]
                )
            before = store.read_project("sample-app")
            before_events = store.read_events("sample-app")

            with self.assertRaisesRegex(ValueError, "delivery activation requires"):
                main(
                    [
                        "work", "activate", "--root", str(root),
                        "--work-id", "M10-09-delivery",
                        "--work-title", "Deliver M10-09",
                        "--owner-ref", "agent-main",
                        "--claim-id", "claim-m10-09-delivery",
                        "--scope", "capability:release-delivery",
                        "--execution-class", "delivery",
                    ]
                )

            self.assertEqual(store.read_project("sample-app"), before)
            self.assertEqual(store.read_events("sample-app"), before_events)
            self.assertFalse(
                (root / ".continuity/checkpoint-ref.transition-pending.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
