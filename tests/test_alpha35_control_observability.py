"""Alpha35 regressions for worker observability, capacity and state consistency."""

from __future__ import annotations

import json
import select
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from continuity_plane.collaboration_inbox import send_message
from continuity_plane.collaboration_registry import (
    _atomic_write,
    _load_ledger,
    _record_change,
    add_task,
    latest_change_cursor,
    list_changes,
    list_tasks,
    project_data_dir,
    register_project,
    update_task,
)
from continuity_plane.controller_lease import acquire
from continuity_plane.dispatch_health import record as record_health
from continuity_plane.dispatch_health import require_available, show
from continuity_plane.execution_lifecycle import bind_execution
from continuity_plane.git_guard import prepare as prepare_git_guard
from continuity_plane.host_capacity import enforce, set_limits, usage
from continuity_plane.runtime_origin import current
from continuity_plane.supervisor import daemon_once, run_attempt
from continuity_plane.worker_dispatch import configure, dispatch, list_jobs
from continuity_plane.worker_policy import set_completion_hook, set_fallback


class Alpha35Fixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.data = self.base / "data"
        self.root = self.base / "repo"
        self.root.mkdir()
        register_project(self.data, project_id="sample", control_root=self.root)

    def add_card(self, *, task_id="task", assignee="worker-sol", body="Do the work"):
        return add_task(
            self.data,
            project_id="sample",
            task_id=task_id,
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="A35 fixture",
            objective="Verify observability",
            next_action="Run",
            exit_criteria=["PASS"],
            assignee=assignee,
            body=body,
        )

    def configure_sol(self, code=None):
        code = code or 'import json;print(json.dumps({"type":"thread.started","thread_id":"thread-sol"}));print("done")'
        configure(
            self.data,
            model="sol",
            provider="custom",
            argv=[sys.executable, "-c", code, "{effort}"],
            resume_argv=[
                sys.executable,
                "-c",
                code,
                "{effort}",
                "{thread_id}",
            ],
        )
        return self.data / "executor-adapters.json"


