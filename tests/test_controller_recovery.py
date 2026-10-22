"""Controller recovery and delivery contracts on isolated, synthetic local data."""

from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from continuity_plane.cli import main
from continuity_plane.collaboration_registry import (
    _atomic_write,
    _load_ledger,
    _record_change,
    add_task,
    change_cursor_at_or_before,
    list_changes,
    latest_change_cursor,
    project_data_dir,
    register_project,
    update_task,
)
from continuity_plane.controller_observation import (
    handoff_view,
    observe,
    report_facts,
)
from continuity_plane.execution_lifecycle import bind_execution
from continuity_plane.lifecycle import handle_event
from continuity_plane.native_subagents import (
    observe as native_observe,
    session_event,
)
from continuity_plane.planning_api import execute, handle_tool
from continuity_plane.project_context import _load, _path, handoff
from continuity_plane.retry_policy import classify
from continuity_plane.runtime_origin import current
from continuity_plane.supervisor import run_attempt
from continuity_plane.worker_dispatch import configure, dispatch, run_job


class ControllerRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "repository"
        self.root.mkdir()
        self.data = Path(temporary.name) / "data"
        register_project(self.data, project_id="sample", control_root=self.root)
        self.card()

    def card(self, task_id="task", **extra):
        return add_task(
            self.data,
            project_id="sample",
            task_id=task_id,
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="Fixture",
            objective="Check recovery",
            next_action="Run tests",
            exit_criteria=["PASS"],
            worktree=self.root,
            body="Current task instructions",
            **extra,
        )

    def execute(self, action, **parameters):
        return execute(
            action,
            parameters,
            root=str(self.root),
            project_id="sample",
            data_root=str(self.data),
        )

    def cli(self, args):
        text = io.StringIO()
        with redirect_stdout(text):
            result = main(args)
        self.assertEqual(result, 0)
        return json.loads(text.getvalue())

    def test_new_session_reads_project_handoff_without_false_negative(self):
        handoff(
            self.root,
            self.data,
            "sample",
            plan="Finish the current integration",
            next_action="Review existing report",
        )
        result = handle_event(
            {
                "cwd": str(self.root),
                "hook_event_name": "SessionStart",
                "session_id": "new-controller",
            },
            host="claude",
            data_root=self.data,
        )
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("A project controller handoff exists", text)
        self.assertIn("Finish the current integration", text)
        self.assertNotIn("do not revive an older saved Work", text)
        self.assertIn("controller_scene", text)
        self.assertIn("inbox", text)

    def test_missing_session_and_missing_project_handoff_are_distinct(self):
        result = handle_event(
            {
                "cwd": str(self.root),
                "session_id": "new",
                "hook_event_name": "SessionStart",
            },
            host="claude",
            data_root=self.data,
        )
        self.assertIn(
            "Project controller handoff is not recorded",
            result["hookSpecificOutput"]["additionalContext"],
        )

    def test_current_observation_clock_does_not_cause_duplicate_recovery_injection(
        self,
    ):
        handoff(
            self.root, self.data, "sample", plan="Keep order", next_action="Read report"
        )
        payload = {
            "cwd": str(self.root),
            "session_id": "same",
            "hook_event_name": "SessionStart",
        }
        with patch(
            "continuity_plane.controller_observation._now",
            return_value="2026-10-17T00:00:00Z",
        ):
            first = handle_event(payload, host="claude", data_root=self.data)
        with patch(
            "continuity_plane.controller_observation._now",
            return_value="2026-10-17T00:01:00Z",
        ):
            second = handle_event(payload, host="claude", data_root=self.data)
        self.assertGreater(first["injected_bytes"], 0)
        self.assertEqual(second["status"], "already-delivered")

    def test_project_handoff_lookup_failure_is_not_silent_absence(self):
        with patch(
            "continuity_plane.project_context.handoff_summary",
            side_effect=OSError("fixture unavailable"),
        ):
            result = handle_event(
                {
                    "cwd": str(self.root),
                    "session_id": "new",
                    "hook_event_name": "SessionStart",
                },
                host="claude",
                data_root=self.data,
            )
        self.assertIn(
            "lookup failed", result["hookSpecificOutput"]["additionalContext"]
        )

    def test_plan_survives_failure_of_the_optional_live_scene(self):
        handoff(
            self.root,
            self.data,
            "sample",
            plan="Known controller plan",
            next_action="Review",
        )
        with patch(
            "continuity_plane.controller_awareness.scene",
            side_effect=OSError("fixture scene"),
        ):
            result = handle_event(
                {
                    "cwd": str(self.root),
                    "session_id": "new",
                    "hook_event_name": "SessionStart",
                },
                host="claude",
                data_root=self.data,
            )
        text = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("A project controller handoff exists", text)
        self.assertIn("Known controller plan", text)
        self.assertIn("unavailable", text)

    def test_terminal_event_overrides_stale_running_binding_without_rewriting_plan(
        self,
    ):
        bind_execution(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            attempt_id="current",
        )
        handoff(
            self.root, self.data, "sample", plan="Declared order", next_action="Review"
        )
        saved = _path(self.data, "sample").read_bytes()
        _record_change(
            self.data,
            "sample",
            "attempt.finished",
            "task",
            attempt_id="current",
            status="completed",
        )
        value = handoff_view(self.data, "sample")
        self.assertEqual(value["declared"]["plan"], "Declared order")
        self.assertEqual(value["observed"]["running_workers_total"], 0)
        self.assertEqual(value["scene"]["workers_total"], 0)
        self.assertEqual(value["scene"]["workers"], [])
        self.assertEqual(value["observed"]["workers"][0]["status"], "completed")
        self.assertGreater(value["observed"]["changes_since_handoff_total"], 0)
        self.assertEqual(_path(self.data, "sample").read_bytes(), saved)

    def test_old_attempt_terminal_event_cannot_end_new_attempt(self):
        bind_execution(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            attempt_id="new",
        )
        _record_change(
            self.data,
            "sample",
            "attempt.finished",
            "task",
            attempt_id="old",
            status="completed",
        )
        self.assertEqual(observe(self.data, "sample")["running_workers_total"], 1)

    def test_auto_draft_uses_events_and_preserves_declared_plan(self):
        handoff(
            self.root, self.data, "sample", plan="A before B", next_action="Inspect A"
        )
        update_task(
            self.data, project_id="sample", task_id="task", next_action="New outcome"
        )
        value = self.execute("handoff.write", auto=True)
        self.assertEqual(value["plan"], "A before B")
        self.assertEqual(value["next_action"], "Inspect A")
        self.assertEqual(value["draft"]["authority"], "candidate-not-approved-plan")
        self.assertIn(
            "task.updated", [r["event"] for r in value["draft"]["candidates"]]
        )
        self.assertEqual(
            value["draft"]["changed_cards"][0]["next_action"], "New outcome"
        )

    def test_new_report_is_visible_to_handoff_and_drift_without_reading_contents(self):
        file = self.data / "work/task/report.md"
        file.parent.mkdir(parents=True)
        file.write_text("Synthetic report; its verdict must not be inferred.")
        card = _load_ledger(self.data, "sample")["tasks"][0]
        when = datetime.fromisoformat(card["updated_at"]).timestamp() + 2
        os.utime(file, (when, when))
        facts = report_facts(self.data, "sample")
        self.assertEqual(facts[0]["kind"], "report-without-card-update")
        self.assertNotIn("text", facts[0])
        drift = self.execute("drift")
        self.assertIn(
            "report-without-card-update", [r["kind"] for r in drift["findings"]]
        )
        self.assertEqual(
            handoff_view(self.data, "sample")["observed"]["reports_total"], 1
        )
        self.assertEqual(
            _load_ledger(self.data, "sample")["tasks"][0]["status"], "queued"
        )

    def test_report_symlink_outside_artifact_root_is_not_inspected(self):
        outside = self.root / "outside-report.md"
        outside.write_text("Not an admitted artifact")
        target = self.data / "work/task/report.md"
        target.parent.mkdir(parents=True)
        target.symlink_to(outside)
        self.assertEqual(report_facts(self.data, "sample"), [])

    def test_legacy_report_attribution_is_explicitly_only_a_candidate(self):
        legacy = self.data / "work/task/report.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("Synthetic legacy report")
        scoped = self.data / "work/sample/task/report.md"
        scoped.parent.mkdir(parents=True)
        scoped.write_text("Synthetic project-qualified report")
        rows = report_facts(self.data, "sample")
        by_kind = {r["attribution"]: r for r in rows}
        self.assertTrue(
            by_kind["unattributed-legacy-candidate"]["verify_project_before_reading"]
        )
        self.assertFalse(
            by_kind["project-scoped-work"]["verify_project_before_reading"]
        )

    def test_inbox_summary_excludes_acknowledged_and_other_recipients(self):
        from continuity_plane.collaboration_inbox import send_message, acknowledge

        send_message(
            self.data,
            project_id="sample",
            message_id="first",
            sender="worker",
            recipient="controller",
            body="Review",
        )
        send_message(
            self.data,
            project_id="sample",
            message_id="other",
            sender="worker",
            recipient="other",
            body="Other",
        )
        before = observe(self.data, "sample", recipient="controller")["inbox"]
        self.assertEqual(before["count"], 1)
        self.assertIsNotNone(before["oldest_at"])
        self.assertIn("--all", before["ack_all_command"])
        self.assertIn("archive", before["archive_command"])
        self.assertIn("--before", before["bulk_ack_command"])
        self.assertTrue(
            before["acknowledge_command"].startswith(
                "continuity collaboration inbox ack --project-id sample --recipient controller"
            )
        )
        self.assertTrue(
            before["archive_command"].startswith(
                "continuity collaboration inbox archive --project-id sample --recipient controller"
            )
        )
        acknowledge(
            self.data, project_id="sample", recipient="controller", message_id="first"
        )
        self.assertEqual(
            observe(self.data, "sample", recipient="controller")["inbox"]["count"], 0
        )

    def test_native_session_end_is_profile_scoped_and_not_success(self):
        origin = current("claude")
        other = {**origin, "profile_sha256": "b" * 64}
        for index, scope in enumerate((origin, other)):
            native_observe(
                self.data,
                "sample",
                {
                    "hook_event_name": "SubagentStart",
                    "session_id": "owner",
                    "agent_id": str(index),
                    "collection_origin": scope,
                },
            )
        session_event(
            self.data,
            "sample",
            {
                "hook_event_name": "SessionEnd",
                "session_id": "owner",
                "collection_origin": origin,
            },
        )
        values = list(_load(_path(self.data, "sample"))["native_attempts"].values())
        self.assertEqual(sorted(r["status"] for r in values), ["lost", "running"])
        self.assertIn(
            "unknown", next(r for r in values if r["status"] == "lost")["authority"]
        )
        self.assertIn(
            "attempt.lost",
            [
                r["event"]
                for r in list_changes(self.data, project_id="sample")["changes"]
            ],
        )

    def test_boot_change_marks_lost_but_no_heartbeat_alone_does_not(self):
        native_observe(
            self.data,
            "sample",
            {
                "hook_event_name": "SubagentStart",
                "session_id": "owner",
                "agent_id": "one",
                "boot_id_sha256": "a" * 64,
            },
        )
        session_event(
            self.data,
            "sample",
            {
                "hook_event_name": "SessionStart",
                "session_id": "new",
                "boot_id_sha256": "b" * 64,
            },
        )
        self.assertEqual(
            observe(self.data, "sample")["native_workers"][0]["status"], "lost"
        )
        native_observe(
            self.data,
            "sample",
            {
                "hook_event_name": "SubagentStart",
                "session_id": "new",
                "agent_id": "two",
            },
        )
        path = _path(self.data, "sample")
        doc = _load(path)
        value = list(doc["native_attempts"].values())[-1]
        value["observed_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        _atomic_write(path, doc)
        self.assertIn(
            "unknown-stale",
            [r["status"] for r in observe(self.data, "sample")["native_workers"]],
        )

    def adapter(self, model="sol"):
        argv = [sys.executable, "-c", "print('synthetic done')"]
        configure(
            self.data,
            model=model,
            provider="custom",
            argv=argv,
            resume_argv=[*argv, "{thread_id}"],
        )

    def test_dispatch_rejects_named_model_mismatch_before_launch(self):
        self.adapter()
        update_task(
            self.data, project_id="sample", task_id="task", assignee="worker-grok"
        )
        with patch("continuity_plane.worker_dispatch.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "conflicts"):
                dispatch(self.root, data_root=self.data, task_id="task", model="sol")
            launch.assert_not_called()

    def test_explicit_reassignment_uses_owner_checks_then_current_prompt(self):
        self.adapter()
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker-grok",
            body="Round two instructions",
        )
        value = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            reassign=True,
            reassign_actor="worker-grok",
        )
        self.assertEqual(value["status"], "completed")
        self.assertEqual(
            _load_ledger(self.data, "sample")["tasks"][0]["assignee"], "worker-sol"
        )

    def test_task_changed_after_background_preparation_never_launches_worker(self):
        self.adapter()
        original = subprocess.Popen

        def launch(argv, *args, **kwargs):
            return (
                Mock(pid=999999)
                if "continuity_plane.worker_dispatch" in argv
                else original(argv, *args, **kwargs)
            )

        with patch(
            "continuity_plane.worker_dispatch.subprocess.Popen", side_effect=launch
        ):
            value = dispatch(
                self.root,
                data_root=self.data,
                task_id="task",
                model="sol",
                background=True,
            )
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            body="New round; old prompt is invalid",
        )
        file = (
            project_data_dir(self.data, "sample")
            / "worker-jobs"
            / value["job_id"]
            / "job.json"
        )
        with patch("continuity_plane.supervisor.subprocess.Popen") as launched:
            result = run_job(file)
            self.assertFalse(result["executed"])
            self.assertIn("task changed", result["reason"])
            self.assertEqual(result["failure_class"], "dispatch-validation")
            launched.assert_not_called()

    def test_structured_upstream_reset_takes_precedence_over_http_429(self):
        value = classify('429 {"code":"upstream_reset_replay_refused"}')
        self.assertEqual(value["failure_class"], "upstream-reset")
        self.assertFalse(value["retryable"])
        self.assertTrue(value["requires_new_session"])
        self.assertEqual(
            classify("429 too many requests")["failure_class"], "rate-limit"
        )
        self.assertEqual(
            classify("401 upstream_reset_replay_refused")["failure_class"],
            "authentication",
        )
        self.assertEqual(
            classify("cyber_policy 429 upstream_reset_replay_refused")["failure_class"],
            "policy-review",
        )

    def test_upstream_reset_stops_same_thread_retry_and_has_actionable_advice(self):
        thread = self.root / "synthetic-thread"
        thread.write_text("synthetic-thread")
        command = [
            sys.executable,
            "-c",
            "print('429 upstream_reset_replay_refused'); raise SystemExit(1)",
        ]
        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            model="sol",
            command=command,
            retry=2,
            thread_file=str(thread),
            resume_command=[*command, "{thread_id}"],
            retry_delay_seconds=0,
        )
        self.assertEqual(result["failure_class"], "upstream-reset")
        self.assertEqual(result["next_actions"][0]["kind"], "explicit-new-session")
        self.assertNotIn("--resume", result["next_actions"][0]["command"])
        starts = [
            r
            for r in list_changes(self.data, project_id="sample")["changes"]
            if r["event"] == "attempt.started"
        ]
        self.assertEqual(len(starts), 1)

    def test_real_rate_limit_never_suggests_new_thread_as_quota_reset(self):
        result = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            model="sol",
            command=[
                sys.executable,
                "-c",
                "print('429 too many requests'); raise SystemExit(1)",
            ],
        )
        self.assertEqual(result["failure_class"], "rate-limit")
        self.assertEqual(result["next_actions"][0]["kind"], "wait-for-availability")
        self.assertIn("does not reset quota", result["next_actions"][0]["note"])

    def test_top_level_task_aliases_share_the_collaboration_contract(self):
        common = ["--project-id", "sample", "--data-root", str(self.data)]
        rows = self.cli(["task", "list", *common])
        self.assertEqual(rows["items"][0]["task_id"], "task")
        value = self.cli(
            ["task", "update", *common, "--task-id", "task", "--body", "Alias update"]
        )
        self.assertEqual(value["task_id"], "task")
        self.assertEqual(
            self.execute("task.show", task_id="task")["details"]["body"], "Alias update"
        )

    def test_brief_mcp_projection_reports_total_and_omitted_items(self):
        for index in range(1, 8):
            self.card("task-" + str(index))
        result = handle_tool(
            "continuity_plan",
            {
                "action": "todo.from-cards",
                "project_id": "sample",
                "data_root": str(self.data),
                "max_output_bytes": 2300,
                "format": "brief",
            },
        )
        text = result["content"][0]["text"]
        self.assertLessEqual(len(text.encode()) + 1, 2300)
        if "count=8" not in text:
            self.assertIn("total=8", text)
            self.assertIn("remaining=", text)
            self.assertIn("truncated=true", text)

    def test_consumer_replays_until_explicit_ack_and_then_advances(self):
        for index in range(3):
            _record_change(self.data, "sample", "fixture.changed", "task", number=index)
        first = self.execute("changes", consumer="controller", max_results=1)
        again = self.execute("changes", consumer="controller", max_results=1)
        self.assertEqual(first["next_cursor"], again["next_cursor"])
        next_page = self.execute(
            "changes", consumer="controller", ack=first["next_cursor"], max_results=1
        )
        self.assertGreater(next_page["next_cursor"], first["next_cursor"])
        with self.assertRaisesRegex(ValueError, "previously delivered"):
            self.execute("changes", consumer="controller", ack=99999)

    def test_consumer_truncation_does_not_ack_unreturned_events(self):
        for index in range(5):
            _record_change(
                self.data, "sample", "fixture.changed", "task", payload="x" * 4000
            )
        first = self.execute("changes", consumer="controller", max_output_bytes=1024)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_cursor"], first["changes"][-1]["cursor"])
        next_page = self.execute(
            "changes",
            consumer="controller",
            ack=first["next_cursor"],
            max_output_bytes=1024,
        )
        self.assertEqual(next_page["changes"][0]["cursor"], first["next_cursor"] + 1)
        self.assertGreater(next_page["high_water_cursor"], next_page["next_cursor"])

    def test_consumer_failure_does_not_commit_or_silently_reset_cursor(self):
        with self.assertRaises(ValueError):
            self.execute("changes", consumer="controller", max_output_bytes=1)
        directory = project_data_dir(self.data, "sample") / "change-consumers"
        self.assertEqual(list(directory.glob("*.json")), [])
        self.execute("changes", consumer="controller")
        with self.assertRaisesRegex(ValueError, "reset-consumer"):
            self.execute("changes", consumer="controller", since=0)
        reset = self.execute(
            "changes", consumer="controller", since=0, reset_consumer=True
        )
        self.assertEqual(reset["since"], 0)

    def test_consumer_cursor_survives_fresh_process_and_is_project_scoped(self):
        _record_change(self.data, "sample", "fixture.changed", "task")
        first = self.execute("changes", consumer="controller", max_results=1)
        self.execute(
            "changes", consumer="controller", ack=first["next_cursor"], max_results=1
        )
        command = [
            sys.executable,
            "-m",
            "continuity_plane.cli",
            "collaboration",
            "changes",
            "--project-id",
            "sample",
            "--data-root",
            str(self.data),
            "--consumer",
            "controller",
        ]
        fresh = subprocess.run(
            command, capture_output=True, text=True, check=True, timeout=20
        )
        self.assertEqual(json.loads(fresh.stdout)["since"], first["next_cursor"])
        second_root = self.root / "other"
        second_root.mkdir()
        register_project(self.data, project_id="other", control_root=second_root)
        value = execute(
            "changes",
            {"consumer": "controller"},
            project_id="other",
            data_root=str(self.data),
        )
        self.assertEqual(value["since"], 0)

    def test_failed_reassignment_does_not_prepare_or_launch_old_card(self):
        self.adapter()
        update_task(
            self.data, project_id="sample", task_id="task", assignee="worker-grok"
        )
        with patch("continuity_plane.supervisor.subprocess.Popen") as launch:
            with self.assertRaisesRegex(ValueError, "--actor worker-grok"):
                dispatch(
                    self.root,
                    data_root=self.data,
                    task_id="task",
                    model="sol",
                    reassign=True,
                    reassign_actor="not-the-owner",
                )
            launch.assert_not_called()
        self.assertEqual(
            _load_ledger(self.data, "sample")["tasks"][0]["assignee"], "worker-grok"
        )

    def test_recorded_model_binding_protects_a_generic_executor_identity(self):
        self.adapter("first-adapter")
        self.adapter("second-adapter")
        value = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="first-adapter",
            assignee="executor-one",
        )
        self.assertEqual(value["status"], "completed")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            dispatch(
                self.root, data_root=self.data, task_id="task", model="second-adapter"
            )

    def test_turn_stop_does_not_falsely_end_native_worker(self):
        native_observe(
            self.data,
            "sample",
            {
                "hook_event_name": "SubagentStart",
                "session_id": "owner",
                "agent_id": "one",
            },
        )
        handle_event(
            {"cwd": str(self.root), "session_id": "owner", "hook_event_name": "Stop"},
            host="claude",
            data_root=self.data,
        )
        self.assertEqual(
            list(_load(_path(self.data, "sample"))["native_attempts"].values())[0][
                "status"
            ],
            "running",
        )

    def test_legacy_scene_excludes_stopped_native_attempts_from_live_count(self):
        payload = {"session_id": "owner", "agent_id": "one"}
        native_observe(
            self.data, "sample", {**payload, "hook_event_name": "SubagentStart"}
        )
        native_observe(
            self.data, "sample", {**payload, "hook_event_name": "SubagentStop"}
        )
        value = handoff_view(self.data, "sample")
        self.assertEqual(value["observed"]["native_workers_total"], 1)
        self.assertEqual(value["scene"]["native_workers_total"], 0)
        self.assertEqual(value["scene"]["native_workers"], [])

    def test_auto_handoff_does_not_bypass_controller_lease(self):
        from continuity_plane.controller_lease import acquire

        token = acquire(self.data, "sample", owner="owner", ttl_seconds=120)["token"]
        with self.assertRaises(ValueError):
            self.execute("handoff.write", auto=True, controller="other")
        value = self.execute(
            "handoff.write", auto=True, controller="owner", controller_token=token
        )
        self.assertEqual(value["draft"]["authority"], "candidate-not-approved-plan")

    def test_bounded_handoff_preserves_plan_and_current_observation(self):
        handoff(
            self.root,
            self.data,
            "sample",
            plan="Keep integration order",
            next_action="Inspect reports",
        )
        for index in range(12):
            task_id = "long-" + str(index)
            self.card(task_id)
            update_task(
                self.data,
                project_id="sample",
                task_id=task_id,
                next_action="合成场景" * 60,
            )
        value = self.execute("handoff.show", max_output_bytes=8192)
        self.assertEqual(value["next_action"], "Inspect reports")
        self.assertEqual(value["declared"]["plan"], "Keep integration order")
        self.assertIn("as_of", value["observed"])
        self.assertTrue(value["truncated"])

    def test_fast_handoff_records_current_change_cursor_without_scene_scan(self):
        _record_change(
            self.data,
            "sample",
            "task.updated",
            "task",
            status="active",
        )
        value = handoff(
            self.root,
            self.data,
            "sample",
            plan="Keep the current cursor",
            next_action="Resume from the saved event boundary",
        )
        self.assertEqual(value["scene"]["status"], "deferred")
        self.assertEqual(
            value["scene"]["change_cursor"],
            latest_change_cursor(self.data, project_id="sample"),
        )

    def test_handoff_can_bound_events_by_saved_timestamp_for_old_snapshots(self):
        base_cursor = list_changes(self.data, project_id="sample")["high_water_cursor"]
        times = iter(
            (
                "2026-10-20T00:01:00+00:00",
                "2026-10-20T00:02:00+00:00",
                "2026-10-20T00:03:00+00:00",
            )
        )
        with patch(
            "continuity_plane.collaboration_registry._now",
            side_effect=lambda: next(times),
        ):
            for event in ("one", "two", "three"):
                _record_change(
                    self.data,
                    "sample",
                    event,
                    "task",
                )
        self.assertEqual(
            change_cursor_at_or_before(
                self.data,
                project_id="sample",
                timestamp="2026-10-20T00:02:30+00:00",
            ),
            base_cursor + 2,
        )

    def test_ordinary_event_append_does_not_read_full_history(self):
        with patch(
            "pathlib.Path.read_text",
            side_effect=AssertionError("ordinary event append scanned full history"),
        ):
            _record_change(
                self.data,
                "sample",
                "task.updated",
                "task",
                status="active",
            )
        latest = list_changes(self.data, project_id="sample")["changes"][-1]
        self.assertEqual(latest["event"], "task.updated")

    def test_truncated_handoff_keeps_pending_decisions_and_attention(self):
        handoff(
            self.root,
            self.data,
            "sample",
            plan="Keep integration order",
            next_action="Resolve the provider reset",
            pending=["Approve the next integration batch"],
        )
        self.card("blocked")
        update_task(
            self.data,
            project_id="sample",
            task_id="blocked",
            status="blocked",
            blocked_reason="quota reset required",
        )
        self.card("failed")
        bind_execution(
            self.data,
            project_id="sample",
            task_id="failed",
            assignee="worker-sol",
            attempt_id="attempt-failed",
        )
        _record_change(
            self.data,
            "sample",
            "attempt.finished",
            "failed",
            attempt_id="attempt-failed",
            status="failed",
            failure_class="quota-exhausted",
            reason="usage limit reached",
        )
        for index in range(20):
            task_id = "change-" + str(index)
            self.card(task_id)
            update_task(
                self.data,
                project_id="sample",
                task_id=task_id,
                next_action="ordinary history " + ("x" * 180),
            )
        value = self.execute("handoff.show", max_output_bytes=1024)
        self.assertEqual(
            value["declared"]["pending_decisions"],
            ["Approve the next integration batch"],
        )
        attention = value["observed"]["attention"]
        self.assertTrue(any(row["kind"] == "blocked-card" for row in attention))
        self.assertTrue(
            any(row["kind"] == "failed-or-interrupted-attempt" for row in attention)
        )
        self.assertTrue(value["details_omitted"])

    def test_corrupt_consumer_state_is_not_silently_recreated(self):
        self.execute("changes", consumer="controller")
        file = next(
            (project_data_dir(self.data, "sample") / "change-consumers").glob("*.json")
        )
        file.write_text("[]")
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.execute("changes", consumer="controller")
        self.assertEqual(file.read_text(), "[]")


if __name__ == "__main__":
    unittest.main()
