"""Alpha36 first-class MCP tools: discoverable, typed and usable end to end."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class McpSession:
    """One stdio MCP server process driven request by request."""

    def __init__(self, env: dict):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "continuity_plane.codex_mcp_server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def call(self, name: str, **arguments) -> dict:
        reply = self.request(
            "tools/call",
            {"name": name, "arguments": {"format": "json", **arguments}},
        )
        result = reply["result"]
        text = result["content"][0]["text"]
        value = json.loads(text)
        return {"error": bool(result.get("isError")), "value": value}

    def close(self):
        self.process.stdin.close()
        self.process.wait(timeout=30)
        self.process.stdout.close()
        self.process.stderr.close()


class AgentToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data = base / "data"
        self.repo = base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CONTINUITY_")
        }
        self.env["CONTINUITY_DATA_ROOT"] = str(self.data)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "continuity_plane.cli",
                "collaboration",
                "register",
                "--project-id",
                "demo",
                "--control-root",
                str(self.repo),
                "--repository-root",
                str(self.repo),
            ],
            env=self.env,
            check=True,
            capture_output=True,
        )
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        self.tmp.cleanup()

    def session(self, **env) -> McpSession:
        session = McpSession({**self.env, **env})
        self.sessions.append(session)
        session.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test"}},
        )
        return session

    def test_initialize_and_tool_list_tell_agents_when_to_use_continuity(self):
        session = McpSession(self.env)
        self.sessions.append(session)
        init = session.request("initialize", {"protocolVersion": "2025-06-18"})
        instructions = init["result"]["instructions"]
        for tool in (
            "continuity_start",
            "continuity_task_add",
            "continuity_worker_dispatch",
            "continuity_events_wait",
            "continuity_report_final",
            "continuity_handoff_write",
            "continuity_context_lookup",
        ):
            self.assertIn(tool, instructions)
        self.assertLess(len(instructions.encode()), 2048)
        tools = session.request("tools/list")["result"]["tools"]
        self.assertEqual(tools[0]["name"], "continuity_start")
        names = [t["name"] for t in tools]
        self.assertIn("continuity_plan", names)  # long tail stays available
        for tool in tools[:14]:
            self.assertTrue(tool["description"].startswith("Use "), tool["name"])
            self.assertNotIn("parameters", tool["inputSchema"]["properties"])
        add = next(t for t in tools if t["name"] == "continuity_task_add")
        self.assertEqual(
            add["inputSchema"]["required"], ["task_id", "title"]
        )
        # A single criterion may also be sent as a plain string (alpha37).
        self.assertIn(
            "array", add["inputSchema"]["properties"]["exit_criterion"]["type"]
        )

    def test_default_tool_list_costs_no_more_than_alpha35(self):
        # Many hosts send every listed schema and the server instructions with
        # each model request. Alpha35 listed 19 tools in 18,419 bytes.
        session = McpSession(self.env)
        self.sessions.append(session)
        init = session.request("initialize", {"protocolVersion": "2025-06-18"})
        tools = session.request("tools/list")["result"]["tools"]
        size = len(json.dumps(tools, ensure_ascii=False).encode()) + len(
            init["result"]["instructions"].encode()
        )
        self.assertLessEqual(size, 18419)
        names = {t["name"] for t in tools}
        self.assertNotIn("continuity_work_transition", names)
        self.assertNotIn("continuity_todo_list", names)
        # Unlisted tools remain callable for existing prompts.
        listed = session.call("continuity_todo_list", project_id="demo")
        self.assertFalse(listed["error"], listed)

    def test_controller_and_worker_loop_without_cli(self):
        controller = self.session()
        empty = controller.call(
            "continuity_task_add", project_id="demo", task_id="t1", title="Fix", body=" "
        )
        self.assertTrue(empty["error"])
        self.assertIn("body is required", empty["value"]["message"])

        added = controller.call(
            "continuity_task_add",
            project_id="demo",
            task_id="t1",
            title="Fix parser edge case",
            body="Fix the off-by-one in parse(); add a regression test.",
            assignee="worker-a",
        )
        self.assertFalse(added["error"], added)
        self.assertIn("continuity_worker_dispatch", added["value"]["next"][0])
        listed = controller.call("continuity_task_list", project_id="demo")
        self.assertIn("t1", json.dumps(listed["value"]))

        worker = self.session(
            CONTINUITY_PROJECT_ID="demo",
            CONTINUITY_ASSIGNEE="worker-a",
            CONTINUITY_TASK_ID="t1",
            CONTINUITY_CONTROLLER="controller",
            CONTINUITY_ROLE="worker",
        )
        start = worker.call("continuity_start")
        self.assertFalse(start["error"], start)
        self.assertEqual(start["value"]["role"], "worker")
        self.assertIn("Fix parser edge case", json.dumps(start["value"]["task"]))
        progress = worker.call("continuity_progress", note="Reproduced; writing test")
        self.assertFalse(progress["error"], progress)

        final = worker.call(
            "continuity_report_final",
            summary="Fixed the off-by-one.\nDetails follow.",
            checks=[
                {
                    "name": "unit",
                    "command": "python -m unittest",
                    "status": "pass",
                    "result": "12 passed",
                }
            ],
            unverified=["Windows paths"],
        )
        self.assertFalse(final["error"], final)
        report = Path(final["value"]["report_file"])
        self.assertTrue(report.is_file())
        self.assertIn("Windows paths", report.read_text())
        shown = controller.call(
            "continuity_task_show", project_id="demo", task_id="t1"
        )
        self.assertIn("ready-for-review", json.dumps(shown["value"]))
        inbox = controller.call(
            "continuity_inbox",
            project_id="demo",
            root=str(self.repo),
            action="list",
            recipient="controller",
        )
        self.assertIn("t1 → ready-for-review", json.dumps(inbox["value"], ensure_ascii=False))

        review = worker.call(
            "continuity_review_request", brief="Check the boundary test", reviewer="rev-b"
        )
        self.assertFalse(review["error"], review)
        review_id = review["value"]["review_task_id"]
        card = controller.call(
            "continuity_task_show", project_id="demo", task_id=review_id
        )
        self.assertIn("rev-b", json.dumps(card["value"]))

        handoff = controller.call(
            "continuity_handoff_write",
            project_id="demo",
            plan="Review t1, then release",
            next_action="Wait for rev-b",
        )
        self.assertFalse(handoff["error"], handoff)
        self.assertIn(
            "Review t1",
            json.dumps(controller.call("continuity_handoff_show", project_id="demo")["value"]),
        )

    def test_worker_cannot_reassign_its_card(self):
        controller = self.session()
        controller.call(
            "continuity_task_add",
            project_id="demo",
            task_id="t2",
            title="Own card",
            body="Do it.",
            assignee="worker-a",
        )
        worker = self.session(
            CONTINUITY_PROJECT_ID="demo",
            CONTINUITY_ASSIGNEE="worker-a",
            CONTINUITY_ROLE="worker",
        )
        moved = worker.call("continuity_task_update", task_id="t2", assignee="someone-else")
        self.assertTrue(moved["error"])
        self.assertIn("assignee conflicts", moved["value"]["message"])

    def test_events_wait_starts_at_the_tail_and_remembers_position(self):
        controller = self.session()
        for n in range(3):
            controller.call(
                "continuity_task_add",
                project_id="demo",
                task_id=f"old-{n}",
                title="Old",
                body="History",
            )
        first = controller.call("continuity_events_wait", timeout_seconds=0)
        self.assertFalse(first["error"], first)
        self.assertEqual(first["value"]["events"], [])
        self.assertTrue(first["value"]["timed_out"])
        cursor_file = self.data / "agent-cursors" / "controller.json"
        self.assertGreater(json.loads(cursor_file.read_text())["demo"], 0)

    def test_events_wait_watches_every_project_without_project_id(self):
        other = self.repo.parent / "other"
        other.mkdir()
        subprocess.run(
            [sys.executable, "-m", "continuity_plane.cli", "collaboration",
             "register", "--project-id", "other", "--control-root", str(other),
             "--repository-root", str(other)],
            env=self.env, check=True, capture_output=True,
        )
        session = self.session()
        waited = session.call("continuity_events_wait", timeout_seconds=0)
        self.assertFalse(waited["error"], waited)
        self.assertEqual(sorted(waited["value"]["watching"]), ["demo", "other"])
        narrowed = session.call(
            "continuity_events_wait", project_id="other", timeout_seconds=0
        )
        self.assertEqual(narrowed["value"]["watching"], ["other"])
        bad = session.call("continuity_events_wait", project_ids=["nope"], timeout_seconds=0)
        self.assertTrue(bad["error"])

    def test_dispatch_previews_by_default_and_explains_failures(self):
        controller = self.session()
        controller.call(
            "continuity_task_add",
            project_id="demo",
            task_id="t3",
            title="Dispatch me",
            body="Do it.",
            assignee="worker-a",
        )
        preview = controller.call(
            "continuity_worker_dispatch", project_id="demo", task_id="t3", model="none"
        )
        # No adapter is configured in this sandbox: a clear, actionable error,
        # never a crash and never a started worker.
        self.assertTrue(preview["error"])
        self.assertIn("worker configure", preview["value"]["message"])
        adapters = self.data / "executor-adapters.json"
        adapters.write_text(json.dumps({"sol": {}, "grok": {}}))
        missing = controller.call(
            "continuity_worker_dispatch", project_id="demo", task_id="t3"
        )
        self.assertIn("choose model: one of grok, sol", missing["value"]["message"])
        self.assertIn("recommend", missing["value"]["message"])
        adapters.unlink()
        self.assertFalse(list((self.data / "projects" / "demo").glob("worker-jobs/*")))

    def test_project_is_required_only_when_ambiguous(self):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "continuity_plane.cli",
                "collaboration",
                "register",
                "--project-id",
                "other",
                "--control-root",
                str(self.repo.parent),
                "--repository-root",
                str(self.repo.parent),
            ],
            env=self.env,
            check=True,
            capture_output=True,
        )
        session = self.session()
        listed = session.call("continuity_task_list")
        self.assertTrue(listed["error"])
        self.assertIn("pass project_id", listed["value"]["message"])
        self.assertIn("demo", listed["value"]["message"])


if __name__ == "__main__":
    unittest.main()