class CursorStreamTests(Alpha35Fixture):
    def test_inbox_follow_delivers_two_messages_without_exiting(self):
        from continuity_plane.collaboration_stream import inbox_messages

        stream = inbox_messages(
            self.data, project_id="sample", recipient="worker-sol",
            since_cursor=0, follow=True, timeout_seconds=1, poll_seconds=0.01,
        )
        for number in range(2):
            message = send_message(
                self.data, project_id="sample", sender="controller",
                recipient="worker-sol", body=f"message {number}",
            )
            self.assertEqual(next(stream)["message"]["message_id"], message["message_id"])
        stream.close()

    def test_journal_offsets_consume_only_the_appended_tail(self):
        from continuity_plane.journal_index import offset_after
        from continuity_plane.collaboration_registry import iter_changes

        journal = project_data_dir(self.data, "sample") / "changes.jsonl"
        for number in range(20):
            _record_change(self.data, "sample", "fixture", number=number)
        cursor = latest_change_cursor(self.data, project_id="sample")
        offset = offset_after(journal, cursor)
        self.assertEqual(offset, journal.stat().st_size)
        _record_change(self.data, "sample", "last", event_id="stable-last")
        self.assertEqual(offset_after(journal, cursor), offset)
        rows = list(iter_changes(self.data, project_id="sample", since=cursor))
        self.assertEqual([row["event"] for row in rows], ["last"])
        _record_change(self.data, "sample", "last", event_id="stable-last")
        self.assertEqual(latest_change_cursor(self.data, project_id="sample"), cursor + 1)

    def test_corrupt_journal_index_preserves_events_and_idempotency(self):
        from continuity_plane.collaboration_registry import iter_changes

        _record_change(self.data, "sample", "fixture", event_id="stable-event")
        folder = project_data_dir(self.data, "sample")
        (folder / "changes-index.sqlite3").write_bytes(b"invalid cache")
        _record_change(self.data, "sample", "fixture", event_id="stable-event")
        self.assertEqual(len(list(iter_changes(self.data, project_id="sample", since=0))), 1)

    def test_runtime_routing_reuses_current_projection(self):
        from continuity_plane.runtime_routing import statistics

        _record_change(self.data, "sample", "attempt.started", "task", attempt_id="routing", model="sol")
        self.assertEqual(statistics(self.data, "sample")["sol"]["running"], 1)
        with patch("continuity_plane.runtime_routing.iter_changes", side_effect=AssertionError("warm read replayed history")):
            self.assertEqual(statistics(self.data, "sample")["sol"]["running"], 1)
        _record_change(self.data, "sample", "attempt.finished", "task", attempt_id="routing", model="sol", status="completed")
        self.assertEqual(statistics(self.data, "sample")["sol"]["running"], 0)

    def test_multi_project_stream_cursor_preserves_both_offsets(self):
        from continuity_plane.collaboration_stream import worker_events, parse_cursor_map

        other = self.base / "other"
        other.mkdir()
        register_project(self.data, project_id="other", control_root=other)
        for project in ("sample", "other"):
            _record_change(self.data, project, "attempt.started", "task", attempt_id=project)
        events = list(worker_events(self.data, project_ids=["sample", "other"], since_cursors={"sample": 0, "other": 0}, follow=False, poll_seconds=0.01, max_events=None))
        cursors = parse_cursor_map(events[-1]["stream_cursor"], ["sample", "other"])
        self.assertGreater(cursors["sample"], 0)
        self.assertGreater(cursors["other"], 0)
        self.assertEqual(list(worker_events(self.data, project_ids=["sample", "other"], since_cursors=cursors, follow=False, poll_seconds=0.01, max_events=None)), [])

    def test_worker_follow_stream_uses_cursor_and_reads_append_after_start(self):
        self.add_card()
        cursor = latest_change_cursor(self.data, project_id="sample")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "continuity_plane.cli",
                "worker",
                "events",
                "--data-root",
                str(self.data),
                "--project-id",
                "sample",
                "--since-cursor",
                str(cursor),
                "--follow",
                "--max-events",
                "1",
                "--poll-seconds",
                "0.05",
            ],
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            time.sleep(0.1)
            _record_change(
                self.data,
                "sample",
                "attempt.started",
                "task",
                attempt_id="attempt-stream",
                assignee="worker-sol",
                pid=123,
                host_id="fixture",
            )
            ready, _, _ = select.select([process.stdout], [], [], 5)
            self.assertTrue(ready, "worker event stream did not emit a new event")
            row = json.loads(process.stdout.readline())
            self.assertEqual(row["kind"], "started")
            self.assertEqual(row["stream_cursor"], f"sample:{row['cursor']}")
            self.assertGreater(row["cursor"], cursor)
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout, "")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    def test_inbox_wait_and_unread_count_use_same_persisted_fact(self):
        message = send_message(
            self.data,
            project_id="sample",
            sender="controller",
            recipient="worker-sol",
            body="inspect this",
        )
        from continuity_plane.collaboration_inbox import unread_summary
        from continuity_plane.collaboration_stream import wait_for_inbox_message

        self.assertEqual(
            unread_summary(
                self.data, project_id="sample", recipient="worker-sol"
            )["count"],
            1,
        )
        value = wait_for_inbox_message(
            self.data,
            project_id="sample",
            recipient="worker-sol",
            since_cursor=0,
            follow=False,
        )
        self.assertEqual(value["message"]["message_id"], message["message_id"])

    def test_all_project_cursor_parser_rejects_ambiguous_integer(self):
        from continuity_plane.collaboration_stream import parse_cursor_map

        with self.assertRaisesRegex(ValueError, "project:cursor"):
            parse_cursor_map("3", ["one", "two"])
        self.assertEqual(
            parse_cursor_map("one:3,two:7", ["one", "two"]),
            {"one": 3, "two": 7},
        )


