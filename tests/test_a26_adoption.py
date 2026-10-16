"""Synthetic adoption regressions; no provider requests or native profile access."""

import contextlib
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from continuity_plane.collaboration_registry import register_project, add_task, update_task, _load_ledger
from continuity_plane.execution_lifecycle import bind_execution, finish_execution
from continuity_plane.planning_api import handle_tool, execute, help_for
from continuity_plane.reference_tsv import import_tsv, export_tsv
from continuity_plane.reference_library import find, location


class Alpha26AdoptionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "repo with spaces"
        self.root.mkdir()
        self.data = Path(directory.name) / "external"
        register_project(self.data, project_id="sample", control_root=self.root)
        add_task(self.data, project_id="sample", task_id="task", lane_id="lane", mode="patch", priority="p1", title="Fixture", objective="Verify adoption", next_action="Run tests", exit_criteria=["PASS"], assignee="stable-worker")

    def details(self):
        return _load_ledger(self.data, "sample").get("task_details", {}).get("task", {})

    def bind(self, attempt):
        bind_execution(self.data, project_id="sample", task_id="task", assignee="stable-worker", attempt_id=attempt)

    def finish(self, attempt, **extra):
        return finish_execution(self.data, project_id="sample", task_id="task", assignee="stable-worker", attempt_id=attempt, status="completed", return_code=0, report={"text": "Final fixture report " + attempt}, **extra)

    def test_report_attaches_after_early_status_changes_without_regressing_status(self):
        for status in ("ready-for-review", "completed", "merged", "blocked"):
            with self.subTest(status=status):
                update_task(self.data, project_id="sample", task_id="task", status="queued")
                self.bind(status)
                update_task(self.data, project_id="sample", task_id="task", status=status, branch="fixture", commit="01234567", verification="focused PASS")
                receipt = self.finish(status)
                self.assertEqual(receipt["card_status"], status)
                self.assertEqual(self.details()["report"]["attempt_id"], status)
                report = self.details()["report"]
                self.finish(status)
                self.assertEqual(report, self.details()["report"])

    def test_stale_or_reassigned_attempt_cannot_overwrite_report(self):
        self.bind("old")
        self.bind("new")
        self.finish("old")
        self.assertNotIn("report", self.details())
        self.finish("new")
        self.finish("old")
        self.assertEqual(self.details()["report"]["attempt_id"], "new")
        self.bind("reassigned")
        update_task(self.data, project_id="sample", task_id="task", assignee="other", force=True, force_reason="fixture reassignment")
        self.finish("reassigned")
        self.assertEqual(self.details()["report"]["attempt_id"], "new")

    def test_report_replay_after_early_status_and_write_failure(self):
        from continuity_plane.supervisor import daemon_once

        self.bind("recover")
        update_task(self.data, project_id="sample", task_id="task", status="ready-for-review")
        with patch("continuity_plane.execution_lifecycle._atomic_write", side_effect=OSError("fixture crash")):
            with self.assertRaises(OSError):
                self.finish("recover")
        daemon_once(self.data, project_id="sample")
        self.assertEqual(self.details()["report"]["attempt_id"], "recover")

    def test_stable_identity_survives_connection_ids_and_rejects_other_executor(self):
        execute("todo.from-cards", root=str(self.root), data_root=str(self.data))
        for session in ("connection-one", "connection-two"):
            handle_tool("continuity_plan", {"action": "todo.activate", "root": str(self.root), "data_root": str(self.data), "executor_id": "stable-worker", "parameters": {"item_id": "card:task", "session_id": session, "reason": "continue fixture"}})
            with patch.dict(os.environ, {"CONTINUITY_ASSIGNEE": "stable-worker"}):
                handle_tool("continuity_plan", {"action": "task.update", "root": str(self.root), "data_root": str(self.data), "parameters": {"task_id": "task", "body": session}})
        with self.assertRaisesRegex(ValueError, "another session"):
            handle_tool("continuity_plan", {"action": "task.update", "root": str(self.root), "data_root": str(self.data), "executor_id": "other", "parameters": {"task_id": "task", "body": "no takeover"}})
        self.assertEqual(self.details()["body"], "connection-two")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            handle_tool("continuity_plan", {"action": "task.update", "root": str(self.root), "data_root": str(self.data), "executor_id": "other", "parameters": {"task_id": "task", "assignee": "stable-worker"}})
        with patch.dict(os.environ, {"CONTINUITY_ASSIGNEE": "controller", "CONTINUITY_ROLE": "controller"}):
            handle_tool("continuity_plan", {"action": "task.update", "root": str(self.root), "data_root": str(self.data), "parameters": {"task_id": "task", "body": "Controller annotation without reassignment"}})
        self.assertEqual(_load_ledger(self.data, "sample")["tasks"][0]["assignee"], "stable-worker")

    def test_fresh_mcp_processes_keep_configured_executor_identity(self):
        env = {**os.environ, "CONTINUITY_ASSIGNEE": "stable-worker"}
        for index in range(2):
            request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "continuity_plan", "arguments": {"action": "task.update", "root": str(self.root), "data_root": str(self.data), "parameters": {"task_id": "task", "body": f"connection-{index}"}}}}
            result = subprocess.run([sys.executable, "-m", "continuity_plane.codex_mcp_server"], input=json.dumps(request) + "\n", env=env, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("error", json.loads(result.stdout))
        self.assertEqual(self.details()["body"], "connection-1")

    def test_card_workflow_does_not_create_second_queue_and_commands_parse(self):
        from continuity_plane.planning_adoption import context
        from continuity_plane.intent_todo import load_queue
        from continuity_plane.cli import build_parser

        payload = {"cwd": str(self.root), "session_id": "connection", "hook_event_name": "UserPromptSubmit", "prompt": "请完成：\n- 修复代码\n- 补充测试"}
        with patch.dict(os.environ, {"CONTINUITY_ASSIGNEE": "stable-worker"}):
            text = context(payload, data_root=self.data)
            self.assertIsNone(context(payload, data_root=self.data))
        self.assertIsNone(load_queue(self.root, self.data))
        self.assertLessEqual(len(text.encode()), 1024)
        command = next(line.split(": ", 1)[1] for line in text.splitlines() if line.startswith("Inspect current work: "))
        build_parser().parse_args(shlex.split(command)[1:])
        with patch.dict(os.environ, {"CONTINUITY_ASSIGNEE": "controller"}):
            context({**payload, "session_id": "controller-session"}, data_root=self.data)
        self.assertIsNone(load_queue(self.root, self.data))

    def test_omitted_checklist_hint_is_actionable_once(self):
        from continuity_plane.planning_adoption import omitted_checklist_hint
        from continuity_plane.cli import build_parser

        payload = {"cwd": str(self.root), "session_id": "fixture", "prompt_omitted_sha256": "synthetic"}
        self.assertIsNone(omitted_checklist_hint(payload, data_root=self.data, max_bytes=5))
        text = omitted_checklist_hint(payload, data_root=self.data)
        self.assertIn("no separate Todo", text)
        build_parser().parse_args(shlex.split(text.split("Inspect: ")[1])[1:])
        self.assertIsNone(omitted_checklist_hint(payload, data_root=self.data))

    def tsv(self, text):
        path = self.root / "source-index.tsv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_tsv_roundtrip_preserves_keys_unicode_extra_and_review_declarations(self):
        source = self.tsv('key\ttitle\turl\tkind\tsection\textra\nfixture\t官方资料\thttps://example.invalid/spec\tstandard\t"a\tb"\tquoted\n')
        result = import_tsv(self.root, file=source, data_root=self.data)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(import_tsv(self.root, file=source, data_root=self.data)["unchanged"], 1)
        self.assertEqual(find(self.root, data_root=self.data)["items"][0]["review_status"], "candidate")
        output = self.root / "export.tsv"
        export_tsv(self.root, file=output, data_root=self.data)
        other = self.data.parent / "other-data"
        import_tsv(self.root, file=output, data_root=other)
        self.assertEqual(find(self.root, data_root=other)["items"][0]["section"], "a\tb")
        with self.assertRaises(FileExistsError):
            export_tsv(self.root, file=output, data_root=self.data)
        reply = handle_tool("continuity_plan", {"action": "reference.import-tsv", "root": str(self.root), "data_root": str(self.data), "parameters": {"file": str(source)}})
        self.assertIn("unchanged", reply["content"][0]["text"])
        self.assertTrue(help_for("reference.export-tsv")["parameters"])

    def test_tsv_conflict_and_invalid_late_row_do_not_partially_write(self):
        source = self.tsv("key\ttitle\turl\na\tFirst\thttps://example.invalid/a\n")
        import_tsv(self.root, file=source, data_root=self.data)
        previous = location(self.root, self.data).read_bytes()
        for rows in (
            "b\tNew\thttps://example.invalid/b\na\tChanged\thttps://example.invalid/a\n",
            "b\tNew\thttps://example.invalid/b\nc\tSecret\thttps://user:secret@example.invalid/c\n",
            "b\tNew\thttps://example.invalid/b\nb\tDuplicate\thttps://example.invalid/b\n",
        ):
            source = self.tsv("key\ttitle\turl\n" + rows)
            with self.assertRaises(ValueError):
                import_tsv(self.root, file=source, data_root=self.data)
            self.assertEqual(location(self.root, self.data).read_bytes(), previous)

    def route(self):
        path = self.root / "bridge.json"
        path.write_text(json.dumps({"host": "fixture-ssh", "mappings": [{"local_root": str(self.root), "remote_root": "/srv/fixture"}]}))
        return path

    def test_remote_lookup_forwards_only_code_query_and_budget(self):
        from continuity_plane.remote_lookup import lookup

        with patch("continuity_plane.remote_lookup.forward", return_value={"return_code": 0, "stdout": "src/fixture.c:9 function example"}) as forward:
            result = lookup({"root": str(self.root), "query": "example", "path_prefix": "src", "extensions": [".c"], "max_output_bytes": 1024}, self.route())
        argv = forward.call_args.args[0]
        self.assertEqual(argv[:4], ["context", "lookup", "--query", "example"])
        self.assertNotIn(str(self.root), argv)
        self.assertNotIn("--cache-path", argv)
        self.assertEqual(forward.call_args.kwargs["root"], "/srv/fixture")
        self.assertFalse(result["_meta"]["continuity"]["local_fallback"])

    def test_remote_mapping_failures_never_trigger_local_lookup(self):
        from continuity_plane.context_mcp_server import _call_lookup

        config = self.route()
        for extra in ({"root": str(self.root.parent)}, {"cache_path": "/private/local-cache"}, {"data_root": "/private/native-data"}):
            output = io.StringIO()
            with patch("continuity_plane.context_mcp_server.lookup_code_index") as local, contextlib.redirect_stdout(output):
                _call_lookup(1, {"root": str(self.root), "query": "example", "bridge_config": str(config), **extra})
            self.assertIn("error", json.loads(output.getvalue()))
            local.assert_not_called()
        output = io.StringIO()
        with patch("continuity_plane.context_mcp_server.lookup_code_index") as local, contextlib.redirect_stdout(output):
            _call_lookup(2, {"root": str(self.root), "query": "example", "bridge_config": ""})
        self.assertIn("error", json.loads(output.getvalue()))
        local.assert_not_called()

    def test_remote_over_budget_response_rejected(self):
        from continuity_plane.remote_lookup import lookup

        with patch("continuity_plane.remote_lookup.forward", return_value={"return_code": 0, "stdout": "x" * 1025}):
            with self.assertRaisesRegex(ValueError, "over-budget"):
                lookup({"root": str(self.root), "query": "example", "max_output_bytes": 1024}, self.route())

    def test_remote_cli_protocol_indexes_only_mapped_synthetic_repository(self):
        from continuity_plane.remote_lookup import lookup

        remote = self.root.parent / "remote-fixture"
        remote.mkdir()
        subprocess.run(["git", "init", "-q", str(remote)], check=True)
        (remote / "example.py").write_text("def remote_fixture_symbol():\n    return 1\n")
        subprocess.run(["git", "-C", str(remote), "add", "example.py"], check=True)
        subprocess.run(["git", "-C", str(remote), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "commit", "-qm", "test: synthetic lookup"], check=True)
        data = self.root.parent / "remote-registry"
        register_project(data, project_id="remote-fixture", control_root=remote)
        config = self.route()
        config.write_text(json.dumps({"host": "fixture-ssh", "local_root": str(self.root), "remote_root": str(remote), "remote_data_root": str(data)}))

        def transport(argv, **route):
            result = subprocess.run([sys.executable, "-c", "from continuity_plane.remote_cli import receive_main; receive_main()"], input=json.dumps({"root": route["root"], "argv": argv}), env={**os.environ, "CONTINUITY_DATA_ROOT": str(data)}, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        with patch("continuity_plane.remote_lookup.forward", side_effect=transport):
            response = lookup({"root": str(self.root), "query": "remote_fixture_symbol", "max_output_bytes": 1024}, config)
        self.assertIn("example.py:1", response["content"][0]["text"])
        self.assertFalse((self.root / "example.py").exists())


if __name__ == "__main__":
    unittest.main()
