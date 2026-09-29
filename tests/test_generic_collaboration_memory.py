from __future__ import annotations

import json

import os
import tempfile
import unittest
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from context_control_plane.collaboration_registry import (
    CollaborationRegistryError,
    add_task,
    claim_task,
    next_task,
    register_project,
    resolve_project,
    update_task,
)
from context_control_plane.collaboration_registry import (
    list_effects,
    request_effect,
    update_effect,
)
from context_control_plane.collaboration_packet import compose_collaboration_packet
from context_control_plane.collaboration_packet import compose_safe_collaboration_packet
from context_control_plane.skills_inventory import audit_skills
from context_control_plane.vocabulary_memory import add_entity, resolve
from context_control_plane.workspace_inventory import inventory_worktrees


class GenericCollaborationTests(unittest.TestCase):
    def test_project_registration_resolves_registered_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory) / "data"
            control = Path(directory) / "control"
            repository = Path(directory) / "repository"
            control.mkdir()
            repository.mkdir()
            document = register_project(
                data,
                project_id="sample",
                control_root=control,
                repository_roots=[repository],
            )
            self.assertEqual(resolve_project(repository, data), document)
            self.assertIsNone(resolve_project(data, data))

    def test_task_can_reach_ready_for_review_but_not_self_accept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            register_project(
                data,
                project_id="sample",
                control_root=directory,
                repository_roots=[],
            )
            task = add_task(
                data,
                project_id="sample",
                task_id="narrow-fix",
                lane_id="release",
                mode="patch",
                priority="p0",
                title="Narrow fix",
                objective="Fix one behavior",
                next_action="Add focused test",
                exit_criteria=["Focused test passes"],
                assignee="session-a",
            )
            self.assertEqual(task["status"], "active")
            ready = update_task(
                data,
                project_id="sample",
                task_id="narrow-fix",
                status="ready-for-review",
                result="Focused test passed",
            )
            self.assertEqual(ready["status"], "ready-for-review")
            with self.assertRaisesRegex(
                CollaborationRegistryError, "acceptance requires project review"
            ):
                update_task(
                    data,
                    project_id="sample",
                    task_id="narrow-fix",
                    status="accepted",
                )

    def test_degraded_metadata_remains_non_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            register_project(
                data, project_id="sample", control_root=directory, repository_roots=[]
            )
            ledger = data / "projects" / "sample" / "dispatch.json"
            ledger.write_text("{", encoding="utf-8")
            packet = compose_safe_collaboration_packet(
                data, project_id="sample", assignee="session-a"
            )
            self.assertTrue(packet["continue"])
            self.assertEqual(packet["metadata_status"], "degraded")

    def test_effects_are_queued_and_never_executed_by_the_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            register_project(
                data, project_id="sample", control_root=directory, repository_roots=[]
            )
            add_task(
                data,
                project_id="sample",
                task_id="patch",
                lane_id="release",
                mode="patch",
                priority="p0",
                title="Patch",
                objective="Prepare patch",
                next_action="Run tests",
                exit_criteria=["Tests pass"],
                assignee="session-a",
            )
            queued = request_effect(
                data,
                project_id="sample",
                effect_id="push-1",
                task_id="patch",
                requested_by="session-a",
                effect="source-control.push",
                target="refs/heads/feature",
                reason="Tests passed",
            )
            self.assertEqual(queued["status"], "queued")
            self.assertEqual(list_effects(data, project_id="sample")[0]["effect_id"], "push-1")
            receipt = update_effect(
                data,
                project_id="sample",
                effect_id="push-1",
                status="approved",
            )
            self.assertEqual(receipt["status"], "approved")

    def test_assignee_has_one_active_card_and_priority_orders_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            register_project(
                data, project_id="sample", control_root=directory, repository_roots=[]
            )
            add_task(
                data,
                project_id="sample",
                task_id="first",
                lane_id="release",
                mode="scout",
                priority="p0",
                title="First",
                objective="Investigate",
                next_action="Read one file",
                exit_criteria=["Find one fact"],
                assignee="session-a",
            )
            with self.assertRaises(CollaborationRegistryError):
                add_task(
                    data,
                    project_id="sample",
                    task_id="second",
                    lane_id="release",
                    mode="scout",
                    priority="p0",
                    title="Second",
                    objective="Investigate",
                    next_action="Read another file",
                    exit_criteria=["Find another fact"],
                    assignee="session-a",
                )
            add_task(
                data,
                project_id="sample",
                task_id="later",
                lane_id="release",
                mode="scout",
                priority="p2",
                title="Later",
                objective="Investigate later",
                next_action="Wait",
                exit_criteria=["Nothing"],
            )
            add_task(
                data,
                project_id="sample",
                task_id="urgent",
                lane_id="release",
                mode="scout",
                priority="p0",
                title="Urgent",
                objective="Investigate urgent",
                next_action="Read urgent file",
                exit_criteria=["Find urgent fact"],
            )
            self.assertEqual(next_task(data, project_id="sample")["task_id"], "urgent")
            claimed = claim_task(
                data,
                project_id="sample",
                task_id="urgent",
                assignee="session-b",
            )
            self.assertEqual(claimed["status"], "active")
            self.assertEqual(claimed["report_policy"], "silent_until_stage_complete")


