"""Adoption telemetry: how agents reach Continuity (MCP versus CLI)."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from continuity_plane import adoption
from continuity_plane.cli import entrypoint


class OperationNamesTests(unittest.TestCase):
    def test_cli_words_map_to_planning_actions(self):
        cases = {
            ("collaboration", "task", "add", "--project-id", "p"): "task.add",
            ("task", "update", "--task-id", "x"): "task.update",
            ("--remote", "state-host", "worker", "dispatch"): "worker.dispatch",
            ("handoff", "write", "--plan", "x"): "handoff.write",
            ("todo", "list"): "todo.list",
        }
        for words, expected in cases.items():
            self.assertEqual(adoption.operation(list(words)), expected, words)

    def test_shell_commands_find_continuity_invocations(self):
        self.assertEqual(
            adoption.shell_operations(
                "ssh state-host 'cd ~/p && continuity task add --project-id p'"
            ),
            ["task.add"],
        )
        self.assertEqual(
            adoption.shell_operations("/opt/bin/continuity worker jobs --status ended"),
            ["worker.jobs"],
        )
        self.assertEqual(adoption.shell_operations("echo continuity-plane"), [])
        self.assertEqual(adoption.shell_operations("ls docs/continuity"), [])

    def test_mcp_equivalents(self):
        self.assertEqual(
            adoption.mcp_equivalent("task.add"), "continuity_plan(action=task.add)"
        )
        self.assertEqual(adoption.mcp_equivalent("todo.list"), "continuity_todo_list")
        self.assertEqual(
            adoption.mcp_equivalent("context.lookup"), "continuity_context_lookup"
        )
        self.assertIsNone(adoption.mcp_equivalent("worker.dispatch"))

    def test_tool_calls_are_classified_without_arguments(self):
        self.assertEqual(
            adoption.classify_tool_call(
                "mcp__continuity__continuity_plan",
                {"action": "task.add", "parameters": {"body": "secret"}},
            ),
            [{"channel": "mcp", "tool": "continuity_plan", "op": "task.add"}],
        )
        self.assertEqual(
            adoption.classify_tool_call(
                "mcp__continuity-search__continuity_context_lookup", {"query": "x"}
            )[0]["channel"],
            "mcp",
        )
        plugin = adoption.classify_tool_call(
            "mcp__plugin_continuity-plane_continuity__continuity_task_add", {}
        )
        self.assertEqual(plugin[0]["tool"], "continuity_task_add")
        search = adoption.classify_tool_call(
            "mcp__plugin_continuity-plane_continuity-search__continuity_context_lookup", {}
        )
        self.assertEqual(search[0]["tool"], "continuity_context_lookup")
        self.assertEqual(adoption.classify_tool_call("mcp__other__continuity_x", {}), [])
        cli = adoption.classify_tool_call(
            "Bash", {"command": "continuity collaboration task update --body hi"}
        )
        self.assertEqual(cli[0]["channel"], "cli")
        self.assertEqual(cli[0]["op"], "task.update")
        self.assertNotIn("secret", json.dumps(cli))
        self.assertEqual(adoption.classify_tool_call("Read", {"path": "x"}), [])


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, tool, tool_input, session="s1", host="claude"):
        adoption.record_hook(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session,
                "cwd": "/work/p",
                "tool_name": tool,
                "tool_input": tool_input,
            },
            host,
            self.root,
        )

    def test_report_counts_mcp_share_per_host_and_gaps(self):
        self.hook("mcp__continuity__continuity_plan", {"action": "task.add"})
        self.hook("mcp__continuity__continuity_plan", {"action": "task.update"})
        self.hook("Bash", {"command": "continuity task update --task-id x"})
        self.hook("Bash", {"command": "continuity worker dispatch --task-id x"}, "s2", "codex")
        self.hook("Read", {"path": "README.md"})
        report = adoption.summarize(adoption.load(self.root))
        agent = report["agent_view"]
        self.assertEqual((agent["mcp_calls"], agent["cli_calls"]), (2, 2))
        self.assertEqual(agent["mcp_share_percent"], 50.0)
        self.assertEqual(agent["sessions"], 2)
        self.assertEqual(agent["sessions_using_mcp"], 1)
        self.assertEqual(agent["by_host"]["codex"]["mcp_share_percent"], 0.0)
        self.assertEqual(agent["cli_with_mcp_tool"][0]["op"], "task.update")
        self.assertEqual(agent["cli_without_mcp_tool"], [("worker.dispatch", 1)])
        self.assertIn("MCP share 50.0%", adoption.brief(report))

    def test_disabled_records_nothing(self):
        with patch.dict(os.environ, {"CONTINUITY_ADOPTION_TELEMETRY": "0"}):
            self.hook("mcp__continuity__continuity_help", {})
        self.assertEqual(adoption.load(self.root), [])

    def test_bridge_spool_survives_coalescing_and_records_remotely(self):
        spool = self.root / "spool.jsonl"
        for _ in range(35):
            adoption.spool_tool_call(
                spool,
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "s",
                    "tool_name": "mcp__continuity__continuity_inbox",
                    "tool_input": {"action": "list"},
                },
                "claude",
            )
        first = adoption.drain_spool(spool)
        self.assertEqual(len(first), 30)
        self.assertEqual(len(adoption.drain_spool(spool)), 5)
        self.assertFalse(spool.exists())
        with patch.dict(os.environ, {"CONTINUITY_DATA_ROOT": str(self.root)}):
            kept = adoption.record_forwarded(first + [{"channel": "bogus"}], "/srv/p")
        self.assertEqual(kept, 30)
        events = adoption.load(self.root)
        self.assertEqual({e["source"] for e in events}, {"hook"})
        self.assertEqual(events[0]["cwd"], "/srv/p")

    def test_cli_entrypoint_records_operation_and_caller(self):
        env = {
            "CONTINUITY_DATA_ROOT": str(self.root),
            "CLAUDECODE": "1",
        }
        with patch.dict(os.environ, env), patch("sys.stdout"):
            code = entrypoint(["observe", "adoption", "--days", "1"])
        self.assertEqual(code, 0)
        cli = [e for e in adoption.load(self.root) if e["source"] == "cli"]
        self.assertEqual(cli[0]["op"], "observe.adoption")
        self.assertEqual(cli[0]["caller"], "claude-code")
        self.assertTrue(cli[0]["ok"])

    def test_mcp_server_records_client_tool_and_outcome(self):
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "clientInfo": {"name": "claude-code", "version": "x"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "continuity_help", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "continuity_plan", "arguments": {"action": "nope"}},
            },
        ]
        env = {**os.environ, "CONTINUITY_DATA_ROOT": str(self.root)}
        subprocess.run(
            [sys.executable, "-m", "continuity_plane.codex_mcp_server"],
            input="".join(json.dumps(r) + "\n" for r in requests),
            text=True,
            capture_output=True,
            env=env,
            timeout=60,
            check=False,
        )
        calls = [e for e in adoption.load(self.root) if e["source"] == "mcp"]
        self.assertEqual([c["op"] for c in calls], ["continuity_help", "nope"])
        self.assertEqual([c["ok"] for c in calls], [True, False])
        self.assertEqual({c["client"] for c in calls}, {"claude-code"})


if __name__ == "__main__":
    unittest.main()
