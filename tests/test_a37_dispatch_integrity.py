"""Alpha37 dispatch integrity: no lost work, no stale instructions, honest previews."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

from continuity_plane import agent_tools
from continuity_plane.collaboration_registry import (
    list_changes,
    list_tasks,
    update_task,
)
from continuity_plane.controller_lease import acquire
from continuity_plane.worker_dispatch import configure, dispatch, model_handoff

from tests.test_a36_card_dispatch import A36Base


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class GitBase(A36Base):
    def setUp(self):
        super().setUp()
        git(self.root, "init", "-q", "-b", "feature/x")
        git(self.root, "config", "user.email", "t@example.test")
        git(self.root, "config", "user.name", "t")
        (self.root / "a.txt").write_text("one\n")
        git(self.root, "add", "a.txt")
        git(self.root, "commit", "-qm", "init")
        (self.root / "a.txt").write_text("one\ntwo\n")
        (self.root / "new.txt").write_text("fresh\n")
        self.head = git(self.root, "rev-parse", "HEAD")
        self.prompt_out = self.tmp / "prompt.out"

    def recorder(self, model):
        configure(
            self.data,
            model=model,
            provider="custom",
            argv=[
                sys.executable,
                "-c",
                f"import sys,pathlib;pathlib.Path({str(self.prompt_out)!r}).write_text(sys.argv[1])",
                "{prompt}",
            ],
        )

    def card(self, task_id="task"):
        return next(
            t for t in list_tasks(self.data, project_id="sample") if t["task_id"] == task_id
        )

    def assert_continuation_context(self, prompt):
        self.assertIn(str(self.root), prompt)
        self.assertIn("feature/x", prompt)
        self.assertIn(self.head, prompt)
        self.assertIn("new.txt", prompt)
        self.assertIn("Latest instruction: rebase on round four", prompt)
        self.assertIn("Half done: parser written", prompt)


HALF_DONE = (
    "import json;"
    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Half done: parser written'}}));"
)


class HandoffContinuityTests(GitBase):
    def setUp(self):
        super().setUp()
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            next_action="Latest instruction: rebase on round four",
            actor="worker",
        )

    def test_automatic_fallback_carries_the_full_handoff_context(self):
        self.adapter("sol", HALF_DONE + "print('429 too many requests');raise SystemExit(1)")
        self.recorder("sonnet")
        # The report excerpt comes from the first attempt's report file.
        result = dispatch(
            self.root, data_root=self.data, task_id="task", model="sol",
            fallback_models=["sonnet"], rate_limit_retries=0,
        )
        self.assertEqual(result["status"], "completed")
        self.assert_continuation_context(self.prompt_out.read_text())

    def test_manual_handoff_and_fallback_use_one_builder(self):
        from continuity_plane import worker_dispatch

        self.assertTrue(hasattr(worker_dispatch, "build_handoff"))
        self.adapter("sol", HALF_DONE)
        self.recorder("sonnet")
        dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        preview = model_handoff(
            self.root, data_root=self.data, task_id="task", to_model="sonnet", dry_run=True
        )
        handoff = preview["handoff"]
        self.assertEqual(handoff["next_action"], "Latest instruction: rebase on round four")
        for key in ("branch", "head", "worktree", "uncommitted_status", "last_report_excerpt"):
            self.assertTrue(handoff[key], key)

    def test_manual_handoff_prompt_shows_the_latest_next_action(self):
        self.adapter("sol", HALF_DONE)
        self.recorder("sonnet")
        dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        model_handoff(self.root, data_root=self.data, task_id="task", to_model="sonnet")
        self.assert_continuation_context(self.prompt_out.read_text())


class FreshStartTests(GitBase):
    def setUp(self):
        super().setUp()
        self.adapter("sol", HALF_DONE)
        self.recorder("sonnet")

    def test_manual_reassignment_then_dispatch_refuses_a_fresh_start(self):
        dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        update_task(
            self.data, project_id="sample", task_id="task",
            assignee="worker-sonnet", actor="worker",
        )
        with self.assertRaisesRegex(ValueError, "worker handoff"):
            dispatch(
                self.root, data_root=self.data, task_id="task", model="sonnet",
                assignee="worker-sonnet",
            )
        self.assertFalse(self.prompt_out.exists())

    def test_card_with_a_commit_refuses_a_fresh_start_unless_forced(self):
        update_task(
            self.data, project_id="sample", task_id="task",
            commit="abc1234", actor="worker",
        )
        with self.assertRaisesRegex(ValueError, "--fresh"):
            dispatch(self.root, data_root=self.data, task_id="task", model="sonnet")
        forced = dispatch(
            self.root, data_root=self.data, task_id="task", model="sonnet", fresh=True
        )
        self.assertEqual(forced["status"], "completed")

    def test_preview_also_refuses_so_the_problem_shows_before_launch(self):
        dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        with self.assertRaisesRegex(ValueError, "worker handoff"):
            dispatch(
                self.root, data_root=self.data, task_id="task", model="sol", dry_run=True
            )

    def test_fresh_flag_is_a_cli_option(self):
        dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        code, _ = self.cli(
            "worker", "dispatch", "--data-root", str(self.data), "--project-id", "sample",
            "--task-id", "task", "--model", "sol", "--dry-run", "--fresh",
        )
        self.assertEqual(code, 0)


class FailedUpdateTests(GitBase):
    def setUp(self):
        super().setUp()
        self.recorder("sonnet")

    def failed_update(self, next_action="x" * 5000):
        with self.assertRaises(ValueError):
            self.task_cli("update", "--task-id", "task", "--next-action", next_action)

    def test_failed_update_blocks_launch_until_a_later_update_succeeds(self):
        self.failed_update()
        with self.assertRaisesRegex(ValueError, "last update of this card failed"):
            dispatch(self.root, data_root=self.data, task_id="task", model="sonnet")
        self.assertFalse(self.prompt_out.exists())
        code, _ = self.task_cli("update", "--task-id", "task", "--next-action", "Run tests", "--actor", "worker")
        self.assertEqual(code, 0)
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sonnet")
        self.assertEqual(result["status"], "completed")

    def test_ignore_flag_overrides_and_preview_shows_the_marker(self):
        self.failed_update()
        preview = dispatch(
            self.root, data_root=self.data, task_id="task", model="sonnet", dry_run=True
        )
        marker = preview["failed_update"]
        self.assertIn("next_action", marker["fields"])
        self.assertTrue(marker["error"])
        self.assertTrue(marker["at"])
        result = dispatch(
            self.root, data_root=self.data, task_id="task", model="sonnet",
            ignore_failed_update=True,
        )
        self.assertEqual(result["status"], "completed")

    def test_marker_does_not_leak_between_cards(self):
        self.add("other")
        self.failed_update()
        result = dispatch(self.root, data_root=self.data, task_id="other", model="sonnet")
        self.assertEqual(result["status"], "completed")

    def test_mcp_failed_update_blocks_mcp_launch(self):
        with self.assertRaises(ValueError):
            agent_tools.handle(
                "continuity_task_update",
                {"project_id": "sample", "task_id": "task", "next_action": "x" * 5000},
            )
        with self.assertRaisesRegex(ValueError, "last update of this card failed"):
            agent_tools.handle(
                "continuity_worker_dispatch",
                {"project_id": "sample", "task_id": "task", "model": "sonnet", "launch": True},
            )
        self.assertFalse(self.prompt_out.exists())

    def test_dispatch_result_shows_effective_next_action_age_and_body_sha(self):
        preview = dispatch(
            self.root, data_root=self.data, task_id="task", model="sonnet", dry_run=True
        )
        self.assertEqual(preview["effective_next_action"], "Run tests")
        self.assertGreaterEqual(preview["next_action_age_seconds"], 0)
        self.assertTrue(preview["next_action_updated_at"])
        self.assertEqual(len(preview["body_sha256"]), 64)


class PreviewTests(GitBase):
    def test_preview_reports_preview_and_leaves_the_card_status_unchanged(self):
        self.recorder("sonnet")
        before = self.card()["status"]
        value = agent_tools.handle(
            "continuity_worker_dispatch",
            {"project_id": "sample", "task_id": "task", "model": "sonnet"},
        )
        self.assertIs(value["preview"], True)
        self.assertFalse(value["executed"])
        self.assertNotEqual(value["status"], "dispatched")
        self.assertEqual(value["card_status"], before)
        self.assertEqual(self.card()["status"], before)
        self.assertFalse(self.prompt_out.exists())


class PolicyReviewTests(GitBase):
    POLICY = (
        "import json;print(json.dumps({'type':'turn.failed','error':"
        "{'type':'cyber_policy','message':'This content may violate our usage policy PASSKEY-WORDING'}}));"
        "raise SystemExit(1)"
    )

    def test_provider_message_is_in_the_job_result_the_event_and_the_report_error(self):
        self.adapter("sol", self.POLICY)
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["failure_class"], "policy-review")
        self.assertIn("PASSKEY-WORDING", result["provider_message"])
        stored = json.loads((Path(result["report_file"]).parent / "result.json").read_text())
        self.assertIn("PASSKEY-WORDING", stored["provider_message"])
        stops = [
            c for c in list_changes(self.data, project_id="sample")["changes"]
            if c["event"] == "dispatch.policy-stopped"
        ]
        self.assertIn("PASSKEY-WORDING", stops[-1]["provider_message"])
        with self.assertRaisesRegex(ValueError, "PASSKEY-WORDING"):
            self.cli(
                "worker", "report", "--data-root", str(self.data), "--project-id", "sample",
                "--task-id", "task",
            )

    def test_review_card_can_cross_model_family_after_a_policy_review(self):
        self.add("rv", mode="review", title="Review the parser")
        self.adapter("sol", self.POLICY)
        self.recorder("sonnet")
        result = dispatch(
            self.root, data_root=self.data, task_id="rv", model="sol",
            cross_family_on_policy_review=True,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.card("rv")["assignee"], "worker-sonnet")
        self.assertIn("PASSKEY-WORDING", self.prompt_out.read_text())
        events = [
            c for c in list_changes(self.data, project_id="sample")["changes"]
            if c["event"] == "dispatch.cross-family"
        ]
        self.assertEqual((events[-1]["from_model"], events[-1]["to_model"]), ("sol", "sonnet"))

    def test_cross_family_is_opt_in_and_limited_to_review_cards(self):
        self.add("rv", mode="review", title="Review the parser")
        self.adapter("sol", self.POLICY)
        self.recorder("sonnet")
        result = dispatch(self.root, data_root=self.data, task_id="rv", model="sol")
        self.assertEqual(result["failure_class"], "policy-review")
        self.assertFalse(self.prompt_out.exists())
        with self.assertRaisesRegex(ValueError, "review-mode cards only"):
            dispatch(
                self.root, data_root=self.data, task_id="task", model="sol",
                cross_family_on_policy_review=True,
            )

    def test_cross_family_never_picks_the_same_family(self):
        self.add("rv", mode="review", title="Review the parser")
        self.adapter("sol", self.POLICY)
        self.adapter("sol-b", self.POLICY)
        result = dispatch(
            self.root, data_root=self.data, task_id="rv", model="sol",
            cross_family_on_policy_review=True,
        )
        self.assertEqual(result["failure_class"], "policy-review")
        self.assertEqual(self.card("rv")["assignee"], "worker")


class ControllerTokenFileTests(GitBase):
    TOKEN = "tok_0123456789abcdef"

    def setUp(self):
        super().setUp()
        self.recorder("sonnet")
        acquire(self.data, "sample", owner="ctl", token=self.TOKEN)
        os.environ.pop("CONTINUITY_CONTROLLER_TOKEN", None)
        os.environ.pop("CONTINUITY_CONTROLLER_TOKEN_FILE", None)
        os.environ["CONTINUITY_CONTROLLER"] = "ctl"

    def tearDown(self):
        os.environ.pop("CONTINUITY_CONTROLLER", None)
        os.environ.pop("CONTINUITY_CONTROLLER_TOKEN_FILE", None)

    def write(self, path, mode=0o600, text=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text((text or self.TOKEN) + "\n")
        path.chmod(mode)
        return path

    def worker_finished(self):
        from continuity_plane.collaboration_registry import project_data_dir

        try:
            jobs = json.loads((project_data_dir(self.data, "sample") / "worker-jobs.json").read_text())
            return jobs["task"].get("job_status") in {"completed", "failed"}
        except (OSError, ValueError, KeyError):
            return False

    def dispatch(self):
        return dispatch(
            self.root, data_root=self.data, task_id="task", model="sonnet", controller="ctl"
        )

    def test_dispatch_without_a_token_is_refused(self):
        with self.assertRaisesRegex(ValueError, "token is required"):
            self.dispatch()

    def test_token_file_from_the_environment_authorizes_dispatch(self):
        file = self.write(self.tmp / "secret" / "token")
        with mock.patch.dict(os.environ, {"CONTINUITY_CONTROLLER_TOKEN_FILE": str(file)}):
            self.assertEqual(self.dispatch()["status"], "completed")
        self.assertNotIn(self.TOKEN, json.dumps(self.card()))

    def test_per_project_token_file_authorizes_dispatch(self):
        from continuity_plane.collaboration_registry import project_data_dir

        self.write(project_data_dir(self.data, "sample") / "controller-token")
        self.assertEqual(self.dispatch()["status"], "completed")

    def test_mcp_launch_works_with_a_server_side_token_file(self):
        file = self.write(self.tmp / "secret" / "token")
        with mock.patch.dict(os.environ, {"CONTINUITY_CONTROLLER_TOKEN_FILE": str(file)}):
            value = agent_tools.handle(
                "continuity_worker_dispatch",
                {"project_id": "sample", "task_id": "task", "model": "sonnet", "launch": True},
            )
        self.assertTrue(value["executed"])
        # The launched worker runs in the background; let it finish before cleanup.
        for _ in range(150):
            if self.worker_finished():
                break
            time.sleep(0.1)
        time.sleep(0.5)
        self.assertTrue(self.prompt_out.exists())

    def test_world_readable_token_file_is_refused_without_echoing_the_token(self):
        file = self.write(self.tmp / "secret" / "token", mode=0o644)
        with mock.patch.dict(os.environ, {"CONTINUITY_CONTROLLER_TOKEN_FILE": str(file)}):
            with self.assertRaises(ValueError) as caught:
                self.dispatch()
        self.assertIn("permissions", str(caught.exception))
        self.assertNotIn(self.TOKEN, str(caught.exception))
        self.assertTrue(stat.S_IMODE(file.stat().st_mode) & 0o044)

    def test_mcp_never_accepts_a_token_as_a_tool_argument(self):
        with self.assertRaisesRegex(ValueError, "token"):
            agent_tools.handle(
                "continuity_worker_dispatch",
                {
                    "project_id": "sample", "task_id": "task", "model": "sonnet",
                    "controller_token": self.TOKEN, "launch": True,
                },
            )


if __name__ == "__main__":
    unittest.main()
