"""Exercise the installed tool hooks, independent of the legacy effect gate."""

from __future__ import annotations

import io
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from tests import test_m10_11_codex_plugin_lifecycle as lifecycle


class AdvisoryHookTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]
        lifecycle.M1011CodexPluginLifecycleTests.setUpClass()
        self.hook = lifecycle.M1011CodexPluginLifecycleTests()._hook_module()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "repo"
        (self.project / ".continuity").mkdir(parents=True)
        (self.project / ".continuity/project.yaml").write_text("project_id: example\n")
        env = mock.patch.dict(os.environ, {
            "PLUGIN_DATA": str(Path(self.temp.name) / "data"),
            "CONTINUITY_EFFECT_POLICY": "strict",
            "CONTINUITY_OPERATION_SAMPLE_RATE": "1",
        })
        env.start()
        self.addCleanup(env.stop)

    def payload(self, event="PreToolUse", **kwargs):
        return {"hook_event_name": event, "cwd": str(self.project),
                "session_id": "session-a", "tool_use_id": "call-a", "tool_name": "Bash",
                "tool_input": {"command": "git push origin main"}, **kwargs}

    def invoke(self, payload):
        output = io.StringIO()
        with mock.patch.object(self.hook.sys, "stdin", io.StringIO(json.dumps(payload))), \
                mock.patch.object(self.hook.sys, "stdout", output):
            result = self.hook.advisory_main()
        self.assertEqual(result, 0)
        for forbidden in ('permissionDecision', '"decision":"block"', '"continue":false', 'updatedInput'):
            self.assertNotIn(forbidden, output.getvalue())
        return output.getvalue()

    def test_manifest_separates_advisory_from_legacy_strict_handlers(self):
        config = json.loads((self.root / "integrations/codex/continuity-plane/hooks/hooks.json").read_text())
        for event, groups in config["hooks"].items():
            for handler in groups[0]["hooks"]:
                for field in ("command", "commandWindows"):
                    self.assertEqual("continuity-advisory-hook.py" in handler[field], event in {"PreToolUse", "PostToolUse"})

    def test_strict_environment_cannot_gate_business_commands(self):
        with mock.patch.object(self.hook, "_command", side_effect=AssertionError("State called")), \
                mock.patch.object(self.hook.subprocess, "run", side_effect=AssertionError("Git called")):
            for command in ("git push origin main", "tea pulls create", "ssh test-host uptime", "cat MASTER.md", "pytest", "git commit -m test"):
                for event in ("PreToolUse", "PostToolUse"):
                    self.assertEqual(self.invoke(self.payload(event, tool_input={"command": command})), "")

    def test_pending_boundary_restores_lookup_entry_without_state_once(self):
        payload = self.payload()
        self.hook._write_continuation_pending(payload, self.project)
        with mock.patch.object(self.hook, "_command", side_effect=AssertionError("State called")):
            output = self.invoke(payload)
            self.assertEqual(self.invoke(payload), "")
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("continuity_context_lookup", context)
        self.assertIn("latest user intent", context)
        self.assertNotIn("active_work", context)
        self.assertLessEqual(len(context.encode()), 1024)

    def test_delivered_fallback_clears_pending_even_when_state_unavailable(self):
        payload = self.payload("SessionStart", source="compact")
        self.hook._write_continuation_pending(payload, self.project)
        with mock.patch.dict(os.environ, {"CONTINUITY_EFFECT_POLICY": "auto"}), \
                mock.patch.object(self.hook, "_command", return_value=subprocess.CompletedProcess([], 1, "", "unavailable")), \
                mock.patch.object(self.hook.sys, "stdout", io.StringIO()):
            self.hook._session_start(payload, self.project)
        self.assertIsNone(self.hook._take_continuation_pending(payload, self.project))

    def test_telemetry_failure_never_alters_tool_result(self):
        with mock.patch.object(self.hook, "_observe", side_effect=OSError("full disk")):
            self.assertEqual(self.invoke(self.payload("PostToolUse", tool_response={"isError": True})), "")

    def test_running_command_handle_gets_one_poll_hint_without_state_access(self):
        payload = self.payload(
            "PostToolUse",
            tool_response={"session_id": 8913, "output": "", "chunk_id": "4c0a6a"},
        )
        with mock.patch.object(self.hook, "_command", side_effect=AssertionError("State called")):
            output = self.invoke(payload)
        context = json.loads(output)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("8913", context)
        self.assertIn("poll", context.lower())
        self.assertIn("before", context.lower())
        self.assertNotIn("Continuity", context)

    def test_completed_command_does_not_emit_poll_noise(self):
        payload = self.payload(
            "PostToolUse",
            tool_response={"exit_code": 0, "output": "done"},
        )
        with mock.patch.object(self.hook, "_command", side_effect=AssertionError("State called")):
            self.assertEqual(self.invoke(payload), "")

    def test_postcompact_stays_silent_after_canary_verification(self):
        payload = self.payload("PostCompact", trigger="auto")
        output = io.StringIO()
        with mock.patch.object(self.hook, "_command", return_value=subprocess.CompletedProcess([], 0, "{}\n", "")), \
                mock.patch.object(self.hook.sys, "stdout", output):
            self.assertEqual(self.hook._postcompact(payload, self.project), 0)
        self.assertEqual(output.getvalue(), "")

    def test_broad_code_read_gets_one_bounded_lookup_hint(self):
        payload = self.payload(
            "PreToolUse",
            turn_id="turn-read-1",
            tool_input={"command": "find . -type f -print"},
        )
        context = json.loads(self.invoke(payload))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("continuity_context_lookup", context)
        self.assertIn("bounded", context.lower())
        self.assertIn("narrow", context.lower())

    def test_slow_completed_command_gets_one_progress_hint(self):
        payload = self.payload(
            "PostToolUse",
            turn_id="turn-progress-1",
            tool_input={"command": "pytest -q tests/test_example.py"},
            tool_response={"exit_code": 0, "wall_time_seconds": 8.0, "output": "passed"},
        )
        context = json.loads(self.invoke(payload))["hookSpecificOutput"]["additionalContext"]
        self.assertIn("next planned action", context.lower())
        self.assertIn("continue", context.lower())

    def test_real_launcher_with_strict_env_and_broken_binding_never_denies(self):
        binding = self.hook._session_binding_path(self.payload())
        binding.parent.mkdir(parents=True)
        binding.write_text("invalid")
        result = subprocess.run([
            sys.executable, str(self.root / "context_control_plane/codex_hook_launcher.py"),
            str(self.root / "integrations/codex/continuity-plane/scripts/continuity-advisory-hook.py"),
        ], input=json.dumps(self.payload()), capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_real_cli_boundary_and_concurrent_processes_preserve_state(self):
        project = Path(self.temp.name) / "real-project"
        project.mkdir()
        cli = [sys.executable, "-m", "context_control_plane.cli"]
        initialized = subprocess.run(
            [*cli, "init", "--root", str(project), "--project-id", "probe-project"],
            cwd=self.root, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        launcher = [sys.executable, str(self.root / "context_control_plane/codex_hook_launcher.py"),
                    str(self.root / "integrations/codex/continuity-plane/scripts/continuity-hook.py")]
        environment = {**os.environ, "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                       "CONTINUITY_EFFECT_POLICY": "auto"}
        def invoke(payload, advisory=False):
            start = time.perf_counter()
            selected = [*launcher[:2], str(Path(launcher[2]).with_name("continuity-advisory-hook.py"))] if advisory else launcher
            result = subprocess.run(selected,
                                    input=json.dumps(payload), text=True, capture_output=True,
                                    env=environment, cwd=self.root, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            return result.stdout, (time.perf_counter() - start) * 1000

        base = self.payload(cwd=str(project))
        output, _ = invoke({**base, "hook_event_name": "PreCompact", "trigger": "auto"})
        self.assertEqual(output, "")
        output, _ = invoke({**base, "hook_event_name": "PostCompact", "trigger": "auto"})
        self.assertEqual(output, "")
        output, _ = invoke({**base, "hook_event_name": "SessionStart", "source": "compact"})
        self.assertIn("continuity_context_lookup", output)
        self.assertNotIn("work-active", output)
        def state_hashes():
            return {p.relative_to(project).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (project / ".continuity").rglob("*") if p.is_file()}
        before = state_hashes()
        with ThreadPoolExecutor(max_workers=4) as pool:
            race_base = {**base, "hook_event_name": "PreToolUse",
                         "turn_id": "turn-race",
                         "tool_input": {"command": "find . -type f -print"}}
            concurrent = list(pool.map(lambda i: invoke({**race_base, "tool_use_id": f"race-{i}"}, True), range(16)))
        contexts = [json.loads(output)["hookSpecificOutput"]["additionalContext"]
                    for output, _ in concurrent if output]
        self.assertEqual(len(contexts), 1)
        self.assertLessEqual(len(contexts[0].encode()), 1024)
        timings = []
        for i in range(40):
            for event in ("PreToolUse", "PostToolUse"):
                normal_base = {**base, "tool_input": {"command": "python -m unittest tests.test_stage"}}
                output, elapsed = invoke({**normal_base, "hook_event_name": event, "tool_use_id": f"normal-{i}",
                                          "tool_response": {"exit_code": 0, "output": "private-result"}}, True)
                self.assertEqual(output, "")
                timings.append(elapsed)
        self.assertEqual(state_hashes(), before)
        logs = list((Path(os.environ["PLUGIN_DATA"]) / "live-events").glob("*.jsonl"))
        records = [json.loads(line) for path in logs for line in path.read_text().splitlines()]
        self.assertTrue(any(r.get("canary_passed") is True for r in records))
        serialized = json.dumps(records)
        self.assertNotIn("git push", serialized)
        self.assertNotIn("private-result", serialized)
        print(json.dumps({"probe": "real-cli-hook-sequence", "native_compaction": False,
                          "concurrent_calls": 16, "guidance_deliveries": len(contexts),
                          "guidance_bytes": len(contexts[0].encode()), "ordinary_calls": len(timings),
                          "ordinary_context_bytes": 0, "project_state_changed": False,
                          "hook_process_p95_ms": round(sorted(timings)[75], 3)}))


if __name__ == "__main__":
    unittest.main()
