"""Candidate regressions for mixed installs and trustworthy hook observations."""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from context_control_plane.cli import _codex_plugin_status
from tests import test_m10_11_codex_plugin_lifecycle as lifecycle


class CandidateReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        lifecycle.M1011CodexPluginLifecycleTests.setUpClass()
        cls.fixture = lifecycle.M1011CodexPluginLifecycleTests()
        cls.plugin = cls.fixture.plugin
        cls.repo = cls.fixture.root

    def test_plugin_tool_hooks_work_with_released_alpha12_launcher(self):
        prior = subprocess.run(
            ["git", "show", "59bb1b2:context_control_plane/codex_hook_launcher.py"],
            cwd=self.repo, text=True, capture_output=True, check=True,
        ).stdout
        config = json.loads((self.plugin / "hooks/hooks.json").read_text())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "alpha12-launcher.py"
            launcher.write_text(prior)
            (root / ".continuity").mkdir()
            (root / ".continuity/project.yaml").write_text("project_id: test\n")
            for event in ("PreToolUse", "PostToolUse"):
                args = shlex.split(config["hooks"][event][0]["hooks"][0]["command"].replace("${PLUGIN_ROOT}", str(self.plugin)))
                result = subprocess.run(
                    [sys.executable, str(launcher), *args[1:]],
                    input=json.dumps({"cwd": str(root), "session_id": "sample-session", "hook_event_name": event,
                                      "tool_name": "Bash", "tool_input": {"command": "git push origin main"}}),
                    env={**os.environ, "CONTINUITY_EFFECT_POLICY": "strict", "PLUGIN_DATA": str(root / "data")},
                    text=True, capture_output=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")

    def test_unverified_or_expired_active_packet_never_becomes_a_return_point(self):
        packet = {"project_id": "portable-project", "revision": 8,
                  "active_work": {"work_id": "unsafe-old-work"}, "claim": {"claim_id": "claim-active"},
                  "source_fresh": True, "read_only": False, "next_action": "continue-active-work",
                  "checkpoint_verified": True, "lease_valid": True}
        for field in ("checkpoint_verified", "lease_valid"):
            result, calls, _ = self.fixture._run_hook(
                "SessionStart", effect_policy="auto", resume_packet={**packet, field: False}, projection_revision=8,
            )
            self.assertEqual(result.returncode, 0)
            self.assertNotIn("unsafe-old-work", result.stdout)
            self.assertIn("continuity_context_lookup", result.stdout)
            self.assertEqual(len(calls), 1)

    def test_doctor_cannot_count_unrelated_trust_records_as_current_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = '[plugins."continuity-plane@continuity-plane"]\nenabled=true\n'
            for i in range(6):
                text += f'[hooks.state."continuity-plane@continuity-plane:old-hooks:old_{i}:0:0"]\ntrusted_hash="sha256:{i:064x}"\n'
            (root / "config.toml").write_text(text)
            status = _codex_plugin_status(root)
            self.assertEqual(status["trusted_hooks"], 0)
            self.assertEqual(status["status"], "misconfigured")

    def test_tool_observation_respects_explicit_workdir_without_rebinding(self):
        hook = self.fixture._hook_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bound, target = root / "bound", root / "target"
            for project in (bound, target):
                (project / ".continuity").mkdir(parents=True)
                (project / ".continuity/project.yaml").write_text("project_id: test\n")
            payload = {"cwd": str(bound), "session_id": "sample-session", "tool_name": "Bash",
                       "hook_event_name": "PostToolUse", "tool_input": {"command": "pytest", "workdir": str(target)}}
            with mock.patch.dict(os.environ, {"PLUGIN_DATA": str(root / "data"), "CONTINUITY_OPERATION_SAMPLE_RATE": "1"}), \
                    mock.patch.object(hook, "_session_bound_root", return_value=bound), \
                    mock.patch.object(hook.sys, "stdin", io.StringIO(json.dumps(payload))), \
                    mock.patch.object(hook, "_observe") as observe:
                self.assertEqual(hook.advisory_main(), 0)
                self.assertEqual(observe.call_args.args[1], target)

    def test_untrusted_observation_labels_are_bounded_and_redacted(self):
        hook = self.fixture._hook_module()
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {
            "PLUGIN_DATA": directory, "CONTINUITY_OPERATION_SAMPLE_RATE": "1",
        }):
            hook._observe_operation_boundary(
                {"session_id": "sample-session", "tool_name": "private request " * 1000, "model": "private model " * 1000},
                Path(directory), phase="post",
            )
            content = next((Path(directory) / "live-events").glob("*.jsonl")).read_text()
            self.assertLessEqual(len(content.encode()), 2048)
            self.assertNotIn("private request", content)
            self.assertNotIn("private model", content)


if __name__ == "__main__":
    unittest.main()