class VocabularyMemoryTests(unittest.TestCase):
    def test_project_alias_overrides_global_and_secrets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            add_entity(
                data,
                entity_id="edge",
                aliases=["edge"],
                statement="Global edge meaning",
                scope="global",
                source="documentation",
            )
            add_entity(
                data,
                entity_id="project-edge",
                aliases=["edge"],
                statement="The project edge API gateway",
                scope="project",
                project_id="sample",
                source="user",
                confidence="confirmed",
            )
            matches = resolve(data, "inspect the edge endpoint", project_id="sample")
            self.assertEqual([item["entity_id"] for item in matches], ["project-edge"])
            packet = compose_collaboration_packet(
                data,
                project_id="sample",
                query="inspect the edge endpoint",
            )
            self.assertIsNone(packet["task"])
            self.assertEqual(
                [item["entity_id"] for item in packet["vocabulary"]],
                ["project-edge"],
            )
            self.assertTrue(
                packet["execution_contract"][
                    "local_work_continues_when_metadata_is_missing"
                ]
            )
            with self.assertRaisesRegex(Exception, "secret values are forbidden"):
                add_entity(
                    data,
                    entity_id="unsafe",
                    aliases=["unsafe"],
                    statement="token = abc",
                    scope="global",
                    source="user",
                )


