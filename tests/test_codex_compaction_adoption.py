"""Continuity remains discoverable after compaction without governing shell work."""

from __future__ import annotations

import json
import io
import subprocess
import unittest
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tests import test_m10_11_codex_plugin_lifecycle as lifecycle


class CompactionAdoptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        lifecycle.M1011CodexPluginLifecycleTests.setUpClass()

    def setUp(self) -> None:
        self.fixture = lifecycle.M1011CodexPluginLifecycleTests()

    def context(self, completed: subprocess.CompletedProcess[str]) -> str:
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("hookSpecificOutput", completed.stdout)
        output = json.loads(completed.stdout)
        self.assertTrue(output["continue"])
        self.assertNotIn("systemMessage", output)
        self.assertNotIn("permissionDecision", completed.stdout)
        return output["hookSpecificOutput"]["additionalContext"]

    def test_compact_preserves_lookup_and_no_setup_loop_rules(self) -> None:
        completed, calls, _ = self.fixture._run_hook("SessionStart", effect_policy="auto")
        context = self.context(completed)
        self.assertIn("continuity_context_lookup", context)
        self.assertIn("Do not reinstall", context)
        self.assertIn("Current user intent wins", context)
        self.assertIn("Continue silently", context)
        self.assertEqual(len(calls), 1)

    def test_unavailable_state_keeps_bounded_non_blocking_entry(self) -> None:
        completed, calls, observations = self.fixture._run_hook(
            "SessionStart", effect_policy="auto", fail_resume=True
        )
        context = self.context(completed)
        self.assertIn("continuity_context_lookup", context)
        self.assertIn("Continue ordinary project work", context)
        self.assertNotIn("work-active", context)
        self.assertLessEqual(len(context.encode()), 1024)
        self.assertEqual(len(calls), 1)
        receipt = json.loads(observations.splitlines()[-1])
        self.assertEqual(receipt["failed_gate"], "resume_unavailable")
        self.assertTrue(receipt["context_emitted"])
        self.assertFalse(receipt["success"])

    def test_stale_small_project_keeps_tools_without_old_work(self) -> None:
        completed, calls, _ = self.fixture._run_hook(
            "SessionStart", effect_policy="auto", always_stale=True
        )
        context = self.context(completed)
        self.assertIn("continuity_context_lookup", context)
        self.assertIn("native continuation", context)
        self.assertNotIn("work-active", context)
        self.assertNotIn("remain-read-only", context)
        self.assertEqual(len(calls), 1)

    def test_invalid_packet_still_keeps_retrieval_entry(self) -> None:
        completed, _, observations = self.fixture._run_hook(
            "SessionStart", effect_policy="auto", resume_packet={}
        )
        self.assertIn("continuity_context_lookup", self.context(completed))
        self.assertEqual(json.loads(observations.splitlines()[-1])["failed_gate"], "resume_packet")

    def test_idle_project_does_not_require_active_claim_for_adoption(self) -> None:
        completed, _, _ = self.fixture._run_hook(
            "SessionStart", effect_policy="auto", resume_packet={
                "project_id": "portable-project", "revision": 8,
                "active_work": None, "claim": None, "next_action": "activate-next-work",
                "source_fresh": True, "read_only": False,
            }
        )
        context = self.context(completed)
        self.assertIn("continuity_context_lookup", context)
        self.assertNotIn("activate-next-work", context)

    def test_timeout_does_not_remove_continuity_entry(self) -> None:
        module = self.fixture._hook_module()
        output = io.StringIO()
        payload = {"cwd": "/tmp/sample", "hook_event_name": "SessionStart", "source": "compact"}
        with (
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(module.sys, "stdout", output),
            mock.patch.object(module, "_project_root", return_value=Path("/tmp/sample")),
            mock.patch.object(module, "_command", side_effect=subprocess.TimeoutExpired("continuity", 3)),
            mock.patch.object(module, "_effect_policy", return_value="auto"),
        ):
            self.assertEqual(module.main(), 0)
        self.assertIn("continuity_context_lookup", output.getvalue())

    def test_pending_boundary_is_atomically_consumed_once(self) -> None:
        module = self.fixture._hook_module()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"PLUGIN_DATA": directory}):
            root = Path(directory) / "repo"
            payload = {"session_id": "session-a"}
            module._write_continuation_pending(payload, root)
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: module._take_continuation_pending(payload, root), range(16)))
            self.assertEqual(sum(item is not None for item in results), 1)

    def test_pending_is_scoped_expiring_and_does_not_store_work(self) -> None:
        module = self.fixture._hook_module()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"PLUGIN_DATA": directory}):
            root = Path(directory) / "repo"
            payload = {"session_id": "session-a"}
            module._write_continuation_pending(payload, root)
            self.assertIsNone(module._take_continuation_pending({"session_id": "session-b"}, root))
            self.assertIsNone(module._take_continuation_pending(payload, root / "other"))
            path = module._continuation_pending_path(payload, root)
            content = path.read_text()
            for forbidden in ("session-a", str(root), "claim", "active_work", "packet"):
                self.assertNotIn(forbidden, content)
            with mock.patch.object(module.time, "time", return_value=module.time.time() + 601):
                self.assertIsNone(module._take_continuation_pending(payload, root))
            self.assertFalse(path.exists())

    def test_no_pending_prompt_has_no_commands_or_context(self) -> None:
        module = self.fixture._hook_module()
        payload = {"session_id": "session-a", "cwd": "/tmp/sample", "hook_event_name": "UserPromptSubmit"}
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.dict(os.environ, {"PLUGIN_DATA": directory, "CONTINUITY_EFFECT_POLICY": "auto"}),
            mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
            mock.patch.object(module.sys, "stdout", io.StringIO()) as output,
            mock.patch.object(module, "_project_root") as discovery,
            mock.patch.object(module, "_command") as command,
        ):
            self.assertEqual(module.main(), 0)
            self.assertEqual(output.getvalue(), "")
            command.assert_not_called()
            discovery.assert_not_called()

    def test_missing_entry_retries_failure_once_without_replaying_question(self) -> None:
        module = self.fixture._hook_module()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"PLUGIN_DATA": directory, "CONTINUITY_EFFECT_POLICY": "auto"}):
            root = Path(directory)
            payload = {"session_id": "session-a", "hook_event_name": "UserPromptSubmit", "prompt": "what failed?"}
            module._write_continuation_pending(payload, root)
            with (
                mock.patch.object(module, "_command", return_value=subprocess.CompletedProcess([], 9, "", "unavailable")) as command,
                mock.patch.object(module.sys, "stdout", io.StringIO()) as output,
            ):
                self.assertEqual(module._prompt_recovery(payload, root), 0)
                self.assertEqual(module._prompt_recovery(payload, root), 0)
            self.assertEqual(command.call_count, 1)
            response = json.loads(output.getvalue())
            self.assertEqual(response["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
            self.assertTrue(response["continue"])
            self.assertNotIn("what failed?", output.getvalue())

    def test_pending_discovery_failure_is_non_blocking(self) -> None:
        module = self.fixture._hook_module()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {"PLUGIN_DATA": directory, "CONTINUITY_EFFECT_POLICY": "auto"}):
            root = Path(directory)
            payload = {"session_id": "session-a", "cwd": str(root), "hook_event_name": "UserPromptSubmit"}
            module._write_continuation_pending(payload, root)
            with (
                mock.patch.object(module.sys, "stdin", io.StringIO(json.dumps(payload))),
                mock.patch.object(module.sys, "stdout", io.StringIO()) as output,
                mock.patch.object(module, "_project_root", side_effect=RuntimeError("ambiguous root")),
            ):
                self.assertEqual(module.main(), 0)
                self.assertNotIn('"continue":false', output.getvalue())

    def test_missed_compact_start_recovers_full_packet_once(self) -> None:
        module = self.fixture._hook_module()
        packet = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": {"work_id": "work-active"},
            "claim": {"claim_id": "claim-active", "actor_ref": "actor-active"},
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ,
            {"PLUGIN_DATA": directory, "CONTINUITY_EFFECT_POLICY": "auto"},
            clear=False,
        ):
            root = Path(directory) / "repo"
            root.mkdir()
            compact_payload = {
                "session_id": "session-a",
                "cwd": str(root),
                "hook_event_name": "PostCompact",
                "trigger": "auto",
            }
            prompt_payload = {
                "session_id": "session-a",
                "cwd": str(root),
                "hook_event_name": "UserPromptSubmit",
                "prompt": "resume-this-user-question",
            }
            with mock.patch.object(
                module,
                "_command",
                return_value=subprocess.CompletedProcess(
                    ["continuity"], 0, "{}\n", ""
                ),
            ) as verify:
                self.assertEqual(module._postcompact(compact_payload, root), 0)
                verify.assert_called_once()
            output = io.StringIO()
            with (
                mock.patch.object(
                    module,
                    "_command",
                    return_value=subprocess.CompletedProcess(
                        ["continuity"], 0, json.dumps(packet) + "\n", ""
                    ),
                ) as resume,
                mock.patch.object(module.sys, "stdout", output),
            ):
                self.assertEqual(module._prompt_recovery(prompt_payload, root), 0)
                self.assertEqual(module._prompt_recovery(prompt_payload, root), 0)
            self.assertEqual(resume.call_count, 1)
            response = json.loads(output.getvalue())
            context = response["hookSpecificOutput"]["additionalContext"]
            self.assertIn("work-active", context)
            self.assertIn("continue-active-work", context)
            self.assertIn("checkpoint_verified", context)
            self.assertNotIn(prompt_payload["prompt"], context)


if __name__ == "__main__":
    unittest.main()
