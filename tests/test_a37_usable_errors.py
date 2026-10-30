"""Alpha37 usable errors: a rejected command says what to do (card a37-usable-errors)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from continuity_plane import dispatch_queue
from continuity_plane.cli import entrypoint, main
from continuity_plane.collaboration_registry import (
    CollaborationRegistryError,
    add_task,
    list_tasks,
    register_project,
    update_task,
)


class A37Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.data = self.tmp / "data"
        patcher = mock.patch.dict(
            os.environ,
            {"CONTINUITY_DATA_ROOT": str(self.data), "TMPDIR": str(self.tmp)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in (
            "CONTINUITY_TASK_ID",
            "CONTINUITY_ROLE",
            "CONTINUITY_PROJECT_ID",
            "CONTINUITY_ASSIGNEE",
        ):
            os.environ.pop(key, None)
        register_project(self.data, project_id="sample", control_root=self.root)
        self.add("task")

    def add(self, task_id, **overrides):
        values = dict(
            project_id="sample",
            task_id=task_id,
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="Implement parser",
            objective="Focused fix",
            next_action="original next action",
            exit_criteria=["PASS"],
            assignee="worker",
            worktree=self.root,
            body="Implement the bounded parser",
        )
        values.update(overrides)
        return add_task(self.data, **values)

    def card(self, task_id="task"):
        return next(
            t for t in list_tasks(self.data, project_id="sample") if t["task_id"] == task_id
        )

    def entry(self, *argv):
        """Run the real CLI entrypoint; returns (code, stdout, stderr)."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = entrypoint(list(argv))
        return code, out.getvalue().strip(), err.getvalue().strip()

    def update_argv(self, *extra, task_id="task"):
        return (
            "collaboration", "task", "update",
            "--data-root", str(self.data), "--project-id", "sample",
            "--task-id", task_id, *extra,
        )


class LimitMessageTests(A37Base):
    def test_next_action_over_limit_names_field_size_and_limit(self):
        with self.assertRaises(CollaborationRegistryError) as caught:
            update_task(
                self.data, project_id="sample", task_id="task",
                next_action="x" * 5000,
            )
        message = str(caught.exception)
        self.assertIn("next_action", message)
        self.assertIn("5000", message)
        self.assertIn("4096", message)
        self.assertEqual(self.card()["next_action"], "original next action")

    def test_handoff_sized_next_action_is_accepted(self):
        update_task(
            self.data, project_id="sample", task_id="task", next_action="y" * 1510
        )
        self.assertEqual(len(self.card()["next_action"]), 1510)
        update_task(
            self.data, project_id="sample", task_id="task", next_action="z" * 4096
        )
        self.assertEqual(len(self.card()["next_action"]), 4096)

    def test_size_counts_utf8_bytes(self):
        with self.assertRaises(CollaborationRegistryError) as caught:
            self.add("wide", title="é" * 300)  # 600 bytes against a 512 limit
        message = str(caught.exception)
        self.assertIn("title", message)
        self.assertIn("600", message)
        self.assertIn("512", message)

    def test_empty_and_control_characters_are_told_apart(self):
        with self.assertRaises(CollaborationRegistryError) as empty:
            update_task(self.data, project_id="sample", task_id="task", next_action="  ")
        self.assertIn("next_action", str(empty.exception))
        self.assertIn("empty", str(empty.exception))
        with self.assertRaises(CollaborationRegistryError) as control:
            update_task(
                self.data, project_id="sample", task_id="task", next_action="a\x00b"
            )
        self.assertIn("next_action", str(control.exception))
        self.assertIn("control character", str(control.exception))

    def test_cli_reports_the_limit_as_json(self):
        code, out, err = self.entry(*self.update_argv("--next-action", "q" * 6000))
        self.assertEqual(code, 2)
        message = json.loads(err)["message"]
        self.assertIn("next_action", message)
        self.assertIn("6000", message)
        self.assertIn("4096", message)


