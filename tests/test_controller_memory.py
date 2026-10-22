"""No real provider profile is accessed: exact synthetic notes and two members."""

import json
from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from continuity_plane import controller_memory as memory
from continuity_plane import controller_session
from continuity_plane.collaboration_registry import (
    _atomic_write,
    add_task,
    register_project,
)
from continuity_plane.cli import main
from continuity_plane.controller_lease import acquire, reserve
from continuity_plane.dispatch_health import (
    clear,
    record,
    require_available,
    show,
)
from continuity_plane.planning_api import handle_tool
from continuity_plane.project_context import handoff
from continuity_plane.runtime_origin import current


class MemoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.data = self.base / "data"
        self.root = self.base / "repo"
        self.root.mkdir()
        self.native = self.base / "native"
        self.native.mkdir()
        self.env = patch.dict(os.environ, {"CODEX_HOME": str(self.native)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def note(self, record_id="lesson", text="Use the checked baseline", **kwargs):
        return memory.remember(
            self.data,
            "sample",
            identity="alice",
            record_id=record_id,
            kind="lesson",
            text=text,
            source="explicit-user-request",
            **kwargs,
        )

    def approve(self, record_id="lesson", revision=1):
        return memory.decide(
            self.data,
            "alice",
            record_id,
            status="approved",
            expected_revision=revision,
            reason="Explicitly adopt project knowledge",
        )

    def test_candidates_are_not_effective_and_no_cross_member_or_project_read(self):
        self.note()
        self.assertEqual(memory.records(self.data, "sample", "alice"), [])
        self.assertEqual(
            len(memory.records(self.data, "sample", "alice", include_candidates=True)),
            1,
        )
        self.approve()
        self.assertEqual(memory.records(self.data, "sample", "bob"), [])
        self.assertEqual(memory.records(self.data, "other", "alice"), [])
        self.assertEqual(len(memory.records(self.data, "sample", "alice")), 1)

    def test_global_preferences_are_private_but_cross_project(self):
        self.note(scope="private-global")
        self.approve()
        self.assertEqual(len(memory.records(self.data, "other", "alice")), 1)
        with self.assertRaises(ValueError):
            memory.share(
                self.root,
                self.data,
                "sample",
                "alice",
                "lesson",
                expected_revision=2,
                reason="Share",
            )

    def test_revision_conflict_preserves_old_record_and_history(self):
        self.note()
        with self.assertRaises(ValueError):
            self.note(expected_revision=0)
        self.approve()
        history = memory.directory(self.data, "alice") / "history/lesson"
        self.assertEqual(len(list(history.glob("*.json"))), 2)

    def test_revoked_and_expired_memory_not_injected(self):
        self.note(expires_at="2020-01-01T00:00:00Z")
        self.approve()
        self.assertEqual(memory.records(self.data, "sample", "alice"), [])
        memory.decide(
            self.data,
            "alice",
            "lesson",
            status="revoked",
            expected_revision=2,
            reason="Superseded",
        )
        self.assertEqual(
            memory.records(self.data, "sample", "alice", include_candidates=True), []
        )

    def test_secret_assignments_and_path_traversal_rejected(self):
        with self.assertRaises(ValueError):
            memory.remember(
                self.data,
                "sample",
                identity="alice",
                record_id="secret",
                kind="lesson",
                text="api_key = secret-value",
                source="note",
            )
        for project in ("../other", "/absolute", ".."):
            with self.assertRaises(ValueError):
                memory.profile(self.data, project, "alice")

    def test_project_policy_locks_and_personal_global_defaults(self):
        memory.profile(
            self.data,
            None,
            "alice",
            patch={"language": "zh", "report_format": "brief"},
            expected_revision=0,
        )
        memory.profile(
            self.data,
            "sample",
            "alice",
            patch={"report_format": "table", "question_policy": "native-if-available"},
            expected_revision=0,
        )
        (self.root / ".continuity").mkdir()
        (self.root / ".continuity/controller-profile.json").write_text(
            json.dumps(
                {
                    "defaults": {"report_format": "verified-vs-unverified"},
                    "locked_fields": ["report_format"],
                }
            )
        )
        result = memory.effective(self.root, self.data, "sample", "alice")
        self.assertEqual(result["values"]["language"], "zh")
        self.assertEqual(result["values"]["report_format"], "verified-vs-unverified")
        self.assertEqual(result["conflicts"], ["report_format"])
        self.assertEqual(
            memory.effective(self.root, self.data, "sample", "bob")["values"],
            {"report_format": "verified-vs-unverified"},
        )
        with self.assertRaises(ValueError):
            memory.profile(
                self.data,
                "sample",
                "alice",
                patch={"allow_deploy": "yes"},
                expected_revision=1,
            )

    def test_shared_export_is_explicit_and_does_not_publish_private_origin(self):
        self.note()
        with self.assertRaises(ValueError):
            memory.share(
                self.root,
                self.data,
                "sample",
                "alice",
                "lesson",
                expected_revision=1,
                reason="Publish",
            )
        self.approve()
        result = memory.share(
            self.root,
            self.data,
            "sample",
            "alice",
            "lesson",
            expected_revision=2,
            reason="Approved for project review",
        )
        value = json.loads(Path(result["path"]).read_text())
        self.assertNotIn("owner", value)
        self.assertNotIn("source", value)
        self.assertFalse(result["published"])

    def grant(self):
        self.file = self.native / "MEMORY.md"
        self.file.write_text("Use bounded lookups.\n[Other note](./never-read.md)")
        return memory.consent(
            self.data,
            "alice",
            "sample",
            consent_id="approved-notes",
            provider="codex",
            files=[str(self.file)],
        )

    def test_no_consent_reads_nothing(self):
        with patch.object(memory, "_source_text") as read:
            with self.assertRaises(ValueError):
                memory.ingest(self.data, "sample", "alice", "missing")
            read.assert_not_called()

    def test_native_intake_is_candidate_incremental_no_link_following(self):
        self.grant()
        first = memory.ingest(self.data, "sample", "alice", "approved-notes")
        self.assertEqual(len(first["changed_ids"]), 1)
        self.assertFalse(first["links_followed"])
        self.assertEqual(memory.records(self.data, "sample", "alice"), [])
        self.assertEqual(
            memory.ingest(self.data, "sample", "alice", "approved-notes")[
                "unchanged_sources"
            ],
            1,
        )
        self.approve(first["changed_ids"][0])
        self.assertEqual(len(memory.records(self.data, "sample", "alice")), 1)
        memory.consent(
            self.data, "alice", "sample", consent_id="approved-notes", revoke=True
        )
        self.assertEqual(memory.records(self.data, "sample", "alice"), [])

    def test_controller_start_refreshes_consented_native_notes_once(self):
        register_project(self.data, project_id="sample", control_root=self.root)
        self.grant()
        first = controller_session.start(
            self.root, self.data, "sample", "alice", budget=4096
        )
        self.assertEqual(first["native_refresh"]["refreshed"][0]["changed"], 1)
        self.assertEqual(first["memory"], [])
        candidates = memory.records(
            self.data, "sample", "alice", include_candidates=True
        )
        self.assertEqual(candidates[0]["status"], "candidate")
        second = controller_session.start(
            self.root, self.data, "sample", "alice", budget=4096
        )
        self.assertEqual(second["native_refresh"]["refreshed"][0]["changed"], 0)

    def test_native_source_origin_project_and_account_data_boundaries(self):
        self.grant()
        with patch.dict(
            os.environ, {"CODEX_HOME": str(self.base / "different-profile")}
        ):
            with self.assertRaises(ValueError):
                memory.ingest(self.data, "sample", "alice", "approved-notes")
        for identity, project in (("bob", "sample"), ("alice", "other")):
            with self.assertRaises(ValueError):
                memory.ingest(self.data, project, identity, "approved-notes")
        for filename in ("auth.md", "session.md", "rollout.jsonl", "../outside.md"):
            with self.assertRaises(ValueError):
                memory.consent(
                    self.data,
                    "alice",
                    "sample",
                    consent_id="bad",
                    provider="codex",
                    files=[str(self.native / filename)],
                )

    def test_symlink_native_source_is_not_read(self):
        self.grant()
        self.file.unlink()
        outside = self.base / "outside.md"
        outside.write_text("Do not read this")
        self.file.symlink_to(outside)
        with self.assertRaises(OSError):
            memory.ingest(self.data, "sample", "alice", "approved-notes")

    def test_bounded_controller_start_combines_plan_profile_memory_and_ack(self):
        register_project(self.data, project_id="sample", control_root=self.root)
        handoff(
            self.root,
            self.data,
            "sample",
            plan="Continue implementation",
            next_action="Run focused tests",
            pending=["Review schema"],
        )
        memory.profile(
            self.data,
            "sample",
            "alice",
            patch={"language": "zh", "question_policy": "native-if-available"},
            expected_revision=0,
        )
        self.note()
        self.approve()
        exported = memory.share(
            self.root,
            self.data,
            "sample",
            "alice",
            "lesson",
            expected_revision=2,
            reason="Project policy",
        )
        packet = controller_session.start(
            self.root,
            self.data,
            "sample",
            "alice",
            capabilities=["question"],
            budget=4096,
        )
        self.assertEqual(packet["packet_kind"], "controller-start/v1")
        self.assertEqual(packet["next_action"], "Run focused tests")
        self.assertEqual(packet["profile"]["values"]["language"], "zh")
        self.assertEqual(len(packet["memory"]), 2)
        self.assertEqual(packet["question_route"], "native-if-actually-available")
        self.assertTrue(Path(exported["path"]).is_file())
        controller_session.start(
            self.root,
            self.data,
            "sample",
            "alice",
            budget=4096,
            acknowledge=packet["handoff_revision"],
            profile_sha256=packet["profile"]["profile_sha256"],
        )
        self.assertTrue(
            (memory.directory(self.data, "alice") / "restores/sample.json").is_file()
        )

    def test_start_fails_closed_without_budget_and_public_never_reads_private(self):
        register_project(self.data, project_id="sample", control_root=self.root)
        handoff(
            self.root,
            self.data,
            "sample",
            plan="P" * 1500,
            next_action="N",
        )
        self.note(text="M" * 2000)
        self.approve()
        packet = controller_session.start(
            self.root, self.data, "sample", "alice", budget=1024
        )
        self.assertFalse(packet["complete"])
        with self.assertRaises(ValueError):
            controller_session.start(
                self.root,
                self.data,
                "sample",
                "alice",
                budget=1024,
                acknowledge=packet["handoff_revision"],
                profile_sha256=packet["profile_sha256"],
            )
        public = controller_session.start(
            self.root, self.data, "sample", "bob", public_only=True
        )
        self.assertEqual(public["memory"], [])
        self.assertEqual(public["profile"]["values"], {})

    def test_controller_start_cli_is_the_same_bounded_contract(self):
        register_project(self.data, project_id="sample", control_root=self.root)
        handoff(
            self.root,
            self.data,
            "sample",
            plan="CLI continuity",
            next_action="Run the CLI path",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "controller",
                        "start",
                        "--project-id",
                        "sample",
                        "--data-root",
                        str(self.data),
                        "--member",
                        "alice",
                        "--format",
                        "json",
                        "--max-output-bytes",
                        "4096",
                    ]
                ),
                0,
            )
        value = json.loads(output.getvalue())
        self.assertEqual(value["next_action"], "Run the CLI path")
        self.assertTrue(value["complete"])

    def test_private_planning_mcp_is_disabled_by_default(self):
        register_project(self.data, project_id="sample", control_root=self.root)
        with self.assertRaisesRegex(ValueError, "private memory MCP is disabled"):
            handle_tool(
                "continuity_plan",
                {
                    "action": "memory.notes",
                    "project_id": "sample",
                    "data_root": str(self.data),
                    "parameters": {"member": "alice"},
                },
            )

    def test_observed_quota_pauses_only_origin_model_and_clear_is_explicit(self):
        register_project(self.data, project_id="sample", control_root=self.root)
        origin = current("codex")
        record(
            self.data,
            "sample",
            origin,
            "gpt-6.1-sol",
            {
                "failure_class": "quota-exhausted",
                "available_at": "2099-01-01T00:00:00+00:00",
                "reason": "usage limit reached",
                "guidance": "Resume after provider reset",
            },
            task_id="task",
            attempt_id="attempt-quota",
        )
        with self.assertRaisesRegex(ValueError, "reason=usage limit reached"):
            require_available(self.data, "sample", origin, "gpt-6.1-sol")
        shown = show(self.data, "sample")
        self.assertEqual(shown["items"][0]["task_id"], "task")
        self.assertEqual(shown["items"][0]["reason"], "usage limit reached")
        require_available(self.data, "sample", origin, "other-model")
        with self.assertRaisesRegex(ValueError, "reason"):
            clear(self.data, "sample", origin, "gpt-6.1-sol", "")
        self.assertEqual(
            clear(self.data, "sample", origin, "gpt-6.1-sol", "Observed reset")[
                "account_query"
            ],
            False,
        )
        require_available(self.data, "sample", origin, "gpt-6.1-sol")

    def test_reservation_error_distinguishes_duplicate_or_stale_attempt(self):
        register_project(self.data, project_id="sample", control_root=self.root)
        add_task(
            self.data,
            project_id="sample",
            task_id="task",
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="Task",
            objective="Check reservation",
            next_action="Run",
            exit_criteria=["Pass"],
        )
        lease = acquire(self.data, "sample", owner="controller")
        profile = {"provider": "codex", "execution_origin": current("codex")}
        reserve(
            self.data,
            "sample",
            task_id="task",
            job_id="job-one",
            owner="controller",
            token=lease["token"],
            profile=profile,
        )
        with self.assertRaisesRegex(
            ValueError,
            "state=unknown reason=no-live-binding-or-terminal-result job=job-one",
        ):
            reserve(
                self.data,
                "sample",
                task_id="task",
                job_id="job-two",
                owner="controller",
                token=lease["token"],
                profile=profile,
            )

    def test_reservation_reports_terminal_result_as_stale(self):
        register_project(self.data, project_id="sample", control_root=self.root)
        add_task(
            self.data,
            project_id="sample",
            task_id="task",
            lane_id="scope",
            mode="patch",
            priority="p1",
            title="Task",
            objective="Check reservation",
            next_action="Run",
            exit_criteria=["Pass"],
        )
        lease = acquire(self.data, "sample", owner="controller")
        profile = {"provider": "codex", "execution_origin": current("codex")}
        reserve(
            self.data,
            "sample",
            task_id="task",
            job_id="job-one",
            owner="controller",
            token=lease["token"],
            profile=profile,
        )
        _atomic_write(
            self.data
            / "projects"
            / "sample"
            / "worker-jobs"
            / "job-one"
            / "result.json",
            {"status": "failed", "failure_class": "quota-exhausted"},
        )
        with self.assertRaisesRegex(
            ValueError, "state=stale reason=terminal-result-failed"
        ):
            reserve(
                self.data,
                "sample",
                task_id="task",
                job_id="job-two",
                owner="controller",
                token=lease["token"],
                profile=profile,
            )


if __name__ == "__main__":
    unittest.main()
