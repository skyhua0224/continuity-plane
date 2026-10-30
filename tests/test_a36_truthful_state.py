"""Alpha36 truthful state (workstream 2D): precise event kinds, interim reports,
ghost jobs, the reconciler and the handoff plan limit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta

from continuity_plane import collaboration_stream
from continuity_plane.collaboration_registry import (
    _atomic_write,
    _ledger_path,
    _load_ledger,
    _record_change,
    list_changes,
    project_data_dir,
    update_task,
)
from continuity_plane.supervisor import run_attempt
from continuity_plane.worker_dispatch import dispatch, list_jobs

from tests import test_a36_card_dispatch as base


def events(case, name):
    return [
        c
        for c in list_changes(case.data, project_id="sample", max_results=500)["changes"]
        if c["event"] == name
    ]


class RetryEventTests(base.A36Base):
    def attempt(self, code, **kwargs):
        return run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=[sys.executable, "-c", code],
            timeout_seconds=10,
            idle_timeout_seconds=5,
            attempt_id="job-0123456789abcdef",
            **kwargs,
        )

    def test_internal_retry_is_not_a_terminal_attempt_event(self):
        marker = self.tmp / "second"
        code = (
            "import pathlib,sys\n"
            f"m=pathlib.Path({str(marker)!r})\n"
            "if m.exists(): sys.exit(0)\n"
            "m.write_text('x'); print('502 Bad Gateway'); sys.exit(1)\n"
        )
        result = self.attempt(code, retry=1, retry_delay_seconds=0, restart_on_retry=True)
        self.assertEqual(result["status"], "completed")
        (retrying,) = events(self, "attempt.retrying")
        self.assertEqual(retrying["attempt_id"], "job-0123456789abcdef")
        self.assertEqual(retrying["status"], "failed")
        self.assertEqual(retrying["failure_class"], "gateway")
        # Only the last attempt finishes; the job completes exactly once.
        (finished,) = events(self, "attempt.finished")
        self.assertEqual(finished["attempt_id"], "job-0123456789abcdef-retry-1")
        (job,) = events(self, "job.completed")
        self.assertEqual(job["job_id"], "job-0123456789abcdef")
        self.assertEqual(job["status"], "completed")

    def test_terminal_failure_emits_job_failed_with_failure_class_and_keeps_attempt_finished(self):
        self.attempt("print('401 unauthorized'); raise SystemExit(1)")
        (job,) = events(self, "job.failed")
        self.assertEqual(job["failure_class"], "authentication")
        self.assertEqual(job["reason"], "exit:1")
        (finished,) = events(self, "attempt.finished")
        self.assertEqual(finished["failure_class"], "authentication")
        self.assertEqual(events(self, "attempt.retrying"), [])

    def test_exhausted_retries_end_with_one_terminal_failure(self):
        self.attempt(
            "print('502 Bad Gateway'); raise SystemExit(1)",
            retry=1,
            retry_delay_seconds=0,
            restart_on_retry=True,
        )
        self.assertEqual(len(events(self, "attempt.retrying")), 1)
        self.assertEqual(len(events(self, "attempt.finished")), 1)
        (job,) = events(self, "job.failed")
        self.assertEqual(job["failure_class"], "gateway")

    def test_killed_worker_is_job_interrupted(self):
        self.attempt("import os,signal; os.kill(os.getpid(), signal.SIGKILL)")
        (job,) = events(self, "job.interrupted")
        self.assertEqual(job["reason"], "signal:9")
        self.assertEqual(events(self, "job.failed"), [])

    def test_stream_kinds(self):
        kind = collaboration_stream.event_kind
        self.assertEqual(kind({"event": "attempt.retrying", "status": "failed"}), "retrying")
        self.assertEqual(kind({"event": "job.completed", "status": "completed"}), "completed")
        self.assertEqual(kind({"event": "job.failed", "status": "failed"}), "failed")
        self.assertEqual(
            kind({"event": "job.failed", "status": "failed", "failure_class": "rate-limit"}),
            "rate-limited",
        )
        self.assertEqual(kind({"event": "job.interrupted", "status": "interrupted"}), "interrupted")
        self.assertEqual(kind({"event": "job.lost", "status": "interrupted"}), "lost")
        for name in ("attempt.retrying", "job.completed", "job.failed", "job.interrupted", "job.lost"):
            self.assertIn(name, collaboration_stream._WORKER_EVENTS)
        self.assertIn("lost", collaboration_stream._WAIT_KINDS)
        self.assertNotIn("retrying", collaboration_stream._WAIT_KINDS)


class WaitAndNoticeTests(base.A36Base):
    def wait(self, timeout="0.6"):
        return self.cli(
            "worker", "wait", "--data-root", str(self.data), "--project-id", "sample",
            "--poll-seconds", "0.05", "--timeout-seconds", timeout,
        )

    def test_wait_ignores_retrying_and_wakes_once_per_terminal_outcome(self):
        def later():
            time.sleep(0.2)
            _record_change(self.data, "sample", "attempt.retrying", "task",
                           attempt_id="job-1", status="failed", reason="exit:1")
            time.sleep(0.2)
            _record_change(self.data, "sample", "attempt.finished", "task",
                           attempt_id="job-1-retry-1", job_id="job-1", status="failed",
                           failure_class="authentication")
            _record_change(self.data, "sample", "job.failed", "task",
                           attempt_id="job-1-retry-1", job_id="job-1", status="failed",
                           failure_class="authentication")

        threading.Thread(target=later, daemon=True).start()
        code, first = self.cli(
            "worker", "wait", "--data-root", str(self.data), "--project-id", "sample",
            "--poll-seconds", "0.05", "--timeout-seconds", "5", "--max-events", "1",
        )
        self.assertEqual(code, 0)
        self.assertEqual((first["kind"], first["failure_class"]), ("failed", "authentication"))
        # The companion job event of the same outcome does not wake a second wait.
        code, second = self.cli(
            "worker", "wait", "--data-root", str(self.data), "--project-id", "sample",
            "--poll-seconds", "0.05", "--timeout-seconds", "0.5",
            "--since-cursor", first["stream_cursor"].split(":")[1],
        )
        self.assertEqual((code, second["status"]), (3, "timeout"))

    def test_wait_wakes_on_lost_job(self):
        def later():
            time.sleep(0.2)
            _record_change(self.data, "sample", "job.lost", "task", job_id="job-1",
                           attempt_id="job-1", status="interrupted", reason="process-lost")

        threading.Thread(target=later, daemon=True).start()
        code, value = self.wait("5")
        self.assertEqual(code, 0)
        self.assertEqual((value["kind"], value["reason"]), ("lost", "process-lost"))

    def notice(self, session):
        from continuity_plane.controller_awareness import notice

        return notice(
            {"cwd": str(self.root), "session_id": session, "hook_event_name": "PostToolUse"},
            data_root=self.data,
            max_bytes=1024,
        )

    def test_notice_fires_only_for_terminal_outcomes(self):
        self.assertIsNone(self.notice("ctl"))
        _record_change(self.data, "sample", "attempt.retrying", "task",
                       attempt_id="job-1", status="failed", reason="exit:1")
        self.assertIsNone(self.notice("ctl"))
        _record_change(self.data, "sample", "attempt.finished", "task",
                       attempt_id="job-1-retry-1", job_id="job-1", status="failed",
                       failure_class="authentication")
        _record_change(self.data, "sample", "job.failed", "task",
                       attempt_id="job-1-retry-1", job_id="job-1", status="failed",
                       failure_class="authentication")
        text = self.notice("ctl")
        self.assertEqual(text.count("worker task failed (authentication)"), 1)


class InterimReportTests(base.A36Base):
    def setUp(self):
        super().setUp()
        update_task(self.data, project_id="sample", task_id="task",
                    status="ready-for-review", report={"text": "# Report\n\nDone so far.\n"})

    def job(self, status):
        index = project_data_dir(self.data, "sample") / "worker-jobs.json"
        index.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(index, {"task": {"job_id": "job-0123456789abcdef", "task_id": "task",
                                        "status": status, "job_status": status,
                                        "dispatched_at": datetime.now(UTC).isoformat()}})

    def report(self):
        code, text = self.cli("worker", "report", "--data-root", str(self.data),
                              "--project-id", "sample", "--task-id", "task")
        self.assertEqual(code, 0)
        return text

    def test_completed_job_is_final(self):
        self.job("completed")
        header, body = self.report().split("\n", 1)
        self.assertIn("final=true", header)
        self.assertIn("job_status=completed", header)
        self.assertNotIn("INTERIM", body)

    def test_running_and_interrupted_jobs_are_labelled_interim(self):
        for status in ("running", "interrupted"):
            self.job(status)
            header, body = self.report().split("\n", 1)
            self.assertIn("final=false", header)
            self.assertIn(f"job_status={status}", header)
            self.assertTrue(body.startswith("INTERIM report"), body[:80])
            self.assertIn(status, body.split("\n", 1)[0])

    def test_header_stays_parseable_for_paging(self):
        self.job("completed")
        code, text = self.cli("worker", "report", "--data-root", str(self.data),
                              "--project-id", "sample", "--task-id", "task", "--offset", "0")
        header = text.split("\n", 1)[0]
        self.assertTrue(header.rsplit("next_offset_chars=", 1)[1] in {"none"} or
                        header.rsplit("next_offset_chars=", 1)[1].isdigit())
        self.assertIn("final=true", header)

    def test_card_without_a_job_is_final(self):
        header = self.report().split("\n", 1)[0]
        self.assertIn("final=true", header)
        self.assertIn("job_status=none", header)

    def test_mcp_tool_reports_interim(self):
        from continuity_plane import agent_tools

        self.job("running")
        old = os.environ.get("CONTINUITY_DATA_ROOT")
        os.environ["CONTINUITY_DATA_ROOT"] = str(self.data)
        self.addCleanup(lambda: os.environ.__setitem__("CONTINUITY_DATA_ROOT", old))
        value = agent_tools.handle(
            "continuity_worker_report", {"project_id": "sample", "task_id": "task"}
        )
        self.assertIn("final=false", value["text"])
        self.assertIn("INTERIM", value["text"])


class GhostJobTests(base.A36Base):
    """A running job whose process is gone must not stay running or hold capacity."""

    def setUp(self):
        super().setUp()
        self.adapter("sol", "print('hi')")

    def dead_pid(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait()
        return process.pid

    def ghost(self, runner_pid=None):
        """Dispatch in the background without a real runner, then mark it running."""
        from unittest import mock

        from continuity_plane import worker_dispatch

        original = subprocess.Popen

        def launch(argv, *args, **kwargs):
            if "continuity_plane.worker_dispatch" in argv:
                return mock.Mock(pid=999999)
            return original(argv, *args, **kwargs)

        with mock.patch.object(worker_dispatch.subprocess, "Popen", side_effect=launch):
            result = dispatch(self.root, data_root=self.data, task_id="task", model="sol",
                              background=True)
        job_file = project_data_dir(self.data, "sample") / "worker-jobs" / result["job_id"] / "job.json"
        job = json.loads(job_file.read_text())
        worker_dispatch._persist_job_running(job)
        running = json.loads(job_file.read_text())
        running["runner_pid"] = runner_pid or self.dead_pid()
        running["runner_identity"] = None
        _atomic_write(job_file, running)
        return result["job_id"]

    def reservations(self):
        from continuity_plane.project_context import _load, _path

        return _load(_path(self.data, "sample")).get("dispatch_reservations", {})

    def later(self, seconds=120):
        return datetime.now(UTC) + timedelta(seconds=seconds)

    def test_dead_runner_with_stale_heartbeat_becomes_lost_and_releases_capacity(self):
        job_id = self.ghost()
        self.assertIn("task", self.reservations())
        listed = list_jobs(self.data, project_id="sample", status="running", now=self.later())
        self.assertEqual(listed["items"], [])
        lost = list_jobs(self.data, project_id="sample", status="lost", now=self.later())
        self.assertEqual([i["job_id"] for i in lost["items"]], [job_id])
        self.assertEqual(lost["items"][0]["reason"], "process-lost")
        self.assertEqual(self.reservations(), {})
        (event,) = events(self, "job.lost")
        self.assertEqual(event["job_id"], job_id)
        # The recorded job document agrees, and listing again is idempotent.
        job_file = project_data_dir(self.data, "sample") / "worker-jobs" / job_id / "job.json"
        self.assertEqual(json.loads(job_file.read_text())["job_status"], "lost")
        list_jobs(self.data, project_id="sample", now=self.later())
        self.assertEqual(len(events(self, "job.lost")), 1)

    def test_killed_background_job_with_a_running_attempt_is_lost_and_projected(self):
        import signal

        self.adapter("sol", "import time; time.sleep(60)")
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol", background=True)
        deadline = time.monotonic() + 20
        binding = {}
        while time.monotonic() < deadline and not binding.get("pid"):
            time.sleep(0.1)
            binding = _load_ledger(self.data, "sample").get("execution_bindings", {}).get("task", {})
        self.assertTrue(binding.get("pid"), "worker never started")
        self.assertEqual(
            list_jobs(self.data, project_id="sample", status="running")["items"][0]["job_id"],
            result["job_id"],
        )
        os.kill(result["supervisor_pid"], signal.SIGKILL)
        os.kill(binding["pid"], signal.SIGKILL)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(
            self.alive(pid) for pid in (result["supervisor_pid"], binding["pid"])
        ):
            time.sleep(0.05)
        listed = list_jobs(self.data, project_id="sample", status="running", now=self.later())
        self.assertEqual(listed["items"], [])
        after = _load_ledger(self.data, "sample")["execution_bindings"]["task"]
        self.assertEqual((after["status"], after["reason"]), ("interrupted", "process-lost"))
        self.assertEqual(self.reservations(), {})
        self.assertEqual(len(events(self, "job.lost")), 1)
        self.assertEqual(events(self, "job.interrupted"), [])

    @staticmethod
    def alive(pid):
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def test_fresh_heartbeat_is_not_lost_yet(self):
        self.ghost()
        listed = list_jobs(self.data, project_id="sample", status="running")
        self.assertEqual(len(listed["items"]), 1)
        self.assertIn("task", self.reservations())
        self.assertEqual(events(self, "job.lost"), [])

    def test_live_runner_is_never_lost(self):
        self.ghost(runner_pid=os.getpid())
        listed = list_jobs(self.data, project_id="sample", status="running", now=self.later(3600))
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(events(self, "job.lost"), [])

    def test_reused_pid_with_other_start_identity_counts_as_gone(self):
        from continuity_plane import worker_dispatch

        job_id = self.ghost(runner_pid=os.getpid())
        job_file = project_data_dir(self.data, "sample") / "worker-jobs" / job_id / "job.json"
        job = json.loads(job_file.read_text())
        job["runner_identity"] = "other-boot:1"
        _atomic_write(job_file, job)
        found = worker_dispatch.find_lost_jobs(self.data, "sample", now=self.later())
        self.assertEqual([row["job_id"] for row in found], [job_id])

    def test_lost_job_frees_the_card_for_a_new_dispatch(self):
        self.ghost()
        list_jobs(self.data, project_id="sample", now=self.later())
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["status"], "completed")

    def test_dispatch_reaps_ghosts_before_taking_capacity(self):
        from continuity_plane import worker_dispatch

        self.ghost()
        real = worker_dispatch.datetime
        later = self.later()

        class Clock(real):
            @classmethod
            def now(cls, tz=None):
                return later

        worker_dispatch.datetime = Clock
        self.addCleanup(setattr, worker_dispatch, "datetime", real)
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(events(self, "job.lost")), 1)


class ReconcileTests(base.A36Base):
    def git(self, *argv):
        return subprocess.run(
            ["git", "-C", str(self.root), *argv], check=True, capture_output=True, text=True
        ).stdout.strip()

    def setUp(self):
        super().setUp()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        (self.root / "a").write_text("a")
        self.git("add", "a")
        self.git("commit", "-qm", "base")
        self.landed = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "feature")
        (self.root / "b").write_text("b")
        self.git("add", "b")
        self.git("commit", "-qm", "feature")
        self.unlanded = self.git("rev-parse", "HEAD")
        self.git("checkout", "-q", "main")

    def card(self, task_id, status=None, result=None, **overrides):
        self.add(task_id, **overrides)
        if status or result:
            update_task(self.data, project_id="sample", task_id=task_id, status=status,
                        result=result, actor="worker")

    def reconcile(self, *argv, project="sample"):
        return self.cli(
            "collaboration", "reconcile", "--data-root", str(self.data),
            "--project-id", project, "--root", str(self.root), *argv,
        )

    def status(self, task_id):
        return next(t for t in _load_ledger(self.data, "sample")["tasks"] if t["task_id"] == task_id)["status"]

    def test_merged_suggestion_is_read_only_by_default(self):
        self.card("landed", status="ready-for-review", commit=self.landed, branch="b1")
        self.card("open", status="ready-for-review", commit=self.unlanded, branch="b2")
        code, value = self.reconcile()
        self.assertEqual(code, 0)
        self.assertFalse(value["applied"])
        merged = [s for s in value["suggestions"] if s["kind"] == "merged"]
        self.assertEqual([s["task_id"] for s in merged], ["landed"])
        self.assertIn("--apply", merged[0]["command"])
        self.assertEqual(self.status("landed"), "ready-for-review")

    def test_apply_records_actor_and_reason(self):
        self.card("landed", status="ready-for-review", commit=self.landed, branch="b1")
        self.assertEqual(self.reconcile("--apply")[0], 2)  # actor is mandatory
        code, value = self.reconcile("--apply", "--actor", "claude-opus")
        self.assertEqual(code, 0)
        self.assertEqual(self.status("landed"), "merged")
        (event,) = events(self, "task.reconciled")
        self.assertEqual((event["actor"], event["task_id"], event["kind"]), ("claude-opus", "landed", "merged"))
        self.assertIn("ancestor", event["reason"])
        self.assertEqual(value["applied"], ["landed"])

    def test_apply_can_be_limited_to_named_tasks(self):
        self.card("one", status="ready-for-review", commit=self.landed, branch="b1")
        self.card("two", status="ready-for-review", commit=self.landed, branch="b2")
        self.reconcile("--apply", "--actor", "claude-opus", "--task-id", "one")
        self.assertEqual((self.status("one"), self.status("two")), ("merged", "ready-for-review"))

    def test_accepted_review_makes_the_card_ready_to_merge_but_is_never_applied(self):
        self.card("feature", status="ready-for-review", commit=self.unlanded, branch="b2")
        self.card("review-feature-0101", lane_id="review", title="Review feature",
                 objective="Review card feature", status="completed",
                 result="Verdict: ACCEPT. No blocking findings.")
        self.card("review-other-0101", lane_id="review", title="Review other",
                 objective="Review card other", status="completed", result="ACCEPT")
        code, value = self.reconcile("--apply", "--actor", "claude-opus")
        ready = [s for s in value["suggestions"] if s["kind"] == "ready-to-merge"]
        self.assertEqual([s["task_id"] for s in ready], ["feature"])
        self.assertEqual(ready[0]["review_task_id"], "review-feature-0101")
        self.assertEqual(self.status("feature"), "ready-for-review")

    def test_review_asking_for_changes_is_not_an_accept(self):
        self.card("feature", status="ready-for-review", commit=self.unlanded, branch="b2")
        self.card("review-feature-0101", lane_id="review", title="Review feature",
                 objective="Review card feature", status="completed",
                 result="Changes needed; do not accept before the parser fix.")
        _, value = self.reconcile()
        self.assertEqual([s for s in value["suggestions"] if s["kind"] == "ready-to-merge"], [])

    def test_stale_active_card_without_job_is_suggested_only(self):
        self.card("old", status="active")
        ledger = _load_ledger(self.data, "sample")
        old = next(t for t in ledger["tasks"] if t["task_id"] == "old")
        old["updated_at"] = (datetime.now(UTC) - timedelta(days=5)).isoformat()
        _atomic_write(_ledger_path(self.data, "sample"), ledger)
        _, value = self.reconcile("--apply", "--actor", "claude-opus")
        stale = [s for s in value["suggestions"] if s["kind"] == "stale"]
        self.assertEqual([s["task_id"] for s in stale], ["old"])
        self.assertEqual(self.status("old"), "active")
        _, fresh = self.reconcile("--stale-days", "30")
        self.assertEqual([s for s in fresh["suggestions"] if s["kind"] == "stale"], [])

    def test_brief_output_is_one_line_per_suggestion_with_the_command(self):
        self.card("landed", status="ready-for-review", commit=self.landed, branch="b1")
        self.card("feature", status="ready-for-review", commit=self.unlanded, branch="b2")
        self.card("review-feature-0101", lane_id="review", title="Review feature",
                 objective="Review card feature", status="completed", result="ACCEPT")
        code, text = self.reconcile("--format", "brief")
        self.assertEqual(code, 0)
        lines = [l for l in text.splitlines() if l.startswith(("merged", "ready-to-merge", "stale"))]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all("continuity " in l for l in lines))


class HandoffPlanLimitTests(unittest.TestCase):
    def view(self, plan):
        return {
            "revision": 3,
            "declared": {"plan": plan, "next_action": "go", "pending_decisions": []},
            "observed": {"as_of": "now", "attention": [], "inbox": {"count": 0}, "change_cursor": 1},
        }

    def test_plan_survives_up_to_4096_bytes_when_the_budget_allows(self):
        from continuity_plane.planning_api import _critical_handoff

        plan = "p" * 5000
        kept = _critical_handoff(self.view(plan), 16384)["declared"]["plan"]
        self.assertEqual(len(kept.encode()), 4096)

    def test_budget_logic_still_shrinks_the_plan(self):
        from continuity_plane.planning_api import _critical_handoff

        candidate = _critical_handoff(self.view("p" * 5000), 1500)
        self.assertLessEqual(len(json.dumps(candidate, sort_keys=True, ensure_ascii=False).encode()), 1500)
        self.assertLess(len(candidate["declared"]["plan"]), 1000)

    def test_handoff_show_renders_a_long_plan_in_brief(self):
        from continuity_plane.planning_api import render

        text = render({"read_model": "x", "revision": 1, "declared": {"plan": "word " * 600, "next_action": "n",
                                                    "pending_decisions": []},
                       "observed": {"as_of": "now", "attention": [], "inbox": {},
                                    "changed_cards": []}},
                      format="brief", max_output_bytes=16384)
        plan_line = next(l for l in text.splitlines() if l.startswith("plan: "))
        self.assertGreater(len(plan_line), 2500)




class ReconcileEntrypointTests(unittest.TestCase):
    def test_reconcile_works_with_only_a_project_id(self):
        import contextlib
        import io
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from continuity_plane.cli import entrypoint
        from continuity_plane.collaboration_registry import register_project

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            root = Path(folder) / "repo"
            root.mkdir()
            register_project(data, project_id="sample", control_root=root)
            output = io.StringIO()
            with patch.dict(os.environ, {"CONTINUITY_DATA_ROOT": str(data)}), contextlib.redirect_stdout(output):
                code = entrypoint(["collaboration", "reconcile", "--project-id", "sample", "--format", "brief"])
        self.assertEqual(code, 0, output.getvalue())

if __name__ == "__main__":
    unittest.main()