class SupervisorObservationTests(Alpha35Fixture):
    def test_daemon_without_running_bindings_does_not_scan_history(self):
        self.add_card()
        with patch(
            "continuity_plane.supervisor.iter_changes",
            side_effect=AssertionError("history must not be scanned"),
        ):
            value = daemon_once(self.data, project_id="sample")
        self.assertEqual(value["reconciled"], [])

    def test_daemon_does_not_interrupt_bind_before_spawn(self):
        self.add_card()
        bind_execution(
            self.data, project_id="sample", task_id="task",
            assignee="worker-sol", attempt_id="just-bound",
        )
        self.assertEqual(daemon_once(self.data, project_id="sample")["reconciled"], [])
        self.assertEqual(_load_ledger(self.data, "sample")["execution_bindings"]["task"]["status"], "running")

    def test_process_binding_records_actual_git_head_and_finish_cursor(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Fixture"],
            check=True,
        )
        (self.root / "README").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "README"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "fixture"], check=True
        )
        expected = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.add_card()
        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker-sol",
            command=[sys.executable, "-c", "import time;time.sleep(.2)"],
            workdir=str(self.root),
            timeout_seconds=2,
            idle_timeout_seconds=2,
            heartbeat_seconds=0.05,
        )
        binding = _load_ledger(self.data, "sample")["execution_bindings"]["task"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(binding["observed_cwd"], str(self.root.resolve()))
        self.assertEqual(binding["observed_head"], expected)
        self.assertIsInstance(binding["event_start_cursor"], int)
        self.assertIsInstance(binding["finished_cursor"], int)

    def test_heartbeat_classifies_lock_wait_without_killing_worker(self):
        self.add_card()
        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker-sol",
            command=[
                sys.executable,
                "-c",
                "import time;print('waiting for build lock',flush=True);time.sleep(.25)",
            ],
            timeout_seconds=2,
            idle_timeout_seconds=2,
            heartbeat_seconds=0.05,
        )
        self.assertEqual(result["status"], "completed")
        events = list_changes(self.data, project_id="sample")["changes"]
        heartbeats = [row for row in events if row["event"] == "attempt.heartbeat"]
        self.assertTrue(
            any(row.get("idle_reason") == "waiting-for-build-lock" for row in heartbeats)
        )

    def test_process_metadata_failure_warns_without_blocking_spawned_worker(self):
        self.add_card()
        with patch(
            "continuity_plane.supervisor.record_execution_process",
            side_effect=OSError("fixture metadata unavailable"),
        ):
            result = run_attempt(
                self.data,
                project_id="sample",
                task_id="task",
                assignee="worker-sol",
                command=[sys.executable, "-c", "pass"],
                timeout_seconds=2,
                idle_timeout_seconds=2,
            )
        self.assertEqual(result["status"], "completed")
        self.assertIn("runtime_tracking_error", result)


class DispatchStateTests(Alpha35Fixture):
    def test_job_document_and_index_have_terminal_exit_state(self):
        self.add_card()
        self.configure_sol()
        result = dispatch(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="sol",
            timeout_seconds=2,
            idle_timeout_seconds=2,
        )
        job_path = (
            project_data_dir(self.data, "sample")
            / "worker-jobs"
            / result["job_id"]
            / "job.json"
        )
        job = json.loads(job_path.read_text())
        index = json.loads(
            (project_data_dir(self.data, "sample") / "worker-jobs.json").read_text()
        )
        self.assertEqual((job["status"], job["exit_code"]), ("completed", 0))
        self.assertEqual(index["task"]["status"], "completed")
        history = json.loads(
            (
                project_data_dir(self.data, "sample")
                / "worker-job-history.json"
            ).read_text()
        )
        self.assertEqual(history[result["job_id"]]["status"], "completed")
        listed = list_jobs(self.data, project_id="sample", status="ended")
        self.assertEqual(listed["items"][0]["job_id"], result["job_id"])

    def test_project_fallback_policy_reassigns_only_after_transient_failure(self):
        self.add_card()
        self.configure_sol('print("502 Bad Gateway", flush=True); raise SystemExit(1)')
        configure(
            self.data,
            model="sonnet",
            provider="custom",
            argv=[
                sys.executable,
                "-c",
                'import json;print(json.dumps({"type":"thread.started","thread_id":"thread-sonnet"}));print("done")',
                "{effort}",
            ],
            resume_argv=[
                sys.executable,
                "-c",
                'import json;print(json.dumps({"type":"thread.started","thread_id":"thread-sonnet"}));print("done")',
                "{effort}",
                "{thread_id}",
            ],
        )
        set_fallback(self.data, "sample", model="sol", candidates=["sonnet"])
        result = dispatch(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="sol",
            timeout_seconds=2,
            idle_timeout_seconds=2,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["primary_result"]["failure_class"], "gateway")
        card = next(
            row
            for row in list_tasks(self.data, project_id="sample")
            if row["task_id"] == "task"
        )
        self.assertEqual(card["assignee"], "worker-sonnet")
        reassignment = _load_ledger(self.data, "sample")["task_details"]["task"][
            "reassignments"
        ][-1]
        self.assertEqual((reassignment["from"], reassignment["to"]), ("worker-sol", "worker-sonnet"))
        self.assertTrue(reassignment["forced"])

    def test_completion_hook_is_durable_nonblocking_and_idempotent(self):
        self.add_card()
        self.configure_sol()
        marker = self.base / "hook-ran"
        set_completion_hook(
            self.data,
            "sample",
            argv=[
                sys.executable,
                "-c",
                "from pathlib import Path;Path(__import__('sys').argv[1]).write_text('ok')",
                str(marker),
            ],
            enabled=True,
        )
        result = dispatch(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="sol",
            timeout_seconds=2,
            idle_timeout_seconds=2,
        )
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(marker.exists())
        job = json.loads(
            (
                project_data_dir(self.data, "sample")
                / "worker-jobs"
                / result["job_id"]
                / "job.json"
            ).read_text()
        )
        self.assertEqual(job["completion_hook"]["status"], "launched")
        self.assertFalse(job["completion_hook"]["blocking"])


