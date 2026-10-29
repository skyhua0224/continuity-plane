"""CLI/MCP planning parity without State claims, providers or host config writes."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from continuity_plane.cli import main
from continuity_plane.collaboration_registry import list_tasks, register_project
from continuity_plane.intent_todo import load_queue
from continuity_plane.planning_api import (
    OPERATIONS,
    _parser,
    execute,
    handle_tool,
    help_for,
    render,
)


class PlanningAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repo"
        self.root.mkdir()
        self.data = Path(temporary.name) / "data"
        register_project(self.data, project_id="sample", control_root=self.root)

    def execute(self, operation, **parameters):
        return execute(
            operation,
            parameters,
            root=str(self.root),
            project_id="sample",
            data_root=str(self.data),
        )

    def card(self, task_id="task"):
        return self.execute(
            "task.add",
            task_id=task_id,
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="Scoped task",
            objective="Check parity",
            next_action="Run regression",
            exit_criteria=["focused tests pass"],
            allowed_files=["src/"],
            worktree=str(self.root),
            body="Check parity between CLI and MCP",
        )

    def tool(self, name, **arguments):
        return handle_tool(
            name,
            {
                "root": str(self.root),
                "project_id": "sample",
                "data_root": str(self.data),
                **arguments,
            },
        )

    def test_help_covers_every_real_parameter(self):
        for operation in OPERATIONS:
            actual = {
                flag
                for action in _parser(operation)._actions
                for flag in action.option_strings
                if flag not in {"-h", "--help"}
            }
            described = {
                flag
                for spec in help_for(operation)["parameters"]
                for flag in spec["flags"]
            }
            self.assertEqual(actual, described, operation)
        names = {spec["name"] for spec in help_for("task.add")["parameters"]}
        self.assertTrue(
            {"assignee", "worktree", "allowed_file", "exit_criterion", "verification"}
            <= names
        )
        self.assertIn("activate", help_for("todo.activate")["help"])

    def test_task_create_update_show_and_list_are_scoped(self):
        self.card()
        self.execute(
            "task.update",
            task_id="task",
            status="ready-for-review",
            result="Honest worker report",
            verification="focused regression PASS",
            branch="feature",
            commit="01234567",
            allowed_files=["src/new.py"],
            worktree=str(self.root / "worktree"),
        )
        shown = self.execute("task.show", task_id="task")
        self.assertEqual(shown["allowed_files"], ["src/new.py"])
        self.assertEqual(shown["worktree"], str(self.root / "worktree"))
        self.execute("task.update", task_id="task", status="merged")
        self.assertEqual(
            self.execute("task.show", task_id="task")["result"], "Honest worker report"
        )
        self.assertEqual(self.execute("task.list")["items"], [])
        rows = self.execute("task.list", all=True, full=True)
        self.assertEqual(rows["items"][0]["objective"], "Check parity")
        self.assertEqual(
            self.execute("task.list", status="merged")["items"][0]["task_id"],
            "task",
        )

    def test_task_default_filter_honors_updated_after_and_budget(self):
        for i in range(12):
            self.card(f"task-{i}")
        self.assertEqual(
            self.execute("task.list", updated_after="2999-01-01T00:00:00+00:00")[
                "items"
            ],
            [],
        )
        reply = self.tool(
            "continuity_plan",
            action="task.list",
            parameters={"all": True},
            format="json",
            max_output_bytes=1024,
        )
        payload = reply["content"][0]["text"]
        self.assertLessEqual(len(payload.encode()), 1024)
        view = json.loads(payload)
        self.assertTrue(view["truncated"])
        self.assertNotIn("structuredContent", reply)

    def test_todo_full_flow_and_explicit_switch_preserve_progress(self):
        self.card()
        queue = self.execute(
            "todo.compile",
            prompt="- first\n- second\n- third",
            session_id="session-a",
            assignee="controller",
        )
        self.assertEqual(queue["owner_session_id"], "session-a")
        ids = [item["item_id"] for item in queue["items"]]
        self.execute("todo.link", item_id=ids[0], task_id="task")
        self.execute("todo.complete", item_id=ids[0], result="verified proof")
        switched = self.execute(
            "todo.activate",
            item_id=ids[2],
            reason="User inserted urgent work",
            session_id="session-a",
        )
        self.assertEqual(switched["items"][0]["result"], "verified proof")
        self.assertEqual(switched["items"][2]["status"], "active")
        self.assertEqual(switched["items"][1]["status"], "queued")
        self.assertEqual(switched["switch_history"][-1]["from"], [ids[1]])
        self.assertEqual(
            self.execute("todo.activate", item_id=ids[2], reason="same"), switched
        )
        advanced = self.execute("todo.complete", item_id=ids[2], result="done")
        self.assertEqual(advanced["items"][1]["status"], "active")

    def test_unknown_link_is_rejected_without_changing_queue(self):
        queue = self.execute("todo.compile", prompt="- first\n- second")
        with self.assertRaisesRegex(ValueError, "task_id does not exist"):
            self.execute(
                "todo.link", item_id=queue["items"][0]["item_id"], task_id="wrong"
            )
        self.assertEqual(load_queue(self.root, self.data), queue)

    def test_blocking_queued_item_does_not_stop_current_and_blocked_is_not_done(self):
        queue = self.execute("todo.compile", prompt="- first\n- second")
        first, second = [i["item_id"] for i in queue["items"]]
        blocked = self.execute(
            "todo.block", item_id=second, reason="External approval needed"
        )
        self.assertEqual(blocked["status"], "active")
        finished = self.execute("todo.complete", item_id=first, result="pass")
        self.assertEqual(finished["status"], "blocked")
        self.assertEqual(finished["items"][1]["status"], "blocked")
        with self.assertRaisesRegex(ValueError, "unfinished"):
            self.execute("todo.block", item_id=first, reason="wrong")

    def test_project_id_only_todo_alias_and_scope_conflicts(self):
        reply = handle_tool(
            "continuity_todo_compile",
            {
                "project_id": "sample",
                "data_root": str(self.data),
                "prompt": "- first\n- second",
                "session_id": "session",
            },
        )
        self.assertEqual(len(reply["structuredContent"]["items"]), 2)
        output = StringIO()
        with redirect_stdout(output):
            main(
                [
                    "todo",
                    "list",
                    "--project-id",
                    "sample",
                    "--data-root",
                    str(self.data),
                    "--status",
                    "active",
                    "--format",
                    "brief",
                ]
            )
        self.assertIn("first", output.getvalue())
        with self.assertRaisesRegex(ValueError, "conflicting root"):
            self.tool(
                "continuity_plan",
                action="task.add",
                parameters={"root": str(self.root / "other")},
            )
        with self.assertRaisesRegex(ValueError, "different projects"):
            execute(
                "todo.list",
                root=str(self.root),
                project_id="other",
                data_root=str(self.data),
            )

    def test_card_projection_budget_and_migration_history(self):
        from continuity_plane.intent_todo import queue_path

        self.card()
        path = queue_path(self.root, self.data)
        with self.assertRaisesRegex(ValueError, "max_output"):
            self.execute("todo.from-cards", max_output_bytes=64)
        self.assertFalse(path.exists())
        view = self.execute("todo.from-cards", max_output_bytes=2048)
        self.assertEqual(view["source"], "task-cards")
        self.assertNotIn("legacy_items", view)
        self.assertIn("legacy_items", json.loads(path.read_text()))
        self.assertLessEqual(
            len(render(view, max_output_bytes=2048).encode()) + 1, 2048
        )

    def test_multiline_task_body_survives_plan_and_prompt_roundtrip(self):
        self.card()
        body = "Implement the parser.\n\nConstraints:\n\tPreserve existing behavior.\n"
        self.execute(
            "task.update", task_id="task", status="active", assignee="worker", body=body
        )
        value = self.execute("prompt", task_id="task", assignee="worker")
        self.assertIn("Implement the parser.", value["prompt"])
        self.assertIn("Preserve existing behavior.", value["prompt"])
        with self.assertRaisesRegex(ValueError, "task body"):
            self.execute(
                "task.update", task_id="task", status="active", body="bad\x00body"
            )

    def test_invalid_output_or_parameters_do_not_mutate(self):
        parameters = dict(
            task_id="wrong",
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="x",
            objective="x",
            next_action="x",
            exit_criteria=["pass"],
        )
        with self.assertRaisesRegex(ValueError, "max_output"):
            self.tool(
                "continuity_plan",
                action="task.add",
                parameters=parameters,
                max_output_bytes=64,
            )
        with self.assertRaises(ValueError):
            self.execute("task.add", **parameters, unexpected="value")
        self.assertEqual(list_tasks(self.data, project_id="sample"), [])
        with self.assertRaises(ValueError):
            self.execute("run", commandline=["sh"])

    def test_nested_scope_cannot_escape_root_and_registration_cannot_move(self):
        with self.assertRaisesRegex(ValueError, "conflicting data_root"):
            self.tool(
                "continuity_plan",
                action="todo.compile",
                parameters={
                    "prompt": "- a\n- b",
                    "data-root": str(self.data / "other"),
                },
            )
        with self.assertRaisesRegex(ValueError, "different projects"):
            self.execute("project.register", control_root=str(self.root / "other"))
        self.assertIsNone(load_queue(self.root, self.data))

    def test_brief_defaults_and_json_expansion_are_explicit(self):
        self.card()
        reply = self.tool("continuity_plan", action="task.list")
        self.assertNotIn("structuredContent", reply)
        self.assertIn("task", reply["content"][0]["text"])
        expanded = self.tool(
            "continuity_plan",
            action="task.list",
            parameters={"full": True, "format": "json"},
        )
        self.assertEqual(
            expanded["structuredContent"]["items"][0]["objective"],
            "Check parity",
        )
        for format in ("brief", "json"):
            text = render(
                [{"task_id": str(i), "title": "中" * 300} for i in range(20)],
                format=format,
                max_output_bytes=1024,
            )
            self.assertLessEqual(len(text.encode()) + 1, 1024)
            if format == "json":
                self.assertTrue(json.loads(text)["truncated"])

    def test_nonproject_scenarios_accept_optional_project_context(self):
        self.assertIn("scenarios", execute("scenarios"))
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(["collaboration", "scenarios", "--project-id", "not-registered"]),
                0,
            )
        self.assertIn("scenarios", json.loads(output.getvalue()))

    def test_metadata_mcp_stdio_needs_no_state_resume(self):
        scope = {
            "root": str(self.root),
            "project_id": "sample",
            "data_root": str(self.data),
        }
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "continuity_help",
                    "arguments": {"topic": "task.add"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "continuity_plan",
                    "arguments": {
                        **scope,
                        "action": "task.add",
                        "parameters": {
                            "task_id": "wire",
                            "lane_id": "scope",
                            "mode": "patch",
                            "priority": "p1",
                            "title": "wire task",
                            "objective": "Check metadata",
                            "next_action": "Run",
                            "exit_criteria": ["pass"],
                            "allowed_files": ["src/"],
                            "body": "Check wire metadata",
                        },
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "continuity_todo_compile",
                    "arguments": {
                        "project_id": "sample",
                        "data_root": str(self.data),
                        "prompt": "\n".join(f"{i}. item {i}" for i in range(1, 15)),
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "continuity_todo_list",
                    "arguments": {
                        "project_id": "sample",
                        "data_root": str(self.data),
                        "status": "active",
                        "format": "json",
                    },
                },
            },
        ]
        process = subprocess.run(
            [sys.executable, "-m", "continuity_plane.codex_mcp_server"],
            input="".join(json.dumps(r) + "\n" for r in requests),
            text=True,
            capture_output=True,
            timeout=20,
            env={
                **os.environ,
                "CONTINUITY_METER": "0",
                "CONTINUITY_MCP_TOOLSET": "full",
            },
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = {r["id"]: r for r in map(json.loads, process.stdout.splitlines())}
        self.assertTrue(all("error" not in r for r in replies.values()), replies)
        tools = {t["name"]: t for t in replies[2]["result"]["tools"]}
        self.assertTrue(
            {
                "continuity_plan",
                "continuity_help",
                "continuity_todo_link",
                "continuity_todo_activate",
            }
            <= tools.keys()
        )
        self.assertIn(
            "session_id", tools["continuity_todo_compile"]["inputSchema"]["properties"]
        )
        from jsonschema import Draft202012Validator

        for tool in tools.values():
            Draft202012Validator.check_schema(tool["inputSchema"])
        self.assertEqual(len(replies[5]["result"]["structuredContent"]["items"]), 14)
        self.assertEqual(len(replies[6]["result"]["structuredContent"]["items"]), 1)
        self.assertEqual(
            list_tasks(self.data, project_id="sample")[0]["task_id"], "wire"
        )


if __name__ == "__main__":
    unittest.main()
