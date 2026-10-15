import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from continuity_plane.collaboration_registry import (
    register_project,
    add_task,
    update_task,
    list_tasks,
    _record_change,
)


class AdoptionWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "repo"
        self.root.mkdir()
        self.data = Path(temp.name) / "data"
        register_project(self.data, project_id="sample", control_root=self.root)
        add_task(
            self.data,
            project_id="sample",
            task_id="task",
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="Implement parser",
            objective="Focused fix",
            next_action="Run tests",
            exit_criteria=["PASS"],
            assignee="worker",
            worktree=self.root,
            body="Implement the bounded parser",
        )

    def test_scoped_rules_attachments_versions_and_affected_workers(self):
        from continuity_plane.rules_delivery import (
            put_rule,
            bundle,
            attachment,
            affected,
        )

        policy = self.root / "policy.md"
        policy.write_text("Read current official sources.\n" * 100)
        put_rule(
            self.data,
            project_id=None,
            scope="global",
            rule_id="shared",
            text="Global preference",
        )
        put_rule(
            self.data,
            project_id="sample",
            rule_id="shared",
            text="Project override",
            audience="worker",
            attachment_file=policy,
        )
        put_rule(
            self.data,
            project_id="sample",
            rule_id="review",
            text="Review checklist",
            audience="reviewer",
        )
        initial = bundle(self.data, project_id="sample", audience="worker")
        self.assertEqual([r["text"] for r in initial["rules"]], ["Project override"])
        self.assertEqual(initial["rules"][0]["scope"], "project")
        digest = initial["rules"][0]["attachment"]["sha256"]
        first = attachment(self.data, project_id="sample", sha256=digest, max_chars=20)
        self.assertIsNotNone(first["next_offset_chars"])
        self.assertEqual(
            attachment(self.data, project_id="sample", sha256=digest, max_chars=65536)[
                "text"
            ],
            policy.read_text(),
        )
        _record_change(
            self.data,
            "sample",
            "attempt.started",
            "task",
            attempt_id="running",
            rules_digest=initial["rules_digest"],
            rules_audience="worker",
        )
        put_rule(
            self.data,
            project_id="sample",
            rule_id="shared",
            text="Updated preference",
            audience="worker",
            update=True,
        )
        self.assertEqual(
            affected(self.data, project_id="sample")["items"][0]["status"], "changed"
        )
        self.assertEqual(
            bundle(self.data, project_id="sample", audience="reviewer")["rules"][0][
                "rule_id"
            ],
            "review",
        )

    def test_policy_export_never_silently_truncates_and_snapshot_sync_detects_conflict(
        self,
    ):
        from continuity_plane.rules_delivery import (
            put_rule,
            bundle,
            snapshot_export,
            snapshot_import,
        )
        from continuity_plane.planning_api import execute, handle_tool

        for index in range(6):
            put_rule(
                self.data,
                project_id="sample",
                rule_id="r" + str(index),
                text=("Policy " + str(index) + " ") * 200,
                audience="worker",
            )
        with self.assertRaisesRegex(ValueError, "no policy was silently truncated"):
            execute(
                "rules.export",
                {"max_output_bytes": 1024},
                root=str(self.root),
                data_root=str(self.data),
            )
        reply = handle_tool(
            "continuity_plan",
            {
                "action": "rules.export",
                "root": str(self.root),
                "data_root": str(self.data),
                "max_output_bytes": 65536,
            },
        )
        text = reply["content"][0]["text"]
        for row in bundle(self.data, project_id="sample")["rules"]:
            self.assertIn(row["text"], text)
        export = snapshot_export(self.data, "sample")
        target = self.root.parent / "other-data"
        register_project(target, project_id="target", control_root=self.root)
        self.assertEqual(
            snapshot_import(target, "target", export["path"])["applied"], 6
        )
        put_rule(
            target,
            project_id="target",
            rule_id="r0",
            text="Local newer policy",
            audience="worker",
            update=True,
        )
        self.assertEqual(
            snapshot_import(target, "target", export["path"])["status"], "conflict"
        )
        self.assertEqual(
            snapshot_import(
                target,
                "target",
                export["path"],
                force=True,
                reason="Reviewed replacement",
            )["status"],
            "imported",
        )

    def test_dispatch_captures_thread_and_report_and_resumes_original_session(self):
        from continuity_plane.worker_dispatch import configure, dispatch

        code = "import json; print(json.dumps({'type':'thread.started','thread_id':'thread-original'})); print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Parser updated; unit tests PASS'}}))"
        resume = "import json,sys; assert sys.argv[1]=='thread-original'; print(json.dumps({'type':'result','session_id':sys.argv[1],'result':'Resumed original session; regression PASS'}))"
        configure(
            self.data,
            model="sol",
            provider="custom",
            argv=[sys.executable, "-c", code],
            resume_argv=[sys.executable, "-c", resume, "{thread_id}"],
        )
        preview = dispatch(
            self.root, data_root=self.data, task_id="task", model="sol", dry_run=True
        )
        self.assertFalse(preview["executed"])
        self.assertFalse(Path(preview["job_dir"]).exists())
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(Path(result["thread_file"]).read_text(), "thread-original")
        self.assertIn("unit tests PASS", Path(result["report_file"]).read_text())
        result = dispatch(self.root, data_root=self.data, task_id="task", resume=True)
        self.assertEqual(result["status"], "completed")
        self.assertIn("Resumed original", Path(result["report_file"]).read_text())

    def test_metadata_io_failure_does_not_stop_command_and_outbox_replays_once(self):
        from continuity_plane.supervisor import run_attempt
        from continuity_plane.execution_outbox import replay

        with patch.dict(
            os.environ, {"CONTINUITY_OUTBOX_ROOT": str(self.root.parent / "outbox")}
        ):
            with (
                patch(
                    "continuity_plane.supervisor.bind_execution",
                    side_effect=OSError("offline"),
                ),
                patch(
                    "continuity_plane.supervisor.finish_execution",
                    side_effect=OSError("offline"),
                ),
                patch(
                    "continuity_plane.execution_outbox._record_change",
                    side_effect=OSError("offline"),
                ),
            ):
                result = run_attempt(
                    self.data,
                    project_id="sample",
                    task_id="task",
                    assignee="worker",
                    command=[sys.executable, "-c", "pass"],
                )
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["card_projection"], "pending-outbox")
            self.assertGreater(replay(self.data, project_id="sample")["replayed"], 0)
            self.assertEqual(
                list_tasks(self.data, project_id="sample")[0]["status"],
                "ready-for-review",
            )
            self.assertEqual(replay(self.data, project_id="sample")["replayed"], 0)

    def test_changed_only_notification_and_controller_scene(self):
        from continuity_plane.controller_awareness import notice, scene
        from continuity_plane.collaboration_context import add_commitment

        payload = {
            "cwd": str(self.root),
            "session_id": "controller",
            "hook_event_name": "SessionStart",
        }
        self.assertIsNone(notice(payload, data_root=self.data))
        _record_change(
            self.data,
            "sample",
            "attempt.finished",
            "task",
            attempt_id="done",
            status="completed",
        )
        note = notice(
            {**payload, "hook_event_name": "PostToolUse"}, data_root=self.data
        )
        self.assertIn("1 worker ended", note)
        self.assertIsNone(notice(payload, data_root=self.data))
        add_commitment(
            self.data,
            project_id="sample",
            commitment_id="decision",
            text="Owner decision pending",
        )
        self.assertEqual(scene(self.data, "sample")["commitments_total"], 1)

    def test_force_requires_reason_and_preserves_evidence_gaps(self):
        from continuity_plane.delivery_records import detail_view

        with self.assertRaisesRegex(ValueError, "requires a recorded"):
            update_task(
                self.data,
                project_id="sample",
                task_id="task",
                status="merged",
                force=True,
            )
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="merged",
            force=True,
            force_reason="Record already-performed integration; tests remain pending",
        )
        details = detail_view(self.data, "sample", "task")
        self.assertTrue(details["evidence_gaps"])
        self.assertEqual(len(details["overrides"]), 1)

    def test_policy_refusal_never_retries_or_blindly_resumes(self):
        from continuity_plane.worker_dispatch import configure, dispatch
        from continuity_plane.collaboration_registry import list_changes

        code = "import json; print(json.dumps({'type':'thread.started','thread_id':'declined-thread'})); print(json.dumps({'error':{'type':'cyber_policy','code':400}})); raise SystemExit(1)"
        configure(
            self.data,
            model="sol",
            provider="custom",
            argv=[sys.executable, "-c", code],
            resume_argv=[sys.executable, "-c", "raise SystemExit(99)", "{thread_id}"],
        )
        result = dispatch(
            self.root, data_root=self.data, task_id="task", model="sol", retry=3
        )
        self.assertEqual(result["failure_class"], "policy-review")
        self.assertEqual(result["reason"], "provider-policy-review")
        starts = [
            r
            for r in list_changes(self.data, project_id="sample")["changes"]
            if r["event"] == "attempt.started"
        ]
        self.assertEqual(len(starts), 1)
        with self.assertRaisesRegex(ValueError, "blind resume is disabled"):
            dispatch(self.root, data_root=self.data, task_id="task", resume=True)
        with self.assertRaisesRegex(
            ValueError, "automatic fresh-session/provider replay is disabled"
        ):
            dispatch(self.root, data_root=self.data, task_id="task", model="sol")

    def test_followup_checklist_adds_cards_without_reopening_completed_work(self):
        from continuity_plane.card_todo import enable
        from continuity_plane.intent_todo import (
            compile_queue,
            load_queue,
            complete_item,
        )

        enable(self.root, self.data)
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="ready-for-review",
            result="PASS",
        )
        queue = complete_item(
            self.root, "card:task", result="PASS", data_root=self.data
        )
        self.assertEqual(queue["items"][0]["status"], "completed")
        compile_queue(self.root, "- Add focused regression", data_root=self.data)
        repeated = compile_queue(
            self.root, "- Add focused regression", data_root=self.data
        )
        self.assertEqual(len(repeated["items"]), 2)
        self.assertEqual(len(list_tasks(self.data, project_id="sample")), 2)
        self.assertEqual(
            load_queue(self.root, self.data)["items"][0]["status"], "completed"
        )

    def test_task_pages_preserve_all_rows_and_are_stable_during_updates(self):
        from continuity_plane.planning_api import execute

        for n in range(15):
            add_task(
                self.data,
                project_id="sample",
                task_id="queued-" + str(n),
                lane_id="lane",
                mode="patch",
                priority="p1",
                title="Queued item " + str(n),
                objective="Bounded task",
                next_action="Run",
                exit_criteria=["PASS"],
            )
        params = {"max_output_bytes": 1024}
        page = execute(
            "task.list", params, root=str(self.root), data_root=str(self.data)
        )
        seen = [r["task_id"] for r in page["items"]]
        self.assertIsNotNone(page["next_cursor"])
        update_task(
            self.data,
            project_id="sample",
            task_id="queued-0",
            status="blocked",
            blocked_reason="changed after snapshot",
        )
        while page["next_cursor"]:
            page = execute(
                "task.list",
                {**params, "cursor": page["next_cursor"]},
                root=str(self.root),
                data_root=str(self.data),
            )
            self.assertLessEqual(page["returned_bytes"], 1024)
            seen.extend(r["task_id"] for r in page["items"])
        self.assertEqual(len(seen), 16)
        self.assertEqual(len(set(seen)), 16)

    def test_usage_rotation_partial_lines_and_unknown_usage_are_explicit(self):
        from continuity_plane.provider_usage import ingest, report

        f = self.root / "usage.jsonl"
        one = {
            "message": {"id": "one", "usage": {"input_tokens": 10, "output_tokens": 2}}
        }
        f.write_text(json.dumps(one) + "\n")
        ingest(
            self.root,
            transcript=f,
            provider="claude",
            session_id="s",
            data_root=self.data,
        )
        f.rename(self.root / "usage.old")
        two = {
            "message": {"id": "two", "usage": {"input_tokens": 20, "output_tokens": 4}}
        }
        f.write_text(json.dumps(one) + "\n" + json.dumps(two))
        ingest(
            self.root,
            transcript=f,
            provider="claude",
            session_id="s",
            data_root=self.data,
        )
        self.assertEqual(
            report(self.root, self.data)["by_provider"]["claude"]["tokens"][
                "input_tokens"
            ],
            10,
        )
        with f.open("a") as out:
            out.write(
                "\n"
                + json.dumps(
                    {"message": {"id": "unknown", "usage": {"input_tokens": "unknown"}}}
                )
                + "\n"
            )
        ingest(
            self.root,
            transcript=f,
            provider="claude",
            session_id="s",
            data_root=self.data,
        )
        self.assertEqual(
            report(self.root, self.data)["by_provider"]["claude"]["tokens"][
                "input_tokens"
            ],
            30,
        )

    def test_policy_error_with_zero_process_exit_is_still_failed(self):
        from continuity_plane.worker_dispatch import configure, dispatch

        code = "import json; print(json.dumps({'type':'turn.failed','error':{'type':'cyber_policy','message':'provider declined'}}))"
        configure(
            self.data, model="sol", provider="custom", argv=[sys.executable, "-c", code]
        )
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["return_code"], 0)
        self.assertEqual(result["failure_class"], "policy-review")

    def test_remote_saved_handoff_is_mirrored_for_offline_postcompact(self):
        import hashlib
        import subprocess
        from continuity_plane.lifecycle import handle_event
        from continuity_plane.remote_hook import forward

        remote = self.root.parent / "remote"
        remote.mkdir()
        remote_data = self.root.parent / "remote-data"
        payload = {
            "cwd": str(self.root),
            "session_id": "portable",
            "hook_event_name": "PreCompact",
        }
        result = handle_event(
            {
                **payload,
                "cwd": str(remote),
                "next_action": "Verified remote next action",
            },
            host="claude",
            data_root=remote_data,
        )
        key = hashlib.sha256((str(remote) + ":portable").encode()).hexdigest()
        saved = json.loads((remote_data / "handoffs" / (key + ".json")).read_text())
        config = {
            "host": "state-host",
            "local_root": str(self.root),
            "remote_root": str(remote),
            "local_data_root": str(self.data),
        }

        def remote_runner(*args, **kwargs):
            return subprocess.CompletedProcess(
                args[0], 0, json.dumps({**result, "portable_handoff": saved}), ""
            )

        forward(payload, config, runner=remote_runner)

        def offline(*args, **kwargs):
            raise OSError("network unavailable")

        restored = forward(
            {**payload, "hook_event_name": "SessionStart", "source": "compact"},
            config,
            runner=offline,
        )
        self.assertIn(
            "Verified remote next action",
            restored["hookSpecificOutput"]["additionalContext"],
        )
        self.assertTrue(restored["continue"])

    def test_remote_cli_preserves_literal_argv_and_returns_scoped_receipts(self):
        import subprocess
        import io
        from continuity_plane.remote_cli import forward, receive_main

        literal = "$(not-a-shell-command)"

        def transport(argv, **kwargs):
            request = json.loads(kwargs["input"])
            self.assertIn(literal, request["argv"])
            self.assertNotIn(literal, " ".join(argv))
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"return_code": 0, "stdout": "ok\n", "stderr": ""}),
                "",
            )

        result = forward(
            [
                "--remote",
                "state",
                "--remote-root",
                "/srv/project",
                "collaboration",
                "task",
                "list",
                "--query",
                literal,
            ],
            host="state",
            root="/srv/project",
            runner=transport,
        )
        self.assertEqual(result["stdout"], "ok\n")
        request = {
            "root": str(self.root),
            "argv": [
                "collaboration",
                "task",
                "list",
                "--project-id",
                "other",
                "--data-root",
                str(self.data),
            ],
        }
        output = io.StringIO()
        with (
            patch(
                "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(request).encode()))
            ),
            patch("sys.stdout", output),
        ):
            self.assertEqual(receive_main(), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["return_code"], 2)
        self.assertIn("does not match", receipt["stderr"])

    def test_bridge_counts_controller_output_once_and_imports_cumulative_snapshot_once(
        self,
    ):
        import io
        from continuity_plane.remote_hook import main, _delivery_snapshot
        from continuity_plane.usage_meter import report, import_hook_snapshot

        config = self.root / "bridge.json"
        local = self.root.parent / "client-meter"
        config.write_text(
            json.dumps({"host": "state", "local_data_root": str(local), "mappings": []})
        )
        payload = {
            "cwd": str(self.root),
            "session_id": "controller-output",
            "hook_event_name": "UserPromptSubmit",
        }
        result = {
            "continue": True,
            "status": "generated",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "one context frame",
            },
        }
        output = io.StringIO()
        with (
            patch(
                "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()))
            ),
            patch("sys.stdout", output),
            patch("continuity_plane.remote_hook.forward", return_value=result),
        ):
            self.assertEqual(main(["--config", str(config)]), 0)
        self.assertEqual(set(json.loads(output.getvalue())), {"hookSpecificOutput"})
        counter = _delivery_snapshot(payload, {"local_data_root": str(local)})
        self.assertEqual(counter["calls"], 1)
        self.assertEqual(counter["hook_injected_bytes"], len("one context frame"))
        self.assertEqual(
            report(self.root, data_root=local)["hook_injected_bytes"],
            len("one context frame"),
        )
        import_hook_snapshot(self.root, counter, self.data)
        import_hook_snapshot(self.root, counter, self.data)
        self.assertEqual(
            report(self.root, data_root=self.data)["hook_injected_bytes"],
            len("one context frame"),
        )

    def test_body_file_digest_is_verified_before_task_mutation(self):
        import hashlib
        from continuity_plane.planning_api import execute
        from continuity_plane.delivery_records import detail_view

        file = self.root / "task.md"
        file.write_text("Implement the scoped parser and run focused tests.")
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        execute(
            "task.update",
            {
                "task_id": "task",
                "status": "active",
                "body_file": str(file),
                "expected_body_sha256": digest,
            },
            root=str(self.root),
            data_root=str(self.data),
        )
        before = detail_view(self.data, "sample", "task")["body"]
        file.write_text("Wrong task file")
        with self.assertRaisesRegex(ValueError, "does not match"):
            execute(
                "task.update",
                {
                    "task_id": "task",
                    "status": "active",
                    "body_file": str(file),
                    "expected_body_sha256": digest,
                },
                root=str(self.root),
                data_root=str(self.data),
            )
        self.assertEqual(detail_view(self.data, "sample", "task")["body"], before)

    def test_retry_does_not_reclaim_a_newer_attempt(self):
        import time
        from continuity_plane.supervisor import run_attempt
        from continuity_plane.execution_lifecycle import bind_execution
        from continuity_plane.collaboration_registry import _load_ledger

        original_sleep = time.sleep

        def pause(seconds):
            if seconds == 0:
                bind_execution(
                    self.data,
                    project_id="sample",
                    task_id="task",
                    assignee="worker",
                    attempt_id="new-owner",
                )
            else:
                original_sleep(seconds)

        with patch("continuity_plane.supervisor.time.sleep", side_effect=pause):
            result = run_attempt(
                self.data,
                project_id="sample",
                task_id="task",
                assignee="worker",
                command=[
                    sys.executable,
                    "-c",
                    "print('502 Bad Gateway'); raise SystemExit(1)",
                ],
                retry=1,
                restart_on_retry=True,
                retry_delay_seconds=0,
            )
        self.assertIn("superseded", result["retry_stopped"])
        self.assertEqual(
            _load_ledger(self.data, "sample")["execution_bindings"]["task"][
                "attempt_id"
            ],
            "new-owner",
        )

    def test_profile_upgrade_replaces_the_managed_hook_instead_of_duplicating_it(self):
        from continuity_plane.remote_hook import install_profile

        profile = self.root / "profile"
        profile.mkdir()
        config = self.root / "bridge-upgrade.json"
        config.write_text(json.dumps({"host": "state", "mappings": []}))
        old = self.root / "old/continuity-remote-hook"
        old.parent.mkdir()
        old.touch()
        new = self.root / "new/continuity-remote-hook"
        new.parent.mkdir()
        new.touch()
        install_profile(profile, config, old)
        install_profile(profile, config, new)
        settings = json.loads((profile / "settings.json").read_text())
        for entries in settings["hooks"].values():
            commands = [h["command"] for entry in entries for h in entry["hooks"]]
            self.assertEqual(len(commands), 1)
            self.assertIn(str(new), commands[0])

    def test_profile_config_migration_removes_only_explicitly_superseded_hooks(self):
        from continuity_plane.remote_hook import install_profile

        profile = self.root / "profile"
        profile.mkdir()
        executable = self.root / "continuity-remote-hook"
        executable.touch()
        old = self.root / "staging.json"
        other = self.root / "unrelated.json"
        new = self.root / "stable.json"
        for config in (old, other):
            config.write_text(json.dumps({"host": "state", "mappings": []}))
            install_profile(profile, config, executable)
        new.write_text(
            json.dumps(
                {"host": "state", "mappings": [], "supersedes_config_files": [str(old)]}
            )
        )
        first = install_profile(profile, new, executable)
        self.assertTrue(first["changed"])
        self.assertFalse(install_profile(profile, new, executable)["changed"])
        settings = json.loads((profile / "settings.json").read_text())
        for entries in settings["hooks"].values():
            commands = [h["command"] for entry in entries for h in entry["hooks"]]
            self.assertEqual(len(commands), 2)
            self.assertTrue(any(str(new) in cmd for cmd in commands))
            self.assertTrue(any(str(other) in cmd for cmd in commands))
            self.assertFalse(any(str(old) in cmd for cmd in commands))


if __name__ == "__main__":
    unittest.main()
