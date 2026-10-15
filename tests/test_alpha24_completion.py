"""Completion gates for actual adoption gaps, using isolated subprocesses."""

import hashlib
import json
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests import test_adoption_workflows


class Alpha24CompletionTests(unittest.TestCase):
    setUp = test_adoption_workflows.AdoptionWorkflowTests.setUp

    def adapter(self, model, code, resume=None, *, official=False):
        from continuity_plane.worker_dispatch import configure

        return configure(
            self.data,
            model=model,
            provider="custom",
            official_subscription=official,
            argv=[sys.executable, "-c", code],
            resume_argv=[sys.executable, "-c", resume, "{thread_id}"]
            if resume
            else None,
        )

    def test_transient_fails_over_only_to_explicit_allowed_adapter(self):
        from continuity_plane.worker_dispatch import dispatch

        self.adapter("sol", "print('429 too many requests');raise SystemExit(1)")
        self.adapter(
            "sonnet",
            "import json; print(json.dumps({'type':'result','session_id':'secondary','result':'Finished remaining work'}))",
        )
        value = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            fallback_models=["sonnet"],
        )
        self.assertEqual(value["status"], "completed")
        self.assertEqual(len(value["previous_jobs"]), 1)
        self.assertIn("Finished", Path(value["report_file"]).read_text())

    def test_policy_refusal_never_uses_an_allowed_fallback(self):
        from continuity_plane.worker_dispatch import dispatch

        self.adapter("sol", "print('400 cyber_policy');raise SystemExit(1)")
        self.adapter("sonnet", "raise SystemExit('must not launch')")
        result = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            fallback_models=["sonnet"],
        )
        self.assertEqual(result["failure_class"], "policy-review")
        self.assertNotIn("previous_jobs", result)

    def test_rule_intake_previews_checks_hashes_and_preserves_sources(self):
        from continuity_plane.rule_intake import intake

        source = self.root / "policy.md"
        source.write_text("# Rules\n\nRead the current official sources.\n")
        original = source.read_bytes()
        manifest = self.root / "intake.json"
        row = {
            "path": str(source),
            "rule_id": "sources",
            "audience": "worker",
            "summary": "Read the complete attached source policy",
        }
        manifest.write_text(json.dumps({"sources": [row]}))
        preview = intake(self.data, "sample", manifest)
        self.assertFalse(preview["applied"])
        with self.assertRaisesRegex(ValueError, "expected_sha256"):
            intake(self.data, "sample", manifest, apply=True)
        row["expected_sha256"] = hashlib.sha256(original).hexdigest()
        manifest.write_text(json.dumps({"sources": [row]}))
        first = intake(self.data, "sample", manifest, apply=True)
        second = intake(self.data, "sample", manifest, apply=True)
        self.assertEqual(first["items"][0]["revision"], second["items"][0]["revision"])
        self.assertEqual(source.read_bytes(), original)

    def test_status_projection_preserves_batch_verification_and_has_no_side_effect(
        self,
    ):
        from continuity_plane.collaboration_workflow import record
        from continuity_plane.batch_projection import project

        record(
            self.data,
            project_id="sample",
            kind="batch",
            record_id="batch1",
            value={
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
                    "unverified": ["optional OS not run"],
                },
            },
        )
        first = project(self.data, "sample")
        self.assertIn("optional OS not run", first["markdown"])
        self.assertIn("Batch batch1", first["markdown"])
        self.assertEqual(first, project(self.data, "sample"))
        self.assertFalse((self.root / "STATUS.md").exists())

    def test_quota_reset_is_not_assumed_and_primary_resume_is_preserved(self):
        from continuity_plane.worker_dispatch import dispatch
        from continuity_plane.subscription_quota import (
            record,
            report,
            propose,
            return_ready,
        )

        self.adapter(
            "sol",
            "import json;print(json.dumps({'type':'thread.started','thread_id':'primary-thread'}));print('429 too many requests');raise SystemExit(1)",
            "import sys; assert sys.argv[1]=='primary-thread';print('resumed')",
            official=True,
        )
        self.adapter("sonnet", "print('done')")
        initial = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        pending = propose(
            self.data,
            "sample",
            "task",
            "sol",
            "sonnet",
            approval_source="test-only:explicit owner approval",
        )
        dispatch(self.root, data_root=self.data, task_id="task", model="sonnet")
        now = datetime.now(UTC)
        record(
            self.data,
            "sample",
            "sol",
            used_percent=100,
            resets_at=(now + timedelta(seconds=1)).isoformat(),
            source="test-only:quota",
            official_subscription=True,
            provider="custom",
        )
        self.assertEqual(
            report(self.data, "sample", now=now + timedelta(seconds=2))["models"][
                "sol"
            ]["status"],
            "needs-refresh",
        )
        record(
            self.data,
            "sample",
            "sol",
            used_percent=1,
            resets_at=(now + timedelta(hours=1)).isoformat(),
            source="test-only:new quota",
            official_subscription=True,
            provider="custom",
        )
        self.assertEqual(
            return_ready(self.root, self.data, "sample", pending["id"])["next_action"],
            "resume-approved-primary-session",
        )
        result = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            resume=True,
            resume_job=initial["job_id"],
        )
        self.assertEqual(result["status"], "completed")

    def test_native_quota_refresh_uses_the_requested_bucket_not_unrelated_limits(self):
        from continuity_plane.subscription_quota import refresh

        reset = (datetime.now(UTC) + timedelta(hours=1)).timestamp()
        result = refresh(
            self.data,
            "sample",
            "sol",
            limits={
                "rateLimitsByLimitId": {
                    "codex": {"primary": {"usedPercent": 25, "resetsAt": reset}},
                    "other": {"primary": {"usedPercent": 100, "resetsAt": reset}},
                }
            },
        )
        self.assertEqual(result["used_percent"], 25)

    def test_context_window_share_uses_host_capacity_and_keeps_task_attribution(self):
        from continuity_plane.provider_usage import ingest, report, export_snapshot
        from continuity_plane.collaboration_registry import update_task

        log = self.root / "usage.jsonl"
        log.write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "model_context_window": 1000,
                            "last_token_usage": {"input_tokens": 400},
                            "total_token_usage": {
                                "input_tokens": 900,
                                "output_tokens": 20,
                            },
                        },
                    }
                }
            )
            + "\n"
        )
        ingest(
            self.root,
            transcript=log,
            provider="codex",
            session_id="native",
            task_id="task",
            data_root=self.data,
        )
        update_task(self.data, project_id="sample", task_id="task", status="completed")
        value = report(self.root, self.data)
        self.assertEqual(
            next(iter(value["window_utilization"].values()))[
                "last_request_input_fraction"
            ],
            0.4,
        )
        self.assertEqual(value["completed_task_usage"]["tasks_with_usage"], 1)
        self.assertEqual(
            export_snapshot(self.root, "codex", "native", self.data)["task_id"], "task"
        )


if __name__ == "__main__":
    unittest.main()