class TaskUpdateGuidanceTests(A37Base):
    def test_assignee_without_actor_names_the_missing_argument(self):
        code, out, err = self.entry(*self.update_argv("--assignee", "other"))
        self.assertEqual(code, 2)
        error = json.loads(err)
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["missing_argument"], "--actor")
        self.assertIn("--actor worker", error["message"])
        self.assertEqual(self.card()["assignee"], "worker")

    def test_dry_run_shows_result_without_writing(self):
        code, out, err = self.entry(
            *self.update_argv("--next-action", "brand new step", "--dry-run")
        )
        self.assertEqual(code, 0, err)
        shown = json.loads(out)
        self.assertTrue(shown["dry_run"])
        self.assertEqual(shown["next_action"], "brand new step")
        self.assertEqual(self.card()["next_action"], "original next action")

    def test_dry_run_still_validates(self):
        code, out, err = self.entry(
            *self.update_argv("--next-action", "n" * 5000, "--dry-run")
        )
        self.assertEqual(code, 2)
        self.assertIn("4096", json.loads(err)["message"])
        self.assertEqual(self.card()["next_action"], "original next action")

    def test_dry_run_without_actor_reports_the_same_missing_argument(self):
        code, out, err = self.entry(
            *self.update_argv("--assignee", "other", "--dry-run")
        )
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(err)["missing_argument"], "--actor")

    def test_dry_run_with_actor_allows_reassignment_preview(self):
        code, out, err = self.entry(
            *self.update_argv(
                "--assignee", "other", "--actor", "worker", "--dry-run"
            )
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["assignee"], "other")
        self.assertEqual(self.card()["assignee"], "worker")


class McpTaskAddParityTests(A37Base):
    def handle(self, **arguments):
        from continuity_plane import agent_tools

        with mock.patch.dict(os.environ, {"CONTINUITY_ASSIGNEE": "controller"}):
            return agent_tools.handle(
                "continuity_task_add", {"project_id": "sample", **arguments}
            )

    def test_single_exit_criterion_string_is_accepted(self):
        self.handle(
            task_id="mcp-one", title="One", body="Do it", exit_criterion="Tests pass"
        )
        self.assertEqual(self.card("mcp-one")["exit_criteria"], ["Tests pass"])

    def test_exit_criterion_list_still_works(self):
        self.handle(
            task_id="mcp-two", title="Two", body="Do it", exit_criterion=["A", "B"]
        )
        self.assertEqual(self.card("mcp-two")["exit_criteria"], ["A", "B"])

    def test_body_file_on_the_state_host(self):
        path = self.tmp / "body.md"
        path.write_text("Body from a file on the state host\n", encoding="utf-8")
        self.handle(task_id="mcp-file", title="File", body_file=str(path))
        from continuity_plane.delivery_records import detail_view

        detail = detail_view(self.data, "sample", "mcp-file")
        self.assertIn("Body from a file", json.dumps(detail, ensure_ascii=False))

    def test_body_and_body_file_together_are_refused(self):
        path = self.tmp / "body.md"
        path.write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "body"):
            self.handle(
                task_id="mcp-both", title="Both", body="inline", body_file=str(path)
            )

    def test_missing_body_message_mentions_body_file(self):
        with self.assertRaisesRegex(ValueError, "body_file"):
            self.handle(task_id="mcp-none", title="None")

    def test_schema_advertises_both_forms(self):
        from continuity_plane import agent_tools

        add = next(
            t for t in agent_tools.definitions() if t["name"] == "continuity_task_add"
        )
        properties = add["inputSchema"]["properties"]
        self.assertIn("body_file", properties)
        self.assertEqual(
            sorted(properties["exit_criterion"]["type"]), ["array", "string"]
        )
        self.assertNotIn("body", add["inputSchema"]["required"])


