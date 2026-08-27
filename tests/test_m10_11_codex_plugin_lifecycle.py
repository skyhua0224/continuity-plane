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
  if [ \"${SLOW_RESUME:-0}\" = \"1\" ]; then
    sleep 4
  fi
  if [ -n \"${MCP_BINDING_ENVELOPE:-}\" ]; then
    printf '%s\\n' \"$MCP_BINDING_ENVELOPE\"
    exit 0
  fi
  if [ \"${ALWAYS_STALE:-0}\" = \"1\" ]; then
    printf '%s\\n' '{"schema_version":"context.resume-packet/v1alpha1","project_id":"portable-project","revision":7,"active_work":{"work_id":"work-active","title":"Continue active work"},"claim":{"claim_id":"claim-active","actor_ref":"actor-active"},"next_action":"remain-read-only","source_fresh":false,"read_only":true}'
  elif [ \"${AUTO_REFRESH:-0}\" = \"1\" ] && [ ! -f \"$FAKE_CONTINUITY_REFRESHED\" ]; then
    printf '%s\\n' '{"schema_version":"context.resume-packet/v1alpha1","project_id":"portable-project","revision":7,"active_work":{"work_id":"work-active","title":"Continue active work"},"claim":{"claim_id":"claim-active","actor_ref":"actor-active"},"next_action":"remain-read-only","source_fresh":false,"read_only":true}'
  else
    printf '%s\\n' '{"schema_version":"context.resume-packet/v1alpha1","project_id":"portable-project","revision":8,"active_work":{"work_id":"work-active","title":"Continue active work"},"claim":{"claim_id":"claim-active","actor_ref":"actor-active"},"next_action":"continue-active-work","source_fresh":true,"read_only":false}'
  fi
  exit 0
fi
if [ \"$1 $2\" = \"attach refresh\" ]; then
  touch \"$FAKE_CONTINUITY_REFRESHED\"
  printf '%s\\n' '{"status":"refreshed"}'
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
        auto_refresh: bool = False,
        always_stale: bool = False,
        tool_name: str | None = None,
        tool_input: dict | None = None,
        resume_packet: dict | None = None,
        slow_resume: bool = False,
        session_source: str = "compact",
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
                payload["source"] = session_source
            if event == "PreToolUse":
                payload["tool_name"] = tool_name or "Bash"
                payload["tool_use_id"] = "private-tool-use-id"
                payload["tool_input"] = tool_input or {"command": "git status"}
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(self.plugin),
                "FAKE_CONTINUITY_CALLS": str(calls),
                "FAIL_CHECKPOINT_VERIFY": "1" if fail_verify else "0",
                "AUTO_REFRESH": "1" if auto_refresh else "0",
                "FAKE_CONTINUITY_REFRESHED": str(temp / "refreshed"),
                "ALWAYS_STALE": "1" if always_stale else "0",
                "MCP_BINDING_ENVELOPE": json.dumps(resume_packet)
                if resume_packet is not None
                else "",
                "SLOW_RESUME": "1" if slow_resume else "0",
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
        self.assertEqual(
            set(hooks),
            {
                "SessionStart",
                "PreCompact",
                "PostCompact",
                "PreToolUse",
                "PostToolUse",
            },
        )
        self.assertEqual(hooks["PreCompact"][0]["matcher"], "manual|auto")
        self.assertEqual(hooks["PostCompact"][0]["matcher"], "manual|auto")
        self.assertIn("compact", hooks["SessionStart"][0]["matcher"])
        self.assertEqual(hooks["PreToolUse"][0]["matcher"], "Bash")
        self.assertEqual(hooks["PostToolUse"][0]["matcher"], "Bash")
        for groups in hooks.values():
            handler = groups[0]["hooks"][0]
            self.assertIn("command", handler)
            self.assertIn("commandWindows", handler)
        self.assertEqual(
            hooks["SessionStart"][0]["hooks"][0]["additionalContextLimit"],
            5000,
        )

    def test_local_rsync_is_not_classified_as_a_remote_effect(self) -> None:
        module = self._hook_module()

        self.assertIsNone(
            module._effect_class("rsync -a ./plugin/ /home/user/plugins/plugin/")
        )
        self.assertEqual(
            module._effect_class("rsync -a ./plugin/ host:/srv/plugins/plugin/"),
            "remote-effect",
        )
        self.assertEqual(
            module._effect_class("rsync -a rsync://host/module/plugin ./plugin"),
            "remote-effect",
        )

    def test_pretooluse_denies_external_effect_without_a_writable_claim(self) -> None:
        idle = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": None,
            "claim": None,
            "next_action": "activate-next-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        completed, calls, observations = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git push origin main"},
            resume_packet=idle,
        )

        output = json.loads(completed.stdout)
        decision = output["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("active Work", decision["permissionDecisionReason"])
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("resume "))
        self.assertIn('"event_type":"pretooluse"', observations)

    def test_pretooluse_denies_effect_outside_the_claim_scope(self) -> None:
        active = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": {
                "work_id": "work-active",
                "title": "Continue active work",
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "code-edit"}
                ],
            },
            "claim": {
                "claim_id": "claim-active",
                "actor_ref": "actor-active",
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "code-edit"}
                ],
            },
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        completed, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "kubectl apply -f deploy.yaml"},
            resume_packet=active,
        )

        decision = json.loads(completed.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("deployment", decision["permissionDecisionReason"])

    def test_pretooluse_fails_closed_when_authority_lookup_times_out(self) -> None:
        completed, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git push origin main"},
            slow_resume=True,
        )

        decision = json.loads(completed.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["hookEventName"], "PreToolUse")
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("unavailable", decision["permissionDecisionReason"])

    def test_source_control_delivery_accepts_an_opaque_active_work_scope(self) -> None:
        packet = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": {"work_id": "work-active"},
            "claim": {
                "claim_id": "claim-active",
                "actor_ref": "actor-active",
                "status": "active",
                "scope_owners": [
                    {
                        "scope_kind": "capability",
                        "scope_ref": "project-shadow-pilot",
                    }
                ],
            },
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        completed, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git commit -m bounded-change"},
            resume_packet=packet,
        )

        self.assertEqual(completed.stdout, "")

    def test_delivery_effect_scopes_allow_only_the_declared_action(self) -> None:
        packet = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": {"work_id": "work-delivery"},
            "claim": {
                "claim_id": "claim-delivery",
                "actor_ref": "actor-active",
                "status": "active",
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "release-delivery"},
                    {"scope_kind": "effect", "scope_ref": "source-control.push"},
                ],
            },
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        pushed, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git push origin HEAD"},
            resume_packet=packet,
        )
        committed, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git commit -m release-boundary"},
            resume_packet=packet,
        )
        merged, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "tea pulls merge 10"},
            resume_packet=packet,
        )

        self.assertEqual(pushed.stdout, "")
        self.assertEqual(committed.stdout, "")
        decision = json.loads(merged.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("source-control.merge", decision["permissionDecisionReason"])

    def test_pretooluse_allows_read_only_shell_and_claimed_deployment(self) -> None:
        deployment = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": {
                "work_id": "work-active",
                "title": "Deploy verified release",
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "deployment"}
                ],
            },
            "claim": {
                "claim_id": "claim-active",
                "actor_ref": "actor-active",
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "deployment"},
                    {"scope_kind": "effect", "scope_ref": "deployment.deploy"},
                ],
            },
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        read, read_calls, read_observations = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git status --short"},
            resume_packet=deployment,
        )
        release_read, release_read_calls, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "gh release view v0.1.0-alpha.7"},
            resume_packet=deployment,
        )
        tag_read, tag_read_calls, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git tag --list 'v*'"},
            resume_packet=deployment,
        )
        deploy, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "kubectl apply -f deploy.yaml"},
            resume_packet=deployment,
        )

        self.assertEqual(read.stdout, "")
        self.assertEqual(read_calls, [])
        self.assertNotIn("git status", read_observations)
        self.assertEqual(release_read.stdout, "")
        self.assertEqual(release_read_calls, [])
        self.assertEqual(tag_read.stdout, "")
        self.assertEqual(tag_read_calls, [])
        self.assertEqual(deploy.stdout, "")

    def test_compact_recovery_enforces_and_accounts_the_actual_read_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "portable-project"
            project.mkdir()
            (project / ".continuity").mkdir()
            (project / ".continuity/project.yaml").write_text(
                "schema_version: context.project/v1alpha1\n",
                encoding="utf-8",
            )
            (project / ".continuity/STATUS.current.md").write_text(
                "active Work: work-active\nnext action: continue\n",
                encoding="utf-8",
            )
            (project / "MASTER.md").write_text("governance\n" * 4096, encoding="utf-8")
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            _, calls = self._fake_continuity(bin_dir)
            plugin_data = temp / "plugin-data"
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(self.plugin),
                "FAKE_CONTINUITY_CALLS": str(calls),
                "MCP_BINDING_ENVELOPE": "",
            }
            common = {
                "session_id": "private-session-id",
                "transcript_path": None,
                "cwd": str(project),
                "model": "provider-model",
                "turn_id": "private-turn-id",
            }

            def invoke(payload: dict) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["python3", str(self.script)],
                    input=json.dumps({**common, **payload}),
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                )

            started = invoke(
                {"hook_event_name": "SessionStart", "source": "compact"}
            )
            denied = invoke(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "read-unbounded",
                    "tool_input": {"command": "cat MASTER.md"},
                }
            )
            allowed = invoke(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "read-bounded",
                    "tool_input": {
                        "command": "sed -n '1,20p' .continuity/STATUS.current.md"
                    },
                }
            )
            measured = invoke(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "read-bounded",
                    "tool_input": {
                        "command": "sed -n '1,20p' .continuity/STATUS.current.md"
                    },
                    "tool_response": "x" * 1024,
                }
            )
            exceeded = invoke(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Bash",
                    "tool_use_id": "read-second",
                    "tool_input": {
                        "command": "sed -n '21,240p' .continuity/STATUS.current.md"
                    },
                    "tool_response": "y" * (12 * 1024),
                }
            )

            self.assertEqual(started.returncode, 0, started.stderr)
            self.assertEqual(
                json.loads(denied.stdout)["hookSpecificOutput"]["permissionDecision"],
                "deny",
            )
            self.assertIn("bounded", denied.stdout.lower())
            self.assertEqual(allowed.stdout, "")
            self.assertEqual(measured.stdout, "")
            self.assertEqual(json.loads(exceeded.stdout)["continue"], False)
            self.assertNotIn("y" * 128, exceeded.stdout)
            records = [
                json.loads(line)
                for path in (plugin_data / "live-events").glob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            reads = [item for item in records if item["event_type"] == "recovery-read"]
            self.assertEqual(reads[-1]["recovery_read_bytes"], 1024)
            self.assertEqual(reads[-1]["recovery_read_budget_bytes"], 12 * 1024)
            self.assertEqual(reads[-1]["tool_output_bytes"], 12 * 1024)
            self.assertEqual(reads[-1]["context_admitted"], False)
            encoded = json.dumps(reads, sort_keys=True)
            self.assertNotIn("MASTER.md", encoded)
            self.assertNotIn("STATUS.current.md", encoded)
            self.assertNotIn("private-session-id", encoded)

    def test_same_repository_sessions_serialize_external_effects(self) -> None:
        deployment = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": {"work_id": "work-active"},
            "claim": {
                "claim_id": "claim-active",
                "actor_ref": "actor-active",
                "status": "active",
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "deployment"},
                    {"scope_kind": "effect", "scope_ref": "deployment.deploy"},
                ],
            },
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "portable-project"
            project.mkdir()
            (project / ".continuity").mkdir()
            (project / ".continuity/project.yaml").write_text(
                "schema_version: context.project/v1alpha1\n",
                encoding="utf-8",
            )
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            _, calls = self._fake_continuity(bin_dir)
            plugin_data = temp / "plugin-data"
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(self.plugin),
                "FAKE_CONTINUITY_CALLS": str(calls),
                "MCP_BINDING_ENVELOPE": json.dumps(deployment),
            }

            def invoke(
                session: str, event: str, tool_use_id: str | None = None
            ) -> subprocess.CompletedProcess[str]:
                payload = {
                    "session_id": session,
                    "transcript_path": None,
                    "cwd": str(project),
                    "hook_event_name": event,
                    "model": "provider-model",
                    "turn_id": f"turn-{session}",
                    "tool_name": "Bash",
                    "tool_use_id": tool_use_id or f"deploy-{session}",
                    "tool_input": {"command": "kubectl apply -f deploy.yaml"},
                }
                if event == "PostToolUse":
                    payload["tool_response"] = {"output": "applied", "exit_code": 0}
                return subprocess.run(
                    ["python3", str(self.script)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                )

            first = invoke("session-a", "PreToolUse", "deploy-session-a-1")
            same_session = invoke("session-a", "PreToolUse", "deploy-session-a-2")
            conflict = invoke("session-b", "PreToolUse")
            released = invoke("session-a", "PostToolUse", "deploy-session-a-2")
            successor = invoke("session-b", "PreToolUse")

            self.assertEqual(first.stdout, "")
            self.assertEqual(same_session.stdout, "")
            decision = json.loads(conflict.stdout)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("another active session", decision["permissionDecisionReason"])
            self.assertEqual(released.stdout, "")
            self.assertEqual(successor.stdout, "")
            observations = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (plugin_data / "live-events").glob("*.jsonl")
            )
            self.assertNotIn("kubectl apply", observations)
            self.assertNotIn("session-a", observations)
            self.assertNotIn("session-b", observations)

    def test_hook_observation_binds_installed_manifest_and_hook_contract(self) -> None:
        completed, _, observations = self._run_hook("SessionStart")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        record = json.loads(observations.splitlines()[-1])
        self.assertRegex(record["plugin_manifest_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(record["hook_contract_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(record["plugin_loaded"], True)

    def test_precompact_creates_checkpoint_without_model_visible_narration(self) -> None:
        completed, calls, observations = self._run_hook("PreCompact")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("checkpoint create --root "))
        self.assertIn('"event_type":"precompact"', observations)

    def test_implementation_claim_cannot_push_without_a_delivery_effect_scope(self) -> None:
        packet = {
            "schema_version": "context.recovery-envelope/v1alpha1",
            "project_id": "portable-project",
            "revision": 8,
            "active_work": {"work_id": "work-implementation"},
            "claim": {
                "claim_id": "claim-implementation",
                "actor_ref": "actor-active",
                "status": "active",
                "scope_owners": [
                    {"scope_kind": "capability", "scope_ref": "code-edit"}
                ],
            },
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        completed, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "git push origin HEAD"},
            resume_packet=packet,
        )

        decision = json.loads(completed.stdout)["hookSpecificOutput"]
        self.assertEqual(decision["permissionDecision"], "deny")
        self.assertIn("source-control.push", decision["permissionDecisionReason"])

        deployed, _, _ = self._run_hook(
            "PreToolUse",
            tool_name="Bash",
            tool_input={"command": "DEPLOY_PROFILE=prod deploy/deploy.sh --backend"},
            resume_packet=packet,
        )
        deploy_decision = json.loads(deployed.stdout)["hookSpecificOutput"]
        self.assertEqual(deploy_decision["permissionDecision"], "deny")
        self.assertIn("deployment.deploy", deploy_decision["permissionDecisionReason"])

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

    def test_new_session_start_reports_that_continuity_is_active_once(self) -> None:
        completed, calls, _ = self._run_hook(
            "SessionStart", session_source="startup"
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(calls), 1)
        output = json.loads(completed.stdout)
        self.assertEqual(
            output["systemMessage"],
            "Continuity active · portable-project · revision 8",
        )
        self.assertNotIn("work-active", output["systemMessage"])
        self.assertNotIn("packet", output["systemMessage"].lower())

    def test_compact_session_start_refreshes_stale_sources_before_resume(self) -> None:
        completed, calls, _ = self._run_hook("SessionStart", auto_refresh=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith("resume "))
        self.assertTrue(calls[1].startswith("attach refresh "))
        self.assertIn("explicit governance approval", completed.stdout.lower())

    def test_compact_session_start_stops_if_refresh_does_not_make_source_fresh(self) -> None:
        completed, calls, observations = self._run_hook(
            "SessionStart", auto_refresh=True, always_stale=True
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn("read-only", completed.stdout)
        self.assertIn('"success":false', observations)

    def test_non_continuity_project_is_a_zero_output_noop(self) -> None:
        completed, calls, observations = self._run_hook(
            "PreCompact", with_project=False
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(calls, [])
        self.assertEqual(observations, "")

    def test_git_main_root_discovers_the_canonical_worktree_control_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            main_root = temp / "repo"
            control_root = temp / "control"
            main_root.mkdir()
            subprocess.run(["git", "init", "-q", str(main_root)], check=True)
            subprocess.run(
                ["git", "-C", str(main_root), "config", "user.email", "test@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(main_root), "config", "user.name", "Continuity Test"],
                check=True,
            )
            (main_root / "README.md").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(main_root), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(main_root), "commit", "-qm", "test: initialize"],
                check=True,
            )
            subprocess.run(
                [
                    "git", "-C", str(main_root), "worktree", "add", "-q",
                    "--detach", str(control_root),
                ],
                check=True,
            )
            (control_root / ".continuity").mkdir()
            (control_root / ".continuity/project.yaml").write_text(
                "schema_version: context.project/v1alpha1\n",
                encoding="utf-8",
            )
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            _, calls = self._fake_continuity(bin_dir)
            plugin_data = temp / "plugin-data"
            payload = {
                "session_id": "private-session-id",
                "transcript_path": None,
                "cwd": str(main_root),
                "hook_event_name": "SessionStart",
                "source": "compact",
                "model": "provider-model",
                "turn_id": "private-turn-id",
            }
            completed = subprocess.run(
                ["python3", str(self.script)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "PLUGIN_DATA": str(plugin_data),
                    "PLUGIN_ROOT": str(self.plugin),
                    "FAKE_CONTINUITY_CALLS": str(calls),
                    "MCP_BINDING_ENVELOPE": "",
                },
                check=False,
            )

            context = json.loads(completed.stdout)["hookSpecificOutput"][
                "additionalContext"
            ]
            call_lines = calls.read_text(encoding="utf-8").splitlines()
            self.assertIn("work-active", context)
            self.assertIn(f"--root {control_root}", call_lines[0])

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
