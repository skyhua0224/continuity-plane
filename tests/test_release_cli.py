"""Release CLI initialization and public-surface tests."""

from __future__ import annotations

import json
import hashlib
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import yaml
from jsonschema import Draft202012Validator, ValidationError

from context_control_plane.cli import _codex_plugin_status, _initial_state, main
from context_control_plane.sqlite_state_store import SQLiteStateStore


class ReleaseCliTests(unittest.TestCase):
    def _write_plugin_runtime(
        self, codex_home: Path, *, noop: bool = False, version: str = "0.1.0a19"
    ) -> None:
        marketplace = codex_home / "dev-marketplaces/continuity-plane-current"
        agents = marketplace / ".agents/plugins"
        plugin = marketplace / "plugins/continuity-plane"
        (agents).mkdir(parents=True, exist_ok=True)
        (plugin / ".codex-plugin").mkdir(parents=True, exist_ok=True)
        (plugin / "scripts").mkdir(parents=True, exist_ok=True)
        (plugin / "hooks").mkdir(parents=True, exist_ok=True)
        (agents / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "continuity-plane",
                    "plugins": [
                        {
                            "name": "continuity-plane",
                            "source": {"source": "local", "path": "./plugins/continuity-plane"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (plugin / ".codex-plugin/plugin.json").write_text(
            json.dumps({"name": "continuity-plane", "version": version}),
            encoding="utf-8",
        )
        hook = "# Emergency no-op bridge\nimport sys\nsys.exit(0)\n" if noop else (
            "def main():\n    return 0\n" + ("# padding\n" * 80) + "\ndef advisory_main():\n    return 0\n"
        )
        advisory = "# Emergency no-op bridge\nimport sys\nsys.exit(0)\n" if noop else (
            "def main():\n    return 0\n" + ("# padding\n" * 80)
        )
        (plugin / "scripts/continuity-hook.py").write_text(hook, encoding="utf-8")
        (plugin / "scripts/continuity-advisory-hook.py").write_text(advisory, encoding="utf-8")
        hooks = {}
        for event, script in (
            ("SessionStart", "continuity-hook.py"),
            ("PreCompact", "continuity-hook.py"),
            ("PostCompact", "continuity-hook.py"),
            ("UserPromptSubmit", "continuity-hook.py"),
            ("PreToolUse", "continuity-advisory-hook.py"),
            ("PostToolUse", "continuity-advisory-hook.py"),
        ):
            hooks[event] = [{"hooks": [{"type": "command", "command": f"python ${{PLUGIN_ROOT}}/scripts/{script}"}]}]
        (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    def test_initialized_project_is_immediately_inspectable_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            before = {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (root / ".continuity").rglob("*")
                if path.is_file()
            }
            output = StringIO()
            with redirect_stdout(output):
                result = main(["inspect", "--root", str(root)])
            packet = json.loads(output.getvalue())
            after = {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (root / ".continuity").rglob("*")
                if path.is_file()
            }
            self.assertEqual(result, 0)
            self.assertEqual(packet["project_id"], "sample-app")
            self.assertIsNone(packet["active_work"])
            self.assertIsNone(packet["claim"])
            self.assertTrue(packet["checkpoint_verified"])
            self.assertTrue(packet["source_fresh"])
            self.assertFalse(packet["read_only"])
            self.assertEqual(before, after)

    def test_initialization_failure_leaves_no_partial_project_and_can_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "context_control_plane.cli.publish_checkpoint",
                side_effect=RuntimeError("checkpoint fault"),
            ):
                with self.assertRaisesRegex(RuntimeError, "checkpoint fault"):
                    main(["init", "--root", str(root), "--project-id", "sample-app"])
            self.assertFalse((root / ".continuity").exists())

            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(["init", "--root", str(root), "--project-id", "sample-app"]),
                    0,
                )
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["inspect", "--root", str(root)]), 0)

    def test_doctor_reports_real_codex_plugin_adoption_without_transcripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            codex_home = base / "codex-home"
            root.mkdir()
            codex_home.mkdir()
            self._write_plugin_runtime(codex_home)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            (codex_home / "config.toml").write_text(
                "[marketplaces.continuity-plane]\n"
                f"source_type = \"local\"\nsource = \"{(codex_home / 'dev-marketplaces/continuity-plane-current').as_posix()}\"\n"
                "[plugins.\"continuity-plane@continuity-plane\"]\n"
                "enabled = true\n"
                "[plugins.\"continuity-plane-search@continuity-plane\"]\n"
                "enabled = true\n"
                "[plugins.\"continuity-plane-search@continuity-plane\".mcp_servers.\"continuity-search\"]\n"
                "enabled = true\n"
                "default_tools_approval_mode = \"approve\"\n"
                "[plugins.\"continuity-plane-state@continuity-plane\"]\n"
                "enabled = true\n"
                "[plugins.\"continuity-plane-state@continuity-plane\".mcp_servers.continuity]\n"
                "enabled = true\n"
                "default_tools_approval_mode = \"approve\"\n"
                "[hooks.state]\n"
                "[hooks.state.\"continuity-plane@continuity-plane:hooks/hooks.json:pre_compact:0:0\"]\n"
                f"trusted_hash = \"sha256:{'1' * 64}\"\n"
                "[hooks.state.\"continuity-plane@continuity-plane:hooks/hooks.json:post_compact:0:0\"]\n"
                f"trusted_hash = \"sha256:{'2' * 64}\"\n"
                "[hooks.state.\"continuity-plane@continuity-plane:hooks/hooks.json:session_start:0:0\"]\n"
                f"trusted_hash = \"sha256:{'3' * 64}\"\n"
                "[hooks.state.\"continuity-plane@continuity-plane:hooks/hooks.json:post_tool_use:0:0\"]\n"
                f"trusted_hash = \"sha256:{'4' * 64}\"\n"
                "[hooks.state.\"continuity-plane@continuity-plane:hooks/hooks.json:user_prompt_submit:0:0\"]\n"
                f"trusted_hash = \"sha256:{'5' * 64}\"\n"
                "[hooks.state.\"continuity-plane@continuity-plane:hooks/hooks.json:pre_tool_use:0:0\"]\n"
                f"trusted_hash = \"sha256:{'6' * 64}\"\n",
                encoding="utf-8",
            )
            events = (
                codex_home
                / "plugins/data/continuity-plane-continuity-plane/live-events"
            )
            events.mkdir(parents=True)
            event = {
                "event_type": "session-start",
                "observed_at": "2026-09-02T03:56:55+00:00",
                "plugin_loaded": True,
                "success": True,
                "context_emitted": True,
                "project_root_sha256": hashlib.sha256(str(root).encode()).hexdigest(),
            }
            (events / f"{hashlib.sha256(b'session').hexdigest()}.jsonl").write_text(
                json.dumps(event) + "\n", encoding="utf-8"
            )
            output = StringIO()

            with redirect_stdout(output):
                result = main(
                    [
                        "doctor",
                        "--root",
                        str(root),
                        "--codex-home",
                        str(codex_home),
                    ]
                )

            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(report["codex_plugin"]["status"], "active")
            self.assertEqual(report["codex_plugin"]["runtime"]["issue"], None)
            self.assertTrue(report["codex_plugin"]["runtime"]["version_matches_package"])
            self.assertEqual(
                report["codex_plugin"]["runtime"]["hook_commands_resolved"], 6
            )
            self.assertEqual(report["codex_plugin"]["trusted_hooks"], 6)
            self.assertEqual(report["codex_plugin"]["expected_hooks"], 6)
            self.assertTrue(report["codex_plugin"]["mcp_auto_approved"])
            self.assertTrue(report["codex_plugin"]["search_mcp_auto_approved"])
            self.assertTrue(report["codex_plugin"]["session_start_observed"])
            self.assertNotIn("transcript", json.dumps(report).lower())

            event_path = events / f"{hashlib.sha256(b'session').hexdigest()}.jsonl"
            for changes in (
                {"success": False, "failed_gate": "resume_unavailable"},
                {"context_emitted": False},
                {"project_root_sha256": "0" * 64},
            ):
                with self.subTest(changes=changes):
                    event_path.write_text(json.dumps({**event, **changes}) + "\n", encoding="utf-8")
                    output = StringIO()
                    with redirect_stdout(output):
                        main(["doctor", "--root", str(root), "--codex-home", str(codex_home)])
                    self.assertNotEqual(json.loads(output.getvalue())["codex_plugin"]["status"], "active")

    def test_doctor_rejects_a_trusted_but_noop_plugin_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            codex_home = base / "codex-home"
            root.mkdir()
            codex_home.mkdir()
            self._write_plugin_runtime(codex_home, noop=True)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            (codex_home / "config.toml").write_text(
                "[marketplaces.continuity-plane]\n"
                f"source_type = \"local\"\nsource = \"{(codex_home / 'dev-marketplaces/continuity-plane-current').as_posix()}\"\n"
                "[plugins.\"continuity-plane@continuity-plane\"]\nenabled = true\n",
                encoding="utf-8",
            )
            status = _codex_plugin_status(codex_home, project_root=root)
            self.assertEqual(status["status"], "misconfigured")
            self.assertEqual(status["runtime"]["issue"], "noop_hook_runtime")
            self.assertEqual(status["trusted_hooks"], 0)

    def test_doctor_reports_plugin_version_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            codex_home = base / "codex-home"
            codex_home.mkdir()
            self._write_plugin_runtime(codex_home, version="0.1.0-alpha.9")
            (codex_home / "config.toml").write_text(
                "[marketplaces.continuity-plane]\n"
                f"source_type = \"local\"\nsource = \"{(codex_home / 'dev-marketplaces/continuity-plane-current').as_posix()}\"\n"
                "[plugins.\"continuity-plane@continuity-plane\"]\nenabled = true\n",
                encoding="utf-8",
            )
            status = _codex_plugin_status(codex_home)
            self.assertEqual(status["status"], "misconfigured")
            self.assertEqual(status["runtime"]["issue"], "plugin_version_mismatch")
            self.assertEqual(status["runtime"]["plugin_version"], "0.1.0-alpha.9")

    def test_doctor_rejects_an_oversized_recovery_packet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            packet = root / ".continuity/resume-packet.json"
            packet.write_text(
                "{\"schema_version\":\"test\",\"padding\":\"" + "x" * 8200 + "\"}",
                encoding="utf-8",
            )
            output = StringIO()
            with redirect_stdout(output):
                main(["doctor", "--root", str(root)])
            report = json.loads(output.getvalue())
            self.assertEqual(report["status"], "degraded")
            self.assertEqual(report["packet_health"]["status"], "oversized")
            self.assertGreater(report["packet_health"]["size_bytes"], 8192)

    def test_resume_packet_has_a_registered_strict_schema(self) -> None:
        root = Path(__file__).parents[1]
        schema_path = root / "schemas/m10-09/resume-packet.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.resume-packet"
        )

        Draft202012Validator.check_schema(schema)
        self.assertEqual(
            entry["current_wire_version"], "context.resume-packet/v1alpha1"
        )
        self.assertEqual(entry["artifact_path"], schema_path.relative_to(root).as_posix())
        self.assertFalse(schema["additionalProperties"])

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
            self.assertEqual(state["project"]["revision"], 1)
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
            self.assertEqual(response["revision"], 3)
            self.assertEqual(state["project"]["active_work_ids"], ["M10-09"])
            self.assertEqual(state["project"]["primary_work_id"], "M10-09")
            self.assertEqual(work["status"], "active")
            self.assertEqual(claim["actor_ref"], "agent-main")
            self.assertEqual(initial["status"], "rejected")
            self.assertEqual(len(events), 3)

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
            self.assertEqual(len(store.read_events("sample-app")), 3)

            with redirect_stdout(StringIO()):
                main(["checkpoint", "create", "--root", str(root)])
            resume_output = StringIO()
            with redirect_stdout(resume_output):
                resume_result = main(["resume", "--root", str(root)])
            packet = json.loads(
                (root / ".continuity/resume-packet.json").read_text(encoding="utf-8")
            )
            self.assertEqual(resume_result, 0)
            self.assertEqual(json.loads(resume_output.getvalue()), packet)
            self.assertEqual(
                packet["schema_version"], "context.recovery-envelope/v1alpha1"
            )
            self.assertEqual(packet["revision"], 3)
            self.assertEqual(packet["active_work"]["work_id"], "M10-09")
            self.assertEqual(packet["claim"]["claim_id"], "claim-current")
            self.assertEqual(packet["source_fresh"], True)
            self.assertEqual(packet["read_only"], False)
            self.assertRegex(packet["packet_sha256"], r"^[0-9a-f]{64}$")
            projection_path = root / ".continuity/status-projection.json"
            projection = json.loads(projection_path.read_text(encoding="utf-8"))
            self.assertEqual(projection["revision"], 3)
            self.assertEqual(
                projection["source_packet_sha256"], packet["packet_sha256"]
            )
            current_status = root / ".continuity/STATUS.current.md"
            self.assertIn("M10-09", current_status.read_text(encoding="utf-8"))

            current_status.write_text("# stale projection\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["resume", "--root", str(root)])
            repaired = current_status.read_text(encoding="utf-8")
            self.assertIn("M10-09", repaired)
            self.assertNotIn("stale projection", repaired)
            schema = json.loads(
                (
                    Path(__file__).parents[1]
                    / "schemas/m10-11/recovery-envelope.schema.json"
                ).read_text(encoding="utf-8")
            )
            validator = Draft202012Validator(schema)
            validator.validate(packet)
            with self.assertRaises(ValidationError):
                validator.validate({**packet, "unexpected": True})

            (root / "STATUS.md").write_text("# Updated Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
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
            refresh_output = StringIO()
            with redirect_stdout(refresh_output):
                refresh_result = main(
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
            refreshed = store.read_project("sample-app")
            refreshed_claim = next(
                item for item in refreshed["claims"] if item["claim_id"] == "claim-current"
            )
            refreshed_work = next(
                item for item in refreshed["works"] if item["work_id"] == "M10-09"
            )
            self.assertEqual(refresh_result, 0)
            self.assertEqual(json.loads(refresh_output.getvalue())["status"], "refreshed")
            self.assertEqual(refreshed["project"]["revision"], 4)
            self.assertEqual(refreshed_claim["expected_project_revision"], 4)
            self.assertEqual(len(refreshed_work["evidence_ids"]), 2)
            self.assertEqual(len(store.read_events("sample-app")), 4)

            with redirect_stdout(StringIO()):
                main(["checkpoint", "create", "--root", str(root)])
            refreshed_resume = StringIO()
            with redirect_stdout(refreshed_resume):
                main(["resume", "--root", str(root)])
            self.assertEqual(json.loads(refreshed_resume.getvalue())["read_only"], False)

    def test_legacy_revision_zero_project_can_attach_without_reinitializing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MASTER.md").write_text("# Existing Master\n", encoding="utf-8")
            (root / "STATUS.md").write_text("# Existing Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            state_path = root / ".continuity/state.sqlite3"
            state_path.unlink()
            store = SQLiteStateStore(state_path)
            store.initialize()
            store.create_project(_initial_state("sample-app"))

            with redirect_stdout(StringIO()):
                main(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "legacy-work", "--work-title", "Continue legacy work",
                        "--owner-ref", "agent-main", "--scope", "capability:legacy-work",
                    ]
                )
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "attach", "approve", "--root", str(root),
                        "--actor-ref", "agent-main", "--claim-id", "claim-legacy",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["revision"], 2)
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
            self.assertEqual(state["project"]["revision"], 1)

    def test_suspend_dependency_preserves_incomplete_work_and_activates_prerequisite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MASTER.md").write_text("# Existing Master\n", encoding="utf-8")
            (root / "STATUS.md").write_text("# Existing Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
                main(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "M10-09", "--work-title", "Finish the throughput gate",
                        "--owner-ref", "agent-main", "--scope", "capability:network-cc",
                    ]
                )
                main(
                    [
                        "attach", "approve", "--root", str(root),
                        "--actor-ref", "agent-main", "--claim-id", "claim-current",
                    ]
                )
                main(["checkpoint", "create", "--root", str(root)])

            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "work", "suspend-dependency", "--root", str(root),
                        "--work-id", "M10-09", "--claim-id", "claim-current",
                        "--actor-ref", "agent-main",
                        "--dependency-work-id", "M10-09-IO",
                        "--dependency-work-title", "Remove the file I/O bottleneck",
                        "--dependency-scope", "capability:filetransfer-transfer",
                        "--reason", "The final network gate requires async durable file I/O",
                    ]
                )

            response = json.loads(output.getvalue())
            store = SQLiteStateStore(root / ".continuity/state.sqlite3")
            state = store.read_project("sample-app")
            old_work = next(item for item in state["works"] if item["work_id"] == "M10-09")
            dependency = next(
                item for item in state["works"] if item["work_id"] == "M10-09-IO"
            )
            old_claim = next(
                item for item in state["claims"] if item["claim_id"] == "claim-current"
            )
            active_claim = next(item for item in state["claims"] if item["status"] == "active")
            blocker = next(
                item for item in state["blockers"] if item["blocker_id"] == response["blocker_id"]
            )

            self.assertEqual(result, 0)
            self.assertEqual(response["status"], "dependency-transitioned")
            self.assertEqual(response["revision"], 5)
            self.assertEqual(response["active_work_id"], "M10-09-IO")
            self.assertEqual(response["claim_id"], active_claim["claim_id"])
            self.assertEqual(response["next_action"], "continue-active-work")
            self.assertEqual(old_work["status"], "ready")
            self.assertNotEqual(old_work["status"], "completed")
            self.assertIn(blocker["blocker_id"], old_work["blocker_ids"])
            self.assertEqual(blocker["status"], "open")
            self.assertEqual(blocker["blocked_work_ids"], ["M10-09"])
            self.assertEqual(old_claim["status"], "released")
            self.assertEqual(dependency["status"], "active")
            self.assertEqual(dependency["parent_work_id"], "M10-09")
            self.assertEqual(
                dependency["scope_refs"],
                [{"scope_kind": "capability", "scope_ref": "filetransfer-transfer"}],
            )
            self.assertEqual(state["project"]["primary_work_id"], "M10-09-IO")
            self.assertEqual(state["project"]["active_work_ids"], ["M10-09-IO"])

            resume = StringIO()
            with redirect_stdout(resume):
                main(["resume", "--root", str(root)])
            packet = json.loads(resume.getvalue())
            self.assertEqual(packet["active_work"]["work_id"], "M10-09-IO")
            self.assertEqual(packet["claim"]["claim_id"], active_claim["claim_id"])
            self.assertEqual(packet["open_blockers"][0]["blocker_id"], blocker["blocker_id"])
            self.assertEqual(packet["return_point_work_id"], "M10-09")
            self.assertTrue(packet["checkpoint_verified"])
            self.assertFalse(packet["read_only"])

    def test_attach_refresh_rebinds_changed_sources_without_state_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            master = root / "MASTER.md"
            status = root / "STATUS.md"
            master.write_text("# Existing Master\n", encoding="utf-8")
            status.write_text("# Existing Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
                main(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "M10-09", "--work-title", "Continue the mainline",
                        "--owner-ref", "agent-main", "--scope", "repo:repo://sample-app",
                    ]
                )
            status.write_text("# Updated Status\n", encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = main(["attach", "refresh", "--root", str(root)])
            proposal = json.loads(
                (root / ".continuity/attach-proposal.json").read_text(encoding="utf-8")
            )
            state = SQLiteStateStore(root / ".continuity/state.sqlite3").read_project(
                "sample-app"
            )
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "refreshed")
            self.assertEqual(proposal["work"]["work_id"], "M10-09")
            self.assertNotEqual(
                proposal["sources"][1]["content_sha256"],
                "0" * 64,
            )
            self.assertEqual(state["project"]["revision"], 1)

    def test_attach_refresh_without_source_change_preserves_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MASTER.md").write_text("# Existing Master\n", encoding="utf-8")
            (root / "STATUS.md").write_text("# Existing Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
                main(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "M10-09", "--work-title", "Continue the mainline",
                        "--owner-ref", "agent-main", "--scope", "repo:repo://sample-app",
                    ]
                )
            proposal_path = root / ".continuity/attach-proposal.json"
            before = proposal_path.read_bytes()
            before_sha256 = json.loads(before)["proposal_sha256"]
            output = StringIO()

            with redirect_stdout(output):
                result = main(["attach", "refresh", "--root", str(root)])

            response = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertEqual(response["status"], "unchanged")
            self.assertEqual(response["proposal_sha256"], before_sha256)
            self.assertEqual(response["changed_sources"], [])
            self.assertEqual(proposal_path.read_bytes(), before)

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
            ignore = (root / ".continuity/.gitignore").read_text(encoding="utf-8")
            self.assertIn("STATUS.current.md", ignore)
            self.assertIn("state.sqlite3", ignore)
            self.assertNotIn("MASTER.md", ignore)
            self.assertNotIn("project.yaml", ignore)
            state = SQLiteStateStore(
                root / ".continuity/state.sqlite3"
            ).read_project("sample-app")
            self.assertEqual(state["project"]["revision"], 1)
            self.assertEqual(state["project"]["primary_work_id"], None)
            self.assertEqual(state["works"][0]["work_id"], "work-initial")
            self.assertEqual(
                len(
                    SQLiteStateStore(
                        root / ".continuity/state.sqlite3"
                    ).read_events("sample-app")
                ),
                1,
            )
            self.assertTrue((root / ".continuity/attach-proposal.json").is_file())
            self.assertTrue((root / ".continuity/checkpoint-ref.json").is_file())
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

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / ".continuity"
            target.write_text("owned file\n", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            self.assertEqual(target.read_text(encoding="utf-8"), "owned file\n")

    def test_git_main_root_and_sibling_worktree_share_one_continuity_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            main_root = base / "repo"
            execution_root = base / "execution"
            sibling_root = base / "sibling"
            main_root.mkdir()
            subprocess.run(["git", "init", "-q", str(main_root)], check=True)
            subprocess.run(
                ["git", "-C", str(main_root), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(main_root), "config", "user.name", "Continuity Test"],
                check=True,
            )
            (main_root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(main_root), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(main_root), "commit", "-qm", "test: initialize"],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(main_root), "worktree", "add", "-q",
                    "--detach", str(execution_root),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(main_root), "worktree", "add", "-q",
                    "--detach", str(sibling_root),
                ],
                check=True,
            )
            (execution_root / "MASTER.md").write_text("# Master\n", encoding="utf-8")
            (execution_root / "STATUS.md").write_text("# Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(execution_root), "--project-id", "sample-app"])
                main(
                    [
                        "attach", "plan", "--root", str(execution_root),
                        "--master", "MASTER.md", "--status", "STATUS.md",
                        "--work-id", "M10-09", "--work-title", "Continue mainline",
                        "--owner-ref", "agent-main", "--scope", "repo:repo://sample-app",
                    ]
                )
                main(
                    [
                        "attach", "approve", "--root", str(execution_root),
                        "--actor-ref", "agent-main", "--claim-id", "claim-current",
                    ]
                )
                main(["checkpoint", "create", "--root", str(execution_root)])

            main_output = StringIO()
            sibling_output = StringIO()
            with redirect_stdout(main_output):
                main(["resume", "--root", str(main_root)])
            with redirect_stdout(sibling_output):
                main(["state", "show", "--root", str(sibling_root)])

            packet = json.loads(main_output.getvalue())
            state = json.loads(sibling_output.getvalue())
            common_dir = Path(
                subprocess.run(
                    [
                        "git", "-C", str(main_root), "rev-parse",
                        "--path-format=absolute", "--git-common-dir",
                    ],
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
            )
            binding = json.loads(
                (common_dir / "continuity-plane/project-root.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(packet["project_id"], "sample-app")
            self.assertEqual(packet["revision"], 3)
            self.assertEqual(state["revision"], 3)
            self.assertEqual(Path(binding["control_root"]), execution_root.resolve())
            self.assertFalse((main_root / ".continuity").exists())
            self.assertFalse((sibling_root / ".continuity").exists())

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

    def test_status_render_writes_current_only_bilingual_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
                main(
                    [
                        "attach", "plan", "--root", str(root),
                        "--master", ".continuity/MASTER.md", "--status", ".continuity/STATUS.md",
                        "--work-id", "M10-09", "--work-title", "Continue the mainline",
                        "--owner-ref", "agent-main", "--scope", "repo:repo://sample-app",
                    ]
                )
                main(
                    [
                        "attach", "approve", "--root", str(root),
                        "--actor-ref", "agent-main", "--claim-id", "claim-current",
                    ]
                )
                main(["checkpoint", "create", "--root", str(root)])
            output = StringIO()
            with redirect_stdout(output):
                result = main(["status", "render", "--root", str(root)])
            self.assertEqual(result, 0)
            response = json.loads(output.getvalue())
            self.assertEqual(response["status"], "rendered")
            chinese = (root / ".continuity/STATUS.current.md").read_text(encoding="utf-8")
            english = (root / ".continuity/STATUS.current.en.md").read_text(encoding="utf-8")
            self.assertIn("M10-09", chinese)
            self.assertIn("claim-current", english)
            self.assertNotIn("work-initial", chinese)
            projection = json.loads(
                (root / ".continuity/status-projection.json").read_text(encoding="utf-8")
            )
            self.assertEqual(projection["state_write_authority"], False)
            schema = json.loads(
                (Path(__file__).parents[1] / "schemas/m10-11/status-projection.schema.json").read_text(
                    encoding="utf-8"
                )
            )
            Draft202012Validator(schema).validate(projection)

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
            self.assertEqual(response["revision"], 1)
            self.assertEqual(response["event_head"]["sequence_no"], 1)
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