class SharedBackendOfflineTests(A37Base):
    def setUp(self):
        super().setUp()
        directory = self.root / ".continuity"
        directory.mkdir()
        (directory / "collaboration.toml").write_text(
            '[collaboration]\nbackend = "gitea-issues"\n'
            'instance = "https://forge.example.test:3443/team"\n'
            'repository = "team/sample"\ncredential_ref = "tea:member"\n',
            encoding="utf-8",
        )

    def argv(self):
        return (
            "collaboration", "task", "request-review",
            "--data-root", str(self.data), "--project-id", "sample",
            "--task-id", "task", "--operation-id", "op-1",
            "--expected-remote-revision", "r1", "--reviewer", "alice",
            "--format", "json",
        )

    def test_unreachable_backend_error_names_backend_and_offline_path(self):
        from continuity_plane import gitea_cli
        from continuity_plane.gitea_transport import GiteaUnavailable

        with mock.patch.object(
            gitea_cli, "backend_for", return_value=object()
        ), mock.patch.object(
            gitea_cli, "route_task", side_effect=GiteaUnavailable("local tea request failed")
        ):
            code, out, err = self.entry(*self.argv())
        self.assertEqual(code, 2)
        message = json.loads(err)["message"]
        self.assertIn("gitea-issues", message)
        self.assertIn("team/sample", message)
        self.assertIn("forge.example.test", message)
        self.assertIn("local tea request failed", message)
        self.assertIn("continuity_review_request", message)

    def test_other_gitea_errors_pass_through_unchanged(self):
        from continuity_plane import gitea_cli
        from continuity_plane.gitea_transport import GiteaError

        with mock.patch.object(
            gitea_cli, "backend_for", return_value=object()
        ), mock.patch.object(
            gitea_cli, "route_task", side_effect=GiteaError("link PR first")
        ):
            code, out, err = self.entry(*self.argv())
        self.assertEqual(json.loads(err)["message"], "link PR first")

    def test_missing_tea_names_the_backend_too(self):
        with mock.patch("continuity_plane.gitea_transport.shutil.which", return_value=None):
            code, out, err = self.entry(*self.argv())
        self.assertEqual(code, 2)
        message = json.loads(err)["message"]
        self.assertIn("gitea-issues", message)
        self.assertIn("team/sample", message)
        self.assertIn("tea", message)

    def test_dry_run_is_refused_for_shared_tasks(self):
        from continuity_plane import gitea_cli

        with mock.patch.object(gitea_cli, "backend_for", return_value=object()):
            code, out, err = self.entry(
                "collaboration", "task", "update",
                "--data-root", str(self.data), "--project-id", "sample",
                "--task-id", "task", "--next-action", "x", "--dry-run",
                "--expected-remote-revision", "r1", "--operation-id", "op-2",
            )
        self.assertEqual(code, 2)
        self.assertIn("--dry-run", json.loads(err)["message"])


class QueueRunnerVisibilityTests(A37Base):
    def enqueue(self):
        return dispatch_queue.enqueue(
            self.root, data_root=self.data, project_id="sample",
            task_id="task", model="luna",
        )

    def test_enqueue_says_no_runner_and_how_to_start_one(self):
        item = self.enqueue()
        runner = item["runner"]
        self.assertFalse(runner["consuming"])
        self.assertIn("worker queue run", runner["start_command"])
        self.assertIn("--project-id sample", runner["start_command"])
        self.assertIn("--watch", runner["start_command"])
        self.assertIn("no queue runner", runner["message"])

    def test_status_says_no_runner_when_items_wait(self):
        self.enqueue()
        value = dispatch_queue.status(self.root, data_root=self.data, project_id="sample")
        self.assertFalse(value["runner"]["consuming"])
        self.assertIn("start_command", value["runner"])

    def test_stale_running_lock_is_not_a_runner(self):
        self.enqueue()
        import socket

        lock = self.data / "projects" / "sample" / "dispatch-queue-runner.lock"
        lock.write_text(
            json.dumps(
                {
                    "state": "running",
                    "pid": 2**22 + 12345,
                    "host_id": socket.gethostname(),
                }
            ),
            encoding="utf-8",
        )
        value = dispatch_queue.status(self.root, data_root=self.data, project_id="sample")
        self.assertFalse(value["runner"]["consuming"])

    def test_live_runner_is_reported_as_consuming(self):
        self.enqueue()
        import socket

        lock = self.data / "projects" / "sample" / "dispatch-queue-runner.lock"
        lock.write_text(
            json.dumps(
                {"state": "running", "pid": os.getpid(), "host_id": socket.gethostname()}
            ),
            encoding="utf-8",
        )
        value = dispatch_queue.status(self.root, data_root=self.data, project_id="sample")
        self.assertTrue(value["runner"]["consuming"])
        self.assertNotIn("start_command", value["runner"])

    def test_brief_status_prints_the_hint(self):
        self.enqueue()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main([
                "worker", "queue", "status", "--root", str(self.root),
                "--data-root", str(self.data), "--project-id", "sample",
            ])
        self.assertIn("worker queue run", out.getvalue())


if __name__ == "__main__":
    unittest.main()