class InventoryTests(unittest.TestCase):
    def test_skills_audit_is_report_only_and_detects_metadata_issues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "skills"
            good = root / "good" / "SKILL.md"
            bad = root / "bad" / "SKILL.md"
            good.parent.mkdir(parents=True)
            bad.parent.mkdir()
            good.write_text(
                "---\nname: good\ndescription: Do one narrow task.\n---\nBody",
                encoding="utf-8",
            )
            bad.write_text(
                '---\nname: bad\ndescription: ">-"\n---\nAlways active body',
                encoding="utf-8",
            )
            report = audit_skills([root], description_budget=8)
            self.assertTrue(report["dry_run"])
            self.assertEqual(report["skill_count"], 2)
            self.assertTrue(report["description_budget_exceeded"])
            actions = {
                item["path"]: item["action"] for item in report["recommendations"]
            }
            self.assertEqual(actions[str(bad)], "repair-or-disable")

    def test_worktree_inventory_never_marks_dirty_or_missing_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            clean = Path(directory) / "clean"
            dirty = Path(directory) / "dirty"
            missing = Path(directory) / "missing"
            root.mkdir()
            clean.mkdir()
            dirty.mkdir()
            os.utime(clean, (0, 0))
            porcelain = "\n".join(
                [
                    f"worktree {root}",
                    f"worktree {clean}",
                    "branch refs/heads/feat/example",
                    f"worktree {dirty}",
                    "detached",
                    f"worktree {missing}",
                ]
            )

            def fake_git(current: Path, *arguments: str) -> str:
                if arguments == ("worktree", "list", "--porcelain"):
                    return porcelain + "\n"
                if arguments[:2] == ("status", "--porcelain=v1"):
                    return " M file\n" if current == dirty else ""
                raise AssertionError(arguments)

            with patch(
                "context_control_plane.workspace_inventory._git",
                side_effect=fake_git,
            ):
                report = inventory_worktrees(root, stale_days=14)
            self.assertTrue(report["dry_run"])
            classifications = {
                Path(item["path"]).name or "repo": item["classification"]
                for item in report["worktrees"]
            }
            self.assertEqual(classifications["clean"], "clean-stale")
            self.assertEqual(classifications["dirty"], "dirty-hold")
            self.assertEqual(classifications["missing"], "missing")
            self.assertEqual(report["cleanup_safe_count"], 1)

    def test_user_prompt_hook_injects_task_and_vocabulary_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            data = Path(directory) / "continuity-data"
            plugin_data = Path(directory) / "plugin-data"
            root.mkdir()
            plugin_data.mkdir()
            register_project(data, project_id="sample", control_root=root)
            add_entity(
                data,
                entity_id="edge",
                aliases=["edge"],
                statement="The project edge API gateway.",
                scope="global",
                source="user",
                confidence="confirmed",
            )
            add_task(
                data,
                project_id="sample",
                task_id="investigate",
                lane_id="release",
                mode="scout",
                priority="p0",
                title="Investigate",
                objective="Find one fact",
                next_action="Inspect the edge endpoint",
                exit_criteria=["One fact found"],
                assignee="hook-session",
            )
            payload = {
                "cwd": str(root),
                "session_id": "hook-session",
                "hook_event_name": "UserPromptSubmit",
                "prompt": "inspect the edge endpoint",
            }
            hook = (
                Path(__file__).resolve().parents[1]
                / "integrations/codex/continuity-plane/scripts/continuity-hook.py"
            )
            command = [sys.executable, str(hook)]
            environment = {
                **os.environ,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "CONTINUITY_DATA_ROOT": str(data),
                "PLUGIN_DATA": str(plugin_data),
                "CONTINUITY_EFFECT_POLICY": "auto",
            }
            first = subprocess.run(
                command,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=environment,
                timeout=5,
                check=True,
            )
            context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
            self.assertIn("Task: investigate", context)
            self.assertIn("The project edge API gateway.", context)
            second = subprocess.run(
                command,
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=environment,
                timeout=5,
                check=True,
            )
            self.assertEqual(second.stdout, "")

    def test_mcp_collaboration_packet_is_read_only_and_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            data = Path(directory) / "data"
            root.mkdir()
            register_project(data, project_id="sample", control_root=root)
            add_task(
                data,
                project_id="sample",
                task_id="mcp-task",
                lane_id="release",
                mode="scout",
                priority="p0",
                title="MCP task",
                objective="Find one fact",
                next_action="Inspect one file",
                exit_criteria=["Fact recorded"],
                assignee="mcp-session",
            )
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05"},
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_collaboration_packet",
                        "arguments": {"root": str(root), "assignee": "mcp-session"},
                    },
                },
            ]
            environment = {
                **os.environ,
                "CONTINUITY_DATA_ROOT": str(data),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
            }
            completed = subprocess.run(
                [sys.executable, "-m", "context_control_plane.codex_mcp_server"],
                input="".join(json.dumps(item) + chr(10) for item in requests),
                capture_output=True,
                text=True,
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                timeout=10,
                check=True,
            )
            packet = json.loads(completed.stdout.splitlines()[-1])["result"][
                "structuredContent"
            ]
            self.assertEqual(
                packet["schema_version"], "context.collaboration-packet/v1alpha1"
            )
            self.assertEqual(packet["task"]["task_id"], "mcp-task")
            self.assertEqual(
                packet["execution_contract"]["authority"],
                "collaboration-hint-only",
            )
            self.assertLess(len(completed.stdout.encode("utf-8")), 16_384)


if __name__ == "__main__":
    unittest.main()
