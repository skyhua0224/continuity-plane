"""Exact, bounded resume after compaction plus a per-session hook budget."""

import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from continuity_plane.collaboration_context import add_commitment
from continuity_plane.collaboration_registry import (
    _record_change,
    add_task,
    register_project,
    update_task,
)
from continuity_plane.controller_awareness import notice
from continuity_plane.handoff_journal import (
    DEFAULT_HOOK_BUDGET_BYTES,
    budget_remaining,
    hook_budget_bytes,
    reserve,
)
from continuity_plane.intent_todo import compile_queue
from continuity_plane.lifecycle import handle_event, host_main
from continuity_plane.planning_adoption import (
    context as planning_context,
    omitted_checklist_hint,
)
from continuity_plane.project_context import handoff

NEXT = "Run tests.test_a36_compaction_resume and commit feat/a36-compaction-resume"
PLAN = "Land the bounded resume packet after the focused checks pass."
DECISION = "Owner chooses whether the bridge applies the same budget"
CONSTRAINT = "continuity_plane/lifecycle.py"
VERIFY = "resume fixture names the active card and next action"
STALE = "STALE-COMPLETED-CARD"


class CompactionResumeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "repo"
        self.root.mkdir()
        (self.root / ".continuity").mkdir()
        (self.root / ".continuity" / "project.yaml").write_text(
            "schema_version: context.project/v1alpha1\nproject_id: fixture\n"
        )
        self.data = Path(temp.name) / "data"
        register_project(self.data, project_id="fixture", control_root=self.root)
        add_task(
            self.data,
            project_id="fixture",
            task_id="active-card",
            lane_id="a36-tokens",
            mode="patch",
            priority="p0",
            title="Exact resume after compaction",
            objective="Resume at once and exactly",
            next_action=NEXT,
            exit_criteria=[VERIFY, "SessionStart compact stays within 1.5 KB"],
            assignee="worker",
            worktree=self.root,
            allowed_files=[CONSTRAINT, "continuity_plane/handoff_journal.py"],
            branch="feat/a36-compaction-resume",
            body="Fixture task body for the resume packet.",
        )
        add_task(
            self.data,
            project_id="fixture",
            task_id="stale-card",
            lane_id="a36-tokens",
            mode="patch",
            priority="p1",
            title=STALE,
            objective=STALE,
            next_action=STALE,
            exit_criteria=[STALE],
            assignee="worker",
            worktree=self.root,
        )
        update_task(
            self.data,
            project_id="fixture",
            task_id="stale-card",
            status="completed",
        )
        compile_queue(
            self.root,
            "- Review the resume fixture\n- Record the measured bytes",
            data_root=self.data,
            session_id="fixture-session",
            assignee="worker",
        )
        handoff(
            self.root,
            self.data,
            "fixture",
            plan=PLAN,
            next_action=NEXT,
            pending=[DECISION],
        )
        add_commitment(
            self.data,
            project_id="fixture",
            commitment_id="open-decision",
            text=DECISION,
        )
        self.environment = {
            "CONTINUITY_DATA_ROOT": str(self.data),
            "CONTINUITY_ASSIGNEE": "worker",
            "CONTINUITY_ROLE": "worker",
            "CONTINUITY_TASK_ID": "active-card",
            "CONTINUITY_SESSION_ID": "fixture-session",
        }

    def payload(self, event, session="fixture-session", **extra):
        return {
            "cwd": str(self.root),
            "session_id": session,
            "hook_event_name": event,
            "model": "fixture-model",
            **extra,
        }

    def compact(self, session="fixture-session", host="claude"):
        with patch.dict(os.environ, {**self.environment, "CONTINUITY_SESSION_ID": session}):
            saved = handle_event(
                self.payload("PreCompact", session),
                host=host,
                data_root=self.data,
            )
            restored = handle_event(
                self.payload("SessionStart", session, source="compact"),
                host=host,
                data_root=self.data,
            )
        return saved, restored

    def host(self, payload):
        output = StringIO()
        with patch("sys.stdin", StringIO(json.dumps(payload))), redirect_stdout(output):
            self.assertEqual(host_main(), 0)
        raw = output.getvalue().strip()
        return json.loads(raw) if raw else {}

    def test_compact_packet_is_brief_exact_and_drops_stale_cards(self):
        saved, restored = self.compact()
        self.assertEqual(saved["status"], "saved")
        self.assertEqual(restored["status"], "restored")
        context = restored["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Task active-card: " + NEXT, context)
        self.assertIn(PLAN, context)
        self.assertIn("Constraints: " + CONSTRAINT, context)
        self.assertIn("Open decisions: " + DECISION, context)
        self.assertIn("Verify: " + VERIFY, context)
        self.assertIn("Review the resume fixture", context)
        self.assertNotIn(STALE, context)
        self.assertNotIn('"controller_scene":{', context)
        self.assertNotIn('"executor_candidates":[]', context)
        self.assertLessEqual(len(context.encode()), 1536)
        for label in (
            "Task active-card: ",
            "Plan: ",
            "Constraints: ",
            "Open decisions: ",
            "Verify: ",
        ):
            self.assertEqual(context.count(label), 1, label)

    def test_compact_and_postcompact_deliver_exactly_once_in_either_order(self):
        for host in ("codex", "claude"):
            for order in ("post-then-start", "start-then-post"):
                session = f"{host}-{order}"
                with patch.dict(
                    os.environ,
                    {**self.environment, "CONTINUITY_SESSION_ID": session},
                ):
                    handle_event(
                        self.payload("PreCompact", session),
                        host=host,
                        data_root=self.data,
                    )
                    events = (
                        ("PostCompact", "SessionStart")
                        if order == "post-then-start"
                        else ("SessionStart", "PostCompact")
                    )
                    results = []
                    for name in events:
                        extra = {"source": "compact"} if name == "SessionStart" else {}
                        results.append(
                            handle_event(
                                self.payload(name, session, **extra),
                                host=host,
                                data_root=self.data,
                            )
                        )
                delivered = [r for r in results if r["injected_bytes"] > 0]
                self.assertEqual(
                    len(delivered), 1, (host, order, [r["status"] for r in results])
                )

    def test_host_main_adds_the_same_compact_packet_once(self):
        with patch.dict(os.environ, self.environment):
            self.host(self.payload("PreCompact"))
            first = self.host(self.payload("SessionStart", source="compact"))
            second = self.host(self.payload("SessionStart", source="compact"))
        context = first["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Task active-card: " + NEXT, context)
        self.assertLessEqual(len(context.encode()), 1536)
        self.assertEqual(second, {})

    def test_session_budget_defaults_overrides_and_hard_cap(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONTINUITY_HOOK_BUDGET_BYTES", None)
            self.assertEqual(hook_budget_bytes(), DEFAULT_HOOK_BUDGET_BYTES)
            self.assertEqual(DEFAULT_HOOK_BUDGET_BYTES, 6144)
        for value, expected in (("4096", 4096), ("bad", 6144), ("-1", 0)):
            with patch.dict(os.environ, {"CONTINUITY_HOOK_BUDGET_BYTES": value}):
                self.assertEqual(hook_budget_bytes(), expected)
        with patch.dict(os.environ, {"CONTINUITY_HOOK_BUDGET_BYTES": "0"}):
            self.assertFalse(
                reserve(self.root, "zero", "x" * 10, data_root=self.data)
            )
        with patch.dict(os.environ, {"CONTINUITY_HOOK_BUDGET_BYTES": "240"}):
            self.assertTrue(reserve(self.root, "room", "x" * 100, data_root=self.data))
            self.assertEqual(budget_remaining(self.root, "room", self.data), 140)
            self.assertTrue(reserve(self.root, "room", "x" * 100, data_root=self.data))
            self.assertEqual(budget_remaining(self.root, "room", self.data), 40)
            self.assertFalse(reserve(self.root, "room", "x" * 100, data_root=self.data))
            self.assertEqual(budget_remaining(self.root, "room", self.data), 40)

    def test_planning_hint_only_delivers_when_it_fits_and_only_once(self):
        payload = self.payload(
            "UserPromptSubmit",
            prompt="fix the bounded resume packet",
        )
        with patch.dict(os.environ, self.environment):
            self.assertIsNone(
                planning_context(payload, data_root=self.data, max_bytes=40)
            )
            hint = planning_context(payload, data_root=self.data, max_bytes=4096)
            self.assertIsNotNone(hint)
            self.assertIn("continuity_task_show", hint)
            self.assertIn("continuity_progress", hint)
            self.assertIn("continuity_report_final", hint)
            self.assertIn("continuity_review_request", hint)
            self.assertIsNone(
                planning_context(payload, data_root=self.data, max_bytes=4096)
            )

    def test_exhausted_hint_budget_is_not_retried_for_the_same_session(self):
        payload = self.payload(
            "UserPromptSubmit",
            session="exhausted",
            prompt="fix the exhausted hint",
        )
        with patch.dict(
            os.environ,
            {**self.environment, "CONTINUITY_HOOK_BUDGET_BYTES": "1"},
        ):
            self.assertIsNone(
                planning_context(payload, data_root=self.data, max_bytes=4096)
            )
        with patch.dict(os.environ, self.environment):
            self.assertIsNone(
                planning_context(payload, data_root=self.data, max_bytes=4096)
            )

    def test_omitted_checklist_hint_obeys_the_same_budget(self):
        payload = {
            "cwd": str(self.root),
            "session_id": "omitted-hint",
            "prompt_omitted_sha256": "fixture",
        }
        with patch.dict(
            os.environ, {"CONTINUITY_HOOK_BUDGET_BYTES": "1"}
        ):
            self.assertIsNone(omitted_checklist_hint(payload, data_root=self.data))
        with patch.dict(os.environ, {"CONTINUITY_HOOK_BUDGET_BYTES": "6144"}):
            hint = omitted_checklist_hint(payload, data_root=self.data)
            self.assertIn("continuity_task_list", hint)
            self.assertIsNone(omitted_checklist_hint(payload, data_root=self.data))

    def test_notice_budget_drops_once_then_advances_to_the_next_change(self):
        payload = self.payload("PostToolUse")
        with patch.dict(os.environ, self.environment):
            self.assertIsNone(notice(payload, data_root=self.data))
            _record_change(
                self.data,
                "fixture",
                "attempt.finished",
                "active-card",
                attempt_id="dropped",
                status="completed",
            )
            with patch.dict(
                os.environ, {"CONTINUITY_HOOK_BUDGET_BYTES": "1"}
            ):
                self.assertIsNone(notice(payload, data_root=self.data))
            _record_change(
                self.data,
                "fixture",
                "attempt.finished",
                "active-card",
                attempt_id="delivered",
                status="completed",
            )
            note = notice(payload, data_root=self.data)
            self.assertIsNotNone(note)
            self.assertIn("[fixture] worker active-card completed", note)
            self.assertIn(
                "continuity_worker_report(project_id='fixture', task_id='active-card')",
                note,
            )
            self.assertIsNone(notice(payload, data_root=self.data))

    def test_host_main_budget_caps_hints_but_keeps_the_resume_packet(self):
        for session, budget, has_hint in (
            ("no-hint", "0", False),
            ("with-hint", "6144", True),
        ):
            with patch.dict(
                os.environ,
                {
                    **self.environment,
                    "CONTINUITY_SESSION_ID": session,
                    "CONTINUITY_HOOK_BUDGET_BYTES": budget,
                },
            ):
                output = self.host(self.payload("SessionStart", session, source="startup"))
            context = output["hookSpecificOutput"]["additionalContext"]
            self.assertIn(NEXT, context)
            self.assertEqual("Continuity is connected" in context, has_hint)


if __name__ == "__main__":
    unittest.main()