class SafetyAndCapacityTests(Alpha35Fixture):
    def test_git_guard_blocks_ref_writes_to_canonical_root(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "fixture@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Fixture"],
            check=True,
        )
        (self.root / "README").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "README"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "fixture"], check=True
        )
        _atomic_write(
            project_data_dir(self.data, "sample") / "project-context.json",
            {
                "schema_version": "context.project-context/v1",
                "sessions": {},
                "lessons": {},
                "worker_write_guard": {
                    "enabled": True,
                    "canonical_roots": [str(self.root.resolve())],
                    "mechanism": "PATH git wrapper; not an OS security sandbox",
                },
            },
        )
        wrapper = prepare_git_guard(self.data, "sample") / "git"
        completed = subprocess.run(
            [str(wrapper), "-C", str(self.root), "update-ref", "refs/heads/forbidden", "HEAD"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 78)
        self.assertIn("canonical-repository-worker-write-guard", completed.stderr)
        ref = subprocess.run(
            ["git", "-C", str(self.root), "show-ref", "--verify", "refs/heads/forbidden"],
            capture_output=True,
        )
        self.assertNotEqual(ref.returncode, 0)
        clone = self.base / "clone"
        subprocess.run(["git", "clone", "-q", str(self.root), str(clone)], check=True)
        named_remote = subprocess.run(
            [str(wrapper), "-C", str(clone), "push", "origin", "HEAD:refs/heads/forbidden"],
            capture_output=True, text=True,
        )
        self.assertEqual(named_remote.returncode, 78)

    def test_expired_health_is_not_paused_and_model_clear_does_not_guess_provider(self):
        self.add_card()
        origin = current("custom")
        record_health(
            self.data,
            "sample",
            origin,
            "sol",
            {
                "failure_class": "rate-limit",
                "available_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                "reason": "fixture",
            },
        )
        require_available(self.data, "sample", origin, "sol")
        item = show(self.data, "sample")["items"][0]
        self.assertEqual((item["status"], item["effective_paused"]), ("expired", False))
        from continuity_plane.dispatch_health import clear

        cleared = clear(
            self.data, "sample", None, "sol", "expired fixture reconciled", all_matching=True
        )
        self.assertEqual(len(cleared["cleared"]), 1)

    def test_host_capacity_counts_across_projects_but_ignores_prior_boot(self):
        from continuity_plane.controller_lease import _boot_fingerprint, _host_fingerprint

        for project_id in ("one", "two", "three"):
            register_project(self.data, project_id=project_id, control_root=self.root)
        current_host = _host_fingerprint()
        current_boot = _boot_fingerprint()
        _atomic_write(
            project_data_dir(self.data, "one") / "project-context.json",
            {
                "schema_version": "context.project-context/v1",
                "sessions": {},
                "lessons": {},
                "dispatch_reservations": {
                    "old": {
                        "client": "codex",
                        "host_sha256": current_host,
                        "boot_sha256": "prior-boot",
                        "origin": current("codex"),
                    }
                },
            },
        )
        _atomic_write(
            project_data_dir(self.data, "two") / "project-context.json",
            {
                "schema_version": "context.project-context/v1",
                "sessions": {},
                "lessons": {},
                "dispatch_reservations": {
                    "live": {
                        "client": "codex",
                        "host_sha256": current_host,
                        "boot_sha256": current_boot,
                        "origin": current("codex"),
                    }
                },
            },
        )
        set_limits(self.data, {"codex": 2})
        value = usage(self.data)
        self.assertEqual(value["counts"]["codex"], 1)
        enforce(self.data, "codex")

    def test_host_total_limit_covers_all_client_families(self):
        from continuity_plane.controller_lease import reserve

        self.add_card()
        other_root = self.base / "other-repo"
        other_root.mkdir()
        register_project(self.data, project_id="other", control_root=other_root)
        set_limits(self.data, {"total": 1})
        reserve(
            self.data, "sample", task_id="task", job_id="first", owner="operator", token=None,
            profile={"provider": "codex", "execution_origin": current("codex")},
        )
        with self.assertRaisesRegex(ValueError, "host-wide total"):
            reserve(
                self.data, "other", task_id="second", job_id="second", owner="operator", token=None,
                profile={"provider": "custom", "execution_origin": current("custom")},
            )

    def test_concurrent_projects_have_one_host_capacity_winner(self):
        from continuity_plane.controller_lease import reserve

        other = self.base / "other"
        other.mkdir()
        register_project(self.data, project_id="other", control_root=other)
        set_limits(self.data, {"total": 1})
        barrier = threading.Barrier(2)

        def launch(project):
            barrier.wait(timeout=5)
            try:
                reserve(self.data, project, task_id="task", job_id=project,
                        owner="operator", token=None,
                        profile={"provider": "custom", "execution_origin": current("custom")})
                return "reserved"
            except ValueError as exc:
                self.assertIn("capacity is full", str(exc))
                return "full"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(launch, ["sample", "other"]))
        self.assertCountEqual(outcomes, ["reserved", "full"])


