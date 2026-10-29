"""Alpha36 card and dispatch fixes from the a35 field report (workstream 2C)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from continuity_plane.cli import main
from continuity_plane.collaboration_registry import (
    add_task,
    claim_task,
    list_changes,
    list_tasks,
    register_project,
    update_task,
)
from continuity_plane.delivery_records import detail_view
from continuity_plane.retry_policy import classify, provider_error_text
from continuity_plane.worker_dispatch import (
    configure,
    dispatch,
    model_handoff as worker_handoff,
)


class A36Base(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.data = self.tmp / "data"
        # Never let a test fall through to the live Continuity data root.
        patcher = mock.patch.dict(
            os.environ,
            {"CONTINUITY_DATA_ROOT": str(self.data), "TMPDIR": str(self.tmp)},
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in (
            "CONTINUITY_TASK_ID",
            "CONTINUITY_ROLE",
            "CONTINUITY_PROJECT_ID",
            "CONTINUITY_ASSIGNEE",
        ):
            os.environ.pop(key, None)
        register_project(self.data, project_id="sample", control_root=self.root)
        self.add("task")

    def add(self, task_id, **overrides):
        values = dict(
            project_id="sample",
            task_id=task_id,
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
        values.update(overrides)
        return add_task(self.data, **values)

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(list(argv))
        text = out.getvalue().strip()
        try:
            return code, json.loads(text)
        except ValueError:
            return code, text

    def task_cli(self, action, *argv):
        return self.cli(
            "collaboration", "task", action,
            "--data-root", str(self.data), "--project-id", "sample", *argv,
        )

    def adapter(self, model, code):
        return configure(
            self.data,
            model=model,
            provider="custom",
            argv=[sys.executable, "-c", code, "{effort}"],
            resume_argv=[sys.executable, "-c", "pass", "{thread_id}"],
        )


class PolicyMisclassificationTests(A36Base):
    CODEX_LINES = (
        "import json;"
        "print(json.dumps({'type':'item.completed','item':{'type':'command_execution',"
        "'command':'cat src.py','aggregated_output':"
        "'# content policy filter\\nclass PolicyViolation: cyber_policy = True\\n'}}));"
        "print(json.dumps({'type':'error','message':'exceeded retry limit, last status: 429 Too Many Requests'}));"
        "print(json.dumps({'type':'turn.failed','error':{'message':'exceeded retry limit, last status: 429 Too Many Requests'}}));"
        "raise SystemExit(1)"
    )

    def test_tool_output_with_policy_words_does_not_make_a_429_a_policy_refusal(self):
        self.adapter("sol", self.CODEX_LINES)
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_class"], "rate-limit")
        self.assertNotEqual(result.get("reason"), "provider-policy-review")

    def test_provider_error_text_drops_tool_and_message_events_keeps_plain_text(self):
        tool = json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "aggregated_output": "policy_violation"},
            }
        )
        error = json.dumps({"type": "error", "message": "429 Too Many Requests"})
        text = provider_error_text("\n".join([tool, error, "plain stderr cyber_policy"]))
        self.assertNotIn("policy_violation", text)
        self.assertIn("429 Too Many Requests", text)
        self.assertIn("cyber_policy", text)
        self.assertEqual(classify(text)["failure_class"], "policy-review")

    def test_real_policy_error_event_is_still_policy_review(self):
        self.adapter(
            "sol",
            "import json;print(json.dumps({'type':'turn.failed','error':{'type':'cyber_policy','message':'blocked'}}));raise SystemExit(1)",
        )
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["failure_class"], "policy-review")


class CardBodyRevisionTests(A36Base):
    def test_local_update_revises_body_and_definition_fields(self):
        before = detail_view(self.data, "sample", "task")
        self.assertEqual(before["body_revision"], 1)
        code, task = self.task_cli(
            "update", "--task-id", "task", "--body", "Second body",
            "--title", "New title", "--objective", "New objective",
            "--priority", "p0", "--exit-criterion", "A", "--exit-criterion", "B",
            "--actor", "worker",
        )
        self.assertEqual(code, 0)
        self.assertEqual(task["title"], "New title")
        self.assertEqual(task["objective"], "New objective")
        self.assertEqual(task["priority"], "p0")
        self.assertEqual(task["exit_criteria"], ["A", "B"])
        detail = detail_view(self.data, "sample", "task")
        self.assertEqual(detail["body"], "Second body")
        self.assertEqual(detail["body_revision"], 2)
        self.assertEqual(
            detail["body_sha256"], hashlib.sha256(b"Second body").hexdigest()
        )
        events = [c for c in list_changes(self.data, project_id="sample")["changes"]
                  if c["event"] == "task.updated"]
        self.assertEqual(events[-1]["body_revision"], 2)

    def test_identical_body_does_not_bump_revision_and_blank_is_rejected(self):
        self.task_cli("update", "--task-id", "task", "--body", "Implement the bounded parser")
        self.assertEqual(detail_view(self.data, "sample", "task")["body_revision"], 1)
        with self.assertRaisesRegex(ValueError, "task body is empty"):
            self.task_cli("update", "--task-id", "task", "--body", "   ")

    def test_body_file_revision_matches_expected_sha(self):
        file = self.tmp / "body.md"
        file.write_text("From a file")
        code, _ = self.task_cli(
            "update", "--task-id", "task", "--body-file", str(file),
            "--expected-body-sha256", hashlib.sha256(b"From a file").hexdigest(),
        )
        self.assertEqual(code, 0)
        self.assertEqual(detail_view(self.data, "sample", "task")["body_revision"], 2)

    def test_task_add_without_a_body_fails_at_creation(self):
        with self.assertRaisesRegex(ValueError, "task body is required"):
            self.task_cli(
                "add", "--task-id", "nobody", "--lane-id", "lane", "--mode", "patch",
                "--priority", "p1", "--title", "T", "--objective", "O",
                "--next-action", "N", "--exit-criterion", "PASS",
            )
        code, task = self.task_cli(
            "add", "--task-id", "withbody", "--lane-id", "lane", "--mode", "patch",
            "--priority", "p1", "--title", "T", "--objective", "O",
            "--next-action", "N", "--exit-criterion", "PASS", "--body", "Do it",
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            detail_view(self.data, "sample", "withbody")["body_sha256"],
            hashlib.sha256(b"Do it").hexdigest(),
        )

    def test_dispatch_records_the_body_revision_and_sha(self):
        self.task_cli("update", "--task-id", "task", "--body", "Second body")
        self.adapter("sol", "pass")
        plan = dispatch(self.root, data_root=self.data, task_id="task", model="sol", dry_run=True)
        self.assertEqual(plan["body_sha256"], hashlib.sha256(b"Second body").hexdigest())
        self.assertEqual(plan["body_revision"], 2)


class DependencyAtCreationTests(A36Base):
    def test_incomplete_dependency_is_declared_at_add_and_card_stays_queued(self):
        task = self.add("child", dependencies=["task"], assignee="worker")
        self.assertEqual(task["status"], "queued")
        self.assertEqual(task["assignee"], "worker")
        self.assertEqual(task["dependencies"], ["task"])
        # A dependency card that does not exist yet can be declared too.
        later = self.add("later", dependencies=["not-yet"], assignee="worker")
        self.assertEqual(later["status"], "queued")

    def test_gates_hold_until_the_dependency_completes(self):
        self.add("child", dependencies=["task"], assignee="worker")
        with self.assertRaisesRegex(ValueError, "dependencies are not complete"):
            update_task(self.data, project_id="sample", task_id="child", status="active")
        with self.assertRaisesRegex(ValueError, "dependencies are not complete"):
            claim_task(self.data, project_id="sample", task_id="child", assignee="worker")
        self.adapter("sol", "pass")
        with self.assertRaisesRegex(ValueError, "dependencies are not complete"):
            dispatch(self.root, data_root=self.data, task_id="child", model="sol", dry_run=True)
        _, listed = self.task_cli("list", "--dependencies-ready")
        self.assertNotIn("child", json.dumps(listed))
        update_task(self.data, project_id="sample", task_id="task", status="completed")
        _, listed = self.task_cli("list", "--dependencies-ready")
        self.assertIn("child", json.dumps(listed))
        claimed = claim_task(self.data, project_id="sample", task_id="child", assignee="worker")
        self.assertEqual(claimed["status"], "active")

    def test_dependencies_can_be_declared_on_a_queued_card_and_not_on_itself(self):
        self.add("child", assignee=None)
        updated = update_task(self.data, project_id="sample", task_id="child", dependencies=["task"])
        self.assertEqual(updated["status"], "queued")
        with self.assertRaisesRegex(ValueError, "cannot depend on itself"):
            self.add("loop", dependencies=["loop"])


class UsabilityErrorTests(A36Base):
    def acquire(self, *argv):
        return self.cli(
            "controller", "acquire", "--data-root", str(self.data),
            "--project-id", "sample", "--owner", "controller", *argv,
        )

    def test_controller_acquire_honours_an_explicit_token(self):
        token = "a" * 8 + "-explicit-token-0123"
        _, lease = self.acquire("--token", token)
        self.assertEqual(lease["token"], token)
        from continuity_plane.controller_lease import require

        self.assertTrue(require(self.data, "sample", "controller", token))

    def test_controller_acquire_rejects_a_malformed_token_clearly(self):
        with self.assertRaisesRegex(ValueError, "controller token must be"):
            self.acquire("--token", "bad token!")

    def test_controller_acquire_prints_the_active_token_in_both_formats(self):
        code, lease = self.acquire()
        self.assertEqual(code, 0)
        self.assertRegex(lease["token"], r"^[0-9a-f]{32}$")
        # The same owner re-acquiring its live lease with the right token
        # returns the same token; brief output must not hide it.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main([
                "controller", "acquire", "--data-root", str(self.data),
                "--project-id", "sample", "--owner", "controller",
                "--token", lease["token"], "--format", "brief",
            ])
        self.assertIn(lease["token"], out.getvalue())

    def test_dispatch_with_a_missing_worktree_fails_before_spawning(self):
        self.add("gone", worktree=self.tmp / "no-such-checkout")
        self.adapter("sol", "raise SystemExit('must not launch')")
        with self.assertRaisesRegex(
            ValueError, "worktree directory does not exist: .*no-such-checkout"
        ):
            dispatch(self.root, data_root=self.data, task_id="gone", model="sol")

    def test_review_mode_is_accepted_and_invalid_values_are_listed(self):
        task = self.add("rv", mode="review")
        self.assertEqual(task["mode"], "review")
        with self.assertRaisesRegex(ValueError, "valid modes: .*review.*scout"):
            self.add("bad", mode="audit")
        with self.assertRaisesRegex(ValueError, "valid priorities: p0, p1, p2"):
            self.add("badp", priority="p9")
        with self.assertRaisesRegex(ValueError, "valid statuses: .*queued"):
            update_task(self.data, project_id="sample", task_id="task", status="nope")


class QuotaResetParsingTests(unittest.TestCase):
    MESSAGE = (
        "rate limit exceeded: Weekly/Monthly Limit Exhausted. "
        "Your limit will reset at 2026-10-29 15:31:01"
    )

    def local_tz(self, name):
        saved = os.environ.get("TZ")

        def restore():
            if saved is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = saved
            time.tzset()

        self.addCleanup(restore)
        os.environ["TZ"] = name
        time.tzset()

    def test_timezone_less_reset_is_read_as_host_local_time(self):
        self.local_tz("Asia/Shanghai")
        now = datetime(2026, 10, 29, 6, 31, 1, tzinfo=UTC)
        value = classify(self.MESSAGE, now=now)
        self.assertEqual(value["failure_class"], "quota-exhausted")
        # 15:31:01 in UTC+8 is 07:31:01Z, one hour after `now`.
        self.assertEqual(value["retry_after_seconds"], 3600)
        self.assertTrue(value["available_at"].startswith("2026-10-29T07:31:01"))

    def test_explicit_offsets_and_a_past_reset_are_honoured(self):
        self.local_tz("America/Los_Angeles")
        now = datetime(2026, 10, 29, 0, 0, 0, tzinfo=UTC)
        offset = classify(
            "Weekly Limit Exhausted. Your limit will reset at 2026-10-29 08:00:00 +08:00",
            now=now,
        )
        self.assertEqual(offset["retry_after_seconds"], 0)
        past = classify(self.MESSAGE, now=datetime(2026, 11, 1, tzinfo=UTC))
        self.assertEqual(past["retry_after_seconds"], 0)

    def test_pause_expires_at_the_parsed_reset(self):
        from continuity_plane.dispatch_health import record, require_available
        from continuity_plane.runtime_origin import current

        self.local_tz("UTC")
        with tempfile.TemporaryDirectory() as data:
            origin = current("custom")
            failure = classify(self.MESSAGE, now=datetime(2026, 10, 29, 12, tzinfo=UTC))
            item = record(data, "sample", origin, "sol", failure, task_id="t")
            self.assertTrue(item["available_at"].startswith("2026-10-29T15:31:01"))
            with self.assertRaisesRegex(ValueError, "reset=2026-10-29T15:31:01"):
                require_available(
                    data, "sample", origin, "sol", now=datetime(2026, 10, 29, 13, tzinfo=UTC)
                )
            require_available(
                data, "sample", origin, "sol", now=datetime(2026, 10, 29, 16, tzinfo=UTC)
            )


class WorkerHandoffTests(A36Base):
    def setUp(self):
        super().setUp()
        git = lambda *a: subprocess.run(
            ["git", "-C", str(self.root), *a], check=True, capture_output=True
        )
        git("init", "-q", "-b", "feature/x")
        git("config", "user.email", "t@example.test")
        git("config", "user.name", "t")
        (self.root / "a.txt").write_text("one\n")
        git("add", "a.txt")
        git("commit", "-qm", "init")
        (self.root / "a.txt").write_text("one\ntwo\n")
        (self.root / "new.txt").write_text("fresh\n")
        self.head = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.prompt_out = self.tmp / "prompt.out"
        self.adapter(
            "sol",
            "import json;"
            "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Half done: parser written, tests not run'}}));"
            "raise SystemExit(0)",
        )
        configure(
            self.data,
            model="sonnet",
            provider="custom",
            argv=[
                sys.executable, "-c",
                f"import sys,pathlib;pathlib.Path({str(self.prompt_out)!r}).write_text(sys.argv[1])",
                "{prompt}",
            ],
        )
        dispatch(self.root, data_root=self.data, task_id="task", model="sol")

    def handoff(self, **kwargs):
        return worker_handoff(
            self.root, data_root=self.data, task_id="task", to_model="sonnet", **kwargs
        )

    def test_handoff_starts_a_new_attempt_on_another_model_with_context(self):
        result = self.handoff(continuation="Run the parser tests next")
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["handoff_from"], result["handoff_to"]), ("sol", "sonnet"))
        prompt = self.prompt_out.read_text()
        self.assertIn(str(self.root), prompt)
        self.assertIn("feature/x", prompt)
        self.assertIn(self.head, prompt)
        self.assertIn("new.txt", prompt)  # git status --short
        self.assertIn("a.txt", prompt)  # diffstat
        self.assertIn("Half done: parser written", prompt)
        self.assertIn("Run the parser tests next", prompt)
        self.assertIn("sol", prompt)
        card = next(t for t in list_tasks(self.data, project_id="sample") if t["task_id"] == "task")
        self.assertEqual(card["assignee"], "worker-sonnet")
        event = [
            c for c in list_changes(self.data, project_id="sample")["changes"]
            if c["event"] == "dispatch.handoff"
        ][-1]
        self.assertEqual((event["from_model"], event["to_model"]), ("sol", "sonnet"))
        self.assertEqual(event["reason"], "explicit-handoff")

    def test_handoff_dry_run_previews_without_starting_or_reassigning(self):
        preview = self.handoff(dry_run=True)
        self.assertFalse(preview["executed"])
        self.assertEqual(preview["handoff"]["branch"], "feature/x")
        self.assertEqual(preview["handoff"]["head"], self.head)
        self.assertFalse(self.prompt_out.exists())
        card = next(t for t in list_tasks(self.data, project_id="sample") if t["task_id"] == "task")
        self.assertEqual(card["assignee"], "worker")

    def test_handoff_refuses_same_model_missing_run_and_unknown_model(self):
        with self.assertRaisesRegex(ValueError, "use worker resume"):
            worker_handoff(self.root, data_root=self.data, task_id="task", to_model="sol")
        with self.assertRaisesRegex(ValueError, "not configured"):
            worker_handoff(self.root, data_root=self.data, task_id="task", to_model="ghost")
        self.add("fresh")
        with self.assertRaisesRegex(ValueError, "no recorded run"):
            worker_handoff(self.root, data_root=self.data, task_id="fresh", to_model="sonnet")

    def test_handoff_needs_the_controller_lease(self):
        from continuity_plane.controller_lease import acquire

        lease = acquire(self.data, "sample", owner="controller")
        with self.assertRaises(ValueError):
            self.handoff(controller="intruder")
        self.assertFalse(self.prompt_out.exists())
        result = self.handoff(controller="controller", controller_token=lease["token"])
        self.assertEqual(result["status"], "completed")

    def test_handoff_is_exposed_to_cli_help_and_mcp_actions(self):
        from continuity_plane import agent_tools
        from continuity_plane.planning_api import CLI_HELP

        self.assertIn("worker.handoff", CLI_HELP)
        self.assertEqual(agent_tools.CLI_OPERATIONS["worker.handoff"], ("worker", "handoff"))
        # Callable by name; listed only with CONTINUITY_MCP_TOOLSET=full.
        self.assertIn("continuity_worker_handoff", agent_tools.BY_NAME)
        from continuity_plane.codex_mcp_server import _STATE_TOOLS

        self.assertIn("continuity_worker_handoff", _STATE_TOOLS)
        schema = next(
            t for t in agent_tools.definitions() if t["name"] == "continuity_worker_handoff"
        )["inputSchema"]
        self.assertEqual(schema["required"], ["task_id", "to_model"])
        self.assertIn("launch", schema["properties"])
        preview = agent_tools.handle(
            "continuity_worker_handoff",
            {"task_id": "task", "to_model": "sonnet", "project_id": "sample", "root": str(self.root)},
        )
        self.assertFalse(preview["executed"])
        self.assertEqual(preview["handoff"]["from_model"], "sol")
        code, preview = self.cli(
            "worker", "handoff", "--root", str(self.root), "--data-root", str(self.data),
            "--project-id", "sample", "--task-id", "task", "--to-model", "sonnet", "--dry-run",
        )
        self.assertEqual(preview["handoff"]["to_model"], "sonnet")


class ModelConstraintTests(A36Base):
    def setUp(self):
        super().setUp()
        for model in ("sol", "sonnet", "luna"):
            self.adapter(model, "pass")
        configure(self.data, model="mystery", provider="custom", tier="strong", argv=[sys.executable, "-c", "pass"])

    def constrain(self, task_id="task", **fields):
        argv = ["--task-id", task_id]
        for flag, value in fields.items():
            for item in value if isinstance(value, list) else [value]:
                argv += [f"--{flag.replace('_', '-')}", item]
        return self.task_cli("update", *argv)

    def test_constraints_are_recorded_on_the_card_at_add_and_update(self):
        self.task_cli(
            "add", "--task-id", "c1", "--lane-id", "lane", "--mode", "patch", "--priority", "p1",
            "--title", "T", "--objective", "O", "--next-action", "N", "--exit-criterion", "PASS",
            "--body", "B", "--min-tier", "standard", "--deny-model", "luna",
        )
        self.assertEqual(
            detail_view(self.data, "sample", "c1")["model_constraints"],
            {"min_tier": "standard", "deny_models": ["luna"]},
        )
        self.constrain(min_tier="strong", deny_model=["luna", "glm"])
        self.assertEqual(
            detail_view(self.data, "sample", "task")["model_constraints"],
            {"min_tier": "strong", "deny_models": ["luna", "glm"]},
        )
        self.task_cli("update", "--task-id", "task", "--clear-model-constraints")
        self.assertNotIn("model_constraints", detail_view(self.data, "sample", "task"))
        with self.assertRaisesRegex(SystemExit, "2"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.constrain(min_tier="godlike")

    def test_dispatch_refuses_a_model_below_min_tier_and_lists_allowed_models(self):
        self.constrain(min_tier="strong")
        with self.assertRaisesRegex(
            ValueError,
            r"model luna \(light\) is below the min_tier strong.*allowed models: mystery \(strong\), sol \(strong\)",
        ):
            dispatch(self.root, data_root=self.data, task_id="task", model="luna", dry_run=True)
        with self.assertRaisesRegex(ValueError, "model sonnet \\(standard\\) is below"):
            dispatch(self.root, data_root=self.data, task_id="task", model="sonnet", dry_run=True)
        plan = dispatch(self.root, data_root=self.data, task_id="task", model="sol", dry_run=True)
        self.assertFalse(plan["executed"])
        # An operator tier on the adapter overrides the family default.
        dispatch(self.root, data_root=self.data, task_id="task", model="mystery", dry_run=True)

    def test_deny_model_applies_to_dispatch_handoff_and_queue(self):
        self.constrain(deny_model="luna")
        with self.assertRaisesRegex(ValueError, "model luna is denied for task.*allowed models:.*sol"):
            dispatch(self.root, data_root=self.data, task_id="task", model="luna", dry_run=True)
        from continuity_plane.dispatch_queue import enqueue

        with self.assertRaisesRegex(ValueError, "is denied for task"):
            enqueue(self.root, data_root=self.data, task_id="task", model="luna")
        dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        with self.assertRaisesRegex(ValueError, "is denied for task"):
            worker_handoff(self.root, data_root=self.data, task_id="task", to_model="luna")

    def test_denied_fallbacks_are_dropped_and_explicit_ones_refused(self):
        self.constrain(min_tier="standard", deny_model="sonnet")
        self.adapter("sol", "print('429 too many requests');raise SystemExit(1)")
        with self.assertRaisesRegex(ValueError, "model luna \\(light\\) is below"):
            dispatch(
                self.root, data_root=self.data, task_id="task", model="sol",
                fallback_models=["luna"], dry_run=True,
            )
        from continuity_plane.worker_policy import set_fallback

        set_fallback(self.data, "sample", model="sol", candidates=["luna", "sonnet", "mystery"])
        plan = dispatch(self.root, data_root=self.data, task_id="task", model="sol", dry_run=True)
        self.assertEqual(plan["fallback_models"], ["mystery"])


class RateLimitRetryTests(A36Base):
    """A short 429 retries the same session before any other model is tried."""

    THREAD = "print(json.dumps({'type':'thread.started','thread_id':'thread-1'}));"

    def setUp(self):
        super().setUp()
        self.resumed = self.tmp / "resumed"
        self.fell_back = self.tmp / "fell-back"

    def adapters(self, first, resume, fallback="pathlib.Path(%r).write_text('x')"):
        configure(
            self.data, model="sol", provider="custom",
            argv=[sys.executable, "-c", "import json,pathlib;" + self.THREAD + first, "{effort}"],
            resume_argv=[sys.executable, "-c", "import json,pathlib;" + resume, "{thread_id}"],
        )
        configure(
            self.data, model="sonnet", provider="custom",
            argv=[sys.executable, "-c", "import pathlib;" + fallback % str(self.fell_back), "{effort}"],
            resume_argv=[sys.executable, "-c", "pass", "{thread_id}"],
        )

    def run_dispatch(self, **kwargs):
        return dispatch(
            self.root, data_root=self.data, task_id="task", model="sol",
            fallback_models=["sonnet"], rate_limit_backoff_seconds=0, **kwargs,
        )

    def events(self, name):
        return [c for c in list_changes(self.data, project_id="sample")["changes"] if c["event"] == name]

    def test_short_rate_limit_resumes_the_same_model_before_any_fallback(self):
        self.adapters(
            "print('429 too many requests');raise SystemExit(1)",
            f"pathlib.Path({str(self.resumed)!r}).write_text('x');print(json.dumps({{'type':'turn.completed'}}))",
        )
        result = self.run_dispatch()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.resumed.exists())
        self.assertFalse(self.fell_back.exists())
        self.assertEqual(len(self.events("attempt.retry-scheduled")), 1)
        self.assertEqual(self.events("dispatch.fallback"), [])

    def test_persistent_rate_limit_falls_back_after_bounded_retries_with_clear_event(self):
        self.adapters(
            "print('429 too many requests');raise SystemExit(1)",
            "print('429 too many requests');raise SystemExit(1)",
        )
        result = self.run_dispatch(rate_limit_retries=2)
        self.assertEqual(len(self.events("attempt.retry-scheduled")), 2)
        self.assertTrue(self.fell_back.exists())
        self.assertEqual(result["status"], "completed")
        fallback = self.events("dispatch.fallback")[-1]
        self.assertEqual(fallback["message"], "fallback from sol (rate-limit) -> sonnet")
        self.assertEqual((fallback["from_model"], fallback["to_model"], fallback["reason"]), ("sol", "sonnet", "rate-limit"))

    def test_quota_exhaustion_moves_on_without_retrying_the_same_model(self):
        self.adapters(
            "print('Weekly Limit Exhausted. Your limit will reset at 2099-01-01 00:00:00 +00:00');raise SystemExit(1)",
            f"pathlib.Path({str(self.resumed)!r}).write_text('x')",
        )
        self.run_dispatch()
        self.assertFalse(self.resumed.exists())
        self.assertTrue(self.fell_back.exists())
        self.assertEqual(self.events("attempt.retry-scheduled"), [])
        self.assertIn("quota-exhausted", self.events("dispatch.fallback")[-1]["message"])

    def test_long_retry_after_is_not_waited_for(self):
        self.adapters(
            "print('429 too many requests retry-after: 7200');raise SystemExit(1)",
            f"pathlib.Path({str(self.resumed)!r}).write_text('x')",
        )
        self.run_dispatch()
        self.assertFalse(self.resumed.exists())
        self.assertTrue(self.fell_back.exists())

    def test_zero_rate_limit_retries_restores_immediate_fallback(self):
        self.adapters(
            "print('429 too many requests');raise SystemExit(1)",
            f"pathlib.Path({str(self.resumed)!r}).write_text('x')",
        )
        self.run_dispatch(rate_limit_retries=0)
        self.assertFalse(self.resumed.exists())
        self.assertTrue(self.fell_back.exists())


if __name__ == "__main__":
    unittest.main()
