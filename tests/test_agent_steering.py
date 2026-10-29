"""Alpha36 steering: hooks and prompts point agents at MCP tools."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from continuity_plane import adoption
from continuity_plane.collaboration_registry import (
    _record_change,
    add_task,
    register_project,
)


class NudgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)

    def post(self, command, session="s"):
        return adoption.cli_nudge(
            {
                "hook_event_name": "PostToolUse",
                "session_id": session,
                "tool_name": "Bash",
                "tool_input": {"command": command},
            },
            self.data,
        )

    def test_names_the_mcp_tool_once_per_operation(self):
        text = self.post("continuity collaboration task update --task-id x --status active")
        self.assertIn("continuity_plan(action=task.update)", text)
        self.assertIsNone(self.post("continuity task update --task-id y"))
        self.assertIsNotNone(self.post("continuity handoff show"))

    def test_caps_reminders_per_session_and_skips_cli_only_operations(self):
        self.assertIsNone(self.post("continuity worker queue run --watch"))
        for op in ("task add", "task list", "handoff write", "inbox list"):
            self.post(f"continuity collaboration {op}" if op != "handoff write" else f"continuity {op}")
        self.assertIsNone(self.post("continuity todo list"))

    def test_can_be_turned_off(self):
        with patch.dict(os.environ, {"CONTINUITY_MCP_NUDGE": "0"}):
            self.assertIsNone(self.post("continuity task update"))

    def test_merge_context_respects_budget(self):
        output = {"hookSpecificOutput": {"additionalContext": "a" * 4090}}
        adoption.merge_context(output, "PostToolUse", "too long", 4096)
        self.assertEqual(len(output["hookSpecificOutput"]["additionalContext"]), 4090)


class NoticeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.data = base / "data"
        self.main_root = base / "main"
        self.other_root = base / "other"
        self.main_root.mkdir()
        self.other_root.mkdir()
        register_project(self.data, project_id="main", control_root=self.main_root)
        register_project(self.data, project_id="other", control_root=self.other_root)

    def notice(self, event="PostToolUse"):
        from continuity_plane.controller_awareness import notice

        return notice(
            {"cwd": str(self.main_root), "session_id": "ctl", "hook_event_name": event},
            data_root=self.data,
            max_bytes=1024,
        )

    def test_watched_project_failures_reach_the_controller(self):
        with patch.dict(os.environ, {"CONTINUITY_WATCH_PROJECTS": "other"}):
            self.assertIsNone(self.notice("SessionStart"))  # first call sets positions
            self.assertIsNone(self.notice())  # other project position recorded
            _record_change(
                self.data,
                "other",
                "attempt.finished",
                "remote-task",
                attempt_id="a1",
                status="failed",
                failure_class="authentication",
            )
            text = self.notice()
        self.assertIn("[other] worker remote-task failed (authentication)", text)
        self.assertIn(
            "continuity_worker_report(project_id='other', task_id='remote-task')", text
        )

    def test_unwatched_projects_stay_quiet(self):
        self.assertIsNone(self.notice("SessionStart"))
        _record_change(
            self.data, "other", "attempt.finished", "t", attempt_id="a", status="failed"
        )
        self.assertIsNone(self.notice())


class WorkerPromptTests(unittest.TestCase):
    def test_prompt_names_mcp_tools_and_explicit_ids(self):
        from continuity_plane.collaboration_workflow import prompt

        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            root = Path(temp) / "repo"
            root.mkdir()
            register_project(data, project_id="demo", control_root=root)
            add_task(
                data,
                project_id="demo",
                task_id="t1",
                lane_id="main",
                mode="patch",
                priority="p1",
                title="Fix",
                objective="Fix",
                next_action="Start",
                exit_criteria=["Done"],
                assignee="worker-a",
                body="Fix the bug",
            )
            text = prompt(data, project_id="demo", task_id="t1", assignee="worker-a")[
                "prompt"
            ]
        for tool in (
            "continuity_start",
            "continuity_progress",
            "continuity_report_final",
            "continuity_context_lookup",
        ):
            self.assertIn(tool, text)
        self.assertIn("project_id='demo', task_id='t1', assignee='worker-a'", text)
        self.assertIn("If the MCP tools are not available", text)


if __name__ == "__main__":
    unittest.main()
