"""Shared project context, native-hook observations and cooperating controller locks."""

import contextlib
from contextlib import redirect_stdout
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import test_adoption_workflows
from continuity_plane.cli import main
from continuity_plane.collaboration_registry import (
    add_task,
    list_changes,
    register_project,
)
from continuity_plane.controller_lease import (
    acquire,
    reconcile_host,
    require,
    reservation_report,
    reserve,
    finish_reservation,
    view,
)
from continuity_plane.lifecycle import handle_event
from continuity_plane.planning_api import execute, handle_tool, help_for
from continuity_plane.project_context import _path, _load
from continuity_plane.provider_usage import import_snapshot, report
from continuity_plane.runtime_origin import current
from continuity_plane.worker_dispatch import configure, dispatch


class ProjectContextTests(unittest.TestCase):
    setUp = test_adoption_workflows.AdoptionWorkflowTests.setUp

    def execute(self, operation, **parameters):
        return execute(
            operation,
            parameters,
            root=str(self.root),
            project_id="sample",
            data_root=str(self.data),
        )

    def snapshot(self, tokens, *, session="owned-session", origin=None):
        import_snapshot(
            self.root,
            {
                "provider": "claude",
                "session": hashlib.sha256(session.encode()).hexdigest(),
                "tokens": {"input_tokens": tokens},
                "collection_origin": origin or current("claude"),
            },
            self.data,
        )

    def test_generated_rule_text_contains_every_rule_and_never_truncates(self):
        from continuity_plane.rules_delivery import put_rule

        for i in range(5):
            put_rule(
                self.data,
                project_id="sample",
                rule_id="rule-" + str(i),
                text=("Keep scope " + str(i) + ". ") * 150,
            )
        from continuity_plane.cli import build_parser

        args = build_parser().parse_args(
            [
                "rules",
                "export",
                "--root",
                str(self.root),
                "--data-root",
                str(self.data),
                "--project-id",
                "sample",
                "--audience",
                "worker",
                "--format",
                "text",
                "--max-output-bytes",
                "32768",
            ]
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            args.handler(args)
        text = output.getvalue()
        for i in range(5):
            self.assertIn("Keep scope " + str(i), text)
        self.assertIn("generated", text)
        self.assertIn("Rules digest:", text)
        with self.assertRaisesRegex(ValueError, "no policy was truncated"):
            args.max_output_bytes = 1024
            args.handler(args)

    def test_lesson_is_project_shared_immutable_and_visible_in_packet(self):
        row = self.execute(
            "lesson.add",
            lesson_id="load-misread",
            text="Distinguish a failing guard from a load limit.",
            source="batch-14 review",
            tag=["verification"],
        )
        self.assertIn("not-policy", row["authority"])
        self.assertEqual(self.execute("lesson.list", query="guard")["total_items"], 1)
        self.assertEqual(
            self.execute("lesson.show", lesson_id="load-misread")["source"],
            "batch-14 review",
        )
        self.assertEqual(
            self.execute(
                "lesson.add",
                lesson_id="load-misread",
                text=row["text"],
                source=row["source"],
                tag=["verification"],
            ),
            row,
        )
        with self.assertRaisesRegex(ValueError, "correction id"):
            self.execute(
                "lesson.add",
                lesson_id="load-misread",
                text="Other interpretation",
                source="review",
            )
        packet = self.execute("packet", assignee="worker")
        self.assertEqual(packet["lesson_count"], 1)
        self.assertEqual(packet["lessons"][0]["lesson_id"], "load-misread")

    def test_session_binding_counts_only_future_deltas_per_task_and_batch(self):
        self.snapshot(100)
        self.execute(
            "session.bind",
            provider="claude",
            session_id="owned-session",
            task_id="task",
            batch_id="batch16",
        )
        self.snapshot(140)
        usage = report(self.root, self.data)["session_binding_usage"]
        self.assertEqual(usage["by_task"]["task"]["input_tokens"], 40)
        self.assertEqual(usage["by_batch"]["batch16"]["input_tokens"], 40)
        self.assertFalse(usage["cost_available"])
        self.assertNotIn("owned-session", _path(self.data, "sample").read_text())
        add_task(
            self.data,
            project_id="sample",
            task_id="other",
            lane_id="other",
            mode="patch",
            priority="p2",
            title="other",
            objective="test",
            next_action="test",
            exit_criteria=["pass"],
        )
        self.execute(
            "session.bind",
            provider="claude",
            session_id="owned-session",
            task_id="other",
            batch_id="batch17",
        )
        self.snapshot(170)
        usage = report(self.root, self.data)["session_binding_usage"]
        self.assertEqual(usage["by_task"]["task"]["input_tokens"], 40)
        self.assertEqual(usage["by_task"]["other"]["input_tokens"], 30)
        self.assertEqual(report(self.root, self.data)["by_task"], {})

    def test_binding_before_usage_uses_first_observation_as_baseline(self):
        self.execute(
            "session.bind",
            provider="claude",
            session_id="owned-session",
            batch_id="planned-batch",
        )
        self.snapshot(100)
        self.assertEqual(
            report(self.root, self.data)["session_binding_usage"]["by_batch"][
                "planned-batch"
            ]["input_tokens"],
            0,
        )
        self.snapshot(123)
        self.assertEqual(
            report(self.root, self.data)["session_binding_usage"]["by_batch"][
                "planned-batch"
            ]["input_tokens"],
            23,
        )
        self.execute("session.unbind", provider="claude", session_id="owned-session")
        self.snapshot(150)
        self.assertEqual(
            report(self.root, self.data)["session_binding_usage"]["by_batch"][
                "planned-batch"
            ]["input_tokens"],
            23,
        )

    def test_binding_never_merges_different_origin_profiles(self):
        self.snapshot(10)
        self.execute(
            "session.bind",
            provider="claude",
            session_id="owned-session",
            task_id="task",
        )
        other = {**current("claude"), "profile_sha256": "f" * 64}
        self.snapshot(20, origin=other)
        with self.assertRaisesRegex(ValueError, "multiple collection origins"):
            self.execute(
                "session.bind",
                provider="claude",
                session_id="owned-session",
                batch_id="other",
            )
        self.assertEqual(
            report(self.root, self.data)["session_binding_usage"]["by_task"], {}
        )

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.root), *args], stderr=subprocess.DEVNULL, text=True
        ).strip()

    def init_git(self):
        self.git("init", "-b", "main")
        (self.root / "toy.py").write_text("def sample_symbol():\n    return 1\n")
        self.git("add", "toy.py")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture main",
        )
        return self.git("rev-parse", "HEAD")

    def test_handoff_head_uses_main_ref_not_the_checkout_branch(self):
        main = self.init_git()
        self.git("switch", "-c", "stale-checkout")
        (self.root / "toy.py").write_text("def sample_symbol():\n    return 2\n")
        self.git("add", "toy.py")
        self.git(
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-m",
            "fixture checkout",
        )
        checkout = self.git("rev-parse", "HEAD")
        payload = {
            "cwd": str(self.root),
            "session_id": "compact",
            "hook_event_name": "PreCompact",
        }
        handle_event(payload, host="claude", data_root=self.data)
        key = hashlib.sha256((str(self.root) + ":compact").encode()).hexdigest()
        saved = json.loads((self.data / "handoffs" / (key + ".json")).read_text())
        self.assertEqual(saved["head"], main)
        self.assertEqual(saved["checkout_head"], checkout)
        self.assertFalse(saved["integration"]["checkout_head_substituted"])
        self.assertEqual(self.git("rev-parse", "HEAD"), checkout)
        with self.assertRaisesRegex(ValueError, "not available"):
            self.execute(
                "project.context-set",
                integration_root=str(self.root),
                integration_ref="missing",
            )

    def test_shared_handoff_is_available_to_new_models_and_worker_does_not_overwrite(
        self,
    ):
        self.execute(
            "handoff.write",
            batch_id="batch16",
            plan="Integrate regression fixes before other branches after transport-quick passes.",
            next_action="Run transport-quick",
            pending=["Owner chooses baseline"],
        )
        bound = self.execute(
            "session.bind",
            provider="claude",
            session_id="worker-session",
            task_id="task",
            role="worker",
        )
        self.assertEqual(bound["role"], "worker")
        with patch.dict(os.environ, {"CONTINUITY_ROLE": "worker"}):
            handle_event(
                {
                    "cwd": str(self.root),
                    "session_id": "worker-session",
                    "hook_event_name": "PreCompact",
                    "next_action": "Worker-only action",
                },
                host="claude",
                data_root=self.data,
            )
        self.assertEqual(
            self.execute("handoff.show")["next_action"], "Run transport-quick"
        )
        start = handle_event(
            {
                "cwd": str(self.root),
                "session_id": "new-codex-controller",
                "hook_event_name": "SessionStart",
                "source": "startup",
            },
            host="codex",
            data_root=self.data,
        )
        self.assertIn(
            "transport-quick", start["hookSpecificOutput"]["additionalContext"]
        )
        self.assertLessEqual(start["injected_bytes"], 4096)
        packet = self.execute("packet", assignee="worker")
        self.assertEqual(packet["controller_handoff"]["current_batch"], "batch16")

    def test_native_subagents_are_observed_not_assumed_successful_or_pid_probed(self):
        payload = {
            "cwd": str(self.root),
            "session_id": "parent",
            "agent_id": "child",
            "agent_type": "reviewer",
            "hook_event_name": "SubagentStart",
        }
        start = handle_event(payload, host="claude", data_root=self.data)
        again = handle_event(payload, host="claude", data_root=self.data)
        self.assertEqual(start["injected_bytes"], 0)
        self.assertEqual(again["observation"]["status"], "duplicate-native-observation")
        stop = handle_event(
            {
                **payload,
                "hook_event_name": "SubagentStop",
                "last_assistant_message": "PRIVATE_REPORT",
                "agent_transcript_path": "/private/native-session.jsonl",
            },
            host="claude",
            data_root=self.data,
        )
        self.assertEqual(stop["observation"]["status"], "stopped")
        self.assertNotIn("return_code", stop["observation"])
        self.assertNotIn("PRIVATE_REPORT", _path(self.data, "sample").read_text())
        events = [
            r
            for r in list_changes(self.data, project_id="sample")["changes"]
            if r.get("observed_native")
        ]
        self.assertEqual(
            [r["event"] for r in events], ["attempt.started", "attempt.finished"]
        )
        from continuity_plane.supervisor import daemon_once

        self.assertEqual(daemon_once(self.data, project_id="sample")["reconciled"], [])

    def test_native_unmatched_stop_does_not_invent_an_attempt(self):
        result = handle_event(
            {
                "cwd": str(self.root),
                "session_id": "parent",
                "agent_id": "internal",
                "agent_type": "",
                "hook_event_name": "SubagentStop",
            },
            host="claude",
            data_root=self.data,
        )
        self.assertFalse(result["observation"]["attempt_created"])
        self.assertFalse(_load(_path(self.data, "sample")).get("native_attempts"))

    def test_other_clients_never_become_claude_native_observations(self):
        result = handle_event(
            {
                "cwd": str(self.root),
                "session_id": "parent",
                "agent_id": "child",
                "hook_event_name": "SubagentStart",
            },
            host="codex",
            data_root=self.data,
        )
        self.assertEqual(result["status"], "unsupported-native-agent-host")
        self.assertFalse(_load(_path(self.data, "sample")).get("native_attempts"))

    def test_code_context_contains_hash_bound_routes(self):
        self.init_git()
        self.execute(
            "task.update",
            task_id="task",
            body="Check sample_symbol",
            allowed_file=["toy.py"],
        )
        assembled = self.execute("prompt", task_id="task", assignee="worker")
        self.assertIn("sample_symbol", assembled["prompt"])
        self.assertIn("file_sha256", assembled["prompt"])
        self.assertIn(
            hashlib.sha256((self.root / "toy.py").read_bytes()).hexdigest(),
            assembled["prompt"],
        )

    def test_controller_lease_has_one_winner_and_fences_old_tokens(self):
        gate = threading.Barrier(2)

        def take(owner):
            gate.wait(timeout=5)
            try:
                return acquire(self.data, "sample", owner=owner)
            except ValueError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            winners = list(pool.map(take, ["one", "two"]))
        self.assertEqual(sum(v is not None for v in winners), 1)
        winner = next(v for v in winners if v)
        self.assertTrue(view(self.data, "sample", "other")["read_only"])
        self.assertNotIn("token", view(self.data, "sample", "other")["lease"])
        with patch(
            "continuity_plane.controller_lease.time.time",
            return_value=winner["expires_at"] + 1,
        ):
            replacement = acquire(self.data, "sample", owner="new")
            self.assertGreater(replacement["fence"], winner["fence"])
            with self.assertRaisesRegex(ValueError, "read-only"):
                require(self.data, "sample", winner["owner"], winner["token"])

    def test_controller_capacity_and_duplicate_dispatch_reservation_are_atomic(self):
        profile = configure(
            self.data,
            model="sol",
            provider="custom",
            profile_provider="codex",
            argv=[sys.executable, "-c", "print('done')"],
        )
        lease = acquire(
            self.data, "sample", owner="controller", client_limits={"codex": 1}
        )
        reserve(
            self.data,
            "sample",
            task_id="task",
            job_id="job-a",
            owner="controller",
            token=lease["token"],
            profile=profile,
        )
        with self.assertRaisesRegex(ValueError, "reservation"):
            reserve(
                self.data,
                "sample",
                task_id="task",
                job_id="job-b",
                owner="controller",
                token=lease["token"],
                profile=profile,
            )
        with self.assertRaisesRegex(ValueError, "capacity"):
            reserve(
                self.data,
                "sample",
                task_id="other",
                job_id="job-b",
                owner="controller",
                token=lease["token"],
                profile=profile,
            )
        finish_reservation(self.data, "sample", "task", "wrong-job")
        self.assertIn(
            "task", _load(_path(self.data, "sample"))["dispatch_reservations"]
        )
        finish_reservation(self.data, "sample", "task", "job-a")
        self.assertEqual(_load(_path(self.data, "sample"))["dispatch_reservations"], {})

    def test_host_reboot_reconciles_stale_reservations_before_capacity(self):
        profile = configure(
            self.data,
            model="sol",
            provider="custom",
            profile_provider="codex",
            argv=[sys.executable, "-c", "print('done')"],
        )
        lease = acquire(
            self.data, "sample", owner="controller", client_limits={"codex": 1}
        )
        with patch(
            "continuity_plane.controller_lease._boot_fingerprint",
            return_value="a" * 64,
        ):
            reserve(
                self.data,
                "sample",
                task_id="task",
                job_id="job-old",
                owner="controller",
                token=lease["token"],
                profile=profile,
            )
        with patch(
            "continuity_plane.controller_lease._boot_fingerprint",
            return_value="b" * 64,
        ):
            report = reservation_report(self.data, "sample")
            self.assertEqual(report["items"][0]["reason"], "host-boot-changed")
            self.assertEqual(
                report["capacity"][0]["scope"],
                "project+host+profile+client-family",
            )
            listed = self.execute(
                "controller.reservations",
                state="stale",
            )
            self.assertEqual(listed["items"][0]["job_id"], "job-old")
            status = self.execute(
                "controller.status",
            )
            self.assertIn("per project", status["capacity_semantics"])
            reserve(
                self.data,
                "sample",
                task_id="other",
                job_id="job-new",
                owner="controller",
                token=lease["token"],
                profile=profile,
            )
            dry_run = reconcile_host(self.data, "sample")
            applied = reconcile_host(
                self.data,
                "sample",
                apply=True,
                reason="verified host reboot",
                owner="controller",
                token=lease["token"],
            )
        self.assertEqual(dry_run["cleared_count"], 0)
        self.assertEqual(applied["cleared_count"], 1)
        self.assertEqual(
            list(_load(_path(self.data, "sample"))["dispatch_reservations"]),
            ["other"],
        )

    def test_clear_reservation_reports_already_absent_exactly(self):
        lease = acquire(self.data, "sample", owner="controller")
        with redirect_stdout(io.StringIO()) as output:
            code = main(
                [
                    "controller",
                    "clear-reservation",
                    "--root",
                    str(self.root),
                    "--data-root",
                    str(self.data),
                    "--project-id",
                    "sample",
                    "--owner",
                    "controller",
                    "--token",
                    lease["token"],
                    "--task-id",
                    "task",
                    "--job-id",
                    "job-missing",
                    "--reason",
                    "verified absent",
                ]
            )
        self.assertEqual(code, 0)
        value = json.loads(output.getvalue())
        self.assertFalse(value["cleared"])
        self.assertEqual(value["status"], "not-found")
        self.assertIn("nothing was changed", value["message"])

    def test_dispatch_and_baseline_respect_controller_lease_but_body_updates_do_not(
        self,
    ):
        configure(
            self.data,
            model="sol",
            provider="custom",
            argv=[sys.executable, "-c", "print('done')"],
        )
        lease = self.execute("controller.acquire", owner="controller")
        with self.assertRaisesRegex(ValueError, "read-only"):
            dispatch(
                self.root,
                data_root=self.data,
                task_id="task",
                model="sol",
                controller="other",
            )
        self.execute(
            "task.update", task_id="task", body="Ordinary work remains allowed"
        )
        result = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            controller="controller",
            controller_token=lease["token"],
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(_load(_path(self.data, "sample"))["dispatch_reservations"], {})

    def test_all_new_operations_are_discoverable_and_mcp_claim_free(self):
        for topic in (
            "session.bind",
            "lesson.add",
            "handoff.write",
            "controller.acquire",
            "controller.reservations",
            "controller.reconcile-host",
            "project.context-set",
        ):
            self.assertTrue(help_for(topic)["parameters"])
        response = handle_tool(
            "continuity_plan",
            {
                "action": "lesson.add",
                "root": str(self.root),
                "project_id": "sample",
                "data_root": str(self.data),
                "parameters": {
                    "lesson_id": "reported",
                    "text": "Check evidence",
                    "source": "review",
                },
            },
        )
        self.assertFalse(response.get("isError", False))
        self.assertFalse((self.root / ".continuity/state.sqlite3").exists())

    def test_native_event_pending_delivery_retries_without_duplicate_events(self):
        from continuity_plane.native_subagents import observe, flush

        payload = {
            "hook_event_name": "SubagentStart",
            "session_id": "parent",
            "agent_id": "agent",
            "agent_type": "review",
        }
        with patch(
            "continuity_plane.native_subagents._record_change",
            side_effect=OSError("fixture I/O failure"),
        ):
            observe(self.data, "sample", payload)
        self.assertTrue(_load(_path(self.data, "sample"))["native_pending_events"])
        flush(self.data, "sample")
        observe(self.data, "sample", payload)
        rows = [
            r
            for r in list_changes(self.data, project_id="sample")["changes"]
            if r.get("observed_native")
        ]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["event_id"].startswith("native:"))

    def test_native_hooks_forward_only_metadata_not_private_paths_or_bodies(self):
        from continuity_plane.remote_hook import forward

        sent = []

        def ssh(argv, **kwargs):
            sent.append(json.loads(kwargs["input"]))
            return subprocess.CompletedProcess(argv, 0, '{"continue":true}', "")

        payload = {
            "cwd": str(self.root),
            "session_id": "parent",
            "hook_event_name": "SubagentStop",
            "agent_id": "agent",
            "agent_type": "reviewer",
            "agent_transcript_path": "/private/native.jsonl",
            "last_assistant_message": "PRIVATE_REPORT",
        }
        config = {
            "host": "state",
            "local_root": str(self.root),
            "remote_root": "/workspace/project",
            "remote_endpoint": "/opt/continuity/continuity-remote-receive",
            "local_data_root": str(self.data),
        }
        with patch(
            "continuity_plane.remote_hook.Path.home", return_value=self.root
        ):
            forward(payload, config, runner=ssh)
        self.assertEqual(sent[0]["agent_id"], "agent")
        self.assertIn("collection_origin", sent[0])
        self.assertNotIn("agent_transcript_path", sent[0])
        self.assertNotIn("PRIVATE_REPORT", json.dumps(sent[0]))

    def test_compaction_updates_bound_controller_plan_but_not_task_ownership(self):
        self.execute(
            "session.bind",
            provider="claude",
            session_id="controller-session",
            task_id="task",
            batch_id="batch16",
        )
        handle_event(
            {
                "cwd": str(self.root),
                "session_id": "controller-session",
                "hook_event_name": "PreCompact",
            },
            host="claude",
            data_root=self.data,
        )
        shared = self.execute("handoff.show")
        self.assertEqual(shared["current_batch"], "batch16")
        self.assertEqual(shared["next_action"], "Run tests")
        self.assertEqual(shared["batch_state"], "planned-or-unbound")

    def test_worker_prompt_includes_project_integration_base_as_non_authority(self):
        from continuity_plane.collaboration_workflow import prompt

        profile = self.root / ".continuity" / "controller-profile.json"
        profile.parent.mkdir(parents=True)
        profile.write_text(
            json.dumps(
                {
                    "defaults": {"integration_base": "release/next"},
                    "locked_fields": ["integration_base"],
                }
            ),
            encoding="utf-8",
        )
        value = prompt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            audience="worker",
            controller="controller",
        )
        self.assertEqual(
            value["prompt"].count('"integration_base"'),
            1,
        )
        self.assertIn('"ref": "refs/heads/release/next"', value["prompt"])
        self.assertIn(
            "not merge, push, deploy, or ownership authorization",
            value["prompt"],
        )

    def test_two_simultaneous_reservations_for_one_card_have_one_winner(self):
        profile = configure(
            self.data,
            model="sol",
            provider="custom",
            argv=[sys.executable, "-c", "pass"],
        )
        lease = acquire(self.data, "sample", owner="controller")
        barrier = threading.Barrier(2)

        def launch(job):
            barrier.wait(timeout=5)
            try:
                reserve(
                    self.data,
                    "sample",
                    task_id="task",
                    job_id=job,
                    owner="controller",
                    token=lease["token"],
                    profile=profile,
                )
                return True
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(launch, ["job-one", "job-two"])), 1)

    def test_protected_cli_operations_serialize_and_propagate_failure(self):
        lease = acquire(self.data, "sample", owner="controller")
        base = [
            sys.executable,
            "-m",
            "continuity_plane.cli",
            "controller",
            "run",
            "--root",
            str(self.root),
            "--data-root",
            str(self.data),
            "--owner",
            "controller",
            "--token",
            lease["token"],
            "--operation",
            "integration",
            "--",
        ]
        marker = self.root / "operation-marker"
        code = f"from pathlib import Path; import time; p=Path({str(marker)!r}); assert not p.exists(); p.write_text('held'); time.sleep(.15); p.unlink()"
        first = subprocess.Popen(
            [*base, sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second = subprocess.Popen(
            [*base, sys.executable, "-c", code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for process in (first, second):
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertTrue(json.loads(stdout)["lease_still_current"])
        failure = subprocess.run(
            [*base, sys.executable, "-c", "raise SystemExit(3)"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(failure.returncode, 3)

    def test_failed_background_launch_releases_its_reservation(self):
        configure(
            self.data,
            model="sol",
            provider="custom",
            argv=[sys.executable, "-c", "pass"],
        )
        original = subprocess.Popen

        def launch(argv, *args, **kwargs):
            if "continuity_plane.worker_dispatch" in argv:
                raise OSError("fixture failed launch")
            return original(argv, *args, **kwargs)

        with patch(
            "continuity_plane.worker_dispatch.subprocess.Popen", side_effect=launch
        ):
            with self.assertRaisesRegex(OSError, "failed launch"):
                dispatch(
                    self.root,
                    data_root=self.data,
                    task_id="task",
                    model="sol",
                    background=True,
                )
        self.assertEqual(_load(_path(self.data, "sample"))["dispatch_reservations"], {})

    def test_rule_mirror_detects_manual_drift_without_importing_it(self):
        from continuity_plane.rules_delivery import bundle, export_text

        mirror = self.root / "common.txt"
        mirror.write_text(
            export_text(bundle(self.data, project_id="sample", audience="worker"))
        )
        self.assertEqual(
            self.execute("rules.check-mirror", file=str(mirror))["status"], "current"
        )
        mirror.write_text(mirror.read_text() + "Manually added policy\n")
        self.assertEqual(
            self.execute("rules.check-mirror", file=str(mirror))["status"], "drift"
        )
        self.assertEqual(
            bundle(self.data, project_id="sample", audience="worker")["rule_count"], 0
        )

    def test_fenced_project_has_explicit_schema_upgrade_and_missing_metadata_fails_closed(
        self,
    ):
        from continuity_plane.collaboration_registry import (
            _ledger_path,
            _load_ledger,
        )

        acquire(self.data, "sample", owner="controller")
        ledger = json.loads(_ledger_path(self.data, "sample").read_text())
        self.assertTrue(ledger["schema_version"].endswith("v4alpha1"))
        self.assertEqual(ledger["minimum_reader_schema"], 4)
        self.assertTrue(
            _load_ledger(self.data, "sample")["controller_fencing_required"]
        )
        _path(self.data, "sample").unlink()  # Disposable fixture only.
        with self.assertRaisesRegex(ValueError, "missing"):
            require(self.data, "sample", "other", "wrong")
        self.assertTrue(view(self.data, "sample", "other")["read_only"])

    def test_new_metadata_commands_accept_project_id_without_root(self):
        for command in (
            ["controller", "status"],
            ["lesson", "list"],
            ["handoff", "show"],
        ):
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "continuity_plane.cli",
                    *command,
                    "--project-id",
                    "sample",
                    "--data-root",
                    str(self.data),
                ],
                cwd=self.root.parent,
                env={
                    **os.environ,
                    "PYTHONPATH": os.path.dirname(os.path.dirname(__file__)),
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsInstance(json.loads(result.stdout), dict)

    def test_root_project_mismatch_reports_both_registered_identities(self):
        other_root = self.root.parent / "other-project"
        other_root.mkdir()
        register_project(
            self.data,
            project_id="other-project",
            control_root=other_root,
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuity_plane.cli",
                "handoff",
                "show",
                "--root",
                str(self.root),
                "--project-id",
                "other-project",
                "--data-root",
                str(self.data),
            ],
            cwd=self.root.parent,
            env={
                **os.environ,
                "PYTHONPATH": os.path.dirname(os.path.dirname(__file__)),
            },
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(f"requested_root={self.root}", result.stderr)
        self.assertIn("requested_project_id=other-project", result.stderr)
        self.assertIn("root_project_id=sample", result.stderr)
        self.assertIn(f"requested_project_registered_root={other_root}", result.stderr)
        self.assertIn("requested_project_registered_id=other-project", result.stderr)

    def test_batch_record_refreshes_shared_scene_without_erasing_the_plan(self):
        self.execute(
            "handoff.write",
            batch_id="batch16",
            plan="Regression before other branches",
            next_action="Run transport gate",
        )
        value = {
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
        }
        self.execute("batch-add", record_id="batch15", record_json=json.dumps(value))
        shared = self.execute("handoff.show")
        self.assertEqual(shared["latest_integrated_batch"], "batch15")
        self.assertEqual(shared["current_batch"], "batch16")
        self.assertEqual(shared["plan"], "Regression before other branches")

    def test_shared_notes_reject_credential_assignments(self):
        with self.assertRaisesRegex(ValueError, "credential"):
            self.execute(
                "lesson.add",
                lesson_id="unsafe",
                text='{"api_key":"fixture-not-a-real-secret"}',
                source="fixture",
            )
        with self.assertRaisesRegex(ValueError, "credential"):
            self.execute(
                "handoff.write", pending=["password=fixture-not-a-real-secret"]
            )

    def test_missing_worker_checkout_uses_labeled_registered_root_routes(self):
        self.init_git()
        self.execute(
            "task.update",
            task_id="task",
            body="Check sample_symbol",
            worktree=str(self.root / "future-worker-checkout"),
            allowed_file=["toy.py"],
        )
        assembled = self.execute("prompt", task_id="task", assignee="worker")
        self.assertIn("registered-project-root; verify", assembled["prompt"])
        self.assertIn("file_sha256", assembled["prompt"])

    def test_unknown_controller_operation_survives_lease_takeover_until_acknowledged(
        self,
    ):
        from continuity_plane.controller_lease import (
            begin_operation,
            end_operation,
        )

        lease = acquire(self.data, "sample", owner="old")
        started = begin_operation(
            self.data,
            "sample",
            owner="old",
            token=lease["token"],
            operation="integration",
            argv=["fixture-command"],
        )
        with patch(
            "continuity_plane.controller_lease.time.time",
            return_value=lease["expires_at"] + 1,
        ):
            fresh = acquire(self.data, "sample", owner="new")
            with self.assertRaisesRegex(ValueError, "no confirmed finish"):
                begin_operation(
                    self.data,
                    "sample",
                    owner="new",
                    token=fresh["token"],
                    operation="integration",
                    argv=["fixture-command"],
                )
            end_operation(
                self.data,
                "sample",
                started["operation_id"],
                reason="Fixture owner confirmed no running operation",
            )
            newer = begin_operation(
                self.data,
                "sample",
                owner="new",
                token=fresh["token"],
                operation="integration",
                argv=["fixture-command"],
            )
            with self.assertRaisesRegex(ValueError, "no newer record overwritten"):
                end_operation(
                    self.data, "sample", started["operation_id"], return_code=0
                )
            self.assertEqual(
                view(self.data, "sample")["operation"]["operation_id"],
                newer["operation_id"],
            )

    def test_unknown_wait_keeps_operation_unconfirmed(self):
        from continuity_plane.cli import build_parser

        lease = acquire(self.data, "sample", owner="controller")
        argv = [
            "controller",
            "run",
            "--root",
            str(self.root),
            "--data-root",
            str(self.data),
            "--owner",
            "controller",
            "--token",
            lease["token"],
            "--operation",
            "integration",
            "--",
            "fixture-command",
        ]
        args = build_parser().parse_args(argv)
        with patch(
            "continuity_plane.controller_lease.subprocess.run",
            side_effect=FileNotFoundError("fixture missing executable"),
        ):
            with self.assertRaises(FileNotFoundError):
                args.handler(args)
        self.assertEqual(view(self.data, "sample")["operation"]["status"], "finished")
        with patch(
            "continuity_plane.controller_lease.subprocess.run",
            side_effect=OSError("fixture unknown wait outcome"),
        ):
            with self.assertRaises(OSError):
                args.handler(args)
        self.assertEqual(view(self.data, "sample")["operation"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
