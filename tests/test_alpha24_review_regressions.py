"""Independent review reproductions; no live provider or credential access."""

import json
import sys
import unittest
import time
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from tests import test_adoption_workflows as adoption


class ReviewRegressionTests(unittest.TestCase):
    setUp = adoption.AdoptionWorkflowTests.setUp

    def adapter(self, model, code, *, official=False):
        from continuity_plane.worker_dispatch import configure

        return configure(
            self.data,
            model=model,
            provider="custom",
            official_subscription=official,
            argv=[sys.executable, "-c", code],
            resume_argv=[
                sys.executable,
                "-c",
                "raise SystemExit('must not resume')",
                "{thread_id}",
            ],
        )

    def test_early_policy_refusal_survives_large_tail_and_no_fallback(self):
        from continuity_plane.worker_dispatch import dispatch

        self.adapter(
            "sol",
            "print('cyber_policy');print('x'*24000);print('429 too many requests');raise SystemExit(1)",
        )
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

    def test_policy_marker_split_across_chunks_is_sticky(self):
        from continuity_plane.supervisor import run_attempt

        value = run_attempt(
            self.data,
            project_id="sample",
            task_id="task",
            assignee="worker",
            command=[
                sys.executable,
                "-c",
                "import sys,time;sys.stdout.write('cyber_');sys.stdout.flush();time.sleep(.1);print('policy');print('x'*10000);print('502 bad gateway');raise SystemExit(1)",
            ],
        )
        self.assertEqual(value["failure_class"], "policy-review")
        self.assertFalse(value["retryable"])

    def test_older_job_resume_cannot_skip_later_refusal(self):
        from continuity_plane.worker_dispatch import dispatch
        from continuity_plane.subscription_quota import (
            propose,
            record,
            return_ready,
        )

        self.adapter(
            "sol",
            "import json;print(json.dumps({'type':'thread.started','thread_id':'original'}));print('429');raise SystemExit(1)",
            official=True,
        )
        first = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.adapter("sonnet", "print('cyber_policy');raise SystemExit(1)")
        handoff = propose(
            self.data, "sample", "task", "sol", "sonnet", approval_source="test:owner"
        )
        record(
            self.data,
            "sample",
            "sol",
            used_percent=0,
            resets_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            source="test:quota",
            official_subscription=True,
            provider="custom",
        )
        dispatch(self.root, data_root=self.data, task_id="task", model="sonnet")
        with self.assertRaisesRegex(ValueError, "policy refusal"):
            dispatch(
                self.root,
                data_root=self.data,
                task_id="task",
                model="sol",
                resume=True,
                resume_job=first["job_id"],
            )
        with self.assertRaisesRegex(ValueError, "policy refusal"):
            return_ready(self.root, self.data, "sample", handoff["id"], execute=True)

    def test_old_exhaustion_and_unknown_quota_do_not_allow_auto_fallback(self):
        from continuity_plane.worker_dispatch import dispatch
        from continuity_plane.subscription_quota import (
            record,
            report,
            automatic_available,
        )

        self.adapter("sol", "print('429');raise SystemExit(1)")
        self.adapter("sonnet", "print('must not launch')", official=True)
        now = datetime.now(UTC)
        self.assertFalse(automatic_available(self.data, "sample", "sonnet"))
        record(
            self.data,
            "sample",
            "sonnet",
            used_percent=100,
            resets_at=(now + timedelta(hours=2)).isoformat(),
            now=now - timedelta(minutes=16),
            source="test:quota",
            official_subscription=True,
            provider="custom",
        )
        self.assertEqual(
            report(self.data, "sample", now=now)["models"]["sonnet"]["status"],
            "exhausted",
        )
        self.assertFalse(automatic_available(self.data, "sample", "sonnet", now=now))
        result = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            fallback_models=["sonnet"],
        )
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("previous_jobs", result)

    def test_same_named_gitfile_cannot_bypass_relink_or_absent_git(self):
        from continuity_plane.repository_identity import matches

        self.assertFalse(matches(self.root, self.root.name))
        gitdir = self.root / "metadata"
        gitdir.mkdir()
        (self.root / ".git").write_text(f"gitdir: {gitdir}\n")
        (gitdir / "commondir").write_text("/not-a-repository\n")
        self.assertFalse(matches(self.root, self.root.name))

    def test_cumulative_usage_is_ambiguous_after_task_switch_and_windows_are_scoped(
        self,
    ):
        from continuity_plane.provider_usage import ingest, report
        from continuity_plane.runtime_origin import current

        path = self.root / "usage.jsonl"

        def row(n):
            return (
                json.dumps(
                    {
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "total_token_usage": {
                                    "input_tokens": n,
                                    "output_tokens": 1,
                                },
                                "last_token_usage": {"input_tokens": n},
                                "model_context_window": 1000,
                            },
                        }
                    }
                )
                + "\n"
            )

        path.write_text(row(10))
        ingest(
            self.root,
            transcript=path,
            provider="codex",
            session_id="same",
            task_id="a",
            data_root=self.data,
        )
        with path.open("a") as stream:
            stream.write(row(20))
        ingest(
            self.root,
            transcript=path,
            provider="codex",
            session_id="same",
            task_id="b",
            data_root=self.data,
        )
        value = report(self.root, self.data)
        self.assertEqual(value["by_task"], {})
        self.assertEqual(value["ambiguous_task_records"], 1)
        other = {**current("codex"), "host_sha256": "f" * 64}
        with patch("continuity_plane.runtime_origin.current", return_value=other):
            ingest(
                self.root,
                transcript=path,
                provider="codex",
                session_id="same",
                task_id="c",
                data_root=self.data,
            )
        self.assertEqual(len(report(self.root, self.data)["window_utilization"]), 2)

    def test_json_credential_source_is_rejected_before_any_rule_write(self):
        from continuity_plane.rule_intake import intake

        source = self.root / "unsafe.md"
        source.write_text(
            'Example: {"api_key": "redacted-example", "access_token": "redacted-example"}'
        )
        manifest = self.root / "intake.json"
        manifest.write_text(
            json.dumps(
                {
                    "sources": [
                        {
                            "path": str(source),
                            "rule_id": "example",
                            "summary": "example",
                        }
                    ]
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "credential assignments"):
            intake(self.data, "sample", manifest)

    def test_linux_unknown_mounts_are_not_a_durability_claim(self):
        from continuity_plane.persistent_storage import describe

        self.assertFalse(describe(self.root, mountinfo="")["recovery_suitable"])

    def test_incomplete_drain_blocks_later_replay_without_claiming_policy_refusal(self):
        from continuity_plane.worker_dispatch import dispatch

        class IncompleteTail:
            policy_seen = False
            drained = False
            thread = SimpleNamespace(join=lambda **kwargs: None)

            def __init__(self, stream, on_output):
                stream.close()

            def snapshot(self):
                return time.monotonic(), "429 too many requests"

        self.adapter("sol", "raise SystemExit(1)")
        with patch("continuity_plane.supervisor._OutputTail", IncompleteTail):
            result = dispatch(
                self.root, data_root=self.data, task_id="task", model="sol"
            )
        self.assertEqual(result["failure_class"], "output-incomplete")
        self.assertFalse(result["retryable"])
        self.assertTrue(result["safety_review_required"])
        for resume in (False, True):
            with self.assertRaisesRegex(ValueError, "unverified provider output"):
                dispatch(
                    self.root,
                    data_root=self.data,
                    task_id="task",
                    model="sol",
                    resume=resume,
                )


if __name__ == "__main__":
    unittest.main()
