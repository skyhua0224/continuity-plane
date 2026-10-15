import json
import importlib.util
from contextlib import redirect_stdout
from io import StringIO
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from continuity_plane.collaboration_registry import register_project
from continuity_plane.intent_todo import compile_queue, load_queue
from continuity_plane.planning_adoption import context
from continuity_plane.reference_library import preference


class PlanningAdoptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "project"
        self.root.mkdir()
        self.data = Path(temp.name) / "external"
        register_project(self.data, project_id="fixture", control_root=self.root)

    def payload(self, **extra):
        return {
            "cwd": str(self.root),
            "session_id": "current",
            "hook_event_name": "UserPromptSubmit",
            **extra,
        }

    def codex_hook(self):
        base = Path(__file__).resolve().parents[1]
        script = base / "integrations/codex/continuity-plane/scripts/continuity-hook.py"
        if not script.exists():
            script = base / "plugins/continuity-plane/scripts/continuity-hook.py"
        spec = importlib.util.spec_from_file_location("planning_codex_hook", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_codex_stop_only_continues_owned_explicit_execution_once(self):
        hook = self.codex_hook()
        compile_queue(
            self.root, "- first\n- second", session_id="current", data_root=self.data
        )
        with (
            patch.dict(os.environ, {"CONTINUITY_DATA_ROOT": str(self.data)}),
            patch.object(hook, "_effect_policy", return_value="auto"),
            patch.object(
                hook, "_read_cursor", return_value={"response_mode": "answer"}
            ),
        ):
            output = StringIO()
            with redirect_stdout(output):
                hook._stop_continuation(self.payload(), self.root)
            self.assertEqual(output.getvalue(), "")
            with patch.object(
                hook,
                "_read_cursor",
                return_value={"response_mode": "continue-silently"},
            ):
                with redirect_stdout(output):
                    hook._stop_continuation(self.payload(), self.root)
                self.assertEqual(json.loads(output.getvalue())["decision"], "block")
                output = StringIO()
                with redirect_stdout(output):
                    hook._stop_continuation(
                        self.payload(stop_hook_active=True), self.root
                    )
                self.assertEqual(output.getvalue(), "")

    def test_codex_stop_stale_state_and_foreign_queue_do_not_force_continuation(self):
        hook = self.codex_hook()
        compile_queue(
            self.root,
            "- foreign\n- second",
            session_id="someone-else",
            data_root=self.data,
        )
        output = StringIO()
        with (
            patch.dict(os.environ, {"CONTINUITY_DATA_ROOT": str(self.data)}),
            patch.object(hook, "_effect_policy", return_value="auto"),
            patch.object(
                hook,
                "_read_cursor",
                return_value={"response_mode": "continue-silently"},
            ),
            patch.object(
                hook,
                "_inspect_packet",
                return_value={
                    "active_work": {"title": "obsolete"},
                    "next_action": "remain-read-only",
                    "source_fresh": False,
                    "read_only": True,
                },
            ),
            redirect_stdout(output),
        ):
            hook._stop_continuation(self.payload(), self.root)
        self.assertEqual(output.getvalue(), "")

    def test_question_and_unrelated_tool_do_not_create_work(self):
        self.assertIsNone(
            context(
                self.payload(prompt="请比较两个实现，有什么不同？"), data_root=self.data
            )
        )
        self.assertIsNone(
            context(
                self.payload(prompt="fix this", hook_event_name="PostToolUse"),
                data_root=self.data,
            )
        )
        self.assertIsNone(load_queue(self.root, self.data))

    def test_checklist_capture_deduplicates_and_maintains_stage_reminder(self):
        payload = self.payload(prompt="请完成：\n- 修复代码\n- 补充测试")
        result = context(payload, data_root=self.data)
        queue = load_queue(self.root, self.data)
        self.assertEqual(len(queue["items"]), 2)
        self.assertEqual(queue["owner_session_id"], "current")
        self.assertIn("nonterminal", result)
        self.assertLessEqual(len(result.encode()), 1024)
        self.assertIsNone(context(payload, data_root=self.data))
        self.assertEqual(load_queue(self.root, self.data), queue)

    def test_foreign_queue_is_not_injected_as_current_work(self):
        compile_queue(
            self.root,
            "- secret other task\n- second",
            session_id="other",
            data_root=self.data,
        )
        result = context(
            self.payload(hook_event_name="SessionStart"), data_root=self.data
        )
        self.assertNotIn("secret other task", result)
        context(
            self.payload(prompt="请完成：\n- 修改我的代码\n- 添加我的测试"),
            data_root=self.data,
        )
        self.assertEqual(len(load_queue(self.root, self.data)["items"]), 2)

    def test_opt_in_reference_hint_dedup_and_corruption_does_not_gate_work(self):
        preference(self.root, mode="primary-first", data_root=self.data)
        payload = self.payload(prompt="实现协议解析")
        result = context(payload, data_root=self.data)
        self.assertIn("Reference preference", result)
        self.assertIn("nonterminal", result)
        self.assertLessEqual(len(result.encode()), 1024)
        self.assertIsNone(context(payload, data_root=self.data))
        with patch(
            "continuity_plane.reference_library.hint",
            side_effect=ValueError("bad data"),
        ):
            result = context(
                self.payload(session_id="new", prompt="继续实现"), data_root=self.data
            )
        self.assertIn("ordinary work remains allowed", result)

    def test_actual_claude_script_captures_once_without_stopping(self):
        script = (
            Path(__file__).resolve().parents[1]
            / "integrations/claude/continuity-plane/scripts/continuity-claude-hook.py"
        )
        env = {
            **os.environ,
            "CONTINUITY_DATA_ROOT": str(self.data),
            "PYTHONPATH": str(script.parents[4]),
        }
        payload = self.payload(prompt="请完成：\n- 修复第一个问题\n- 补充第二个测试")
        for index in range(2):
            result = subprocess.run(
                [sys.executable, str(script)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=env,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            if index == 0:
                value = json.loads(result.stdout)
                self.assertNotIn("decision", value)
                self.assertLessEqual(
                    len(value["hookSpecificOutput"]["additionalContext"].encode()), 1024
                )
            else:
                self.assertEqual(result.stdout, "")
        self.assertEqual(len(load_queue(self.root, self.data)["items"]), 2)


if __name__ == "__main__":
    unittest.main()
