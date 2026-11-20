"""Antigravity native hook, installation and bridge contracts."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from continuity_plane import agy_hook, agy_integration, remote_hook
from continuity_plane.collaboration_registry import list_changes, register_project
from continuity_plane.handoff_journal import report
from continuity_plane.intent_todo import load_queue
from continuity_plane.release_version import VERSION


class AntigravityIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.data = self.base / "data"
        isolated = patch.dict(
            os.environ,
            {
                "CONTINUITY_DATA_ROOT": str(self.data),
                "CONTINUITY_UPDATE_CHECK": "0",
                "CONTINUITY_ASSIGNEE": "fixture-worker",
            },
            clear=False,
        )
        isolated.start()
        self.addCleanup(isolated.stop)
        for name in (
            "ROLE",
            "TASK_ID",
            "CONTROLLER",
            "CONTROLLER_TOKEN",
            "DISABLED",
            "WATCH_PROJECTS",
        ):
            os.environ.pop(f"CONTINUITY_{name}", None)
        register_project(self.data, project_id="fixture", control_root=self.root)

    def native(self, event, **fields):
        return {
            "hookEventName": event,
            "conversationId": "conversation",
            "workspacePaths": [str(self.root)],
            "transcriptPath": str(self.base / "transcript.jsonl"),
            **fields,
        }

    def inject(self, output):
        self.assertEqual(set(output), {"injectSteps"})
        steps = output["injectSteps"]
        self.assertEqual(len(steps), 1)
        self.assertEqual(set(steps[0]), {"systemMessage"})
        return steps[0]["systemMessage"]["systemMessage"]

    def test_normalization_and_supported_event_mapping(self):
        expected = {
            "SessionStart": "SessionStart",
            "PreInvocation": "UserPromptSubmit",
            "PreToolUse": "PreToolUse",
            "PostToolUse": "PostToolUse",
            "PostInvocation": "PostInvocation",
            "Stop": "PreCompact",
        }
        for native, shared in expected.items():
            with self.subTest(event=native):
                payload = agy_hook.normalize(
                    self.native(native, lastUserInput="prompt")
                )
                self.assertEqual(payload["session_id"], "conversation")
                self.assertEqual(payload["cwd"], str(self.root))
                self.assertEqual(
                    payload["transcript_path"], str(self.base / "transcript.jsonl")
                )
                self.assertEqual(payload["prompt"], "prompt")
                self.assertEqual(payload["hook_event_name"], shared)
        cwd = self.base / "nested"
        payload = agy_hook.normalize(self.native("SessionStart", cwd=str(cwd)))
        self.assertEqual(payload["cwd"], str(cwd))
        self.assertIsNone(agy_hook.normalize(self.native("PreCompact")))

    def test_start_and_prompt_once_per_turn_including_repeated_text(self):
        start = self.native("SessionStart")
        with patch(
            f"{agy_hook.__package__}.update_check.session_note",
            return_value="A newer release is available.",
        ):
            text = self.inject(agy_hook.respond(start))
        self.assertIn("Resume the interrupted task", text)
        self.assertIn("A newer release is available", text)
        first = self.native(
            "PreInvocation",
            invocationNum=0,
            initialNumSteps=1,
            lastUserInput="Complete the checklist:\n- Fix the parser\n- Add tests",
        )
        self.assertIn("Continuity is connected", self.inject(agy_hook.respond(first)))
        queue = load_queue(self.root, self.data)
        self.assertEqual(queue["owner_session_id"], "conversation")
        self.assertEqual(len(queue["items"]), 2)
        self.assertEqual(agy_hook.respond(first), {})
        self.assertEqual(
            agy_hook.respond({**first, "invocationNum": 1, "initialNumSteps": 4}), {}
        )
        new_turn = {**first, "initialNumSteps": 6}
        with patch(
            f"{agy_hook.__package__}.planning_adoption.context",
            return_value="New user turn",
        ) as plan:
            self.assertIn("New user turn", self.inject(agy_hook.respond(new_turn)))
            self.assertEqual(agy_hook.respond(new_turn), {})
            plan.assert_called_once()
        dedup = next((self.data / "agy-hooks").glob("*.json")).read_text()
        self.assertNotIn(first["lastUserInput"], dedup)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_post_tool_accounts_and_defers_reminders_to_pre_invocation(self):
        post = self.native(
            "PostToolUse",
            toolCall={
                "name": "run_command",
                "args": {"CommandLine": "continuity collaboration task list"},
            },
            error="failure",
        )
        self.assertEqual(agy_hook.respond(post), {})
        mcp = self.native(
            "PostToolUse",
            toolCall={
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "continuity-plane_continuity",
                    "ToolName": "continuity_plan",
                    "Arguments": {"action": "task.show"},
                },
            },
        )
        self.assertEqual(agy_hook.respond(mcp), {})
        rows = [
            json.loads(line)
            for path in (self.data / "adoption").glob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        self.assertEqual(
            [(row["host"], row["channel"], row["op"], row["ok"]) for row in rows],
            [
                ("antigravity", "cli", "task.list", False),
                ("antigravity", "mcp", "task.show", True),
            ],
        )
        self.assertEqual(agy_hook.respond(self.native("PostInvocation")), {})
        pre = self.native("PreInvocation", invocationNum=1, lastUserInput="Same turn")
        self.assertIn("tool continuity_plan", self.inject(agy_hook.respond(pre)))
        self.assertEqual(agy_hook.respond(pre), {})

    def test_pre_tool_is_always_allow(self):
        pre = self.native("PreToolUse", toolCall={"name": "run_command", "args": {}})
        with patch.object(agy_hook, "handle_event", side_effect=AssertionError):
            self.assertEqual(agy_hook.respond(pre), {"decision": "allow"})
            self.assertEqual(
                agy_hook.respond(pre, config_path=self.base / "missing"),
                {"decision": "allow"},
            )
        with patch.dict(os.environ, {"CONTINUITY_DISABLED": "1"}):
            self.assertEqual(agy_hook.respond(pre), {"decision": "allow"})

    def test_stop_writes_shared_handoff_and_resume_verifies_it(self):
        start = self.native("SessionStart")
        agy_hook.respond(start)
        agy_hook.respond(
            self.native("PreInvocation", invocationNum=0, lastUserInput="First turn")
        )
        stop = self.native(
            "Stop", executionNum=0, fullyIdle=True, terminationReason="ERROR"
        )
        self.assertEqual(agy_hook.respond(stop), {"decision": "allow"})
        self.assertIn(
            "session.stop-failed",
            [
                row["event"]
                for row in list_changes(self.data, project_id="fixture")["changes"]
            ],
        )
        pre = self.native("PreInvocation", lastUserInput="Next turn", initialNumSteps=5)
        self.assertIn(
            "Continue the host's latest unfinished user instruction",
            self.inject(agy_hook.respond(pre)),
        )
        self.assertEqual(agy_hook.respond(pre), {})
        receipts = report(self.root, self.data)
        self.assertEqual(receipts["turn_boundaries"]["paired_handoffs"], 1)
        self.assertEqual(receipts["native_events"]["paired_compactions"], 0)
        self.assertEqual(
            agy_hook.respond(stop, config_path=self.base / "missing"),
            {"decision": "allow"},
        )

    def test_opted_in_state_checkpoint_uses_existing_create_and_verify(self):
        package = agy_hook.__package__
        with (
            patch.dict(os.environ, {"CONTINUITY_STATE_CHECKPOINTS": "1"}),
            patch(f"{package}.lifecycle.state_checkpoint") as checkpoint,
        ):
            agy_hook.respond(self.native("Stop"))
            agy_hook.respond(self.native("SessionStart"))
        self.assertEqual(
            [call.args[1] for call in checkpoint.call_args_list], ["create", "verify"]
        )

    def test_workspace_install_and_dry_run_are_idempotent(self):
        from continuity_plane.cli import entrypoint
        from contextlib import redirect_stdout
        from io import StringIO

        args = ["agy", "install", "--workspace", str(self.root)]
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(entrypoint([*args, "--dry-run"]), 0)
        self.assertTrue(json.loads(output.getvalue())["dry_run"])
        self.assertFalse((self.root / ".agents").exists())
        self.assertFalse((self.data / "adoption").exists())
        installed = agy_integration.install("workspace", self.root)
        target = Path(installed["target"])
        mtime = (target / "plugin.json").stat().st_mtime_ns
        self.assertTrue(installed["changed"])
        self.assertFalse(agy_integration.install("workspace", self.root)["changed"])
        self.assertEqual((target / "plugin.json").stat().st_mtime_ns, mtime)
        self.assertEqual(
            {p.name for p in target.iterdir()},
            {"plugin.json", "mcp_config.json", "hooks.json", "skills"},
        )
        bundle = agy_integration.bundle()
        claude = (
            bundle.parents[1]
            / "claude/continuity-plane/skills/continuity-collaboration"
        )
        if claude.is_dir():
            for path in claude.rglob("*.md"):
                installed = (
                    target
                    / "skills/continuity-collaboration"
                    / path.relative_to(claude)
                )
                self.assertEqual(installed.read_bytes(), path.read_bytes())
        with patch.object(Path, "home", return_value=self.base / "isolated-home"):
            plan = agy_integration.install("global", self.root, dry_run=True)
            self.assertIn(".gemini/config/plugins", Path(plan["target"]).as_posix())
            self.assertFalse((self.base / "isolated-home").exists())

    @unittest.skipUnless(shutil.which("agy"), "Antigravity CLI is not installed")
    def test_native_strict_validation(self):
        done = subprocess.run(
            [
                "agy",
                "plugin",
                "validate",
                str(agy_integration.bundle()),
                "--strict",
                "--json",
            ],
            capture_output=True,
            check=True,
            timeout=20,
            text=True,
        )
        components = json.loads(done.stdout)["components"]
        self.assertEqual(
            (components["mcpServers"], components["hooks"], components["skills"]),
            (2, 1, 1),
        )

    def test_status_checks_native_discovery_and_each_mcp_version(self):
        target = Path(agy_integration.install("workspace", self.root)["target"])
        actions = [
            {"event": event, "command": "continuity-agy-hook"}
            for event in agy_hook.EVENTS.keys() - {"PostInvocation"}
        ]
        hooks = {
            "hooks": [
                {
                    "name": "continuity-plane",
                    "enabled": True,
                    "source": str(target / "hooks.json"),
                    "actions": actions,
                }
            ]
        }
        package = agy_hook.__package__
        with (
            patch(f"{package}.agy_probe._read", side_effect=["1.5.13", VERSION]),
            patch(f"{package}.agy_probe.slash", return_value=hooks),
            patch.object(shutil, "which", side_effect=lambda command: command),
            patch.object(
                agy_integration, "_mcp_version", side_effect=[VERSION, "0.0.0"]
            ),
            patch.object(Path, "home", return_value=self.base / "isolated-home"),
        ):
            result = agy_integration.status(self.root, "agy")
        self.assertEqual(result["status"], "degraded")
        self.assertCountEqual(
            result["hooks"]["discovered_events"], [row["event"] for row in actions]
        )
        self.assertTrue(result["hooks"]["versions_match"])
        self.assertTrue(result["mcp"]["continuity"]["version_matches"])
        self.assertFalse(result["mcp"]["continuity-search"]["version_matches"])

    @unittest.skipIf(
        os.name == "nt", "SSH bridge fixture uses Unix SSH command quoting"
    )
    def test_bridge_installs_and_runs_real_receiver_without_transcript_transfer(self):
        remote = self.base / "remote"
        remote.mkdir()
        state = self.base / "remote-state"
        register_project(state, project_id="fixture", control_root=remote)
        config_path = self.base / "bridge config.json"
        config_path.write_text(
            json.dumps(
                {
                    "host": "state-host",
                    "local_root": str(self.root),
                    "remote_root": str(remote),
                    "local_data_root": str(self.data),
                    "forward_checklists": False,
                }
            )
        )
        installed = agy_integration.install(
            "workspace", self.root, bridge_config=config_path
        )
        hooks = json.loads((Path(installed["target"]) / "hooks.json").read_text())
        command = hooks["continuity-plane"]["Stop"][0]["command"]
        self.assertEqual(
            shlex.split(command),
            ["continuity-remote-hook", "--agy", "--config", str(config_path)],
        )
        self.assertFalse(
            agy_integration.install("workspace", self.root, bridge_config=config_path)[
                "changed"
            ]
        )

        sent = []

        def runner(argv, **kwargs):
            outgoing = json.loads(kwargs["input"])
            sent.append(outgoing)
            self.assertEqual(argv[-2], "state-host")
            self.assertEqual(outgoing["cwd"], str(remote))
            self.assertNotIn("transcript_path", outgoing)
            self.assertNotIn("prompt", outgoing)
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    f"from {agy_hook.__package__}.remote_hook import receive_main; raise SystemExit(receive_main())",
                ],
                input=kwargs["input"],
                capture_output=True,
                text=True,
                timeout=10,
                env={**os.environ, "CONTINUITY_DATA_ROOT": str(state)},
            )

        forward = remote_hook.forward
        with (
            patch.object(Path, "home", return_value=self.base / "isolated-home"),
            patch.object(
                remote_hook,
                "forward",
                side_effect=lambda payload, config, **kw: forward(
                    payload, config, runner=runner, **kw
                ),
            ),
        ):
            stop = self.native("Stop", terminationReason="ERROR")
            self.assertEqual(
                agy_hook.respond(stop, config_path=config_path), {"decision": "allow"}
            )
            start = self.native("SessionStart")
            self.assertIn(
                "Continue the host's latest unfinished",
                self.inject(agy_hook.respond(start, config_path=config_path)),
            )
        self.assertEqual(
            [row["hook_provider"] for row in sent], ["antigravity", "antigravity"]
        )
        self.assertEqual(sent[0]["termination_reason"], "ERROR")
        self.assertEqual(report(remote, state)["turn_boundaries"]["paired_handoffs"], 1)
        self.assertIn(
            "session.stop-failed",
            [
                row["event"]
                for row in list_changes(state, project_id="fixture")["changes"]
            ],
        )

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                remote_hook.__name__,
                "--agy",
                "--config",
                str(config_path),
            ],
            input=json.dumps(self.native("PreToolUse")),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        self.assertEqual(json.loads(completed.stdout), {"decision": "allow"})


if __name__ == "__main__":
    unittest.main()
