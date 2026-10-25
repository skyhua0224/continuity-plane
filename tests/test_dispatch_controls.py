"""Dispatch controls and low-cost controller operations on disposable local data."""

import json
import sys
import subprocess
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
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
        job.pop("expected_updated_at", None)  # Legacy plans predate snapshot fencing.
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

    def test_usage_limit_is_quota_exhaustion_and_is_recorded_for_subscription(self):
        reset_at = (
            (datetime.now(UTC) + timedelta(hours=1))
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        output = (
            "Usage policy violation: usage limit reached; retry at " + reset_at
        )
        self.adapter(
            code=f'print({json.dumps(output)}); raise SystemExit(1)'
        )
        profiles = json.loads((self.data / "executor-adapters.json").read_text())
        self.assertTrue(profiles["sol"]["official_subscription"] is False)
        configure(
            self.data,
            model="sol",
            provider="custom",
            official_subscription=True,
            argv=[
                sys.executable,
                "-c",
                f"print({json.dumps(output)}); raise SystemExit(1)",
                "{effort}",
            ],
            resume_argv=[
                sys.executable,
                "-c",
                "pass",
                "{effort}",
                "{thread_id}",
            ],
        )
        result = self.dispatch(model="sol")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_class"], "quota-exhausted")
        self.assertEqual(result["quota_record"]["used_percent"], 100)
        self.assertEqual(
            result["quota_record"]["resets_at"],
            reset_at.replace("Z", "+00:00"),
        )
        from continuity_plane.collaboration_registry import _atomic_write

        result_file = self.job_path(result).parent / "result.json"
        legacy = json.loads(result_file.read_text())
        legacy.update(
            failure_class="policy-review",
            reason="provider-policy-review",
        )
        _atomic_write(result_file, legacy)
        job_document = json.loads(self.job_path(result).read_text())
        _record_change(
            self.data,
            "sample",
            "dispatch.policy-stopped",
            "task",
            job_id=result["job_id"],
            attempt_id=result["job_id"],
            scope_sha256=job_document["scope_sha256"],
        )
        (self.job_path(result).parent / "thread").write_text(
            "thread-legacy", encoding="utf-8"
        )
        resumed = self.dispatch(resume=True, dry_run=True)
        self.assertEqual(resumed["resume"], True)
        repaired = json.loads(result_file.read_text())
        self.assertEqual(repaired["failure_class"], "quota-exhausted")
        self.assertEqual(
            repaired["classification_correction"]["from"],
            "policy-review",
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

    def test_todo_distinguishes_assignment_from_live_execution(self):
        view = self.execute("todo.from-cards")
        self.assertEqual(view["items"][0]["execution_state"], "assigned")
        from continuity_plane.execution_lifecycle import bind_execution

        bind_execution(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            attempt_id="attempt-live",
        )
        view = self.execute("todo.from-cards")
        self.assertEqual(view["items"][0]["execution_state"], "running")
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="blocked",
            blocked_reason="waiting for provider reset",
        )
        blocked = self.execute("todo.from-cards")["items"][0]
        self.assertEqual(blocked["execution_state"], "blocked")
        self.assertEqual(blocked["blocked_reason"], "waiting for provider reset")

    def test_local_dependencies_gate_claim_and_dispatch_and_filter_lists(self):
        self.assertIn(
            "dependency",
            {row["name"] for row in help_for("task.add")["parameters"]},
        )
        self.assertIn(
            "dependencies_ready",
            {row["name"] for row in help_for("task.list")["parameters"]},
        )
        add_task(
            self.data,
            project_id="sample",
            task_id="dependent",
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="Dependent",
            objective="Wait for prerequisite",
            next_action="Start only after dependency",
            exit_criteria=["Pass"],
            dependencies=["task"],
            body="Dependent task",
        )
        with self.assertRaisesRegex(ValueError, "dependencies"):
            self.execute(
                "task.claim",
                task_id="dependent",
                assignee="worker-dependent",
            )
        self.adapter()
        with self.assertRaisesRegex(ValueError, "dependencies"):
            dispatch(
                self.root,
                data_root=self.data,
                task_id="dependent",
                model="sol",
                assignee="worker-dependent",
            )
        self.assertNotIn(
            "dependent",
            {
                row["task_id"]
                for row in list_tasks(
                    self.data,
                    project_id="sample",
                    dependencies_ready=True,
                )
            },
        )
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="completed",
            result="Prerequisite verified",
        )
        ready = list_tasks(
            self.data,
            project_id="sample",
            dependencies_ready=True,
        )
        self.assertIn("dependent", {row["task_id"] for row in ready})

    def test_task_list_local_and_shared_shape_is_always_paged_object(self):
        value = self.execute("task.list", format="json")
        self.assertIsInstance(value, dict)
        self.assertEqual(
            {"items", "total_items", "next_cursor", "has_more"} - set(value),
            set(),
        )

    def test_non_code_decision_does_not_require_commit(self):
        value = self.execute(
            "decision-add",
            record_id="ui-choice",
            record_json=json.dumps(
                {
                    "actor": "owner",
                    "source": "user-message-2026-10-21",
                    "text": "Use the reviewed UI direction",
                    "approved": True,
                }
            ),
        )
        self.assertEqual(value["value"]["text"], "Use the reviewed UI direction")
        self.assertNotIn("commit", value["value"])

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

    def test_task_update_batch_file_runs_without_shell_quoting(self):
        add_task(
            self.data,
            project_id="sample",
            task_id="second",
            lane_id="scope",
            mode="patch",
            priority="p2",
            title="Second",
            objective="Batch",
            next_action="Start",
            exit_criteria=["Pass"],
            body="Initial second body",
        )
        path = self.root / "updates.json"
        path.write_text(
            json.dumps(
                {
                    "updates": [
                        {
                            "task_id": "task",
                            "body": "Updated first body",
                            "next_action": "Continue first",
                        },
                        {
                            "task_id": "second",
                            "status": "blocked",
                            "blocked_reason": "waiting on evidence",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        result = self.execute("task.update", batch=str(path))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(result["succeeded"]), 2)
        cards = {
            row["task_id"]: row for row in list_tasks(self.data, project_id="sample")
        }
        self.assertEqual(cards["second"]["status"], "blocked")
        self.assertEqual(
            detail_view(self.data, "sample", "task")["body"],
            "Updated first body",
        )

    def test_queue_is_single_instance_ordered_and_duplicate_safe(self):
        from continuity_plane import dispatch_queue as queue

        add_task(
            self.data,
            project_id="sample",
            task_id="second",
            lane_id="scope",
            mode="patch",
            priority="p2",
            title="Second",
            objective="Queue",
            next_action="Start",
            exit_criteria=["Pass"],
            assignee="worker",
            body="Second queued body",
        )
        queue.enqueue(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="sol",
            request_id="request-low",
            priority=20,
            timeout_seconds=10,
            idle_timeout_seconds=5,
        )
        queue.enqueue(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="second",
            model="sol",
            request_id="request-high",
            priority=10,
            timeout_seconds=10,
            idle_timeout_seconds=5,
        )
        calls = []

        def launch(*args, **kwargs):
            calls.append((args, kwargs))
            return {"job_id": "job-" + format(len(calls), "016x")}

        with patch(
            "continuity_plane.dispatch_queue._dispatch", side_effect=launch
        ):
            result = queue.run(
                self.root,
                data_root=self.data,
                project_id="sample",
                controller="controller",
                max_launches=2,
            )
        self.assertEqual(
            [row["task_id"] for row in result["launched"]], ["second", "task"]
        )
        self.assertEqual(len(calls), 2)
        duplicate = queue.enqueue(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="sol",
            request_id="request-other",
            timeout_seconds=10,
            idle_timeout_seconds=5,
        )
        self.assertTrue(duplicate["duplicate_suppressed"])
        lock_path = project_data_dir(self.data, "sample") / "dispatch-queue-runner.lock"
        with queue._runner_lock(lock_path):
            second_runner = queue.run(
                self.root, data_root=self.data, project_id="sample"
            )
        self.assertEqual(second_runner["status"], "already-running")

    def test_queue_honors_host_pause_and_capacity_gate(self):
        from continuity_plane import dispatch_queue as queue

        queue.pause_host(self.data, "maintenance")
        with patch("continuity_plane.dispatch_queue._dispatch") as launch:
            paused = queue.run(
                self.root,
                data_root=self.data,
                project_id="sample",
            )
        self.assertEqual(paused["status"], "paused")
        launch.assert_not_called()
        queue.resume_host(self.data)
        queue.enqueue(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="sol",
            request_id="capacity-request",
            timeout_seconds=10,
            idle_timeout_seconds=5,
        )
        with patch(
            "continuity_plane.dispatch_queue._dispatch",
            side_effect=ValueError("client dispatch capacity is full (1)"),
        ):
            result = queue.run(
                self.root,
                data_root=self.data,
                project_id="sample",
            )
        self.assertEqual(result["waiting"][0]["gate"], "dispatch capacity is full")
        status = queue.status(self.root, data_root=self.data, project_id="sample")
        self.assertEqual(status["items"][0]["status"], "queued")
        self.assertEqual(status["items"][0]["last_gate"], "dispatch capacity is full")
        events = {
            row["event"]
            for row in list_changes(self.data, project_id="sample")["changes"]
        }
        self.assertIn("dispatch.queue-enqueued", events)
        self.assertIn("dispatch.queue-waiting", events)

    def test_queue_requeues_transient_failure_with_retry_backoff(self):
        from continuity_plane import dispatch_queue as queue

        queue.enqueue(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="sol",
            request_id="transient-request",
            timeout_seconds=10,
            idle_timeout_seconds=5,
        )
        with patch(
            "continuity_plane.dispatch_queue._dispatch",
            return_value={"job_id": "job-" + "a" * 16},
        ):
            first = queue.run(self.root, data_root=self.data, project_id="sample")
        self.assertEqual(len(first["launched"]), 1)
        result_file = (
            project_data_dir(self.data, "sample")
            / "worker-jobs"
            / ("job-" + "a" * 16)
            / "result.json"
        )
        result_file.parent.mkdir(parents=True)
        result_file.write_text(
            json.dumps(
                {
                    "status": "failed",
                    "failure_class": "rate-limit",
                    "reason": "429 too many requests",
                }
            ),
            encoding="utf-8",
        )
        with patch("continuity_plane.dispatch_queue._dispatch") as launch:
            second = queue.run(self.root, data_root=self.data, project_id="sample")
        launch.assert_not_called()
        self.assertEqual(second["launched"], [])
        item = queue.status(self.root, data_root=self.data, project_id="sample")[
            "items"
        ][0]
        self.assertEqual(item["status"], "queued")
        self.assertEqual(item["last_gate"], "rate-limit")
        self.assertIsNotNone(item["retry_not_before"])
        events = {
            row["event"]
            for row in list_changes(self.data, project_id="sample")["changes"]
        }
        self.assertIn("dispatch.queue-launched", events)
        self.assertIn("dispatch.queue-retry-scheduled", events)

    def test_mcp_parity_exposes_safe_queue_enqueue_and_status(self):
        queued = self.execute(
            "worker.queue.enqueue",
            task_id="task",
            model="sol",
            request_id="mcp-enqueue",
            timeout_seconds=10,
            idle_timeout_seconds=5,
        )
        self.assertEqual(queued["request_id"], "mcp-enqueue")
        status = self.execute(
            "worker.queue.status",
            format="json",
        )
        self.assertEqual(status["items"][0]["request_id"], "mcp-enqueue")
        specs = {row["name"] for row in help_for("worker.queue.enqueue")["parameters"]}
        self.assertTrue(
            {
                "task_id",
                "model",
                "effort",
                "timeout_seconds",
                "idle_timeout_seconds",
                "request_id",
                "priority",
            }
            <= specs
        )

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

    def test_queued_preserves_assignment_until_explicit_unassign(self):
        self.execute("task.update", task_id="task", status="queued")
        self.execute("task.update", task_id="task", body="Queued instructions")
        self.assertEqual(
            list_tasks(self.data, project_id="sample")[0]["assignee"], "worker"
        )
        self.execute("task.update", task_id="task", status="active")
        self.execute("task.update", task_id="task", status="queued")
        self.execute("task.update", task_id="task", actor="worker", unassign=True)
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