class TaskSemanticsTests(Alpha35Fixture):
    def test_queued_preserves_assignee_and_explicit_unassign_clears_it(self):
        self.add_card(assignee="worker-sol")
        queued = update_task(
            self.data, project_id="sample", task_id="task", status="queued"
        )
        self.assertEqual(queued["assignee"], "worker-sol")
        from continuity_plane.collaboration_registry import claim_task

        with self.assertRaisesRegex(ValueError, "another session"):
            claim_task(self.data, project_id="sample", task_id="task", assignee="other-worker")
        cleared = update_task(
            self.data,
            project_id="sample",
            task_id="task",
            unassign=True,
            actor="worker-sol",
        )
        self.assertIsNone(cleared["assignee"])

    def test_controller_lease_holder_can_reassign_with_recorded_reason(self):
        self.add_card(assignee="worker-sol")
        lease = acquire(self.data, "sample", owner="controller", ttl_seconds=60)
        with self.assertRaisesRegex(ValueError, "token"):
            update_task(
                self.data, project_id="sample", task_id="task",
                assignee="worker-sonnet", actor="controller", force_reason="missing fence",
                controller_token="wrong-token",
            )
        with self.assertRaisesRegex(ValueError, "--reason"):
            update_task(
                self.data,
                project_id="sample",
                task_id="task",
                assignee="worker-sonnet",
                actor="controller",
                controller_token=lease["token"],
            )
        value = update_task(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker-sonnet",
            actor="controller",
            controller_token=lease["token"],
            force_reason="controller moved the card to the available worker",
        )
        self.assertEqual(value["assignee"], "worker-sonnet")
        record = _load_ledger(self.data, "sample")["task_details"]["task"][
            "reassignments"
        ][-1]
        self.assertEqual(record["authority"], "controller-lease")
        self.assertEqual(record["actor"], "controller")
        self.assertTrue(lease["token"])

    def test_superseded_dependency_is_terminal(self):
        from continuity_plane.collaboration_registry import dependencies_ready

        self.assertTrue(
            dependencies_ready(
                {"dependencies": ["old"]},
                [{"task_id": "old", "status": "superseded"}],
            )
        )
        self.assertFalse(
            dependencies_ready(
                {"dependencies": ["old"]},
                [{"task_id": "old", "status": "blocked"}],
            )
        )

    def test_task_show_default_budget_is_large_but_bounded(self):
        from continuity_plane.cli import build_parser

        args = build_parser().parse_args(
            [
                "collaboration",
                "task",
                "show",
                "--project-id",
                "sample",
                "--task-id",
                "task",
            ]
        )
        self.assertEqual(args.max_output_bytes, 65536)


if __name__ == "__main__":
    unittest.main()
