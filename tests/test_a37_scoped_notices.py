"""Alpha37: notices reach the right session once; reports state facts from git."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from continuity_plane.collaboration_registry import (
    _record_change,
    add_task,
    list_tasks,
    register_project,
)
from continuity_plane.controller_awareness import notice
from continuity_plane.controller_lease import acquire


def get_task(data, *, project_id, task_id):
    return next(
        t for t in list_tasks(data, project_id=project_id) if t["task_id"] == task_id
    )


def _clean_env():
    return patch.dict(
        os.environ,
        {
            k: v
            for k, v in os.environ.items()
            if not k.startswith("CONTINUITY_")
        },
        clear=True,
    )


class ScopedNoticeBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.data = base / "data"
        self.roots = {}
        for name in ("main", "other"):
            self.roots[name] = base / name
            self.roots[name].mkdir()
            register_project(self.data, project_id=name, control_root=self.roots[name])
        env = _clean_env()
        env.start()
        self.addCleanup(env.stop)

    def notice(self, session="ctl", event="PostToolUse"):
        return notice(
            {
                "cwd": str(self.roots["main"]),
                "session_id": session,
                "hook_event_name": event,
            },
            data_root=self.data,
            max_bytes=1024,
        )

    def fail_other(self, task="remote", job="job-1", event="job.failed", **extra):
        _record_change(
            self.data,
            "other",
            event,
            task,
            attempt_id=job,
            job_id=job,
            status=extra.pop("status", "failed"),
            **extra,
        )


class ScopeTests(ScopedNoticeBase):
    def test_shared_controller_identity_does_not_pull_in_other_projects(self):
        # Several controllers share one name; holding the other project's lease
        # under that name must not make this session its recipient.
        acquire(self.data, "other", owner="claude-opus")
        with patch.dict(
            os.environ,
            {"CONTINUITY_ASSIGNEE": "claude-opus", "CONTINUITY_CONTROLLER": "claude-opus"},
        ):
            self.assertIsNone(self.notice(event="SessionStart"))
            self.assertIsNone(self.notice())
            self.fail_other()
            self.assertIsNone(self.notice())

    def test_watch_projects_still_opts_in(self):
        with patch.dict(os.environ, {"CONTINUITY_WATCH_PROJECTS": "other"}):
            self.assertIsNone(self.notice(event="SessionStart"))
            self.assertIsNone(self.notice())
            self.fail_other()
            self.assertIn("[other] worker remote failed", self.notice())

    def test_session_bound_to_the_other_project_opts_in_only_that_session(self):
        from continuity_plane.project_context import bind

        add_task(
            self.data,
            project_id="other",
            task_id="lead",
            lane_id="main",
            mode="patch",
            priority="p1",
            title="Lead",
            objective="Lead",
            next_action="Go",
            exit_criteria=["Done"],
            assignee="claude-opus",
            body="Lead the work",
        )
        bind(
            self.roots["other"],
            self.data,
            "other",
            provider="claude",
            session_id="ctl",
            task_id="lead",
        )
        self.assertIsNone(self.notice("ctl", "SessionStart"))
        self.assertIsNone(self.notice("ctl"))
        self.assertIsNone(self.notice("bystander", "SessionStart"))
        self.assertIsNone(self.notice("bystander"))
        self.fail_other()
        self.assertIn("[other] worker remote failed", self.notice("ctl"))
        self.assertIsNone(self.notice("bystander"))

    def test_current_project_notices_are_unaffected(self):
        self.assertIsNone(self.notice(event="SessionStart"))
        _record_change(
            self.data, "main", "job.completed", "mine", job_id="j", attempt_id="j",
            status="completed",
        )
        self.assertIn("[main] worker mine completed", self.notice())


class DeduplicationTests(ScopedNoticeBase):
    def record(self, project, event, task, **fields):
        _record_change(self.data, project, event, task, **fields)

    def test_attempt_and_job_events_for_one_outcome_give_one_line(self):
        self.assertIsNone(self.notice(event="SessionStart"))
        # An attempt.finished without a job_id, then the job event that names it.
        self.record("main", "attempt.finished", "t", attempt_id="a1", status="completed")
        self.record("main", "job.completed", "t", attempt_id="a1", job_id="j1", status="completed")
        text = self.notice()
        self.assertEqual(text.count("worker t completed"), 1, text)

    def test_the_same_job_reported_twice_is_one_line(self):
        self.assertIsNone(self.notice(event="SessionStart"))
        for _ in range(2):
            self.record("main", "job.completed", "t", attempt_id="a1", job_id="j1", status="completed")
        self.assertEqual(self.notice().count("worker t completed"), 1)

    def test_a_repeat_after_the_first_notice_stays_quiet(self):
        self.assertIsNone(self.notice(event="SessionStart"))
        self.record("main", "job.completed", "t", attempt_id="a1", job_id="j1", status="completed")
        self.assertIn("worker t completed", self.notice())
        self.record("main", "job.completed", "t", attempt_id="a1", job_id="j1", status="completed")
        self.assertIsNone(self.notice())

    def test_lost_then_completed_reports_the_final_outcome_once(self):
        self.assertIsNone(self.notice(event="SessionStart"))
        self.record("main", "job.lost", "t", attempt_id="a1", job_id="j1", status="interrupted", reason="process-lost")
        self.record("main", "job.completed", "t", attempt_id="a1", job_id="j1", status="completed")
        text = self.notice()
        self.assertEqual(text.count("worker t "), 1, text)
        self.assertIn("worker t completed", text)
        self.assertNotIn("lost", text)

    def test_completed_after_an_earlier_lost_notice_is_still_reported(self):
        self.assertIsNone(self.notice(event="SessionStart"))
        self.record("main", "job.lost", "t", attempt_id="a1", job_id="j1", status="interrupted", reason="process-lost")
        self.assertIn("worker t lost", self.notice())
        self.record("main", "job.completed", "t", attempt_id="a1", job_id="j1", status="completed")
        self.assertIn("worker t completed", self.notice())

    def test_a_late_lost_does_not_overturn_a_completed_job(self):
        self.assertIsNone(self.notice(event="SessionStart"))
        self.record("main", "job.completed", "t", attempt_id="a1", job_id="j1", status="completed")
        self.assertIn("worker t completed", self.notice())
        self.record("main", "job.lost", "t", attempt_id="a1", job_id="j1", status="interrupted", reason="process-lost")
        self.assertIsNone(self.notice())

    def test_distinct_jobs_of_one_card_stay_separate(self):
        self.assertIsNone(self.notice(event="SessionStart"))
        self.record("main", "job.failed", "t", attempt_id="a1", job_id="j1", status="failed")
        self.record("main", "job.completed", "t", attempt_id="a2", job_id="j2", status="completed")
        text = self.notice()
        self.assertIn("worker t failed", text)
        self.assertIn("worker t completed", text)

    def test_other_projects_deduplicate_the_same_way(self):
        with patch.dict(os.environ, {"CONTINUITY_WATCH_PROJECTS": "other"}):
            self.assertIsNone(self.notice(event="SessionStart"))
            self.assertIsNone(self.notice())
            self.record("other", "job.failed", "r", attempt_id="a1", job_id="j1", status="failed")
            self.record("other", "job.failed", "r", attempt_id="a1", job_id="j1", status="failed")
            self.record("other", "job.lost", "q", attempt_id="b1", job_id="k1", status="interrupted", reason="process-lost")
            self.record("other", "job.completed", "q", attempt_id="b1", job_id="k1", status="completed")
            text = self.notice()
        self.assertEqual(text.count("worker r failed"), 1, text)
        self.assertEqual(text.count("worker q "), 1, text)
        self.assertIn("worker q completed", text)


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    ).stdout.strip()


class ReportFactsBase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        self.data = base / "data"
        self.repo = base / "repo"
        self.repo.mkdir()
        _git(self.repo, "init", "-q", "-b", "main")
        for name in ("a", "b"):
            (self.repo / name).write_text(name)
            _git(self.repo, "add", name)
            _git(self.repo, "commit", "-qm", name)
        self.first = _git(self.repo, "rev-parse", "HEAD~1")
        self.base_sha = _git(self.repo, "rev-parse", "HEAD")
        _git(self.repo, "checkout", "-q", "-b", "feat/x")
        (self.repo / "c").write_text("c")
        _git(self.repo, "add", "c")
        _git(self.repo, "commit", "-qm", "c")
        self.head = _git(self.repo, "rev-parse", "HEAD")
        register_project(self.data, project_id="demo", control_root=self.repo)
        add_task(
            self.data,
            project_id="demo",
            task_id="job",
            lane_id="main",
            mode="patch",
            priority="p1",
            title="Job",
            objective="Job",
            next_action="Go",
            exit_criteria=["Done"],
            assignee="worker-a",
            body="Do it",
        )
        env = _clean_env()
        env.start()
        self.addCleanup(env.stop)
        os.environ["CONTINUITY_DATA_ROOT"] = str(self.data)
        os.environ["CONTINUITY_WORK_ROOT"] = str(base / "work")
        os.environ["CONTINUITY_ASSIGNEE"] = "worker-a"
        cwd = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, cwd)


class WorktreeFactsTests(ReportFactsBase):
    def test_facts_come_from_the_worktree_and_integration_ref(self):
        from continuity_plane.report_facts import worktree_facts

        facts = worktree_facts(self.repo, integration_ref="refs/heads/main")
        self.assertEqual(facts["branch"], "feat/x")
        self.assertEqual(facts["head"], self.head)
        self.assertEqual(facts["merge_base"], self.base_sha)
        self.assertFalse(facts["dirty"])

    def test_unavailable_checkout_gives_no_facts(self):
        from continuity_plane.report_facts import worktree_facts

        with tempfile.TemporaryDirectory() as plain:
            self.assertIsNone(worktree_facts(plain, integration_ref="refs/heads/main"))

    def test_mismatches_name_each_wrong_claim(self):
        from continuity_plane.report_facts import mismatches, worktree_facts

        facts = worktree_facts(self.repo, integration_ref="refs/heads/main")
        self.assertEqual(
            mismatches(facts, branch="feat/x", commit=self.head[:9], base=self.base_sha[:7]),
            [],
        )
        wrong = mismatches(facts, branch="feat/y", commit=self.first, base=self.first)
        self.assertEqual(len(wrong), 3)
        self.assertTrue(any("base" in w and self.first in w for w in wrong))

    def test_prose_base_claim_is_checked(self):
        from continuity_plane.report_facts import claimed_bases

        text = f"Work started from base {self.first[:9]}.\nBase commit: `{self.base_sha}`\nbased on nothing"
        self.assertEqual(claimed_bases(text), [self.first[:9], self.base_sha])


class ReportFinalFactsTests(ReportFactsBase):
    def final(self, **arguments):
        from continuity_plane.agent_tools import _report_final

        return _report_final(
            {"task_id": "job", "summary": "Done.", **arguments}, "demo", str(self.repo)
        )

    def test_branch_and_commit_are_filled_from_the_worktree(self):
        result = self.final()
        card = get_task(self.data, project_id="demo", task_id="job")
        self.assertEqual((card["branch"], card["commit"]), ("feat/x", self.head))
        text = Path(result["report_file"]).read_text()
        self.assertIn(self.head, text)
        self.assertIn(self.base_sha, text)
        self.assertNotIn("MISMATCH", text)
        self.assertNotIn("mismatch", result)

    def test_a_wrong_claimed_base_and_commit_are_flagged(self):
        result = self.final(branch="feat/x", commit=self.first, base=self.first)
        text = Path(result["report_file"]).read_text()
        self.assertEqual(text.count("MISMATCH"), 2, text)
        self.assertEqual(len(result["mismatch"]), 2)
        card = get_task(self.data, project_id="demo", task_id="job")
        # What the worker supplied is kept, the discrepancy is on the record.
        self.assertEqual(card["commit"], self.first)

    def test_a_base_claimed_in_prose_is_flagged(self):
        result = self.final(summary=f"Started from base {self.first}.")
        self.assertEqual(len(result["mismatch"]), 1)
        self.assertIn("base", result["mismatch"][0])

    def test_without_a_git_worktree_nothing_is_invented(self):
        with tempfile.TemporaryDirectory() as plain:
            os.chdir(plain)
            result = self.final(commit="abc1234")
        card = get_task(self.data, project_id="demo", task_id="job")
        self.assertEqual(card["commit"], "abc1234")
        self.assertIn("no git worktree", Path(result["report_file"]).read_text())


class SupervisorAttachmentTests(ReportFactsBase):
    def test_attached_report_carries_git_facts_and_flags_a_wrong_base(self):
        from continuity_plane.report_facts import annotate_report

        report = {"sha256": "x", "bytes": 3, "text": f"All done from base {self.first}.\n"}
        out = annotate_report(report, self.repo, data_root=self.data, project_id="demo")
        self.assertIn(self.head, out["text"])
        self.assertIn("MISMATCH", out["text"])
        self.assertEqual(out["bytes"], len(out["text"].encode()))
        self.assertEqual(out["source_sha256"], "x")

    def test_missing_checkout_leaves_the_report_unchanged(self):
        from continuity_plane.report_facts import annotate_report

        report = {"sha256": "x", "bytes": 3, "text": "ok\n"}
        self.assertEqual(
            annotate_report(report, "/nonexistent/path", data_root=self.data, project_id="demo"),
            report,
        )

    def test_run_attempt_annotates_the_report_it_attaches(self):
        import sys

        from continuity_plane.supervisor import run_attempt

        report = self.repo.parent / "report.md"
        script = (
            f"from pathlib import Path; Path({str(report)!r}).write_text("
            f"'Done, base {self.first}.\\n')"
        )
        result = run_attempt(
            self.data,
            project_id="demo",
            task_id="job",
            assignee="worker-a",
            command=[sys.executable, "-c", script],
            report_file=str(report),
            workdir=str(self.repo),
        )
        self.assertEqual(result["status"], "completed")
        from continuity_plane.delivery_records import detail_view

        stored = detail_view(self.data, "demo", "job", include_report=True)
        self.assertIn("MISMATCH", stored["report"]["text"])



class BridgeWatchListTests(unittest.TestCase):
    def forward(self, mapping_extra=None, env=None):
        import json as _json
        import subprocess
        from unittest.mock import patch

        from continuity_plane import remote_hook

        sent = {}

        def runner(argv, input=None, **kwargs):
            sent.update(_json.loads(input))
            return subprocess.CompletedProcess(argv, 0, '{"continue": true}', "")

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "repo"
            root.mkdir()
            config = {
                "host": "state-host",
                "local_data_root": str(Path(folder) / "local"),
                "mappings": [
                    {"local_root": str(root), "remote_root": "/srv/repo", **(mapping_extra or {})}
                ],
            }
            payload = {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "s",
                "cwd": str(root),
            }
            clean = {k: v for k, v in os.environ.items() if k != "CONTINUITY_WATCH_PROJECTS"}
            with patch.dict(os.environ, {**clean, **(env or {})}, clear=True):
                remote_hook.forward(payload, config, runner=runner)
        return sent

    def test_a_mapping_can_watch_other_projects(self):
        sent = self.forward({"watch_projects": ["foundation-account"]})
        self.assertEqual(sent["watch_projects"], ["foundation-account"])

    def test_the_environment_overrides_the_mapping(self):
        sent = self.forward(
            {"watch_projects": ["a"]}, {"CONTINUITY_WATCH_PROJECTS": "b, c"}
        )
        self.assertEqual(sent["watch_projects"], ["b", "c"])

    def test_nothing_is_sent_without_a_list(self):
        self.assertNotIn("watch_projects", self.forward())


class EventsWaitDedupTests(unittest.TestCase):
    def test_one_job_outcome_is_one_event(self):
        from continuity_plane import agent_tools
        from continuity_plane.collaboration_registry import _record_change, register_project

        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder) / "data"
            root = Path(folder) / "repo"
            root.mkdir()
            with patch.dict(os.environ, {"CONTINUITY_DATA_ROOT": str(data)}):
                register_project(data, project_id="sample", control_root=root)
                _record_change(data, "sample", "task.updated", "seed", status="queued")
                agent_tools._events_wait({"timeout_seconds": 0, "member": "m"}, None, None)
                _record_change(
                    data, "sample", "attempt.finished", "t1",
                    attempt_id="a1", job_id="job-1", status="completed",
                )
                _record_change(
                    data, "sample", "job.completed", "t1", attempt_id="a1", job_id="job-1",
                )
                result = agent_tools._events_wait(
                    {"timeout_seconds": 0, "member": "m"}, None, None
                )
        self.assertEqual([e["event"] for e in result["events"]], ["job.completed"])
        self.assertEqual(len(result["next"]), 1)

if __name__ == "__main__":
    unittest.main()
