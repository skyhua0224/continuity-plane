"""Codex PreCompact/PostCompact lifecycle integration for M10-11."""

from __future__ import annotations

import hashlib
import io
import json
import importlib.util
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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
if [ \"$1\" = \"autorun\" ]; then
  if [ \"${HOOK_AUTORUN_RETRY:-0}\" = \"1\" ] && [ ! -f \"$HOOK_AUTORUN_RETRIED\" ]; then touch \"$HOOK_AUTORUN_RETRIED\"; printf 'transport closed\\n' >&2; exit 1; fi
  printf '%s\\n' '{\"status\":\"continued\",\"state_event_created\":false,\"next_action\":\"continue-active-work\",\"resume_packet\":'\"$MCP_BINDING_ENVELOPE\"'}'
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
        stage_success: bool = False,
        autorun_retry: bool = False,
        effect_policy: str | None = "strict",
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
                subprocess.run(["git", "init", "-q", str(project)], check=True)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(project),
                        "config",
                        "user.email",
                        "test@example.invalid",
                    ],
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(project),
                        "config",
                        "user.name",
                        "Continuity Test",
                    ],
                    check=True,
                )
                (project / "README.md").write_text("fixture\n", encoding="utf-8")
                subprocess.run(
                    ["git", "-C", str(project), "add", "README.md"],
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(project),
                        "commit",
                        "-qm",
                        "test: initialize",
                    ],
                    check=True,
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
            if event == "PostToolUse":
                payload["tool_name"] = tool_name or "Bash"
                payload["tool_use_id"] = "private-tool-use-id"
                payload["tool_input"] = tool_input or {"command": "git status"}
                payload["tool_response"] = {
                    "exit_code": 0 if stage_success else 1,
                    "output": "ok" if stage_success else "failed",
                }
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
                "HOOK_AUTORUN_RETRY": "1" if autorun_retry else "0",
                "HOOK_AUTORUN_RETRIED": str(temp / "hook-autorun-retried"),
            }
            if effect_policy is not None:
                environment["CONTINUITY_EFFECT_POLICY"] = effect_policy
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
        self.assertEqual(
            [group["matcher"] for group in hooks["PostToolUse"]],
            [
                "Bash",
                "mcp__continuity__continuity_resume",
                "continuity_resume",
                "continuity/continuity_resume",
            ],
        )
        for groups in hooks.values():
            handler = groups[0]["hooks"][0]
            self.assertIn("command", handler)
            self.assertIn("commandWindows", handler)
        self.assertEqual(
            hooks["SessionStart"][0]["hooks"][0]["additionalContextLimit"],
            5000,
        )

    def test_explicit_resume_binding_outlives_an_unrelated_session_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            bound_project = temp / "bound-project"
            cwd_project = temp / "cwd-project"
            for project, project_id in (
                (bound_project, "bound-project"),
                (cwd_project, "cwd-project"),
            ):
                project.mkdir()
                (project / ".continuity").mkdir()
                (project / ".continuity/project.yaml").write_text(
                    f"project_id: {project_id}\n",
                    encoding="utf-8",
                )
            bound_packet = {
                "schema_version": "context.recovery-envelope/v1alpha1",
                "project_id": "bound-project",
                "revision": 4,
                "active_work": {"work_id": "bound-work"},
                "claim": {
                    "claim_id": "bound-claim",
                    "actor_ref": "bound-actor",
                    "status": "active",
                    "scope_owners": [
                        {"scope_kind": "capability", "scope_ref": "bound-scope"}
                    ],
                },
                "read_only": False,
                "source_fresh": True,
                "checkpoint_verified": True,
                "lease_valid": True,
                "next_action": "continue-active-work",
            }
            cwd_packet = {
                **bound_packet,
                "project_id": "cwd-project",
                "active_work": {"work_id": "cwd-work"},
                "claim": {
                    "claim_id": "cwd-claim",
                    "actor_ref": "cwd-actor",
                    "status": "active",
                    "scope_owners": [
                        {"scope_kind": "capability", "scope_ref": "cwd-scope"}
                    ],
                },
            }
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            calls = temp / "calls.jsonl"
            binary = bin_dir / "continuity"
            binary.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$FAKE_CONTINUITY_CALLS"\n'
                'if [ "$1" = "resume" ]; then case " $* " in '
                '*" --root $BOUND_ROOT "*) printf \'%s\\n\' "$BOUND_PACKET"; '
                'exit 0;; esac; fi\n'
                'if [ "$1" = "resume" ]; then printf \'%s\\n\' "$CWD_PACKET"; fi\n',
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            plugin_data = temp / "plugin-data"
            environment = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "PLUGIN_DATA": str(plugin_data),
                "PLUGIN_ROOT": str(self.plugin),
                "FAKE_CONTINUITY_CALLS": str(calls),
                "BOUND_ROOT": str(bound_project),
                "BOUND_PACKET": json.dumps(bound_packet),
                "CWD_PACKET": json.dumps(cwd_packet),
            }

            def invoke(payload: dict) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(self.script)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                )

            base = {
                "session_id": "session-bound-project",
                "transcript_path": None,
                "cwd": str(cwd_project),
                "model": "provider-model",
                "turn_id": "turn-bound-project",
            }
            bound = invoke(
                {
                    **base,
                    "hook_event_name": "PostToolUse",
                    "tool_name": "continuity_resume",
                    "tool_use_id": "resume-bound-project",
                    "tool_input": {"root": str(bound_project)},
                    "tool_response": {
                        "content": [
                            {"type": "text", "text": json.dumps(bound_packet)}
                        ],
                        "isError": False,
                    },
                }
            )
            self.assertEqual(bound.stdout, "")
            binding_files = list(
                (plugin_data / "session-bindings").glob("*.json")
            )
            self.assertEqual(len(binding_files), 1)
            self.assertEqual(stat.S_IMODE(binding_files[0].stat().st_mode), 0o600)

            other_project = invoke(
                {
                    **base,
                    "hook_event_name": "PostToolUse",
                    "tool_name": "continuity/continuity_resume",
                    "tool_use_id": "resume-cwd-project",
                    "tool_input": {"root": str(cwd_project)},
                    "tool_response": {
                        "content": [
                            {"type": "text", "text": json.dumps(cwd_packet)}
                        ],
                        "isError": False,
                    },
                }
            )
            self.assertEqual(other_project.stdout, "")
            self.assertEqual(
                len(list((plugin_data / "session-bindings").glob("*.json"))),
                1,
            )

            rebound = invoke(
                {
                    **base,
                    "hook_event_name": "PostToolUse",
                    "tool_name": "mcp__continuity__continuity_resume",
                    "tool_use_id": "resume-bound-project-again",
                    "tool_input": {"root": str(bound_project)},
                    "tool_response": {
                        "content": [
                            {"type": "text", "text": json.dumps(bound_packet)}
                        ],
                        "isError": False,
                    },
                }
            )
            self.assertEqual(rebound.stdout, "")

            started = invoke(
                {
                    **base,
                    "hook_event_name": "SessionStart",
                    "source": "compact",
                }
            )
            response = json.loads(started.stdout)
            context = response["hookSpecificOutput"]["additionalContext"]
            self.assertIn("bound-work", context)
            self.assertNotIn("cwd-work", context)
            call_lines = calls.read_text(encoding="utf-8").splitlines()
            self.assertIn(f"--root {bound_project}", call_lines[-1])
            self.assertNotIn(f"--root {cwd_project}", call_lines[-1])

    def test_invalid_session_binding_fails_closed_instead_of_using_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            cwd_project = temp / "cwd-project"
            cwd_project.mkdir()
            (cwd_project / ".continuity").mkdir()
            (cwd_project / ".continuity/project.yaml").write_text(
                "project_id: cwd-project\n",
                encoding="utf-8",
            )
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            _, calls = self._fake_continuity(bin_dir)
            plugin_data = temp / "plugin-data"
            bindings = plugin_data / "session-bindings"
            bindings.mkdir(parents=True)
            session_id = "session-tampered-binding"
            binding_path = bindings / (
                hashlib.sha256(session_id.encode("utf-8")).hexdigest() + ".json"
            )
            binding_path.write_text(
                '{"schema_version":"context.codex-session-project-binding/v1alpha1",'
                '"binding_sha256":"tampered"}\n',
                encoding="utf-8",
            )
            binding_path.chmod(0o600)
            payload = {
                "session_id": session_id,
                "transcript_path": None,
                "cwd": str(cwd_project),
                "hook_event_name": "SessionStart",
                "source": "resume",
                "model": "provider-model",
                "turn_id": "turn-tampered-binding",
            }
            completed = subprocess.run(
                [sys.executable, str(self.script)],
                input=json.dumps(payload),
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "PLUGIN_DATA": str(plugin_data),
                    "PLUGIN_ROOT": str(self.plugin),
                    "FAKE_CONTINUITY_CALLS": str(calls),
                },
                check=False,
            )

            response = json.loads(completed.stdout)
            self.assertFalse(response["continue"])
            self.assertIn("binding", response["stopReason"].lower())
            self.assertFalse(calls.exists())

    def test_plugin_data_falls_back_to_a_user_local_directory(self) -> None:
        module = self._hook_module()
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"PLUGIN_DATA": "", "HOME": directory}):
                path = module._session_binding_path(
                    {"session_id": "fallback-session"}
                )
            self.assertIsNotNone(path)
            assert path is not None
            self.assertEqual(
                path.parent.parent,
                Path(directory) / ".codex/plugins/data/continuity-plane",
            )

    def test_registered_workspace_resolves_governance_root_for_effect_preflight(self) -> None:
        module = self._hook_module()
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            governance = temp / "governance"
            workspace = temp / "delivery"
            for repo in (governance, workspace):
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
            (governance / ".continuity").mkdir()
            (governance / ".continuity/project.yaml").write_text(
                "project_id: foundation-account\n", encoding="utf-8"
            )
            repository_sha256 = module._repository_sha256(workspace)
            registry = {
                "schema_version": module.DELIVERY_WORKSPACE_REGISTRY_SCHEMA,
                "project_id": "foundation-account",
                "project_profile_sha256": module._file_hash(
                    governance / ".continuity/project.yaml"
                ),
                "workspaces": [
                    {
                        "workspace_id": "account-service-hosting",
                        "workspace_root": str(workspace),
                        "repository_sha256": repository_sha256,
                        "allowed_effects": ["source-control.push"],
                    }
                ],
                "registry_sha256": "",
            }
            registry["registry_sha256"] = module._hash(
                module._canonical(
                    {
                        key: value
                        for key, value in registry.items()
                        if key != "registry_sha256"
                    }
                )
            )
            path = governance / ".continuity/local/delivery-workspaces.json"
            path.parent.mkdir(parents=True)
            path.write_text(module._canonical(registry), encoding="utf-8")
            self.assertEqual(
                module._registered_governance_root(
                    workspace,
                    effect_action="source-control.push",
                    search_roots=[governance],
                ),
                governance,
            )

    def test_registered_workspace_overrides_a_stale_session_root_for_effects(self) -> None:
        module = self._hook_module()
        stale = Path("/tmp/stale-project")
        registered = Path("/tmp/foundation-governance")
        captured: list[Path] = []
        payload = {
            "hook_event_name": "PreToolUse",
            "cwd": str(stale),
            "session_id": "session-stale-root",
            "tool_name": "Bash",
            "tool_input": {
                "command": "git push origin main",
                "workdir": str(stale),
            },
        }
        with mock.patch.object(module, "_session_bound_root", return_value=stale), \
            mock.patch.object(module, "_registered_governance_root", return_value=registered), \
            mock.patch.object(module, "_project_root", return_value=stale), \
            mock.patch.object(module, "_pretooluse", side_effect=lambda value, root: captured.append(root) or 0), \
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            module.main()
        self.assertEqual(captured, [registered])

    def test_bound_root_is_kept_when_cwd_has_no_explicit_workdir(self) -> None:
        module = self._hook_module()
        stale = Path("/tmp/stale-project")
        bound = Path("/tmp/foundation-governance")
        captured: list[Path] = []
        payload = {
            "hook_event_name": "PreToolUse",
            "cwd": str(stale),
            "session_id": "session-bound-root",
            "tool_name": "Bash",
            "tool_input": {"command": "git push origin main"},
        }
        with mock.patch.object(module, "_session_bound_root", return_value=bound), \
            mock.patch.object(module, "_registered_governance_root", side_effect=AssertionError("cwd must not override binding")), \
            mock.patch.object(module, "_project_root", return_value=stale), \
            mock.patch.object(module, "_pretooluse", side_effect=lambda value, root: captured.append(root) or 0), \
            mock.patch("sys.stdin", io.StringIO(json.dumps(payload))):
            module.main()
        self.assertEqual(captured, [bound])

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

    def test_external_delivery_workspace_owns_local_and_history_effects(self) -> None:
        module = self._hook_module()
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            governance = temp / "governance"
            service = temp / "service"
            unregistered = temp / "unregistered"
            for repository in (governance, service, unregistered):
                repository.mkdir()
                subprocess.run(["git", "init", "-q", str(repository)], check=True)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository),
                        "config",
                        "user.email",
                        "test@example.invalid",
                    ],
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository),
                        "config",
                        "user.name",
                        "Continuity Test",
                    ],
                    check=True,
                )
                (repository / "README.md").write_text(
                    "fixture\n",
                    encoding="utf-8",
                )
                subprocess.run(
                    ["git", "-C", str(repository), "add", "README.md"],
                    check=True,
                )
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(repository),
                        "commit",
                        "-qm",
                        "test: initialize",
                    ],
                    check=True,
                )
            (governance / ".continuity").mkdir()
            profile = governance / ".continuity/project.yaml"
            profile.write_text(
                "schema_version: context.project/v1alpha1\n"
                "project_id: governance-project\n",
                encoding="utf-8",
            )
            registry = {
                "schema_version": "context.delivery-workspace-registry/v1alpha1",
                "project_id": "governance-project",
                "project_profile_sha256": module._file_hash(profile),
                "workspaces": [
                    {
                        "workspace_id": "service",
                        "workspace_root": str(service.resolve()),
                        "repository_sha256": module._repository_sha256(service),
                        "allowed_effects": [
                            "source-control.history-rewrite",
                            "source-control.local",
                            "source-control.push",
                        ],
                    }
                ],
                "registry_sha256": "",
            }
            registry["registry_sha256"] = module._hash(
                module._canonical(
                    {
                        key: value
                        for key, value in registry.items()
                        if key != "registry_sha256"
                    }
                )
            )
            registry_path = (
                governance / ".continuity/local/delivery-workspaces.json"
            )
            registry_path.parent.mkdir(parents=True)
            registry_path.write_text(
                module._canonical(registry) + "\n",
                encoding="utf-8",
            )
            registry_path.chmod(0o600)
            packet = {
                "schema_version": "context.recovery-envelope/v1alpha1",
                "project_id": "governance-project",
                "revision": 8,
                "active_work": {"work_id": "delivery-work"},
                "claim": {
                    "claim_id": "delivery-claim",
                    "actor_ref": "delivery-actor",
                    "status": "active",
                    "scope_owners": [
                        {"scope_kind": "capability", "scope_ref": "delivery"},
                        {"scope_kind": "repo", "scope_ref": "repo://service"},
                        {
                            "scope_kind": "effect",
                            "scope_ref": "source-control.local",
                        },
                        {
                            "scope_kind": "effect",
                            "scope_ref": "source-control.history-rewrite",
                        },
                        {
                            "scope_kind": "effect",
                            "scope_ref": "source-control.push",
                        },
                    ],
                },
                "next_action": "continue-active-work",
                "source_fresh": True,
                "lease_valid": True,
                "checkpoint_verified": True,
                "read_only": False,
            }
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
                "MCP_BINDING_ENVELOPE": json.dumps(packet),
                "CONTINUITY_EFFECT_POLICY": "strict",
            }

            def invoke(
                tool_use_id: str,
                command: str,
                workdir: Path,
                event: str = "PreToolUse",
                *,
                include_workdir: bool = True,
            ) -> subprocess.CompletedProcess[str]:
                payload = {
                    "session_id": "delivery-session",
                    "transcript_path": None,
                    "cwd": str(governance),
                    "hook_event_name": event,
                    "model": "provider-model",
                    "turn_id": "delivery-turn",
                    "tool_name": "Bash",
                    "tool_use_id": tool_use_id,
                    "tool_input": {"command": command},
                }
                if include_workdir:
                    payload["tool_input"]["workdir"] = str(workdir)
                if event == "PostToolUse":
                    payload["tool_response"] = {"exit_code": 0, "output": "ok"}
                return subprocess.run(
                    [sys.executable, str(self.script)],
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    env=environment,
                    check=False,
                )

            allowed = invoke("service-commit", "git commit -m service", service)
            self.assertEqual(allowed.stdout, "")
            connection = sqlite3.connect(
                plugin_data / "recovery-budget.sqlite3"
            )
            try:
                repository_sha256 = connection.execute(
                    "SELECT repository_sha256 FROM effect_intents_v2"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(
                repository_sha256,
                module._repository_sha256(service),
            )
            released = invoke(
                "service-commit",
                "git commit -m service",
                service,
                "PostToolUse",
            )
            self.assertEqual(released.stdout, "")
            connection = sqlite3.connect(
                plugin_data / "recovery-budget.sqlite3"
            )
            try:
                remaining_intents = connection.execute(
                    "SELECT COUNT(*) FROM effect_intents_v2"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(remaining_intents, 0)
            self.assertEqual(
                module._effect_action("git rebase main", "source-control"),
                "source-control.history-rewrite",
            )

            cwd_only = invoke(
                "service-push-cwd-only",
                "git push origin main",
                service,
                include_workdir=False,
            )
            self.assertEqual(cwd_only.stdout, "")

            denied = invoke(
                "unregistered-commit",
                "git commit -m unregistered",
                unregistered,
            )
            self.assertTrue(denied.stdout)
            decision = json.loads(denied.stdout)["hookSpecificOutput"]
            self.assertEqual(decision["permissionDecision"], "deny")
            self.assertIn("workspace", decision["permissionDecisionReason"].lower())

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

    def test_default_effect_policy_observes_without_blocking_development(self) -> None:
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
            effect_policy=None,
        )

        self.assertEqual(completed.stdout, "")
        self.assertEqual(calls, [])
        self.assertNotIn('"decision":"deny"', observations)

    def test_named_tea_pull_tool_uses_the_active_pr_scope(self) -> None:
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
                    {"scope_kind": "capability", "scope_ref": "release"},
                    {"scope_kind": "effect", "scope_ref": "source-control.pr"},
                ],
            },
            "next_action": "continue-active-work",
            "source_fresh": True,
            "lease_valid": True,
            "checkpoint_verified": True,
            "read_only": False,
        }
        completed, calls, _ = self._run_hook(
            "PreToolUse",
            tool_name="tea pulls create",
            tool_input={"command": None},
            resume_packet=packet,
        )
        self.assertEqual(completed.stdout, "")
        self.assertEqual(len(calls), 1)

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
                "CONTINUITY_EFFECT_POLICY": "strict",
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

    def test_distinct_repositories_do_not_share_effect_intents(self) -> None:
        module = self._hook_module()
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            root_a = temp / "foundation-account-contract-fa-00"
            root_b = temp / "project-compute"
            root_a.mkdir()
            root_b.mkdir()
            root_a.joinpath(".continuity").mkdir()
            root_b.joinpath(".continuity").mkdir()
            environment = {**os.environ, "PLUGIN_DATA": str(temp / "plugin-data")}
            claim_a = {
                "claim_id": "claim-a",
                "actor_ref": "owner-a",
                "status": "active",
            }
            claim_b = {
                "claim_id": "claim-b",
                "actor_ref": "owner-b",
                "status": "active",
            }
            payload_a = {
                "session_id": "session-a",
                "tool_use_id": "tool-a",
                "provider": "codex",
                "host_id": "host-a",
            }
            payload_b = {
                "session_id": "session-b",
                "tool_use_id": "tool-b",
                "provider": "codex",
                "host_id": "host-a",
            }
            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                module, "_repository_sha256", return_value="repository-collision"
            ):
                self.assertTrue(
                    module._acquire_effect_intent(
                        payload_a,
                        root_a,
                        effect_class="source-control",
                        claim=claim_a,
                    )
                )
                self.assertTrue(
                    module._acquire_effect_intent(
                        payload_b,
                        root_b,
                        effect_class="remote-effect",
                        claim=claim_b,
                    )
                )
                self.assertFalse(
                    module._acquire_effect_intent(
                        {**payload_b, "tool_use_id": "tool-b-conflict"},
                        root_a,
                        effect_class="remote-effect",
                        claim=claim_b,
                    )
                )

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
        completed, calls, observations = self._run_hook(
            "PostCompact", resume_packet=packet
        )
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

    def test_postcompact_auto_continues_the_current_work_after_canary(self) -> None:
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
        completed, calls, _ = self._run_hook("PostCompact", resume_packet=packet)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["continue"], True)
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertIn("work-active", output["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(calls[0].startswith("checkpoint verify --root "))
        self.assertTrue(calls[1].startswith("autorun --session-id "))

    def test_successful_stage_test_auto_continues_the_current_work(self) -> None:
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
        completed, calls, _ = self._run_hook(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "python -m unittest tests/test_stage.py"},
            resume_packet=packet,
            stage_success=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        output = json.loads(completed.stdout)
        self.assertEqual(output["continue"], True)
        self.assertIn("additionalContext", output["hookSpecificOutput"])
        self.assertIn("work-active", output["hookSpecificOutput"]["additionalContext"])
        self.assertTrue(calls[0].startswith("autorun --session-id "))

    def test_autorun_retries_a_transient_transport_failure(self) -> None:
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
        completed, calls, _ = self._run_hook(
            "PostCompact",
            resume_packet=packet,
            autorun_retry=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["continue"], True)
        self.assertEqual(sum(line.startswith("autorun --session-id ") for line in calls), 2)

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
