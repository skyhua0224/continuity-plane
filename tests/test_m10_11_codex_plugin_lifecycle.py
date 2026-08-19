"""Codex PreCompact/PostCompact lifecycle integration for M10-11."""

from __future__ import annotations

import json
import importlib.util
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class M1011CodexPluginLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.plugin = cls.root / "integrations/codex/continuity-plane"
        cls.hooks_path = cls.plugin / "hooks/hooks.json"
        cls.script = cls.plugin / "scripts/continuity-hook.py"

    def _hook_module(self):
        spec = importlib.util.spec_from_file_location("continuity_hook", self.script)
        if spec is None or spec.loader is None:
            raise AssertionError("hook module is unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def _fake_continuity(self, directory: Path) -> tuple[Path, Path]:
        binary = directory / "continuity"
        calls = directory / "calls.jsonl"
        binary.write_text(
            """#!/bin/sh
printf '%s\\n' \"$*\" >> \"$FAKE_CONTINUITY_CALLS\"
if [ \"$1\" = \"resume\" ]; then
  printf '%s\\n' '{"schema_version":"context.resume-packet/v1alpha1","project_id":"portable-project","revision":7,"active_work":{"work_id":"work-active","title":"Continue active work"},"claim":{"claim_id":"claim-active"},"next_action":"continue-active-work","read_only":false}'
  exit 0
fi
if [ \"${FAIL_CHECKPOINT_VERIFY:-0}\" = \"1\" ] && [ \"$1 $2\" = \"checkpoint verify\" ]; then
  exit 9
fi
printf '%s\\n' '{"status":"ok"}'
""",
            encoding="utf-8",
        )
        binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
        return binary, calls

    def _run_hook(
        self,
        event: str,
        *,
        fail_verify: bool = False,
        with_project: bool = True,
    ) -> tuple[subprocess.CompletedProcess[str], list[str], str]:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "portable-project"
            project.mkdir()
            if with_project:
                (project / ".continuity").mkdir()
                (project / ".continuity/project.yaml").write_text(
                    "schema_version: context.project/v1alpha1\n",
                    encoding="utf-8",
                )
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            _, calls = self._fake_continuity(bin_dir)
            plugin_data = temp / "plugin-data"
            payload = {
                "session_id": "private-session-id",
                "transcript_path": "/private/provider/raw-rollout.jsonl",
                "cwd": str(project),
                "hook_event_name": event,
                "model": "provider-model",
                "turn_id": "private-turn-id",
            }
            if event in {"PreCompact", "PostCompact"}:
                payload["trigger"] = "auto"
            if event == "SessionStart":
                payload["source"] = "compact"
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(self.plugin),
                "FAKE_CONTINUITY_CALLS": str(calls),
                "FAIL_CHECKPOINT_VERIFY": "1" if fail_verify else "0",
            }
            completed = subprocess.run(
                ["python3", str(self.script)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
            call_lines = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
            observations = "\n".join(
                path.read_text(encoding="utf-8")
                for path in plugin_data.rglob("*.jsonl")
            ) if plugin_data.exists() else ""
            return completed, call_lines, observations

    def test_hook_config_registers_real_pre_post_and_compact_start_events(self) -> None:
        config = json.loads(self.hooks_path.read_text(encoding="utf-8"))
        hooks = config["hooks"]
        self.assertEqual(set(hooks), {"SessionStart", "PreCompact", "PostCompact"})
        self.assertEqual(hooks["PreCompact"][0]["matcher"], "manual|auto")
        self.assertEqual(hooks["PostCompact"][0]["matcher"], "manual|auto")
        self.assertIn("compact", hooks["SessionStart"][0]["matcher"])
        for groups in hooks.values():
            handler = groups[0]["hooks"][0]
            self.assertIn("command", handler)
            self.assertIn("commandWindows", handler)
            self.assertLessEqual(handler.get("additionalContextLimit", 2000), 2000)

    def test_precompact_creates_checkpoint_without_model_visible_narration(self) -> None:
        completed, calls, observations = self._run_hook("PreCompact")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("checkpoint create --root "))
        self.assertIn('"event_type":"precompact"', observations)

    def test_postcompact_verifies_canary_before_continuation(self) -> None:
        completed, calls, observations = self._run_hook("PostCompact")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["continue"], True)
        self.assertTrue(calls[0].startswith("checkpoint verify --root "))
        self.assertIn('"canary_passed":true', observations)

        failed, failed_calls, _ = self._run_hook("PostCompact", fail_verify=True)
        self.assertEqual(failed.returncode, 0)
        self.assertTrue(failed_calls[0].startswith("checkpoint verify --root "))
        output = json.loads(failed.stdout)
        self.assertEqual(output["continue"], False)
        self.assertIn("checkpoint", output["stopReason"].lower())

    def test_compact_session_start_injects_only_bounded_silent_packet(self) -> None:
        completed, calls, observations = self._run_hook("SessionStart")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(calls[0].startswith("resume "))
        self.assertIn("--skill-lock ", calls[0])
        output = json.loads(completed.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(context.encode("utf-8")), 12 * 1024)
        self.assertIn("work-active", context)
        self.assertIn("Continue silently", context)
        self.assertNotIn("private-session-id", context)
        self.assertNotIn("raw-rollout", context)
        self.assertNotIn("private-session-id", observations)
        self.assertNotIn("private-turn-id", observations)
        self.assertNotIn("raw-rollout", observations)

    def test_non_continuity_project_is_a_zero_output_noop(self) -> None:
        completed, calls, observations = self._run_hook(
            "PreCompact", with_project=False
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(calls, [])
        self.assertEqual(observations, "")

    def test_codex_tail_parser_records_hash_cursor_without_raw_text(self) -> None:
        hook = self._hook_module()
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "rollout.jsonl"
            events = [
                {
                    "timestamp": "2026-08-20T10:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "private-user-message-id",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "private user text"}],
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "private-turn-id"
                        },
                    },
                },
                {
                    "timestamp": "2026-08-20T10:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "private-assistant-message-id",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "private visible progress"}
                        ],
                        "phase": "commentary",
                        "internal_chat_message_metadata_passthrough": {
                            "turn_id": "private-turn-id"
                        },
                    },
                },
            ]
            transcript.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )

            cursor = hook.derive_recent_interaction_cursor(transcript)

            encoded = json.dumps(cursor, sort_keys=True)
            self.assertEqual(cursor["response_mode"], "continue-without-restatement")
            self.assertEqual(cursor["confirmed_input_refs"], [])
            self.assertTrue(cursor["no_restate"])
            self.assertRegex(cursor["current_input_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(
                cursor["visible_output_high_watermark_sha256"], r"^[0-9a-f]{64}$"
            )
            self.assertNotIn("private user text", encoded)
            self.assertNotIn("private visible progress", encoded)
            self.assertNotIn("private-turn-id", encoded)
            self.assertNotIn("private-user-message-id", encoded)

            events[-1]["payload"]["phase"] = "final_answer"
            transcript.write_text(
                "\n".join(json.dumps(item) for item in events) + "\n",
                encoding="utf-8",
            )
            final_cursor = hook.derive_recent_interaction_cursor(transcript)
            self.assertEqual(final_cursor["response_mode"], "continue-silently")
            self.assertEqual(
                final_cursor["confirmed_input_refs"],
                [final_cursor["current_input_ref"]],
            )


if __name__ == "__main__":
    unittest.main()
