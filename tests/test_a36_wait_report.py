"""Alpha36: waiting happens outside the model and reports are read as summaries."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from continuity_plane import agent_tools
from continuity_plane.cli import main
from continuity_plane.collaboration_registry import (
    _record_change,
    add_task,
    register_project,
    update_task,
)


def run_cli(*argv):
    output, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = main(list(argv))
    return code, output.getvalue()


def lines(text):
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class WorkerWaitTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.data = self.base / "data"
        for project in ("alpha", "beta"):
            root = self.base / project
            root.mkdir()
            register_project(self.data, project_id=project, control_root=root)

    def finish(self, task, project="alpha", **fields):
        fields.setdefault("status", "completed")
        _record_change(
            self.data, project, "attempt.finished", task, attempt_id=f"a-{task}", **fields
        )

    def wait(self, *extra):
        return run_cli(
            "worker", "wait", "--data-root", str(self.data), "--poll-seconds", "0.05", *extra
        )

    def test_starts_at_the_tail_and_times_out_with_exit_3(self):
        self.finish("old")
        code, out = self.wait("--project-id", "alpha", "--timeout-seconds", "0.3")
        self.assertEqual(code, 3)
        (row,) = lines(out)
        self.assertEqual(row["status"], "timeout")
        self.assertEqual(row["emitted"], 0)

    def test_wakes_on_the_awaited_task_only(self):
        def later():
            time.sleep(0.2)
            _record_change(self.data, "alpha", "attempt.heartbeat", "t1", attempt_id="h")
            self.finish("other")
            self.finish("t1", status="failed", failure_class="authentication")

        threading.Thread(target=later, daemon=True).start()
        code, out = self.wait(
            "--project-id", "alpha", "--task-id", "t1", "--timeout-seconds", "10"
        )
        self.assertEqual(code, 0)
        (row,) = lines(out)
        self.assertEqual(
            (row["project_id"], row["task_id"], row["kind"], row["failure_class"]),
            ("alpha", "t1", "failed", "authentication"),
        )
        self.assertEqual(
            row["next"], "continuity_worker_report(project_id='alpha', task_id='t1')"
        )

    def test_any_task_across_projects_and_max_events(self):
        def later():
            time.sleep(0.2)
            self.finish("x", project="beta")
            self.finish("y", project="alpha")

        threading.Thread(target=later, daemon=True).start()
        code, out = self.wait("--all-projects", "--max-events", "2", "--timeout-seconds", "10")
        self.assertEqual(code, 0)
        self.assertEqual(
            sorted((r["project_id"], r["task_id"]) for r in lines(out)),
            [("alpha", "y"), ("beta", "x")],
        )

    def test_blocked_and_rate_limited_are_terminal(self):
        def later():
            time.sleep(0.2)
            self.finish("rl", status="failed", failure_class="rate-limit")

        threading.Thread(target=later, daemon=True).start()
        code, out = self.wait("--project-id", "alpha", "--timeout-seconds", "10")
        (row,) = lines(out)
        self.assertEqual((code, row["kind"]), (0, "rate-limited"))
        self.assertEqual(row["next"], "continuity_worker_jobs(project_id='alpha')")

    def test_since_cursor_resumes_without_missing_the_gap(self):
        self.finish("first")
        code, out = self.wait(
            "--project-id", "alpha", "--since-cursor", "0", "--timeout-seconds", "5"
        )
        (first,) = lines(out)
        self.finish("second")  # lands between two waits
        code, out = self.wait(
            "--project-id",
            "alpha",
            "--since-cursor",
            first["stream_cursor"],
            "--timeout-seconds",
            "5",
        )
        self.assertEqual(code, 0)
        self.assertEqual(lines(out)[0]["task_id"], "second")

    def test_rejects_unknown_project_and_bad_limits(self):
        with self.assertRaises(Exception):
            self.wait("--project-id", "missing", "--timeout-seconds", "1")
        with self.assertRaises(ValueError):
            self.wait("--project-id", "alpha", "--max-events", "0")

    def test_background_process_without_a_tty_flushes_and_exits_0(self):
        env = {**os.environ, "CONTINUITY_DATA_ROOT": str(self.data)}
        process = subprocess.Popen(
            [
                sys.executable, "-c",
                "import sys; from continuity_plane.cli import entrypoint; "
                "sys.exit(entrypoint(sys.argv[1:]))",
                "worker", "wait", "--project-id", "alpha", "--task-id", "bg",
                "--poll-seconds", "0.05", "--timeout-seconds", "30",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=Path(__file__).resolve().parents[1],
        )
        self.addCleanup(process.kill)
        time.sleep(1.0)
        self.assertIsNone(process.poll())
        self.finish("bg")
        out, err = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, err)
        self.assertEqual(json.loads(out)["task_id"], "bg")


class ReportSummaryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.data = self.base / "data"
        register_project(self.data, project_id="sample", control_root=self.base)
        add_task(
            self.data,
            project_id="sample",
            task_id="task",
            lane_id="lane",
            mode="patch",
            priority="p1",
            title="T",
            objective="O",
            next_action="N",
            exit_criteria=["PASS"],
            assignee="worker",
        )
        sections = [
            f"## Section {i}\n" + (f"line {i} filler text for the report body\n" * 20)
            for i in range(30)
        ]
        self.text = "# Report\n\n```\n# not a heading\n```\n" + "\n".join(sections)
        self.text = self.text[:40000]
        update_task(
            self.data,
            project_id="sample",
            task_id="task",
            status="ready-for-review",
            report={"text": self.text},
        )

    def report(self, *extra):
        code, out = run_cli(
            "worker", "report", "--task-id", "task", "--project-id", "sample",
            "--data-root", str(self.data), *extra,
        )
        self.assertEqual(code, 0)
        return out

    def test_default_is_a_bounded_summary_with_headings(self):
        out = self.report()
        header, body = out.split("\n", 1)
        self.assertIn(f"bytes={len(self.text.encode())}", header)
        self.assertIn("summary=true", header)
        self.assertRegex(header, r"next_offset_chars=2\d{3}")
        self.assertIn("# Report @0", body)
        self.assertIn("## Section 0 @", body)
        self.assertNotIn("not a heading @", body)
        self.assertIn("more", body)  # more headings than the cap
        self.assertLess(len(out.encode()), 6000)
        self.assertLess(len(out.encode()), len(self.text.encode()) / 5)

    def test_offset_pages_and_full_still_work(self):
        out = self.report("--offset", "0")
        header, body = out.split("\n", 1)
        self.assertNotIn("summary=true", header)
        self.assertTrue(body.startswith("# Report"))
        nxt = int(header.rsplit("next_offset_chars=", 1)[1])
        self.assertTrue(self.report("--offset", str(nxt)).split("\n", 1)[1])
        full = run_cli(
            "worker", "report", "--task-id", "task", "--project-id", "sample",
            "--data-root", str(self.data), "--full", "--max-output-bytes", "65536",
        )[1]
        self.assertEqual(full.rstrip("\n"), self.text.rstrip("\n"))

    def test_heading_offsets_land_on_the_heading(self):
        body = self.report().split("\n", 1)[1]
        entry = next(l for l in body.splitlines() if l.startswith("## Section 3 @"))
        offset = int(entry.rsplit("@", 1)[1])
        self.assertTrue(self.text[offset:].startswith("## Section 3"))

    def test_mcp_tool_uses_the_summary_default(self):
        old = os.environ.get("CONTINUITY_DATA_ROOT")
        os.environ["CONTINUITY_DATA_ROOT"] = str(self.data)
        self.addCleanup(
            lambda: os.environ.__setitem__("CONTINUITY_DATA_ROOT", old)
            if old is not None
            else os.environ.pop("CONTINUITY_DATA_ROOT", None)
        )
        value = agent_tools.handle(
            "continuity_worker_report", {"project_id": "sample", "task_id": "task"}
        )
        self.assertIn("summary=true", value["text"].split("\n", 1)[0])
        self.assertLess(len(value["text"].encode()), 6000)


class WordingTests(unittest.TestCase):
    def test_tool_text_says_to_wait_outside_the_model(self):
        self.assertIn("worker wait", agent_tools.INSTRUCTIONS)
        description = agent_tools.BY_NAME["continuity_events_wait"].description
        self.assertIn("once", description)
        self.assertIn("worker wait", description)

    def test_skills_stay_small_and_distinct(self):
        base = Path(__file__).resolve().parents[1]
        # Source tree keeps Codex plugins under integrations/codex; the public
        # mirror ships them under plugins/.
        codex = base / "integrations/codex/continuity-plane-state"
        if not codex.is_dir():
            codex = base / "plugins/continuity-plane-state"
        paths = [
            base / "integrations/claude/continuity-plane/skills/continuity-collaboration/SKILL.md",
            codex / "skills/continuity-plane/SKILL.md",
        ]
        for path in paths:
            text = path.read_text()
            self.assertLessEqual(len(text.encode()), 2500, path)
            self.assertIn("worker wait", text)


if __name__ == "__main__":
    unittest.main()
