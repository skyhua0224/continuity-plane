"""Alpha33 dispatch hot-path, pagination and lease diagnostics regressions."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from continuity_plane import dispatch_safety
from continuity_plane.cli import main
from continuity_plane.collaboration_registry import (
    _atomic_write,
    _ledger_path,
    _load_ledger,
    add_task,
    project_data_dir,
)
from continuity_plane.controller_lease import acquire, reconcile_host
from continuity_plane.worker_dispatch import dispatch
from tests import test_adoption_workflows


class Alpha33DispatchHotPathTests(unittest.TestCase):
    setUp = test_adoption_workflows.AdoptionWorkflowTests.setUp

    def test_empty_body_fails_before_dispatch_safety_history(self):
        add_task(
            self.data,
            project_id="sample",
            task_id="empty-body",
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="Empty body",
            objective="Reject early",
            next_action="Add a body",
            exit_criteria=["PASS"],
        )
        with patch("continuity_plane.dispatch_safety.refresh") as refresh:
            with self.assertRaisesRegex(ValueError, "task body is empty"):
                dispatch(
                    self.root,
                    data_root=self.data,
                    task_id="empty-body",
                    dry_run=True,
                )
        refresh.assert_not_called()

    def test_policy_index_consumes_only_the_appended_tail(self):
        directory = project_data_dir(self.data, "sample")
        changes = directory / "changes.jsonl"
        with changes.open("w", encoding="utf-8") as stream:
            for cursor in range(1, 5001):
                stream.write(
                    json.dumps(
                        {
                            "cursor": cursor,
                            "event": "unrelated",
                            "observed_at": "2026-10-21T00:00:00+00:00",
                            "task_id": "task",
                        }
                    )
                    + "\n"
                )
        first = dispatch_safety.refresh(self.data, "sample")
        old_offset = first["journal"]["offset"]
        self.assertEqual(old_offset, changes.stat().st_size)

        with changes.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "cursor": 5001,
                        "event": "dispatch.policy-stopped",
                        "observed_at": "2026-10-21T00:00:01+00:00",
                        "task_id": "task",
                        "job_id": "job-blocked",
                        "scope_sha256": "scope-a",
                    }
                )
                + "\n"
            )
        original = dispatch_safety._scan_journal
        with patch.object(dispatch_safety, "_scan_journal", wraps=original) as scan:
            refusal = dispatch_safety.blocked(
                self.data, "sample", "task", "scope-a"
            )
        self.assertEqual(refusal, ("dispatch.policy-stopped", "job-blocked"))
        self.assertEqual(scan.call_args.args[1], old_offset)

        with changes.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "cursor": 5002,
                        "event": "dispatch.classification-corrected",
                        "observed_at": "2026-10-21T00:00:02+00:00",
                        "task_id": "task",
                        "job_id": "job-blocked",
                        "scope_sha256": "scope-a",
                    }
                )
                + "\n"
            )
        self.assertIsNone(dispatch_safety.blocked(self.data, "sample", "task", "scope-a"))

    def test_corrupt_derived_index_rebuilds_without_weakening_refusal(self):
        changes = project_data_dir(self.data, "sample") / "changes.jsonl"
        changes.write_text(
            json.dumps(
                {
                    "cursor": 1,
                    "event": "dispatch.review-required",
                    "task_id": "task",
                    "job_id": "job-review",
                    "scope_sha256": "scope-b",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        dispatch_safety.index_path(self.data, "sample").write_text("{bad", encoding="utf-8")
        self.assertEqual(
            dispatch_safety.blocked(self.data, "sample", "task", "scope-b"),
            ("dispatch.review-required", "job-review"),
        )

    def test_batch_list_caps_large_budget_and_returns_cursor(self):
        ledger = _load_ledger(self.data, "sample")
        records = ledger.setdefault("workflow_records", {}).setdefault("batch", {})
        for index in range(24):
            record_id = f"batch-{index:02d}"
            records[record_id] = {
                "id": record_id,
                "kind": "batch",
                "value": {"padding": "x" * 9000, "index": index},
                "recorded_at": f"2026-10-21T00:{index:02d}:00+00:00",
                "authority": "declared-metadata-not-execution-permission",
            }
        _atomic_write(_ledger_path(self.data, "sample"), ledger)

        cursor = None
        seen = []
        for _ in range(6):
            output = io.StringIO()
            argv = [
                "collaboration",
                "batch-list",
                "--root",
                str(self.root),
                "--project-id",
                "sample",
                "--data-root",
                str(self.data),
                "--max-output-bytes",
                "200000",
                "--format",
                "json",
            ]
            if cursor:
                argv.extend(["--cursor", cursor])
            with redirect_stdout(output):
                self.assertEqual(main(argv), 0)
            raw = output.getvalue()
            self.assertLessEqual(len(raw.encode()), 131072)
            page = json.loads(raw)
            self.assertEqual(page["budget_capped_to"], 131072)
            seen.extend(item["id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        self.assertEqual(len(seen), 24)
        self.assertEqual(len(set(seen)), 24)

    def test_reconcile_host_explains_wrong_project_token(self):
        lease = acquire(self.data, "sample", owner="controller")
        with self.assertRaisesRegex(ValueError, "token does not match") as caught:
            reconcile_host(
                self.data,
                "sample",
                apply=True,
                reason="verify actionable diagnostic",
                owner="controller",
                token="token-from-another-project",
            )
        message = str(caught.exception)
        self.assertIn("project-scoped", message)
        self.assertNotIn("token-from-another-project", message)
        self.assertNotIn(lease["token"], message)
