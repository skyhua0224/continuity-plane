"""The queue really starts workers, and retries keep the work already done."""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

from continuity_plane import dispatch_queue as queue
from continuity_plane.collaboration_registry import (
    add_task,
    project_data_dir,
    register_project,
)
from continuity_plane.worker_dispatch import configure, dispatch

SESSION = (
    "import json;"
    "print(json.dumps({'type':'thread.started','thread_id':'thread-1'}));"
    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))"
)


class QueueEndToEndTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.data = self.tmp / "data"
        self.root = self.tmp / "repo"
        self.root.mkdir()
        register_project(self.data, project_id="sample", control_root=self.root)
        add_task(
            self.data,
            project_id="sample",
            task_id="task",
            lane_id="main",
            mode="patch",
            priority="p1",
            title="Queued card",
            objective="Run from the queue",
            next_action="Start",
            exit_criteria=["Done"],
            assignee="worker-fake",
            worktree=self.root,
            body="Do the work",
        )
        self.marker = self.tmp / "ran.txt"
        program = (
            f"import pathlib,sys;pathlib.Path({str(self.marker)!r}).write_text('ran');{SESSION}"
        )
        configure(
            self.data,
            model="fake",
            provider="custom",
            argv=[sys.executable, "-c", program, "{prompt}"],
            resume_argv=[sys.executable, "-c", program, "{thread_id}", "{prompt}"],
        )

    def jobs(self):
        return json.loads(
            (project_data_dir(self.data, "sample") / "worker-jobs.json").read_text()
        )

    def wait_for_job(self):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            job = self.jobs().get("task", {})
            if job.get("status") not in (None, "dispatched", "running"):
                return job
            time.sleep(0.1)
        self.fail("the queued worker did not finish")

    def run_queue(self):
        return queue.run(
            self.root,
            data_root=self.data,
            project_id="sample",
            max_launches=1,
        )

    def test_a_queued_request_starts_a_real_worker(self):
        queue.enqueue(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="fake",
            request_id="first",
        )
        result = self.run_queue()
        self.assertEqual(result.get("blocked", []), [], result)
        self.wait_for_job()
        self.assertTrue(self.marker.is_file())

    def test_a_retry_on_the_same_model_continues_the_session(self):
        first = dispatch(
            self.root, data_root=self.data, project_id="sample", task_id="task",
            model="fake", background=True,
        )
        self.assertTrue(first["executed"])
        self.wait_for_job()
        self.marker.unlink()
        queue.enqueue(
            self.root,
            data_root=self.data,
            project_id="sample",
            task_id="task",
            model="fake",
            request_id="retry",
        )
        result = self.run_queue()
        self.assertEqual(result.get("blocked", []), [], result)
        self.wait_for_job()
        self.assertTrue(self.marker.is_file())


class FreshStartTests(unittest.TestCase):
    def test_a_run_that_left_nothing_can_be_restarted(self):
        from continuity_plane.worker_dispatch import _left_work

        with tempfile.TemporaryDirectory() as folder:
            empty = Path(folder) / "thread"
            empty.write_text("")
            self.assertFalse(_left_work({"thread_file": str(empty)}))
            self.assertFalse(_left_work({"thread_file": str(Path(folder) / "missing")}))
            self.assertFalse(_left_work(None))
            empty.write_text("thread-1")
            self.assertTrue(_left_work({"thread_file": str(empty)}))


if __name__ == "__main__":
    unittest.main()
