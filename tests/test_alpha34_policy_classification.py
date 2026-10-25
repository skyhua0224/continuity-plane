"""Alpha34 provider-policy classification and explicit clearance regressions."""

from __future__ import annotations

import json
import sys
import unittest

from continuity_plane.collaboration_registry import (
    _atomic_write,
    _record_change,
    list_changes,
    project_data_dir,
)
from continuity_plane.retry_policy import classify, policy_refusal
from continuity_plane.worker_dispatch import (
    clear_policy_review,
    configure,
    dispatch,
)
from tests import test_adoption_workflows


class Alpha34PolicyClassificationTests(unittest.TestCase):
    setUp = test_adoption_workflows.AdoptionWorkflowTests.setUp

    def adapter(self, code):
        return configure(
            self.data,
            model="sol",
            provider="custom",
            argv=[sys.executable, "-c", code, "{effort}"],
            resume_argv=[
                sys.executable,
                "-c",
                "pass",
                "{thread_id}",
            ],
        )

    def test_cloudflare_403_with_policy_words_is_auth_not_policy(self):
        page = (
            "HTTP 403 Forbidden\n"
            "Attention Required! | Cloudflare\n"
            "cf-ray a4db01497e76aec4-HKG via proxy 127.0.0.1:10102\n"
            "The request was blocked by an access policy violation"
        )
        self.assertFalse(policy_refusal(page))
        value = classify(page)
        self.assertEqual(value["failure_class"], "authentication")
        self.assertTrue(value["resume_same_session"])
        self.assertFalse(value["requires_new_session"])

    def test_explicit_model_safety_marker_wins_even_with_http_403(self):
        text = 'HTTP 403 {"error":{"type":"cyber_policy","message":"blocked"}}'
        self.assertTrue(policy_refusal(text))
        self.assertEqual(classify(text)["failure_class"], "policy-review")

    def test_cloudflare_403_dispatch_records_auth_and_allows_same_thread_resume(self):
        self.adapter(
            "print('{\"type\":\"thread.started\",\"thread_id\":\"thread-403\"}');"
            "print('HTTP 403 Forbidden Cloudflare cf-ray a4db01497e76aec4-HKG '"
            "'proxy 127.0.0.1:10102 access policy violation');"
            "raise SystemExit(1)"
        )
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failure_class"], "authentication")
        resumed = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            resume=True,
            dry_run=True,
        )
        self.assertTrue(resumed["resume"])
        self.assertFalse(resumed["executed"])

    def test_reviewed_policy_block_can_be_cleared_once_with_reason(self):
        self.adapter(
            "print('{\"type\":\"thread.started\",\"thread_id\":\"thread-review\"}');"
            "print('{\"error\":{\"type\":\"cyber_policy\",\"code\":403}}');"
            "raise SystemExit(1)"
        )
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        self.assertEqual(result["failure_class"], "policy-review")
        cleared = clear_policy_review(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            job_id=result["job_id"],
            reason="Reviewed the provider response; it is an HTTP interception error",
            actor="reviewer",
        )
        self.assertEqual(cleared["status"], "cleared")
        self.assertEqual(cleared["failure_class"], "operator-cleared")
        repeated = clear_policy_review(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            job_id=result["job_id"],
            reason="Duplicate review acknowledgement",
            actor="reviewer",
        )
        self.assertEqual(repeated["status"], "already-cleared")
        corrections = [
            row
            for row in list_changes(self.data, project_id="sample", max_results=500)[
                "changes"
            ]
            if row.get("event") == "dispatch.classification-corrected"
            and row.get("job_id") == result["job_id"]
        ]
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["corrected_by"], "reviewer")
        resumed = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            resume=True,
            dry_run=True,
        )
        self.assertTrue(resumed["resume"])

    def test_legacy_cloudflare_policy_label_is_repaired_on_resume(self):
        self.adapter(
            "print('{\"type\":\"thread.started\",\"thread_id\":\"thread-legacy\"}');"
            "print('HTTP 403 Forbidden Cloudflare cf-ray a4db01497e76aec4-HKG '"
            "'proxy 127.0.0.1:10102 access policy violation');"
            "raise SystemExit(1)"
        )
        result = dispatch(self.root, data_root=self.data, task_id="task", model="sol")
        job_dir = (
            project_data_dir(self.data, "sample")
            / "worker-jobs"
            / result["job_id"]
        )
        result_path = job_dir / "result.json"
        legacy = json.loads(result_path.read_text())
        legacy.update(
            failure_class="policy-review",
            reason="provider-policy-review",
        )
        _atomic_write(result_path, legacy)
        job = json.loads((job_dir / "job.json").read_text())
        _record_change(
            self.data,
            "sample",
            "dispatch.policy-stopped",
            "task",
            job_id=result["job_id"],
            attempt_id=result["job_id"],
            scope_sha256=job["scope_sha256"],
        )
        resumed = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            resume=True,
            dry_run=True,
        )
        self.assertTrue(resumed["resume"])
        repaired = json.loads(result_path.read_text())
        self.assertEqual(repaired["failure_class"], "authentication")
        self.assertEqual(
            repaired["classification_correction"]["from"], "policy-review"
        )

    def test_clear_policy_review_requires_an_exact_job_and_reason(self):
        with self.assertRaisesRegex(ValueError, "job-id"):
            clear_policy_review(
                self.root,
                data_root=self.data,
                project_id="sample",
                task_id="task",
                job_id="job-not-exact",
                reason="reviewed",
                actor="reviewer",
            )
        with self.assertRaisesRegex(ValueError, "bounded, nonempty reason"):
            clear_policy_review(
                self.root,
                data_root=self.data,
                project_id="sample",
                task_id="task",
                job_id="job-" + "a" * 16,
                reason="   ",
                actor="reviewer",
            )


if __name__ == "__main__":
    unittest.main()
