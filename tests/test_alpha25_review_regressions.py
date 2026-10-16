"""Independent Alpha25 review regressions; owned fixtures, no native providers."""

import contextlib
import json
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests import test_adoption_workflows
from continuity_plane import collaboration_workflow as workflow
from continuity_plane.collaboration_registry import (
    _locked,
    _ledger_path,
    _load_ledger,
)
from continuity_plane.controller_lease import (
    acquire,
    begin_operation,
    end_operation,
    view,
)
from continuity_plane.lifecycle import handle_event
from continuity_plane.project_context import _path, _load, handoff_summary
from continuity_plane.worker_dispatch import configure, dispatch


class Alpha25ReviewTests(unittest.TestCase):
    setUp = test_adoption_workflows.AdoptionWorkflowTests.setUp

    def batch(self):
        return {
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
        }

    def adapter(self, name="sol", code='print("502 Bad Gateway"); raise SystemExit(1)'):
        return configure(
            self.data, model=name, provider="custom", argv=[sys.executable, "-c", code]
        )

    def test_batch_rechecks_expiry_after_waiting_on_ledger(self):
        lease = acquire(self.data, "sample", owner="old")
        waiting = threading.Event()
        ledger_path = _ledger_path(self.data, "sample")

        @contextlib.contextmanager
        def traced_lock(path):
            if path == ledger_path:
                waiting.set()
            with _locked(path):
                yield

        with ThreadPoolExecutor(max_workers=2) as pool, contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(workflow, "_locked", traced_lock))
            with _locked(ledger_path):
                pending = pool.submit(
                    workflow.record,
                    self.data,
                    project_id="sample",
                    kind="batch",
                    record_id="batch",
                    value=self.batch(),
                    controller="old",
                    controller_token=lease["token"],
                )
                self.assertTrue(waiting.wait(5))
                stack.enter_context(
                    patch(
                        "continuity_plane.controller_lease.time.time",
                        return_value=lease["expires_at"] + 1,
                    )
                )
                takeover = pool.submit(acquire, self.data, "sample", owner="new")
            with self.assertRaisesRegex(ValueError, "read-only"):
                pending.result(timeout=5)
            takeover.result(timeout=5)
        self.assertNotIn(
            "batch",
            _load_ledger(self.data, "sample")
            .get("workflow_records", {})
            .get("batch", {}),
        )

    def test_identical_batch_retry_repairs_failed_handoff_refresh(self):
        kwargs = dict(
            project_id="sample", kind="batch", record_id="batch", value=self.batch()
        )
        with patch(
            "continuity_plane.project_context.handoff",
            side_effect=OSError("owned fixture write failure"),
        ):
            with self.assertRaises(OSError):
                workflow.record(self.data, **kwargs)
        workflow.record(self.data, **kwargs)
        self.assertEqual(
            handoff_summary(self.data, "sample")["latest_integrated_batch"], "batch"
        )
        workflow.record(
            self.data,
            project_id="sample",
            kind="batch",
            record_id="later-batch",
            value=self.batch(),
        )
        workflow.record(self.data, **kwargs)
        self.assertEqual(
            handoff_summary(self.data, "sample")["latest_integrated_batch"],
            "later-batch",
        )

    def test_late_finish_cannot_overwrite_same_id_manual_acknowledgement(self):
        lease = acquire(self.data, "sample", owner="controller")
        started = begin_operation(
            self.data,
            "sample",
            owner="controller",
            token=lease["token"],
            operation="integration",
            argv=["owned-fixture"],
        )
        acknowledged = end_operation(
            self.data,
            "sample",
            started["operation_id"],
            reason="Owner confirmed the unknown result",
        )
        with self.assertRaisesRegex(ValueError, "no newer record overwritten"):
            end_operation(self.data, "sample", started["operation_id"], return_code=0)
        self.assertEqual(view(self.data, "sample")["operation"], acknowledged)

    def test_fallback_keeps_reservation_until_atomic_replacement(self):
        self.adapter()
        profile = self.adapter("sonnet", 'print("done")')
        from continuity_plane.controller_lease import reserve as real_reserve

        seen = []

        def checked(*args, **kwargs):
            if seen:
                previous = _load(_path(self.data, "sample"))["dispatch_reservations"][
                    "task"
                ]
                self.assertEqual(kwargs.get("replace_job_id"), previous["job_id"])
                with self.assertRaisesRegex(ValueError, "reservation"):
                    real_reserve(
                        self.data,
                        "sample",
                        task_id="task",
                        job_id="competitor",
                        owner="operator",
                        token=None,
                        profile=profile,
                    )
            seen.append(kwargs["job_id"])
            return real_reserve(*args, **kwargs)

        with patch(
            "continuity_plane.controller_lease.reserve", side_effect=checked
        ):
            result = dispatch(
                self.root,
                data_root=self.data,
                task_id="task",
                model="sol",
                fallback_models=["sonnet"],
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(seen), 2)

    def test_expired_fallback_preserves_original_finished_result(self):
        self.adapter()
        self.adapter("sonnet", 'print("done")')
        lease = acquire(self.data, "sample", owner="controller")
        from continuity_plane.supervisor import run_attempt as real_attempt

        def expire(*args, **kwargs):
            result = real_attempt(*args, **kwargs)
            path = _path(self.data, "sample")
            value = _load(path)
            value["controller_lease"]["expires_at"] = time.time() - 1
            from continuity_plane.collaboration_registry import _atomic_write

            _atomic_write(path, value)
            return result

        with patch("continuity_plane.supervisor.run_attempt", side_effect=expire):
            result = dispatch(
                self.root,
                data_root=self.data,
                task_id="task",
                model="sol",
                fallback_models=["sonnet"],
                controller="controller",
                controller_token=lease["token"],
            )
        self.assertEqual(result["return_code"], 1)
        self.assertEqual(result["fallback"]["status"], "deferred")
        saved = json.loads(
            (
                self.data
                / "projects/sample/worker-jobs"
                / result["job_id"]
                / "result.json"
            ).read_text()
        )
        self.assertEqual(saved["return_code"], 1)
        self.assertEqual(saved["fallback"]["status"], "deferred")

    def test_compaction_reports_shared_handoff_refresh_failure_without_blocking(self):
        acquire(self.data, "sample", owner="controller")
        with patch.dict(
            os.environ,
            {
                "CONTINUITY_ROLE": "controller",
                "CONTINUITY_CONTROLLER": "other",
                "CONTINUITY_CONTROLLER_TOKEN": "0" * 32,
            },
        ):
            result = handle_event(
                {
                    "cwd": str(self.root),
                    "session_id": "owned-controller",
                    "hook_event_name": "PreCompact",
                },
                host="claude",
                data_root=self.data,
            )
        self.assertTrue(result["continue"])
        self.assertEqual(result["status"], "saved-with-warning")
        self.assertEqual(result["controller_handoff_refresh"]["status"], "deferred")
        restored = handle_event(
            {
                "cwd": str(self.root),
                "session_id": "owned-controller",
                "hook_event_name": "SessionStart",
                "source": "compact",
            },
            host="claude",
            data_root=self.data,
        )
        self.assertIn(
            "controller_handoff_refresh",
            restored["hookSpecificOutput"]["additionalContext"],
        )

    def test_destination_capacity_refusal_preserves_result_and_other_reservation(self):
        self.adapter()
        profile = configure(
            self.data,
            model="sonnet",
            provider="custom",
            profile_provider="codex",
            argv=[sys.executable, "-c", 'print("done")'],
        )
        lease = acquire(
            self.data,
            "sample",
            owner="controller",
            client_limits={"custom": 1, "codex": 1},
        )
        from continuity_plane.controller_lease import reserve

        reserve(
            self.data,
            "sample",
            task_id="other",
            job_id="owned-other",
            owner="controller",
            token=lease["token"],
            profile=profile,
        )
        result = dispatch(
            self.root,
            data_root=self.data,
            task_id="task",
            model="sol",
            fallback_models=["sonnet"],
            controller="controller",
            controller_token=lease["token"],
        )
        self.assertEqual(result["return_code"], 1)
        self.assertIn("capacity", result["fallback"]["reason"])
        self.assertEqual(
            _load(_path(self.data, "sample"))["dispatch_reservations"]["other"][
                "job_id"
            ],
            "owned-other",
        )

    def test_failed_fallback_notification_cannot_replace_finished_result(self):
        self.adapter()
        self.adapter("sonnet")
        finished = {
            "status": "failed",
            "return_code": 1,
            "retryable": True,
            "failure_class": "transient-network",
        }
        with (
            patch(
                "continuity_plane.supervisor.run_attempt", return_value=finished
            ),
            patch(
                "continuity_plane.worker_dispatch._fallback",
                side_effect=ValueError("fixture fallback refusal"),
            ),
            patch(
                "continuity_plane.execution_outbox.change",
                side_effect=ValueError("fixture notification refusal"),
            ),
        ):
            result = dispatch(
                self.root,
                data_root=self.data,
                task_id="task",
                model="sol",
                fallback_models=["sonnet"],
            )
        self.assertEqual(result["return_code"], 1)
        self.assertEqual(result["fallback"]["status"], "deferred")


if __name__ == "__main__":
    unittest.main()
