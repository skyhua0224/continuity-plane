"""Generic regressions for typed onboarding, inbox and mixed-language hints."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from continuity_plane.cli import main
from continuity_plane.collaboration_registry import (
    add_task,
    claim_task,
    list_tasks,
    register_project,
    update_task,
    CollaborationRegistryError,
)
from continuity_plane.collaboration_inbox import (
    send_message,
    list_messages,
    acknowledge,
)
from continuity_plane.collaboration_packet import compose_collaboration_packet
from continuity_plane.vocabulary_memory import add_entity, resolve
from continuity_plane.sqlite_state_store import SQLiteStateStore


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        register_project(self.data, project_id="sample", control_root=self.root)

    def task(self, task_id, lane="main", assignee=None):
        return add_task(
            self.data,
            project_id="sample",
            task_id=task_id,
            lane_id=lane,
            mode="patch",
            priority="p2",
            title="Test",
            objective="Test onboarding",
            next_action="Run tests",
            exit_criteria=["pass"],
            assignee=assignee,
        )

    def call(self, args):
        with redirect_stdout(StringIO()) as output:
            self.assertEqual(main(args), 0)
        return json.loads(output.getvalue())

    def test_update_can_assign_and_activate(self):
        self.task("one")
        result = self.call(
            [
                "collaboration",
                "task",
                "update",
                "--data-root",
                str(self.data),
                "--root",
                str(self.root),
                "--task-id",
                "one",
                "--status",
                "active",
                "--assignee",
                "agent/child",
            ]
        )
        self.assertEqual(result["assignee"], "agent/child")

    def test_same_actor_different_lanes_allowed_same_lane_rejected(self):
        self.task("one", assignee="agent")
        self.task("two", lane="secondary", assignee="agent")
        self.task("three")
        with self.assertRaisesRegex(CollaborationRegistryError, "per lane"):
            update_task(
                self.data,
                project_id="sample",
                task_id="three",
                status="active",
                assignee="agent",
            )
        self.assertEqual(
            list_tasks(self.data, project_id="sample")[-1]["status"], "queued"
        )

    def test_update_cannot_take_over_other_actor(self):
        self.task("one", assignee="owner")
        with self.assertRaisesRegex(CollaborationRegistryError, "another session"):
            update_task(
                self.data,
                project_id="sample",
                task_id="one",
                status="active",
                assignee="intruder",
            )

    def test_concurrent_claim_same_task_has_one_owner(self):
        self.task("one")

        def claim(actor):
            try:
                claim_task(
                    self.data, project_id="sample", task_id="one", assignee=actor
                )
                return True
            except CollaborationRegistryError:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(claim, [f"actor-{i}" for i in range(8)])), 1)

    def test_cli_expected_error_has_no_traceback_and_actionable_message(self):
        self.task("one")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuity_plane.cli",
                "collaboration",
                "task",
                "update",
                "--data-root",
                str(self.data),
                "--project-id",
                "sample",
                "--task-id",
                "one",
                "--status",
                "active",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("--assignee", json.loads(result.stderr)["message"])

    def test_mixed_aliases_and_packet(self):
        add_entity(
            self.data,
            scope="project",
            project_id="sample",
            entity_id="admin",
            aliases=["Admin 方案"],
            statement="Administration plan",
            source="user",
        )
        add_entity(
            self.data,
            scope="project",
            project_id="sample",
            entity_id="a0",
            aliases=["a0"],
            statement="First step",
            source="user",
        )
        for query in [
            "继续 Admin 方案 的 A0",
            "继续ＡＤＭＩＮ\t方案的a0",
            "继续admin  方案的A0",
            "继续Admin方案的A0",
        ]:
            self.assertEqual(
                {
                    e["entity_id"]
                    for e in resolve(self.data, query, project_id="sample")
                },
                {"admin", "a0"},
            )
        self.assertEqual(
            len(
                compose_collaboration_packet(
                    self.data, project_id="sample", query="继续 Admin 方案 的 A0"
                )["vocabulary"]
            ),
            2,
        )
        self.assertEqual(resolve(self.data, "xa0y", project_id="sample"), [])

    def test_cjk_alias_matches_inside_unspaced_sentence(self):
        add_entity(self.data, scope="project", project_id="sample", entity_id="three-config", aliases=["三配置"], statement="Three configuration rule", source="user")
        matches = resolve(self.data, "三配置过了再推 main；PFC 不改", project_id="sample")
        self.assertEqual([item["entity_id"] for item in matches], ["three-config"])

    def test_project_alias_shadows_normalized_global_phrase(self):
        add_entity(
            self.data,
            scope="global",
            entity_id="global",
            aliases=["ADMIN  方案"],
            statement="Global",
            source="user",
        )
        add_entity(
            self.data,
            scope="project",
            project_id="sample",
            entity_id="project",
            aliases=["admin 方案"],
            statement="Project",
            source="user",
        )
        self.assertEqual(
            [
                e["entity_id"]
                for e in resolve(self.data, "继续 Admin 方案", project_id="sample")
            ],
            ["project"],
        )

    def test_inbox_idempotent_isolated_acknowledged_and_packet_bound(self):
        args = dict(
            project_id="sample",
            message_id="handoff-1",
            sender="author",
            recipient="worker",
            body="Continue stage two",
        )
        first = send_message(self.data, **args)
        self.assertEqual(send_message(self.data, **args), first)
        self.assertEqual(
            list_messages(self.data, project_id="sample", recipient="other"), []
        )
        packet = compose_collaboration_packet(
            self.data, project_id="sample", assignee="worker"
        )
        self.assertEqual(packet["inbox"][0]["message_id"], "handoff-1")
        with self.assertRaises(CollaborationRegistryError):
            acknowledge(
                self.data,
                project_id="sample",
                recipient="other",
                message_id="handoff-1",
            )
        acknowledge(
            self.data, project_id="sample", recipient="worker", message_id="handoff-1"
        )
        self.assertEqual(
            list_messages(self.data, project_id="sample", recipient="worker"), []
        )
        with self.assertRaises(CollaborationRegistryError):
            send_message(self.data, **{**args, "body": "Different"})

    def test_concurrent_sends_are_not_lost(self):
        def send(i):
            return send_message(
                self.data,
                project_id="sample",
                message_id=f"message-{i}",
                sender="author",
                recipient="worker",
                body="hint",
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(send, range(24)))
        self.assertEqual(
            len(
                list_messages(
                    self.data, project_id="sample", recipient="worker", limit=100
                )
            ),
            24,
        )

    def test_inbox_survives_process_restart(self):
        send_message(
            self.data,
            project_id="sample",
            message_id="persistent",
            sender="author",
            recipient="worker",
            body="Continue",
        )
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "continuity_plane.cli",
                "collaboration",
                "inbox",
                "list",
                "--data-root",
                str(self.data),
                "--project-id",
                "sample",
                "--recipient",
                "worker",
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["messages"][0]["message_id"], "persistent"
        )

    def test_inbox_rejects_credentials_and_corrupt_documents(self):
        with self.assertRaisesRegex(CollaborationRegistryError, "credential"):
            send_message(
                self.data,
                project_id="sample",
                message_id="secret",
                sender="author",
                recipient="worker",
                body="password=example",
            )
        path = self.data / "projects/sample/inbox.json"
        path.write_text(
            '{"schema_version":"context.collaboration-inbox/v1alpha1","messages":[{}]}',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(CollaborationRegistryError, "fields"):
            list_messages(self.data, project_id="sample", recipient="worker")

    def test_typed_records_preview_commit_replay_and_checkpoint(self):
        self.call(["init", "--root", str(self.root), "--project-id", "sample"])
        source = ".continuity/MASTER.md"
        common = ["--root", str(self.root), "--source-file", source]
        work = ["work", "add", "--work-id", "stage-a", "--title", "Stage A", *common]
        store = SQLiteStateStore(self.root / ".continuity/state.sqlite3")
        before = store.read_project("sample")["project"]["revision"]
        preview = self.call(work)
        self.assertEqual(preview["status"], "preview")
        self.assertEqual(store.read_project("sample")["project"]["revision"], before)
        added = self.call(work + ["--apply", "--expected-revision", str(before)])
        self.assertEqual(added["revision"], before + 1)
        self.assertEqual(
            self.call(work + ["--apply", "--expected-revision", str(before)])["status"],
            "unchanged",
        )
        self.call(
            [
                "decision",
                "add",
                "--decision-id",
                "choice-a",
                "--work-id",
                "stage-a",
                "--statement",
                "Use local metadata",
                "--record-status",
                "accepted",
                *common,
                "--apply",
                "--expected-revision",
                str(before + 1),
            ]
        )
        self.call(
            [
                "constraint",
                "add",
                "--constraint-id",
                "boundary-a",
                "--work-id",
                "stage-a",
                "--statement",
                "No provider configuration changes",
                *common,
                "--apply",
                "--expected-revision",
                str(before + 2),
            ]
        )
        snapshot = store.read_project("sample")
        self.assertIn("choice-a", snapshot["project"]["current_decision_ids"])
        self.assertEqual(snapshot["project"]["active_work_ids"], [])
        self.assertEqual(snapshot["claims"], [])
        self.assertEqual(
            self.call(["checkpoint", "verify", "--root", str(self.root)])["status"],
            "verified",
        )

    def test_state_add_conflict_and_missing_reference_do_not_write(self):
        self.call(["init", "--root", str(self.root), "--project-id", "sample"])
        args = [
            "work",
            "add",
            "--root",
            str(self.root),
            "--source-file",
            ".continuity/MASTER.md",
            "--work-id",
            "child",
            "--title",
            "Child",
            "--parent-work-id",
            "missing",
        ]
        with self.assertRaisesRegex(ValueError, "not in State"):
            self.call(args)
        args = args[:-2]
        with self.assertRaisesRegex(ValueError, "stale"):
            self.call(args + ["--apply", "--expected-revision", "0"])
        self.assertEqual(
            SQLiteStateStore(self.root / ".continuity/state.sqlite3").read_project(
                "sample"
            )["project"]["revision"],
            1,
        )

    def test_added_work_can_activate_and_additions_preserve_live_claim(self):
        self.call(["init", "--root", str(self.root), "--project-id", "sample"])
        common = ["--root", str(self.root), "--source-file", ".continuity/MASTER.md"]
        self.call(
            [
                "work",
                "add",
                "--work-id",
                "stage-a",
                "--title",
                "Stage A",
                "--scope",
                "testing",
                *common,
                "--apply",
                "--expected-revision",
                "1",
            ]
        )
        self.call(
            [
                "work",
                "activate",
                "--root",
                str(self.root),
                "--work-id",
                "stage-a",
                "--work-title",
                "Stage A",
                "--owner-ref",
                "agent",
                "--claim-id",
                "claim-a",
                "--scope",
                "capability:testing",
            ]
        )
        store = SQLiteStateStore(self.root / ".continuity/state.sqlite3")
        before = store.read_project("sample")
        revision = before["project"]["revision"]
        self.call(
            [
                "decision",
                "add",
                "--decision-id",
                "choice-a",
                "--work-id",
                "stage-a",
                "--statement",
                "Keep current plan",
                *common,
                "--apply",
                "--expected-revision",
                str(revision),
            ]
        )
        after = store.read_project("sample")
        self.assertEqual(after["project"]["active_work_ids"], ["stage-a"])
        self.assertEqual(after["claims"][0]["claim_id"], "claim-a")
        self.assertEqual(after["claims"][0]["expected_project_revision"], revision + 1)
        self.assertEqual(
            after["claims"][0]["scope_owners"], before["claims"][0]["scope_owners"]
        )
        self.assertEqual(
            self.call(["checkpoint", "verify", "--root", str(self.root)])["status"],
            "verified",
        )

    def test_pending_dependencies_cannot_activate(self):
        self.call(["init", "--root", str(self.root), "--project-id", "sample"])
        common = ["--root", str(self.root), "--source-file", ".continuity/MASTER.md"]
        self.call(
            [
                "work",
                "add",
                "--work-id",
                "child",
                "--title",
                "Child",
                "--scope",
                "testing",
                "--dependency",
                "work-initial",
                *common,
                "--apply",
                "--expected-revision",
                "1",
            ]
        )
        with redirect_stdout(StringIO()) as output:
            result = main(
                [
                    "work",
                    "activate",
                    "--root",
                    str(self.root),
                    "--work-id",
                    "child",
                    "--work-title",
                    "Child",
                    "--owner-ref",
                    "agent",
                    "--claim-id",
                    "claim-child",
                    "--scope",
                    "capability:testing",
                ]
            )
        self.assertNotEqual(result, 0)
        self.assertIn("work_dependencies", output.getvalue())

    def test_source_refresh_and_plan_add_share_one_event(self):
        self.call(["init", "--root", str(self.root), "--project-id", "sample"])
        self.call(
            [
                "work",
                "activate",
                "--root",
                str(self.root),
                "--work-id",
                "active",
                "--work-title",
                "Active",
                "--owner-ref",
                "agent",
                "--claim-id",
                "claim-active",
                "--scope",
                "capability:testing",
            ]
        )
        store = SQLiteStateStore(self.root / ".continuity/state.sqlite3")
        before = store.read_project("sample")
        events = len(store.read_events("sample"))
        source = self.root / ".continuity/MASTER.md"
        source.write_text(
            source.read_text(encoding="utf-8") + "\n## New plan\nStage B: local test\n",
            encoding="utf-8",
        )
        args = [
            "work",
            "add",
            "--root",
            str(self.root),
            "--source-file",
            ".continuity/MASTER.md",
            "--work-id",
            "stage-b",
            "--title",
            "Stage B",
            "--refresh-source",
        ]
        with self.assertRaisesRegex(ValueError, "active claim owner"):
            self.call(args)
        args += ["--actor-ref", "agent"]
        preview = self.call(args)
        self.assertEqual(preview["changed_sources"], ["master"])
        self.assertEqual(len(store.read_events("sample")), events)
        self.call(
            args
            + ["--apply", "--expected-revision", str(before["project"]["revision"])]
        )
        after = store.read_project("sample")
        self.assertEqual(len(store.read_events("sample")), events + 1)
        self.assertEqual(after["claims"][0]["claim_id"], "claim-active")
        self.assertEqual(
            after["claims"][0]["lease_expires_at"],
            before["claims"][0]["lease_expires_at"],
        )
        self.assertEqual(
            after["claims"][0]["scope_owners"], before["claims"][0]["scope_owners"]
        )
        packet = self.call(["resume", "--root", str(self.root)])
        self.assertTrue(packet["source_fresh"])
        self.assertTrue(packet["checkpoint_verified"])
        self.assertFalse(packet["read_only"])
        self.assertEqual(len(after["works"]), len(before["works"]) + 1)

    def test_status_default_renders_without_state_event(self):
        self.call(["init", "--root", str(self.root), "--project-id", "sample"])
        self.call(["status", "--root", str(self.root)])
        self.call(["--root", str(self.root), "status"])
        with self.assertRaisesRegex(ValueError, "different projects"):
            self.call(["--root", str(self.root), "state", "show", "--root", str(self.data)])
        self.assertEqual(
            len(
                SQLiteStateStore(self.root / ".continuity/state.sqlite3").read_events(
                    "sample"
                )
            ),
            1,
        )

    def test_mcp_inbox_works_without_state_binding(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "continuity_inbox",
                    "arguments": {
                        "root": str(self.root),
                        "action": "send",
                        "sender": "author",
                        "recipient": "worker",
                        "message_id": "mcp-message",
                        "body": "Continue",
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "continuity_collaboration_packet",
                    "arguments": {"root": str(self.root), "assignee": "worker"},
                },
            },
        ]
        env = {**os.environ, "CONTINUITY_DATA_ROOT": str(self.data)}
        result = subprocess.run(
            [sys.executable, "-m", "continuity_plane.codex_mcp_server"],
            input="\n".join(json.dumps(r) for r in requests) + "\n",
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertIn(
            "continuity_inbox", [t["name"] for t in replies[0]["result"]["tools"]]
        )
        self.assertNotIn("error", replies[1])
        packet = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(packet["inbox"][0]["message_id"], "mcp-message")


if __name__ == "__main__":
    unittest.main()
