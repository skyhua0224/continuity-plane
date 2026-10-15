from __future__ import annotations

import tempfile
import sys
import json
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch
import unittest
from pathlib import Path

from continuity_plane.collaboration_registry import (
    list_changes,
    register_project,
    add_task,
)
from continuity_plane.supervisor import run_attempt
from continuity_plane.supervisor import daemon_once
from continuity_plane.collaboration_registry import (
    _record_change,
    list_tasks,
    update_task,
    wait_for_changes,
)
from continuity_plane.execution_lifecycle import bind_execution, finish_execution
from continuity_plane.cli import main


class SupervisorTests(unittest.TestCase):
    def add_card(self, data):
        add_task(
            data,
            project_id="sample",
            task_id="task",
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="Scoped worker",
            objective="Verify lifecycle",
            next_action="Run",
            exit_criteria=["pass"],
            assignee="worker",
        )

    def test_run_records_attempt_start_heartbeats_and_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            register_project(
                data, project_id="sample", control_root=directory, repository_roots=[]
            )
            self.add_card(data)
            result = run_attempt(
                data,
                project_id="sample",
                task_id="task",
                assignee="worker",
                command=[sys.executable, "-c", "import time; time.sleep(.15)"],
                heartbeat_seconds=0.03,
                idle_timeout_seconds=2,
                timeout_seconds=3,
            )
            self.assertEqual(result["status"], "completed")
            events = list_changes(data, project_id="sample")["changes"]
            self.assertIn("attempt.started", [event["event"] for event in events])
            self.assertIn("attempt.finished", [event["event"] for event in events])
            self.assertIn("attempt.heartbeat", [event["event"] for event in events])

    def test_nonzero_exit_is_failed_and_timeout_is_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            register_project(
                data, project_id="sample", control_root=directory, repository_roots=[]
            )
            self.add_card(data)
            failed = run_attempt(
                data,
                project_id="sample",
                task_id="task",
                assignee="worker",
                command=[sys.executable, "-c", "raise SystemExit(3)"],
                timeout_seconds=2,
                idle_timeout_seconds=2,
            )
            timed = run_attempt(
                data,
                project_id="sample",
                task_id="task",
                assignee="worker",
                command=[sys.executable, "-c", "import time; time.sleep(2)"],
                timeout_seconds=0.1,
                idle_timeout_seconds=2,
                heartbeat_seconds=0.03,
            )
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(timed["status"], "interrupted")


class WorkerLifecycleRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name) / "data"
        register_project(self.data, project_id="sample", control_root=temporary.name)
        add_task(
            self.data,
            project_id="sample",
            task_id="task",
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="Synthetic worker",
            objective="Verify lifecycle",
            next_action="Run",
            exit_criteria=["pass"],
            assignee="worker",
        )
        self.cursor = list_changes(self.data, project_id="sample")["next_cursor"]

    def card(self):
        return next(
            t
            for t in list_tasks(self.data, project_id="sample")
            if t["task_id"] == "task"
        )

    def bind(self, attempt):
        bind_execution(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            attempt_id=attempt,
        )

    def finish(self, attempt, **kwargs):
        return finish_execution(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            attempt_id=attempt,
            status=kwargs.pop("status", "completed"),
            return_code=kwargs.pop("return_code", 0),
            **kwargs,
        )

    def test_missing_card_rejected_before_either_launcher(self):
        with patch("continuity_plane.supervisor.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "task_id does not exist"):
                run_attempt(
                    self.data,
                    project_id="sample",
                    task_id="typo",
                    assignee="worker",
                    command=["irrelevant"],
                )
            popen.assert_not_called()
        with patch("continuity_plane.cli.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "task_id does not exist"):
                main(
                    [
                        "collaboration",
                        "run",
                        "--project-id",
                        "sample",
                        "--data-root",
                        str(self.data),
                        "--task-id",
                        "typo",
                        "--assignee",
                        "worker",
                        "--",
                        "irrelevant",
                    ]
                )
            run.assert_not_called()
        self.assertEqual(
            list_changes(self.data, project_id="sample", since=self.cursor)["changes"],
            [],
        )

    def test_supervisor_success_and_failure_project_card(self):
        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=[sys.executable, "-c", "pass"],
        )
        self.assertEqual(result["card_status"], "ready-for-review")
        self.assertIn("review remain pending", self.card()["result"])
        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=[sys.executable, "-c", "raise SystemExit(3)"],
        )
        self.assertEqual(result["card_status"], "blocked")
        self.assertEqual(self.card()["blocked_reason"], "exit:3")

    def test_collaboration_run_also_projects_success_and_failure(self):
        for code, expected, cli_exit in ((0, "ready-for-review", 0), (3, "blocked", 2)):
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "collaboration",
                        "run",
                        "--project-id",
                        "sample",
                        "--data-root",
                        str(self.data),
                        "--task-id",
                        "task",
                        "--assignee",
                        "worker",
                        "--",
                        sys.executable,
                        "-c",
                        f"raise SystemExit({code})",
                    ]
                )
            self.assertEqual(result, cli_exit)
            self.assertEqual(json.loads(output.getvalue())["card_status"], expected)
            self.assertEqual(self.card()["status"], expected)

    def test_spawn_failure_is_recorded_and_projects_blocked(self):
        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=[str(self.data / "missing-executable")],
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.card()["status"], "blocked")
        self.assertIn("spawn-error", self.card()["blocked_reason"])

    def test_wrong_assignee_and_closed_card_do_not_launch(self):
        with patch("continuity_plane.supervisor.subprocess.Popen") as popen:
            with self.assertRaisesRegex(ValueError, "another executor"):
                run_attempt(
                    self.data,
                    project_id="sample",
                    task_id="task",
                    assignee="other",
                    command=["irrelevant"],
                )
            update_task(
                self.data, project_id="sample", task_id="task", status="completed"
            )
            with self.assertRaisesRegex(ValueError, "closed"):
                run_attempt(
                    self.data,
                    project_id="sample",
                    task_id="task",
                    assignee="worker",
                    command=["irrelevant"],
                )
            popen.assert_not_called()

    def test_late_finish_cannot_override_new_run_and_finish_is_idempotent(self):
        self.bind("old")
        self.bind("new")
        self.assertEqual(self.finish("old")["card_projection"], "ignored-stale")
        self.assertEqual(self.card()["status"], "active")
        self.finish("new")
        count = len(list_changes(self.data, project_id="sample")["changes"])
        self.assertEqual(self.finish("new")["card_projection"], "already-applied")
        self.assertEqual(
            len(list_changes(self.data, project_id="sample")["changes"]), count
        )
        self.assertEqual(self.card()["status"], "ready-for-review")

    def test_manual_report_and_merge_are_preserved(self):
        self.bind("review")
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="ready-for-review",
            result="Worker report.",
        )
        self.finish("review")
        self.assertEqual(self.card()["result"], "Worker report.")
        self.bind("merge")
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="merged",
            branch="fixture",
            commit="01234567",
            verification="focused regression PASS",
        )
        self.finish("merge", status="failed", return_code=3, reason="exit:3")
        self.assertEqual(self.card()["status"], "merged")

    def test_attempt_identity_cannot_be_reused_after_another_run(self):
        self.bind("first")
        self.finish("first")
        self.bind("second")
        self.finish("second")
        with self.assertRaisesRegex(ValueError, "already used"):
            self.bind("first")

    def test_finish_projection_recovers_after_journal_to_card_crash(self):
        self.bind("recover")
        with patch(
            "continuity_plane.execution_lifecycle._atomic_write",
            side_effect=OSError("synthetic crash"),
        ):
            with self.assertRaises(OSError):
                self.finish("recover")
        self.assertEqual(self.card()["status"], "active")
        daemon_once(self.data, project_id="sample")
        self.assertEqual(self.card()["status"], "ready-for-review")
        daemon_once(self.data, project_id="sample")
        self.assertEqual(
            len(
                [
                    row
                    for row in list_changes(
                        self.data, project_id="sample", since=self.cursor
                    )["changes"]
                    if row["event"] == "attempt.finished"
                ]
            ),
            1,
        )

    def test_partial_stdout_cannot_block_timeout(self):
        started = time.monotonic()
        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=[
                sys.executable,
                "-c",
                "import sys,time;sys.stdout.write('partial');sys.stdout.flush();time.sleep(3)",
            ],
            timeout_seconds=0.15,
            idle_timeout_seconds=2,
            heartbeat_seconds=0.03,
        )
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(self.card()["status"], "blocked")

    def test_output_is_drained_and_tail_stays_bounded(self):
        run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=[sys.executable, "-c", "import os;os.write(1,b'x'*250000+b'end')"],
        )
        finish = next(
            row
            for row in list_changes(self.data, project_id="sample")["changes"]
            if row["event"] == "attempt.finished"
        )
        self.assertTrue(finish["output_tail"].endswith("end"))
        self.assertLessEqual(len(finish["output_tail"].encode()), 4096)

    def test_concurrent_events_have_unique_monotonic_cursors(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(
                pool.map(
                    lambda i: _record_change(self.data, "sample", "probe", str(i)),
                    range(40),
                )
            )
        rows = list_changes(
            self.data, project_id="sample", since=self.cursor, max_results=100
        )["changes"]
        self.assertEqual(
            [row["cursor"] for row in rows],
            list(range(self.cursor + 1, self.cursor + 41)),
        )

    def test_pagination_never_skips_unreturned_events(self):
        for _ in range(3):
            _record_change(self.data, "sample", "probe")
        cursor = self.cursor
        seen = []
        for _ in range(3):
            page = list_changes(
                self.data, project_id="sample", since=cursor, max_results=1
            )
            self.assertEqual(page["high_water_cursor"], self.cursor + 3)
            cursor = page["next_cursor"]
            seen.extend(row["cursor"] for row in page["changes"])
        self.assertEqual(seen, list(range(self.cursor + 1, self.cursor + 4)))

    def test_wait_filters_events_and_timeout_is_explicit(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                wait_for_changes,
                self.data,
                project_id="sample",
                event="attempt.finished",
                task_id="task",
                assignee="worker",
                timeout_seconds=2,
                poll_seconds=0.01,
            )
            _record_change(
                self.data, "sample", "attempt.heartbeat", "task", assignee="worker"
            )
            _record_change(
                self.data, "sample", "attempt.finished", "task", assignee="worker"
            )
            result = future.result(timeout=3)
        self.assertEqual(len(result["changes"]), 1)
        self.assertEqual(result["changes"][0]["event"], "attempt.finished")
        self.assertTrue(
            wait_for_changes(
                self.data,
                project_id="sample",
                since=result["next_cursor"],
                timeout_seconds=0,
            )["timed_out"]
        )

    def test_cli_changes_budget_preserves_json_and_cursor(self):
        for _ in range(3):
            _record_change(
                self.data,
                "sample",
                "attempt.finished",
                "task",
                assignee="worker",
                output_tail="中" * 5000,
            )
        output = StringIO()
        with redirect_stdout(output):
            main(
                [
                    "collaboration",
                    "changes",
                    "--data-root",
                    str(self.data),
                    "--project-id",
                    "sample",
                    "--max-output-bytes",
                    "1024",
                    "--since",
                    str(self.cursor),
                ]
            )
        value = json.loads(output.getvalue())
        self.assertLessEqual(len(output.getvalue().encode()), 1024)
        self.assertEqual(value["returned_bytes"], len(output.getvalue().encode()))
        self.assertEqual(value["next_cursor"], self.cursor + 1)
        self.assertTrue(value["changes"][0]["details_omitted"])
        self.assertEqual(
            len(
                list_changes(self.data, project_id="sample", since=self.cursor + 1)[
                    "changes"
                ]
            ),
            2,
        )

    def test_cli_wait_timeout_is_machine_readable(self):
        output = StringIO()
        with redirect_stdout(output):
            result = main(
                [
                    "collaboration",
                    "wait",
                    "--data-root",
                    str(self.data),
                    "--project-id",
                    "sample",
                    "--event",
                    "attempt.finished",
                    "--timeout-seconds",
                    "0",
                ]
            )
        self.assertEqual(result, 2)
        self.assertTrue(json.loads(output.getvalue())["timed_out"])

    def test_daemon_reads_beyond_first_page_and_ignores_foreign_pids(self):
        self.bind("paged")
        _record_change(
            self.data,
            "sample",
            "attempt.started",
            "task",
            attempt_id="paged",
            pid=12345,
            assignee="worker",
            host_id=socket.gethostname(),
        )
        for _ in range(501):
            _record_change(self.data, "sample", "probe")
        self.finish("paged")
        with patch("continuity_plane.supervisor._alive", return_value=False):
            self.assertEqual(
                daemon_once(self.data, project_id="sample")["reconciled"], []
            )
        self.assertEqual(self.card()["status"], "ready-for-review")
        self.bind("foreign")
        _record_change(
            self.data,
            "sample",
            "attempt.started",
            "task",
            attempt_id="foreign",
            pid=12345,
            assignee="worker",
            host_id="another-host",
        )
        with patch("continuity_plane.supervisor._alive", return_value=False):
            self.assertEqual(
                daemon_once(self.data, project_id="sample")["reconciled"], []
            )
        self.assertEqual(self.card()["status"], "dispatched")

    def test_daemon_attempt_identity_is_scoped_to_card(self):
        add_task(
            self.data,
            project_id="sample",
            task_id="other",
            lane_id="other",
            mode="patch",
            priority="p1",
            title="Other scoped worker",
            objective="Verify scope",
            next_action="Run",
            exit_criteria=["pass"],
            assignee="worker",
        )
        self.bind("same-name")
        bind_execution(
            self.data,
            project_id="sample",
            task_id="other",
            assignee="worker",
            attempt_id="same-name",
        )
        for task in ("task", "other"):
            _record_change(
                self.data,
                "sample",
                "attempt.started",
                task,
                attempt_id="same-name",
                pid=12345,
                assignee="worker",
                host_id=socket.gethostname(),
            )
        self.finish("same-name")
        with patch("continuity_plane.supervisor._alive", return_value=False):
            result = daemon_once(self.data, project_id="sample")
        self.assertEqual(result["reconciled"], ["same-name"])
        self.assertEqual(self.card()["status"], "ready-for-review")
        other = next(
            t
            for t in list_tasks(self.data, project_id="sample")
            if t["task_id"] == "other"
        )
        self.assertEqual(other["status"], "blocked")
