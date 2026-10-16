"""Dispatch controls and low-cost controller operations on disposable local data."""

import json
import sys
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from unittest.mock import Mock

from tests import test_adoption_workflows
from continuity_plane import supervisor
from continuity_plane.cli import build_parser
from continuity_plane.collaboration_registry import (
    _atomic_write,
    _record_change,
    add_task,
    list_changes,
    list_tasks,
    project_data_dir,
    update_task,
    wait_for_changes,
)
from continuity_plane.delivery_records import detail_view
from continuity_plane.planning_api import execute, help_for
from continuity_plane.worker_dispatch import configure, dispatch, run_job


class DispatchControlTests(unittest.TestCase):
    setUp = test_adoption_workflows.AdoptionWorkflowTests.setUp

    def execute(self, operation, **parameters):
        return execute(
            operation,
            parameters,
            root=str(self.root),
            project_id="sample",
            data_root=str(self.data),
        )

    def adapter(self, model="sol", code=None):
        code = (
            code
            or 'import json; print(json.dumps({"type":"thread.started","thread_id":"owned-thread"})); print("done")'
        )
        argv = [sys.executable, "-c", code, "{effort}"]
        return configure(
            self.data,
            model=model,
            provider="custom",
            argv=argv,
            resume_argv=[*argv, "{thread_id}"],
        )

    def dispatch(self, **kwargs):
        return dispatch(self.root, data_root=self.data, task_id="task", **kwargs)

    def job_path(self, result):
        return (
            project_data_dir(self.data, "sample")
            / "worker-jobs"
            / result["job_id"]
            / "job.json"
        )

    def test_worker_defaults_and_requested_budgets_reach_supervisor(self):
        self.adapter()
        defaults = self.dispatch(model="sol", dry_run=True)
        self.assertEqual(
            (defaults["timeout_seconds"], defaults["idle_timeout_seconds"]),
            (86400, 5400),
        )
        with patch(
            "continuity_plane.supervisor.run_attempt", wraps=supervisor.run_attempt
        ) as attempt:
            result = self.dispatch(
                model="sol", timeout_seconds=3, idle_timeout_seconds=2
            )
        self.assertEqual(result["status"], "completed")
        kwargs = attempt.call_args.kwargs
        self.assertEqual(
            (kwargs["timeout_seconds"], kwargs["idle_timeout_seconds"]), (3, 2)
        )
        recorded = json.loads(self.job_path(result).read_text())
        self.assertEqual(
            (recorded["timeout_seconds"], recorded["idle_timeout_seconds"]), (3, 2)
        )

    def test_background_job_persists_budgets_before_launch(self):
        self.adapter()
        original = subprocess.Popen

        def launch(argv, *args, **kwargs):
            if "continuity_plane.worker_dispatch" in argv:
                return Mock(pid=999999)
            return original(argv, *args, **kwargs)

        with patch(
            "continuity_plane.worker_dispatch.subprocess.Popen", side_effect=launch
        ):
            result = self.dispatch(
                model="sol", background=True, timeout_seconds=9, idle_timeout_seconds=4
            )
        recorded = json.loads(self.job_path(result).read_text())
        self.assertEqual(
            (recorded["timeout_seconds"], recorded["idle_timeout_seconds"]), (9, 4)
        )

    def test_idle_override_actually_interrupts_owned_child(self):
        self.adapter(code="import time; time.sleep(2)")
        result = self.dispatch(
            model="sol", timeout_seconds=1, idle_timeout_seconds=0.05
        )
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["reason"], "no-output-timeout")

    def test_resume_inherits_or_overrides_recorded_limits_and_effort(self):
        self.adapter()
        self.dispatch(
            model="sol", timeout_seconds=3, idle_timeout_seconds=2, effort="medium"
        )
        inherited = self.dispatch(resume=True, dry_run=True)
        self.assertEqual(
            (
                inherited["timeout_seconds"],
                inherited["idle_timeout_seconds"],
                inherited["effort"],
            ),
            (3, 2, "medium"),
        )
        overridden = self.dispatch(
            resume=True,
            dry_run=True,
            timeout_seconds=7,
            idle_timeout_seconds=6,
            effort="xhigh",
        )
        self.assertEqual(
            (
                overridden["timeout_seconds"],
                overridden["idle_timeout_seconds"],
                overridden["effort"],
            ),
            (7, 6, "xhigh"),
        )

    def test_pre_upgrade_jobs_keep_their_legacy_supervisor_budgets(self):
        self.adapter()
        result = self.dispatch(model="sol")
        path = self.job_path(result)
        job = json.loads(path.read_text())
        index = project_data_dir(self.data, "sample") / "worker-jobs.json"
        jobs = json.loads(index.read_text())
        for key in ("timeout_seconds", "idle_timeout_seconds"):
            job.pop(key)
            jobs["task"].pop(key)
        _atomic_write(path, job)
        _atomic_write(index, jobs)
        resumed = self.dispatch(resume=True, dry_run=True)
        self.assertEqual(
            (resumed["timeout_seconds"], resumed["idle_timeout_seconds"]), (3600, 600)
        )
        # Exercise an unstarted legacy record, not replay a completed attempt id.
        job["job_id"] = "job-" + "f" * 16
        _atomic_write(path, job)
        with patch(
            "continuity_plane.supervisor.run_attempt", wraps=supervisor.run_attempt
        ) as attempt:
            run_job(path)
        self.assertEqual(
            (
                attempt.call_args.kwargs["timeout_seconds"],
                attempt.call_args.kwargs["idle_timeout_seconds"],
            ),
            (3600, 600),
        )

    def test_explicit_transient_fallback_retains_budgets(self):
        self.adapter(code='print("502 Bad Gateway"); raise SystemExit(1)')
        self.adapter("sonnet")
        with patch(
            "continuity_plane.supervisor.run_attempt", wraps=supervisor.run_attempt
        ) as attempt:
            self.dispatch(
                model="sol",
                fallback_models=["sonnet"],
                timeout_seconds=4,
                idle_timeout_seconds=2,
            )
        self.assertEqual(attempt.call_count, 2)
        for call in attempt.call_args_list:
            self.assertEqual(
                (call.kwargs["timeout_seconds"], call.kwargs["idle_timeout_seconds"]),
                (4, 2),
            )

    def test_bad_limits_and_effort_never_spawn(self):
        self.adapter()
        for value in (0, -1, float("inf"), float("nan"), True, "5"):
            for key in ("timeout_seconds", "idle_timeout_seconds"):
                with (
                    self.subTest(key=key, value=value),
                    patch("subprocess.Popen") as spawn,
                ):
                    with self.assertRaisesRegex(ValueError, "finite positive"):
                        self.dispatch(model="sol", **{key: value})
                    spawn.assert_not_called()
        for effort in ("", "--bad", "high\nmax", "a" * 33, 1):
            with self.subTest(effort=effort), patch("subprocess.Popen") as spawn:
                with self.assertRaisesRegex(ValueError, "effort"):
                    self.dispatch(model="sol", effort=effort)
                spawn.assert_not_called()

    def test_effort_passthrough_including_explicit_glm_override(self):
        self.adapter("glm")
        self.assertEqual(self.dispatch(model="glm", dry_run=True)["effort"], "max")
        for effort in ("medium", "xhigh", "high", "max", "provider-specific"):
            result = self.dispatch(model="glm", effort=effort, dry_run=True)
            self.assertEqual(result["effort"], effort)
            self.assertEqual(result["command"][-1], effort)

    def test_cli_and_mcp_prepare_share_timeout_and_effort_flags(self):
        self.adapter()
        for verb in (
            ("dispatch",),
            ("worker", "dispatch"),
            ("worker", "resume"),
            ("worker", "prepare"),
        ):
            args = build_parser().parse_args(
                [
                    *verb,
                    "--task-id",
                    "task",
                    "--effort",
                    "xhigh",
                    "--timeout-seconds",
                    "8",
                    "--idle-timeout-seconds",
                    "4",
                ]
            )
            self.assertEqual(
                (args.effort, args.timeout_seconds, args.idle_timeout_seconds),
                ("xhigh", 8, 4),
            )
        plan = self.execute(
            "worker.prepare",
            task_id="task",
            model="sol",
            effort="medium",
            timeout_seconds=8,
            idle_timeout_seconds=4,
        )
        self.assertEqual(
            (plan["effort"], plan["timeout_seconds"], plan["idle_timeout_seconds"]),
            ("medium", 8, 4),
        )
        specs = {x["name"] for x in help_for("worker.prepare")["parameters"]}
        self.assertTrue({"effort", "timeout_seconds", "idle_timeout_seconds"} <= specs)

    def test_short_resume_alias_forwards_budgets_effort_and_controller(self):
        from continuity_plane.cli import _resume
        import contextlib
        import io

        args = build_parser().parse_args(
            [
                "resume",
                "task",
                "Continue",
                "--effort",
                "xhigh",
                "--timeout-seconds",
                "9",
                "--idle-timeout-seconds",
                "4",
                "--controller",
                "owner",
                "--controller-token",
                "fence",
            ]
        )
        with (
            patch(
                "continuity_plane.worker_dispatch.dispatch",
                return_value={"status": "completed"},
            ) as resumed,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(_resume(args), 0)
        kwargs = resumed.call_args.kwargs
        self.assertEqual(
            (
                kwargs["effort"],
                kwargs["timeout_seconds"],
                kwargs["idle_timeout_seconds"],
            ),
            ("xhigh", 9, 4),
        )
        self.assertEqual(
            (kwargs["controller"], kwargs["controller_token"]), ("owner", "fence")
        )

    def test_todo_defaults_to_open_states_and_supports_all_or_selected_states(self):
        for status in (
            "queued",
            "dispatched",
            "blocked",
            "ready-for-review",
            "completed",
            "merged",
            "rejected",
            "superseded",
        ):
            add_task(
                self.data,
                project_id="sample",
                task_id=status,
                lane_id=status,
                mode="patch",
                priority="p2",
                title=status,
                objective="test",
                next_action="test",
                exit_criteria=["pass"],
                assignee="worker",
            )
            update_task(self.data, project_id="sample", task_id=status, status=status)
        view = self.execute("todo.from-cards")
        self.assertEqual(
            {x["task_status"] for x in view["items"]},
            {"queued", "active", "dispatched", "blocked", "ready-for-review"},
        )
        subset = self.execute("todo.from-cards", status=["merged", "superseded"])
        self.assertEqual(
            {x["task_status"] for x in subset["items"]}, {"merged", "superseded"}
        )
        self.assertEqual(len(self.execute("todo.from-cards", all=True)["items"]), 9)
        self.assertEqual(len(list_tasks(self.data, project_id="sample")), 9)

    def test_live_open_view_drops_closed_cards_without_deleting_evidence(self):
        from continuity_plane.intent_todo import load_queue

        self.execute("todo.from-cards")
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="completed",
            result="verified proof",
        )
        self.assertEqual(load_queue(self.root, self.data)["items"], [])
        self.assertEqual(
            self.execute("todo.from-cards", all=True)["items"][0]["result"],
            "verified proof",
        )

    def test_body_only_update_preserves_status_and_blocker(self):
        self.execute(
            "task.update",
            task_id="task",
            status="blocked",
            blocked_reason="waiting on owner",
        )
        self.execute("task.update", task_id="task", body="Updated instructions")
        task = list_tasks(self.data, project_id="sample")[0]
        self.assertEqual(
            (task["status"], task["assignee"], task["blocked_reason"]),
            ("blocked", "worker", "waiting on owner"),
        )
        self.assertEqual(
            detail_view(self.data, "sample", "task")["body"], "Updated instructions"
        )
        spec = next(
            x for x in help_for("task.update")["parameters"] if x["name"] == "status"
        )
        self.assertFalse(spec["required"])

    def test_concurrent_body_write_does_not_restore_a_stale_status(self):
        gate = threading.Barrier(2)

        def change(kwargs):
            gate.wait(timeout=5)
            update_task(self.data, project_id="sample", task_id="task", **kwargs)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(change, kwargs)
                for kwargs in (
                    {"body": "New body"},
                    {"status": "blocked", "blocked_reason": "external"},
                )
            ]
            for future in futures:
                future.result(timeout=5)
        self.assertEqual(
            list_tasks(self.data, project_id="sample")[0]["status"], "blocked"
        )
        self.assertEqual(detail_view(self.data, "sample", "task")["body"], "New body")

    def test_explicit_active_still_requires_assignee(self):
        self.execute("task.update", task_id="task", status="queued")
        self.execute("task.update", task_id="task", body="Queued instructions")
        with self.assertRaisesRegex(ValueError, "assignee"):
            self.execute("task.update", task_id="task", status="active")
        self.assertEqual(
            list_tasks(self.data, project_id="sample")[0]["status"], "queued"
        )

    def test_wait_accepts_or_events_and_old_single_event_mcp_input(self):
        since = list_changes(self.data, project_id="sample")["next_cursor"]
        for event in (
            "unrelated",
            "attempt.finished",
            "attempt.timeout",
            "dispatch.policy-stopped",
        ):
            _record_change(self.data, "sample", event, "task")
        result = self.execute(
            "wait",
            since=since,
            event=["attempt.timeout", "dispatch.policy-stopped"],
            timeout_seconds=0,
        )
        self.assertEqual(
            {x["event"] for x in result["changes"]},
            {"attempt.timeout", "dispatch.policy-stopped"},
        )
        old = self.execute(
            "wait", since=since, event="attempt.finished", timeout_seconds=0
        )
        self.assertEqual(len(old["changes"]), 1)
        args = build_parser().parse_args(
            [
                "collaboration",
                "wait",
                "--event",
                "attempt.finished",
                "--event",
                "attempt.timeout",
            ]
        )
        self.assertEqual(args.event, ["attempt.finished", "attempt.timeout"])

    def test_multi_event_wait_crosses_nonmatching_pages(self):
        for _ in range(501):
            _record_change(self.data, "sample", "other", "task")
        _record_change(self.data, "sample", "attempt.timeout", "task")
        result = wait_for_changes(
            self.data,
            project_id="sample",
            event=["attempt.finished", "attempt.timeout"],
            timeout_seconds=1,
        )
        self.assertEqual([x["event"] for x in result["changes"]], ["attempt.timeout"])
        self.assertFalse(result["timed_out"])

    def batch(self, **extra):
        return {
            "task_ids": ["task"],
            "baseline": "a" * 40,
            "result_commit": "b" * 40,
            "verification": {
                "commit": "b" * 40,
                "actor": "worker",
                "checks": [
                    {
                        "name": "unit",
                        "command": "test",
                        "result": "PASS",
                        "status": "pass",
                        "required": True,
                    }
                ],
            },
            **extra,
        }

    def test_batch_metrics_roundtrip_and_status_projection(self):
        from continuity_plane.batch_projection import project

        metrics = {"runtime_links_module": {"before": 13, "after": 6, "unit": "links"}}
        row = self.execute(
            "batch-add",
            record_id="batch",
            record_json=json.dumps(self.batch()),
            metrics_json=json.dumps(metrics),
        )
        self.assertEqual(row["value"]["metrics"], metrics)
        self.assertEqual(
            self.execute("batch-list")["items"][0]["value"]["metrics"], metrics
        )
        self.assertIn("runtime_links_module", project(self.data, "sample")["markdown"])
        self.assertEqual(row["value"]["baseline"], "a" * 40)
        self.assertEqual(
            self.execute(
                "batch-add", record_id="legacy", record_json=json.dumps(self.batch())
            )["value"],
            self.batch(),
        )

    def test_invalid_batch_metrics_and_conflicting_sources_are_rejected(self):
        from continuity_plane.collaboration_workflow import record

        for metrics in (
            [],
            "13→6",
            {"": 6},
            {"bad": float("nan")},
            {"bad": {"value": float("inf")}},
        ):
            with (
                self.subTest(metrics=metrics),
                self.assertRaisesRegex(ValueError, "metrics"),
            ):
                record(
                    self.data,
                    project_id="sample",
                    kind="batch",
                    record_id="bad",
                    value=self.batch(metrics=metrics),
                )
        with self.assertRaisesRegex(ValueError, "conflicting metrics"):
            self.execute(
                "batch-add",
                record_id="bad",
                record_json=json.dumps(self.batch(metrics={"value": 1})),
                metrics_json='{"value":2}',
            )
        self.assertEqual(self.execute("batch-list")["items"], [])
