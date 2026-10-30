import json
import sys
import tempfile
import unittest
from pathlib import Path

from continuity_plane.collaboration_registry import (
    register_project,
    add_task,
    update_task,
    list_tasks,
    list_changes,
)
from continuity_plane.delivery_records import detail_view, read_report
from continuity_plane.supervisor import run_attempt
from continuity_plane.retry_policy import classify


class FieldWorkflowTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data = self.root / "data"
        register_project(self.data, project_id="sample", control_root=self.root)
        add_task(
            self.data,
            project_id="sample",
            task_id="task",
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="Test",
            objective="Run focused tests",
            next_action="Run",
            exit_criteria=["PASS"],
            assignee="worker",
        )

    def run_attempt(self, command, **kwargs):
        return run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=command,
            timeout_seconds=5,
            idle_timeout_seconds=3,
            **kwargs,
        )

    def test_transient_resume_uses_same_thread_without_restarting(self):
        thread = self.root / "thread"
        initial = [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(thread)!r}).write_text('thread-123'); print('502 Bad Gateway'); raise SystemExit(1)",
        ]
        resumed = [
            sys.executable,
            "-c",
            "import sys; assert sys.argv[1]=='thread-123'",
            "{thread_id}",
        ]
        result = self.run_attempt(
            initial,
            retry=1,
            retry_delay_seconds=0,
            resume_command=resumed,
            thread_file=str(thread),
        )
        self.assertEqual(result["status"], "completed")
        starts = [
            e
            for e in list_changes(self.data, project_id="sample")["changes"]
            if e["event"] == "attempt.started"
        ]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1]["command"][-1], "thread-123")

    def test_missing_thread_and_auth_do_not_restart(self):
        result = self.run_attempt(
            [
                sys.executable,
                "-c",
                "print('429 Too many requests'); raise SystemExit(1)",
            ],
            retry=2,
            retry_delay_seconds=0,
            resume_command=["codex", "exec", "resume", "{thread_id}"],
            thread_file=str(self.root / "missing"),
        )
        self.assertIn("resume unavailable", result["retry_stopped"])
        result = self.run_attempt(
            [sys.executable, "-c", "print('401 unauthorized'); raise SystemExit(1)"],
            retry=2,
            restart_on_retry=True,
        )
        self.assertEqual(result["failure_class"], "authentication")
        self.assertFalse(result["retryable"])

    def test_wait_budget_and_ambiguous_clock(self):
        result = self.run_attempt(
            [
                sys.executable,
                "-c",
                "print('429'); print('Retry-After: 900'); raise SystemExit(1)",
            ],
            retry=1,
            restart_on_retry=True,
            max_retry_wait_seconds=0,
        )
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["retry_stopped"], "retry wait budget exceeded")
        self.assertEqual(
            classify("model at capacity until 15:31")["available_at"], None
        )
        self.assertEqual(classify("429\nRetry-After: 5")["retry_after_seconds"], 5)
        with self.assertRaisesRegex(ValueError, "requires"):
            self.run_attempt([sys.executable, "-c", "pass"], retry=1)

    def test_single_update_merges_with_optional_unverified_and_exact_commit(self):
        record = {
            "commit": "a" * 40,
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
            "unverified": ["Windows device unverified"],
        }
        task = update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="merged",
            branch="feature",
            commit="a" * 40,
            verification_data=record,
        )
        self.assertEqual(task["status"], "merged")
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="merged",
            commit="b" * 40,
        )
        self.assertIn(
            "no verification bound to the current commit",
            detail_view(self.data, "sample", "task")["evidence_gaps"],
        )
        self.assertEqual(
            detail_view(self.data, "sample", "task")["verification_records"][0][
                "commit"
            ],
            "a" * 40,
        )

    def test_missing_required_check_records_evidence_gap_without_blocking_metadata(
        self,
    ):
        record = {
            "commit": "a" * 40,
            "actor": "worker",
            "checks": [
                {
                    "name": "build",
                    "command": "make",
                    "result": "not run",
                    "status": "not-run",
                    "required": True,
                }
            ],
        }
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="merged",
            branch="feature",
            commit="a" * 40,
            verification_data=record,
            report={"text": "report"},
        )
        details = detail_view(self.data, "sample", "task")
        self.assertIn(
            "required checks missing, failed or not run", details["evidence_gaps"]
        )
        self.assertEqual(
            list_tasks(self.data, project_id="sample")[0]["status"], "merged"
        )

    def test_report_attachment_is_bounded_and_expandable(self):
        file = self.root / "report"
        file.write_text("Changed parser; tests PASS; device tests not run.")
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="ready-for-review",
            report=read_report(file),
            body="Implement bounded parser",
        )
        self.assertNotIn("text", detail_view(self.data, "sample", "task")["report"])
        self.assertEqual(
            detail_view(self.data, "sample", "task", True)["report"]["text"],
            file.read_text(),
        )

    def test_card_projection_follows_out_of_order_updates(self):
        from continuity_plane.card_todo import enable
        from continuity_plane.intent_todo import (
            load_queue,
            complete_item,
            queue_path,
        )

        view = enable(self.root, self.data)
        self.assertNotIn("legacy_items", view)
        self.assertNotIn("legacy_items", load_queue(self.root, self.data))
        self.assertIn(
            "legacy_items", json.loads(queue_path(self.root, self.data).read_text())
        )
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="blocked",
            blocked_reason="external",
        )
        self.assertEqual(
            load_queue(self.root, self.data)["items"][0]["status"], "blocked"
        )
        complete_item(self.root, "card:task", result="PASS", data_root=self.data)
        self.assertEqual(
            list_tasks(self.data, project_id="sample")[0]["status"], "ready-for-review"
        )
        self.assertEqual(load_queue(self.root, self.data)["status"], "completed")

    def test_prompt_rejects_empty_body_and_binds_rules_and_task(self):
        from continuity_plane.collaboration_workflow import prompt
        from continuity_plane.collaboration_context import add_rule

        with self.assertRaisesRegex(ValueError, "body is empty"):
            prompt(self.data, project_id="sample", task_id="task")
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="active",
            body="Implement task A",
        )
        add_rule(
            self.data,
            project_id="sample",
            rule_id="rule",
            text="Never merge without review",
        )
        value = prompt(self.data, project_id="sample", task_id="task")
        self.assertIn("Never merge", value["prompt"])
        self.assertIn("Implement task A", value["prompt"])
        self.assertFalse(value["rules_omitted"])
        self.assertTrue(
            prompt(
                self.data,
                project_id="sample",
                task_id="task",
                known_rules_digest=value["rules_digest"],
            )["rules_omitted"]
        )

    def test_batch_requires_existing_cards_and_bound_approval(self):
        from continuity_plane.collaboration_workflow import record

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
            "exception_decisions": ["approval"],
        }
        with self.assertRaisesRegex(ValueError, "explicit approved"):
            record(
                self.data,
                project_id="sample",
                kind="batch",
                record_id="batch",
                value=value,
            )
        record(
            self.data,
            project_id="sample",
            kind="decision",
            record_id="approval",
            value={
                "actor": "owner",
                "source": "test-only:user-message:1",
                "text": "Allow the scoped exception",
                "approved": True,
                "commit": "b" * 40,
            },
        )
        result = record(
            self.data, project_id="sample", kind="batch", record_id="batch", value=value
        )
        self.assertEqual(result["value"]["task_ids"], ["task"])

    def test_runtime_routing_counts_runs_and_temporary_unavailability(self):
        from continuity_plane.runtime_routing import apply
        from continuity_plane.executor_routing import recommend, _model_alias
        from continuity_plane.collaboration_registry import _record_change
        from datetime import UTC, datetime, timedelta

        self.assertEqual(_model_alias("codex-xai/grok-4.7"), "grok")
        self.assertEqual(_model_alias("codex-gpt-6.1-sol"), "sol")
        for n in range(5):
            _record_change(
                self.data,
                "sample",
                "attempt.started",
                "task",
                attempt_id=str(n),
                model="sonnet",
            )
            _record_change(
                self.data,
                "sample",
                "attempt.finished",
                "task",
                attempt_id=str(n),
                model="sonnet",
                status="completed",
                elapsed_seconds=10,
            )
        _record_change(
            self.data,
            "sample",
            "attempt.finished",
            "task",
            attempt_id="grok",
            model="grok",
            retryable=True,
            available_at=(datetime.now(UTC) + timedelta(seconds=60)).isoformat(),
        )
        result = apply(self.data, "sample", recommend())
        self.assertNotEqual(result["candidates"][0], "sonnet")
        self.assertNotIn("grok", result["candidates"])
        self.assertEqual(result["allocation"]["counts_before"]["sonnet"], 5)

    def test_usage_incremental_dedup_and_no_message_body_persistence(self):
        from continuity_plane.provider_usage import ingest, report, path_for

        file = self.root / "claude.jsonl"
        message = {
            "message": {
                "id": "msg-1",
                "content": "SECRET BODY",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 3,
                    "cache_read_input_tokens": 20,
                    "cache_creation_input_tokens": 5,
                },
            }
        }
        file.write_text(json.dumps(message) + "\n" + json.dumps(message) + "\n")
        result = ingest(
            self.root,
            transcript=file,
            provider="claude",
            session_id="session",
            data_root=self.data,
        )
        self.assertTrue(result["provider_usage_available"])
        self.assertEqual(
            ingest(
                self.root,
                transcript=file,
                provider="claude",
                session_id="session",
                data_root=self.data,
            )["read_bytes"],
            0,
        )
        usage = report(self.root, self.data)
        self.assertEqual(usage["by_provider"]["claude"]["tokens"]["input_tokens"], 10)
        self.assertNotIn("SECRET BODY", path_for(self.root, self.data).read_text())
        codex = self.root / "codex.jsonl"
        codex.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 70,
                                "output_tokens": 10,
                            }
                        },
                    }
                }
            )
            + "\n"
        )
        ingest(
            self.root,
            transcript=codex,
            provider="codex",
            session_id="codex",
            data_root=self.data,
        )
        self.assertEqual(
            report(self.root, self.data)["by_provider"]["codex"]["tokens"][
                "cache_read_tokens"
            ],
            70,
        )

    def test_remote_bridge_maps_root_never_sends_transcript_and_falls_back(self):
        from continuity_plane.remote_hook import forward
        import subprocess

        config = {
            "host": "build-host",
            "local_root": str(self.root),
            "remote_root": "/srv/project",
            "local_data_root": str(self.data),
        }
        payload = {
            "cwd": str(self.root),
            "session_id": "bridge",
            "hook_event_name": "PreCompact",
            "next_action": "Run precise test",
            "transcript_path": "/private/raw.jsonl",
        }

        def remote(argv, **kwargs):
            sent = json.loads(kwargs["input"])
            self.assertNotIn("transcript_path", sent)
            self.assertEqual(sent["cwd"], "/srv/project")
            return subprocess.CompletedProcess(argv, 0, "", "")

        forward(payload, config, runner=remote)

        def unavailable(*args, **kwargs):
            raise OSError("offline")

        result = forward(
            {**payload, "hook_event_name": "SessionStart", "source": "compact"},
            config,
            runner=unavailable,
        )
        self.assertTrue(result["continue"])
        self.assertIn(
            "Run precise test", result["hookSpecificOutput"]["additionalContext"]
        )

    def test_worker_report_is_attached_at_finish_and_old_report_is_rejected(self):
        file = self.root / "worker-report"
        result = self.run_attempt(
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(file)!r}).write_text('Changed parser; unit PASS')",
            ],
            report_file=str(file),
        )
        self.assertEqual(result["status"], "completed")
        # The worker's report comes first; the supervisor appends facts from git.
        self.assertTrue(
            detail_view(self.data, "sample", "task", True)["report"]["text"].startswith(
                "Changed parser; unit PASS"
            )
        )
        result = self.run_attempt([sys.executable, "-c", "pass"], report_file=str(file))
        self.assertIn("predates", result["report_error"])

    def test_cursor_repair_keeps_exact_backup_and_corruption_is_not_discarded(self):
        from continuity_plane.event_repair import repair
        from continuity_plane.collaboration_registry import _changes_path

        file = _changes_path(self.data, "sample")
        original = b'{"cursor":1,"event":"first"}\n{"cursor":1,"event":"second"}\n'
        file.write_bytes(original)
        self.assertEqual(repair(self.data, project_id="sample")["changed_cursors"], 1)
        self.assertEqual(file.read_bytes(), original)
        result = repair(self.data, project_id="sample", apply=True)
        self.assertEqual(Path(result["backup"]).read_bytes(), original)
        self.assertEqual(
            [
                r["cursor"]
                for r in list_changes(self.data, project_id="sample")["changes"]
            ],
            [1, 2],
        )
        self.assertEqual(
            repair(self.data, project_id="sample", apply=True)["changed_cursors"], 0
        )
        file.write_bytes(b"not-json\n")
        with self.assertRaisesRegex(ValueError, "corrupt event"):
            repair(self.data, project_id="sample", apply=True)
        self.assertEqual(file.read_bytes(), b"not-json\n")

    def test_reused_local_pid_is_reconciled_without_signalling_the_new_process(self):
        from continuity_plane.supervisor import daemon_once
        from continuity_plane.execution_lifecycle import bind_execution
        from continuity_plane.collaboration_registry import _record_change
        from unittest.mock import patch
        import socket

        bind_execution(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            attempt_id="old",
        )
        _record_change(
            self.data,
            "sample",
            "attempt.started",
            "task",
            attempt_id="old",
            assignee="worker",
            pid=123,
            host_id=socket.gethostname(),
            process_identity="boot:old",
        )
        with (
            patch("continuity_plane.supervisor._alive", return_value=True),
            patch(
                "continuity_plane.supervisor._process_identity",
                return_value="boot:new",
            ),
        ):
            self.assertEqual(
                daemon_once(self.data, project_id="sample")["reconciled"], ["old"]
            )

    def test_profile_installer_preserves_unrelated_settings_and_is_idempotent(self):
        from continuity_plane.remote_hook import install_profile

        profile = self.root / "profile"
        profile.mkdir()
        settings = profile / "settings.json"
        original = b'{"env":{"EXAMPLE":"preserve"},"permissions":{"allow":["Read"]}}\n'
        settings.write_bytes(original)
        config = self.root / "bridge.json"
        config.write_text(json.dumps({"host": "host", "mappings": []}))
        executable = self.root / "continuity-remote-hook"
        executable.touch()
        result = install_profile(profile, config, executable)
        self.assertEqual(Path(result["backup"]).read_bytes(), original)
        updated = json.loads(settings.read_text())
        self.assertEqual(updated["env"], {"EXAMPLE": "preserve"})
        self.assertEqual(updated["permissions"], {"allow": ["Read"]})
        self.assertFalse(install_profile(profile, config, executable)["changed"])

    def test_remote_usage_snapshot_and_compaction_windows_do_not_double_count(self):
        from continuity_plane.provider_usage import (
            import_snapshot,
            report,
            capture_compaction,
        )
        import hashlib

        session = hashlib.sha256(b"session").hexdigest()
        snapshot = {
            "provider": "claude",
            "session": session,
            "tokens": {"input_tokens": 100, "output_tokens": 10},
            "task_id": None,
        }
        import_snapshot(self.root, snapshot, self.data)
        import_snapshot(self.root, snapshot, self.data)
        self.assertEqual(
            report(self.root, self.data)["by_session"][session]["input_tokens"], 100
        )
        capture_compaction(self.root, "session", "first", self.data)
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="ready-for-review",
            result="PASS",
        )
        import_snapshot(
            self.root,
            {**snapshot, "tokens": {"input_tokens": 150, "output_tokens": 20}},
            self.data,
        )
        capture_compaction(self.root, "session", "second", self.data)
        value = report(self.root, self.data)["compaction_windows"][-1]
        self.assertEqual(value["tokens_since_previous"]["input_tokens"], 50)
        self.assertEqual(value["completed_tasks_since_previous"], 1)
        capture_compaction(self.root, "session", "second", self.data)
        self.assertEqual(len(report(self.root, self.data)["compaction_windows"]), 2)

    def test_doctor_finds_registered_git_plugin_cache(self):
        from continuity_plane.cli import _codex_continuity_hook_runtime

        base = self.root / "codex"
        plugin = base / "plugins/cache/continuity-plane/continuity-plane/0.1.0-alpha.23"
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin/plugin.json").write_text(
            json.dumps({"name": "continuity-plane", "version": "0.1.0-alpha.23"})
        )
        (plugin / "hooks").mkdir()
        (plugin / "hooks/hooks.json").write_text(json.dumps({"hooks": {}}))
        value = _codex_continuity_hook_runtime(
            base,
            {
                "marketplaces": {
                    "continuity-plane": {
                        "source": "https://example.org/plugin.git",
                        "ref": "v0.1.0-alpha.23",
                    }
                }
            },
        )
        self.assertNotEqual(value.get("issue"), "plugin_runtime_unresolved")


if __name__ == "__main__":
    unittest.main()
