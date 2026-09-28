"""Codex MCP project/actor/claim binding for write-capable Continuity tools."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from context_control_plane.cli import main as continuity_main
from context_control_plane import codex_mcp_server
from context_control_plane.workspace_binding import register_control_root


class _ExpiredClaimDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2020, 1, 1, tzinfo=UTC)
        return value if tz is not None else value.replace(tzinfo=None)


class M1011CodexMCPBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.server = (
            cls.root
            / "integrations/codex/continuity-plane-state/scripts/continuity-mcp-server.py"
        )
        cls.plugin = cls.root / "integrations/codex/continuity-plane-state"

    def test_plugin_mcp_config_starts_from_plugin_root_and_handshakes(self) -> None:
        config = json.loads((self.plugin / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["continuity"]
        self.assertEqual(server, {"command": "continuity-mcp", "args": []})
        command = [server["command"], *server["args"]]
        with tempfile.TemporaryDirectory() as directory:
            environment = os.environ.copy()
            # Resolve the console script from the interpreter running this test;
            # a user's older global pipx install must not change the handshake.
            interpreter_bin = str(Path(sys.executable).parent)
            environment["PATH"] = interpreter_bin + os.pathsep + environment.get("PATH", "")
            environment["PYTHONPATH"] = str(self.root) + os.pathsep + environment.get("PYTHONPATH", "")
            completed = subprocess.run(
                command,
                cwd=directory,
                input=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18"},
                    }
                )
                + "\n",
                text=True,
                capture_output=True,
                env=environment,
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        response = json.loads(completed.stdout)
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["serverInfo"]["name"], "continuity")
        self.assertEqual(response["result"]["serverInfo"]["version"], "0.1.0-alpha.12")

    def test_packaged_and_plugin_mcp_servers_have_the_same_contract(self) -> None:
        packaged = "context_control_plane.codex_mcp_server"
        plugin_server = self.plugin / "scripts/continuity-mcp-server.py"
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        ) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            plugin_environment = os.environ.copy()
            plugin_environment.pop("PYTHONPATH", None)
            plugin_result = subprocess.run(
                [sys.executable, str(plugin_server)],
                cwd=directory,
                input=request,
                text=True,
                capture_output=True,
                env=plugin_environment,
                check=False,
            )
        package_environment = os.environ.copy()
        package_environment["PYTHONPATH"] = str(self.root)
        package_result = subprocess.run(
            [sys.executable, "-m", packaged],
            cwd=self.root,
            input=request,
            text=True,
            capture_output=True,
            env=package_environment,
            check=False,
        )
        self.assertEqual(plugin_result.returncode, 0, plugin_result.stderr)
        self.assertEqual(package_result.returncode, 0, package_result.stderr)
        self.assertEqual(json.loads(plugin_result.stdout), json.loads(package_result.stdout))

    def test_process_cwd_does_not_prebind_before_explicit_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            bound_project = temp / "bound-project"
            cwd_project = temp / "cwd-project"
            for project, project_id in (
                (bound_project, "bound-project"),
                (cwd_project, "cwd-project"),
                (temp / "unknown-project", "unknown-project"),
            ):
                project.mkdir()
                (project / ".continuity").mkdir()
                (project / ".continuity/project.yaml").write_text(
                    f"project_id: {project_id}\n",
                    encoding="utf-8",
                )
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            calls = temp / "calls.jsonl"
            binary = bin_dir / "continuity"
            binary.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$MCP_BINDING_CALLS"\n'
                'if [ "$1" = "resume" ] && [ "$3" = "$BOUND_ROOT" ]; then '
                'printf \'%s\\n\' "$BOUND_PACKET"; exit 0; fi\n'
                'if [ "$1" = "resume" ]; then printf \'%s\\n\' "$CWD_PACKET"; fi\n',
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            bound_packet = {
                "schema_version": "context.recovery-envelope/v1alpha1",
                "project_id": "bound-project",
                "revision": 4,
                "active_work": {"work_id": "bound-work"},
                "claim": {
                    "claim_id": "bound-claim",
                    "actor_ref": "bound-actor",
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
                },
            }
            requests = [
                self._tool_call(
                    1,
                    "continuity_resume",
                    {"root": str(bound_project)},
                ),
                self._tool_call(
                    2,
                    "continuity_resume",
                    {"root": str(cwd_project)},
                ),
                self._tool_call(
                    3,
                    "continuity_claim_recover",
                    {
                        "root": str(temp / "unknown-project"),
                        "action": "heartbeat",
                        "claim_id": "unknown-claim",
                        "actor_ref": "unknown-actor",
                    },
                ),
            ]
            completed = subprocess.run(
                [sys.executable, str(self.server)],
                cwd=cwd_project,
                input="\n".join(json.dumps(item) for item in requests) + "\n",
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "CONTINUITY_TEST_CLI_EXECUTABLE": str(binary),
                    "MCP_BINDING_CALLS": str(calls),
                    "BOUND_ROOT": str(bound_project),
                    "BOUND_PACKET": json.dumps(bound_packet),
                    "CWD_PACKET": json.dumps(cwd_packet),
                },
                check=True,
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines()]

            self.assertEqual(
                self._tool_text(responses[0])["project_id"],
                "bound-project",
            )
            self.assertNotIn("error", responses[1])
            self.assertIn("error", responses[2])
            self.assertEqual(responses[2]["error"]["code"], -32001)
            call_lines = calls.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                call_lines,
                [f"resume --root {bound_project}", f"resume --root {cwd_project}"],
            )

    def test_registered_git_worktree_resolves_to_its_control_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            control = base / "control"
            control.mkdir()
            subprocess.run(["git", "init", "-q", str(control)], check=True)
            subprocess.run(["git", "-C", str(control), "config", "user.email", "test@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(control), "config", "user.name", "Test"], check=True)
            (control / ".continuity").mkdir()
            (control / ".continuity/project.yaml").write_text("project_id: bound-project\n", encoding="utf-8")
            (control / "README").write_text("fixture\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(control), "add", "."], check=True)
            subprocess.run(["git", "-C", str(control), "commit", "-qm", "fixture"], check=True)
            worktree = base / "worktree"
            subprocess.run(["git", "-C", str(control), "worktree", "add", "-q", str(worktree), "HEAD"], check=True)
            register_control_root(control)

            self.assertEqual(
                codex_mcp_server._requested_root(str(worktree), None),
                control.resolve(),
            )

    def _run(
        self,
        requests: list[dict],
        *,
        read_only: bool = False,
        idle: bool = False,
        start_from_plugin_cache: bool = False,
        checkpoint_failure: bool = False,
        source_fresh: bool | None = None,
        checkpoint_verified: bool | None = None,
        lease_valid: bool | None = None,
        autorun_retry: bool = False,
    ) -> tuple[list[dict], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            project.mkdir()
            (project / ".continuity").mkdir()
            (project / ".continuity/project.yaml").write_text("project: test\n")
            other_project = temp / "other-project"
            other_project.mkdir()
            (other_project / ".continuity").mkdir()
            (other_project / ".continuity/project.yaml").write_text(
                "project: other\n"
            )
            plugin_cache = temp / "plugin-cache"
            plugin_cache.mkdir()
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            calls = temp / "calls.jsonl"
            binary = bin_dir / "continuity"
            envelope = {
                "schema_version": "context.recovery-envelope/v1alpha1",
                "project_id": "project-test",
                "revision": 7,
                "active_work": None if idle else {"work_id": "work-active"},
                "claim": None
                if idle
                else {
                    "claim_id": "claim-active",
                    "actor_ref": "actor-bound",
                },
                "read_only": read_only,
                "source_fresh": (
                    not read_only if source_fresh is None else source_fresh
                ),
                "checkpoint_verified": (
                    not read_only
                    if checkpoint_verified is None
                    else checkpoint_verified
                ),
                "lease_valid": (
                    not read_only if lease_valid is None else lease_valid
                ),
                "next_action": (
                    "activate-next-work"
                    if idle
                    else "remain-read-only"
                    if read_only
                    else "continue-active-work"
                ),
            }
            binary.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$MCP_BINDING_CALLS"\n'
                'if [ "$1" = "resume" ] || [ "$1" = "inspect" ]; then printf \'%s\\n\' "$MCP_BINDING_ENVELOPE"; fi\n'
                'if [ "$1" = "checkpoint" ] && [ "$2" = "create" ] && '
                '[ "$MCP_CHECKPOINT_FAIL" = "1" ]; then '
                'printf \'checkpoint refresh failed\\n\' >&2; exit 9; fi\n'
                'if [ "$1" = "autorun" ] && [ "${MCP_AUTORUN_RETRY:-0}" = "1" ] && [ ! -f "$MCP_AUTORUN_RETRIED" ]; then touch "$MCP_AUTORUN_RETRIED"; printf \'transport closed\\n\' >&2; exit 1; fi\n'
                'if [ "$1" = "autorun" ]; then printf \'%s\\n\' \'{"status":"continued","state_event_created":false}\'; exit 0; fi\n',
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            expanded_requests = json.loads(
                json.dumps(requests)
                .replace("$PROJECT_ROOT", str(project))
                .replace("$OTHER_PROJECT_ROOT", str(other_project))
            )
            completed = subprocess.run(
                ["python3", str(self.server)],
                cwd=plugin_cache if start_from_plugin_cache else project,
                input="\n".join(json.dumps(item) for item in expanded_requests) + "\n",
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "CONTINUITY_TEST_CLI_EXECUTABLE": str(binary),
                    "MCP_BINDING_CALLS": str(calls),
                    "MCP_BINDING_ENVELOPE": json.dumps(envelope),
                    "MCP_CHECKPOINT_FAIL": "1" if checkpoint_failure else "0",
                    "MCP_AUTORUN_RETRY": "1" if autorun_retry else "0",
                    "MCP_AUTORUN_RETRIED": str(temp / "autorun-retried"),
                },
                check=True,
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            call_lines = calls.read_text().splitlines() if calls.exists() else []
            return responses, call_lines

    def test_plugin_cache_process_requires_explicit_resume_for_each_root(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "tools/call",
                "params": {
                    "name": "continuity_claim_recover",
                    "arguments": {
                        "root": "$PROJECT_ROOT",
                        "action": "heartbeat",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "continuity_resume",
                    "arguments": {"root": "$PROJECT_ROOT"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "continuity_claim_recover",
                    "arguments": {
                        "root": "$PROJECT_ROOT",
                        "action": "heartbeat",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "continuity_resume",
                    "arguments": {"root": "$OTHER_PROJECT_ROOT"},
                },
            },
        ]

        responses, calls = self._run(
            requests,
            start_from_plugin_cache=True,
        )

        self.assertIn("error", responses[0])
        self.assertIn("continuity_resume", responses[0]["error"]["message"])
        self.assertNotIn("error", responses[1])
        self.assertNotIn("error", responses[2])
        self.assertNotIn("error", responses[3])
        self.assertEqual(sum("work recover heartbeat" in line for line in calls), 1)

    def test_plugin_cache_resume_reads_state_once(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "continuity_resume",
                "arguments": {"root": "$PROJECT_ROOT"},
            },
        }

        responses, calls = self._run([request], start_from_plugin_cache=True)

        self.assertNotIn("error", responses[0])
        self.assertEqual(sum(line.startswith("resume --root ") for line in calls), 1)

    def test_inspect_is_read_only_and_all_tools_have_safety_annotations(self) -> None:
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            self._tool_call(
                2,
                "continuity_inspect",
                {"root": "$PROJECT_ROOT"},
            ),
        ]

        responses, calls = self._run(
            requests,
            start_from_plugin_cache=True,
            read_only=True,
        )

        tools = {
            item["name"]: item for item in responses[0]["result"]["tools"]
        }
        self.assertEqual(
            tools["continuity_inspect"]["annotations"],
            {
                "readOnlyHint": True,
                "openWorldHint": False,
                "destructiveHint": False,
            },
        )
        self.assertTrue(all("annotations" in item for item in tools.values()))
        self.assertIn(
            "普通项目工作继续",
            tools["continuity_claim_recover"]["description"],
        )
        self.assertFalse(
            any(item["annotations"]["readOnlyHint"] for name, item in tools.items() if name != "continuity_inspect")
        )
        self.assertNotIn("error", responses[1])
        inspect_result = json.loads(
            responses[1]["result"]["content"][0]["text"]
        )
        self.assertEqual(
            responses[1]["result"]["structuredContent"],
            inspect_result,
        )
        self.assertEqual(inspect_result["read_only_scope"], "continuity-state")
        self.assertTrue(inspect_result["ordinary_project_work_allowed"])
        self.assertEqual(
            inspect_result["project_next_action"],
            "continue-ordinary-project-work",
        )
        self.assertEqual(sum(line.startswith("inspect --root ") for line in calls), 1)
        self.assertEqual(sum(line.startswith("resume --root ") for line in calls), 0)

    def test_autorun_tool_continues_the_bound_work_from_a_checkpoint(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "tools/call",
                "params": {
                    "name": "continuity_resume",
                    "arguments": {"root": "$PROJECT_ROOT"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "continuity_autorun",
                    "arguments": {"root": "$PROJECT_ROOT"},
                },
            },
        ]
        responses, calls = self._run(requests)
        self.assertNotIn("error", responses[0])
        self.assertNotIn("error", responses[1])
        self.assertIn("autorun", " ".join(calls))

    def test_autorun_retries_a_transient_transport_failure_on_the_same_root(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "tools/call",
                "params": {
                    "name": "continuity_resume",
                    "arguments": {"root": "$PROJECT_ROOT"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "continuity_autorun",
                    "arguments": {"root": "$PROJECT_ROOT"},
                },
            },
        ]
        responses, calls = self._run(requests, autorun_retry=True)
        self.assertNotIn("error", responses[1])
        self.assertEqual(sum(line.startswith("autorun ") for line in calls), 2)
        self.assertEqual(sum(line.startswith("resume ") for line in calls), 4)

    def test_transition_tool_is_exposed_and_executes_as_one_cli_operation(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
            },
            self._tool_call(
                2,
                "continuity_resume",
                {"root": "$PROJECT_ROOT"},
            ),
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "continuity_work_transition",
                    "arguments": {
                        "root": ".",
                        "work_id": "work-active",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                        "return_work_id": "work-parent",
                        "successor_claim_id": "claim-parent",
                        "successor_scope": ["capability:network"],
                        "resolved_blocker_id": "blocker-dependency",
                        "remaining_blocker_id": "blocker-external",
                        "remaining_blocker_reason": "physical path is unreachable",
                        "workspace_root": "$PROJECT_ROOT",
                        "expected_head": "a" * 40,
                        "expected_ref": "origin/work",
                        "evidence_files": ["evidence.json"],
                    },
                },
            },
        ]

        responses, calls = self._run(requests)

        names = {item["name"] for item in responses[0]["result"]["tools"]}
        self.assertIn("continuity_work_transition", names)
        self.assertNotIn("error", responses[2])
        self.assertEqual(sum("work transition" in line for line in calls), 1)
        self.assertEqual(sum("checkpoint create" in line for line in calls), 0)
        transition = next(line for line in calls if "work transition" in line)
        self.assertIn("--return-work-id work-parent", transition)
        self.assertIn("--successor-claim-id claim-parent", transition)
        self.assertIn("--remaining-blocker-id blocker-external", transition)

    def test_transition_replay_accepts_the_bound_return_claim(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "continuity_work_transition",
                "arguments": {
                    "root": ".",
                    "work_id": "work-child",
                    "claim_id": "claim-child",
                    "actor_ref": "actor-bound",
                    "return_work_id": "work-active",
                    "successor_claim_id": "claim-active",
                    "successor_scope": ["capability:network"],
                    "resolved_blocker_id": "blocker-dependency",
                    "workspace_root": "$PROJECT_ROOT",
                    "expected_head": "a" * 40,
                    "evidence_files": ["evidence.json"],
                },
            },
        }

        responses, calls = self._run(
            [
                self._tool_call(
                    0,
                    "continuity_resume",
                    {"root": "$PROJECT_ROOT"},
                ),
                request,
            ]
        )

        self.assertNotIn("error", responses[1])
        self.assertEqual(sum("work transition" in line for line in calls), 1)

    def test_idle_resume_binds_and_allows_only_successor_activation(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "continuity_resume",
                    "arguments": {"root": "$PROJECT_ROOT"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "continuity_work_activate",
                    "arguments": {
                        "root": "$PROJECT_ROOT",
                        "work_id": "N-69-07",
                        "work_title": "Validate ECN and BBR end to end",
                        "owner_ref": "actor-bound",
                        "claim_id": "claim-n-69-07",
                        "scope": [
                            "capability:network-cc-reliable",
                            "capability:network-transport-diagnostics",
                        ],
                    },
                },
            },
        ]

        responses, calls = self._run(requests, idle=True)

        self.assertNotIn("error", responses[0])
        self.assertNotIn("error", responses[1])
        self.assertEqual(sum("work activate" in line for line in calls), 1)

    def test_idle_session_can_activate_a_fully_bound_delivery_work(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
            },
            self._tool_call(
                2,
                "continuity_resume",
                {"root": "$PROJECT_ROOT"},
            ),
            self._tool_call(
                3,
                "continuity_work_activate",
                {
                    "root": "$PROJECT_ROOT",
                    "work_id": "work-delivery",
                    "work_title": "Deliver verified implementation",
                    "owner_ref": "actor-bound",
                    "claim_id": "claim-delivery",
                    "scope": ["capability:delivery"],
                    "execution_class": "delivery",
                    "source_ref": "issue://737",
                    "predecessor_work_id": "work-implementation",
                    "implementation_evidence_ids": ["evidence-test-verified"],
                    "workspace_id": "service",
                    "workspace_root": "$PROJECT_ROOT",
                    "expected_head": "a" * 40,
                    "expected_ref": "refs/heads/work",
                    "allow_effects": [
                        "source-control.push",
                        "source-control.pr",
                        "source-control.merge",
                        "deployment.deploy",
                    ],
                },
            ),
        ]

        responses, calls = self._run(requests, idle=True)

        activation = next(
            item
            for item in responses[0]["result"]["tools"]
            if item["name"] == "continuity_work_activate"
        )
        properties = activation["inputSchema"]["properties"]
        self.assertIn("execution_class", properties)
        self.assertIn("predecessor_work_id", properties)
        self.assertIn("implementation_evidence_ids", properties)
        self.assertIn("allow_effects", properties)
        self.assertNotIn("error", responses[2], responses[2])
        command = next(line for line in calls if "work activate" in line)
        self.assertIn("--execution-class delivery", command)
        self.assertIn("--source-ref issue://737", command)
        self.assertIn("--predecessor-work-id work-implementation", command)
        self.assertIn("--implementation-evidence-id evidence-test-verified", command)
        self.assertIn("--workspace-id service", command)
        self.assertIn("--workspace-root ", command)
        self.assertIn("/project --expected-head", command)
        self.assertIn(f"--expected-head {'a' * 40}", command)
        self.assertIn("--expected-ref refs/heads/work", command)
        self.assertIn("--allow-effect source-control.push", command)
        self.assertIn("--allow-effect deployment.deploy", command)

    def test_claim_recovery_is_one_cli_operation_with_internal_checkpoint(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "continuity_claim_recover",
                "arguments": {
                    "root": ".",
                    "action": "heartbeat",
                    "claim_id": "claim-active",
                    "actor_ref": "actor-bound",
                },
            },
        }

        responses, calls = self._run(
            [
                self._tool_call(
                    0,
                    "continuity_resume",
                    {"root": "$PROJECT_ROOT"},
                ),
                request,
            ],
            checkpoint_failure=True,
        )

        self.assertFalse(responses[1]["result"]["isError"])
        self.assertEqual(sum("work recover heartbeat" in line for line in calls), 1)
        self.assertEqual(sum("checkpoint create" in line for line in calls), 0)

    def test_cross_project_and_wrong_actor_or_claim_are_rejected_before_cli(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "continuity_resume",
                    "arguments": {"root": "$PROJECT_ROOT"},
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "continuity_work_complete",
                    "arguments": {
                        "root": "$OTHER_PROJECT_ROOT",
                        "work_id": "work-active",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                        "evidence_files": ["evidence.json"],
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "continuity_claim_recover",
                    "arguments": {
                        "root": ".",
                        "action": "heartbeat",
                        "claim_id": "claim-other",
                        "actor_ref": "actor-other",
                    },
                },
            },
        ]
        responses, calls = self._run(requests)
        self.assertNotIn("error", responses[1])
        self.assertIn("error", responses[2])
        self.assertIn("binding", responses[2]["error"]["message"].lower())
        self.assertIn("error", responses[3])
        self.assertIn("binding", responses[3]["error"]["message"].lower())
        self.assertEqual(sum("work complete" in line for line in calls), 0)
        self.assertEqual(sum("work recover" in line for line in calls), 0)

    def test_bound_write_executes_and_read_only_envelope_denies_writes(self) -> None:
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "continuity_claim_recover",
                "arguments": {
                    "root": ".",
                    "action": "heartbeat",
                    "claim_id": "claim-active",
                    "actor_ref": "actor-bound",
                },
            },
        }
        requests = [
            self._tool_call(
                0,
                "continuity_resume",
                {"root": "$PROJECT_ROOT"},
            ),
            request,
        ]
        responses, calls = self._run(requests)
        self.assertNotIn("error", responses[1])
        self.assertEqual(sum("work recover heartbeat" in line for line in calls), 1)

        denied, denied_calls = self._run(requests, read_only=True)
        self.assertIn("error", denied[1])
        self.assertNotIn("read-only", denied[1]["error"]["message"].lower())
        self.assertIn("State writes are not ready", denied[1]["error"]["message"])
        self.assertIn("ordinary project work remains allowed", denied[1]["error"]["message"])
        self.assertEqual(sum("work recover heartbeat" in line for line in denied_calls), 0)

    def test_source_stale_owner_heartbeat_recovers_source_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "project"
            project.mkdir()
            master = project / "MASTER.md"
            status = project / "STATUS.md"
            master.write_text("# Master\n\n- active plan\n", encoding="utf-8")
            status.write_text("# Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    continuity_main(
                        ["init", "--root", str(project), "--project-id", "project-test"]
                    ),
                    0,
                )
                self.assertEqual(
                    continuity_main(
                        [
                            "attach", "plan", "--root", str(project),
                            "--master", "MASTER.md", "--status", "STATUS.md",
                            "--work-id", "work-active", "--work-title", "Active Work",
                            "--owner-ref", "actor-bound", "--scope", "capability:main",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    continuity_main(
                        [
                            "attach", "approve", "--root", str(project),
                            "--actor-ref", "actor-bound", "--claim-id", "claim-active",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    continuity_main(["checkpoint", "create", "--root", str(project)]),
                    0,
                )

            master.write_text(
                "# Master\n\n- active plan\n- in-scope progress\n", encoding="utf-8"
            )
            dirty = project / "implementation-progress.txt"
            dirty.write_text("preserve this active work\n", encoding="utf-8")
            bin_dir = Path(directory) / "bin"
            bin_dir.mkdir()
            launcher = bin_dir / "continuity"
            launcher.write_text(
                f"#!{sys.executable}\n"
                "from context_control_plane.cli import main\n"
                "raise SystemExit(main())\n",
                encoding="utf-8",
            )
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
            requests = [
                self._tool_call(1, "continuity_resume", {"root": str(project)}),
                self._tool_call(
                    2,
                    "continuity_claim_recover",
                    {
                        "root": str(project),
                        "action": "heartbeat",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                        "lease_ttl_ms": 3_600_000,
                    },
                ),
                self._tool_call(3, "continuity_resume", {"root": str(project)}),
            ]
            completed = subprocess.run(
                [sys.executable, str(self.server)],
                cwd=project,
                input="\n".join(json.dumps(item) for item in requests) + "\n",
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "CONTINUITY_TEST_CLI_EXECUTABLE": str(launcher),
                    "PYTHONPATH": os.pathsep.join(
                        filter(None, [str(self.root), os.environ.get("PYTHONPATH", "")])
                    ),
                },
                check=True,
            )
            responses = {
                response["id"]: response
                for response in map(json.loads, completed.stdout.splitlines())
            }

            initial = self._tool_text(responses[1])
            self.assertFalse(initial["source_fresh"])
            self.assertTrue(initial["lease_valid"])
            self.assertTrue(initial["checkpoint_verified"])
            self.assertTrue(initial["read_only"])
            recovered = self._tool_text(responses[2])
            self.assertEqual(recovered["status"], "heartbeat")
            self.assertTrue(recovered["source_recovered"])
            self.assertTrue(recovered["checkpoint_verified"])
            self.assertEqual(recovered["revision"], initial["revision"] + 1)
            resumed = self._tool_text(responses[3])
            self.assertEqual(resumed["revision"], recovered["revision"])
            self.assertTrue(resumed["source_fresh"])
            self.assertTrue(resumed["lease_valid"])
            self.assertTrue(resumed["checkpoint_verified"])
            self.assertFalse(resumed["read_only"])
            self.assertEqual(resumed["claim"]["claim_id"], "claim-active")
            self.assertIn(
                f"evidence-attach-{recovered['source_proposal_sha256'][:16]}",
                resumed["active_work"]["evidence_ids"],
            )
            self.assertEqual(dirty.read_text(encoding="utf-8"), "preserve this active work\n")

    def test_source_stale_expired_claim_can_reclaim_in_the_same_path(self) -> None:
        request = self._tool_call(
            1,
            "continuity_claim_recover",
            {
                "root": "$PROJECT_ROOT",
                "action": "reclaim",
                "claim_id": "claim-active",
                "new_claim_id": "claim-reclaimed",
                "actor_ref": "actor-bound",
            },
        )

        responses, calls = self._run(
            [
                self._tool_call(
                    0,
                    "continuity_resume",
                    {"root": "$PROJECT_ROOT"},
                ),
                request,
            ],
            read_only=True,
            source_fresh=False,
            checkpoint_verified=True,
            lease_valid=False,
        )

        self.assertNotIn("error", responses[1])
        self.assertEqual(sum("work recover reclaim" in line for line in calls), 1)

    def test_expired_read_only_binding_allows_only_reclaim_then_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            project.mkdir()
            (project / "MASTER.md").write_text("# Master\n", encoding="utf-8")
            (project / "STATUS.md").write_text("# Status\n", encoding="utf-8")
            with redirect_stdout(StringIO()):
                with patch(
                    "context_control_plane.cli.datetime", _ExpiredClaimDateTime
                ):
                    self.assertEqual(
                        continuity_main(
                            [
                                "init",
                                "--root",
                                str(project),
                                "--project-id",
                                "project-test",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(
                        continuity_main(
                            [
                                "attach",
                                "plan",
                                "--root",
                                str(project),
                                "--master",
                                "MASTER.md",
                                "--status",
                                "STATUS.md",
                                "--work-id",
                                "work-active",
                                "--work-title",
                                "Active Work",
                                "--owner-ref",
                                "actor-bound",
                                "--scope",
                                "capability:main",
                            ]
                        ),
                        0,
                    )
                    self.assertEqual(
                        continuity_main(
                            [
                                "attach",
                                "approve",
                                "--root",
                                str(project),
                                "--actor-ref",
                                "actor-bound",
                                "--claim-id",
                                "claim-active",
                            ]
                        ),
                        0,
                    )
                self.assertEqual(
                    continuity_main(
                        ["checkpoint", "create", "--root", str(project)]
                    ),
                    0,
                )

            bin_dir = temp / "bin"
            bin_dir.mkdir()
            launcher = bin_dir / "continuity"
            launcher.write_text(
                f"#!{sys.executable}\n"
                "from context_control_plane.cli import main\n"
                "raise SystemExit(main())\n",
                encoding="utf-8",
            )
            launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
            expected_head = "a" * 40
            recovery = {
                "root": str(project),
                "action": "reclaim",
                "claim_id": "claim-active",
                "new_claim_id": "claim-reclaimed-1",
                "actor_ref": "actor-bound",
                "lease_ttl_ms": 3_600_000,
            }
            requests = [
                self._tool_call(1, "continuity_resume", {"root": str(project)}),
                self._tool_call(
                    2,
                    "continuity_checkpoint",
                    {"root": str(project), "action": "create"},
                ),
                self._tool_call(
                    3,
                    "continuity_work_complete",
                    {
                        "root": str(project),
                        "work_id": "work-active",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                        "evidence_files": ["evidence.json"],
                    },
                ),
                self._tool_call(
                    4,
                    "continuity_work_transition",
                    {
                        "root": str(project),
                        "work_id": "work-active",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                        "return_work_id": "work-parent",
                        "successor_claim_id": "claim-parent",
                        "successor_scope": ["capability:main"],
                        "resolved_blocker_id": "blocker-dependency",
                        "workspace_root": str(project),
                        "expected_head": expected_head,
                        "evidence_files": ["evidence.json"],
                    },
                ),
                self._tool_call(
                    5,
                    "continuity_work_activate",
                    {
                        "root": str(project),
                        "work_id": "work-other",
                        "work_title": "Other Work",
                        "owner_ref": "actor-bound",
                        "claim_id": "claim-other",
                        "scope": ["capability:other"],
                    },
                ),
                self._tool_call(
                    6,
                    "continuity_claim_recover",
                    {
                        "root": str(project),
                        "action": "heartbeat",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                    },
                ),
                self._tool_call(
                    7,
                    "continuity_claim_recover",
                    {**recovery, "actor_ref": "actor-other"},
                ),
                self._tool_call(
                    8,
                    "continuity_claim_recover",
                    {**recovery, "claim_id": "claim-other"},
                ),
                self._tool_call(
                    9,
                    "continuity_claim_recover",
                    {**recovery, "scope": ["capability:other"]},
                ),
                self._tool_call(10, "continuity_claim_recover", recovery),
                self._tool_call(11, "continuity_resume", {"root": str(project)}),
                self._tool_call(12, "continuity_claim_recover", recovery),
                self._tool_call(13, "continuity_resume", {"root": str(project)}),
                self._tool_call(
                    14,
                    "continuity_claim_recover",
                    {
                        "root": str(project),
                        "action": "heartbeat",
                        "claim_id": "claim-reclaimed-1",
                        "actor_ref": "actor-bound",
                        "lease_ttl_ms": 3_600_000,
                    },
                ),
                self._tool_call(15, "continuity_resume", {"root": str(project)}),
            ]
            completed = subprocess.run(
                [sys.executable, str(self.server)],
                cwd=temp / "bin",
                input="\n".join(json.dumps(item) for item in requests) + "\n",
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "CONTINUITY_TEST_CLI_EXECUTABLE": str(launcher),
                    "PYTHONPATH": os.pathsep.join(
                        filter(
                            None,
                            [str(self.root), os.environ.get("PYTHONPATH", "")],
                        )
                    ),
                },
                check=True,
            )
            responses = {
                response["id"]: response
                for response in map(json.loads, completed.stdout.splitlines())
            }

            initial = self._tool_text(responses[1])
            self.assertFalse(initial["lease_valid"])
            self.assertTrue(initial["read_only"])
            for request_id in range(2, 9):
                self.assertIn("error", responses[request_id], responses[request_id])
            self.assertEqual(responses[2]["error"]["code"], -32002)
            self.assertEqual(responses[6]["error"]["code"], -32002)

            reclaimed = self._tool_text(responses[10])
            self.assertEqual(reclaimed["status"], "reclaim")
            self.assertEqual(reclaimed["claim_id"], "claim-reclaimed-1")
            self.assertEqual(reclaimed["revision"], initial["revision"] + 1)
            self.assertTrue(reclaimed["checkpoint_verified"])
            resumed = self._tool_text(responses[11])
            self.assertEqual(resumed["revision"], reclaimed["revision"])
            self.assertTrue(resumed["source_fresh"])
            self.assertTrue(resumed["checkpoint_verified"])
            self.assertTrue(resumed["lease_valid"])
            self.assertFalse(resumed["read_only"])
            self.assertEqual(resumed["claim"]["claim_id"], "claim-reclaimed-1")
            self.assertEqual(resumed["claim"]["status"], "active")
            self.assertEqual(
                resumed["claim"]["lease_expires_at"], reclaimed["lease_expires_at"]
            )
            self.assertEqual(resumed["checkpoint_ref"], reclaimed["checkpoint_ref"])

            replayed = self._tool_text(responses[12])
            self.assertEqual(replayed["status"], "already-reclaimed")
            self.assertTrue(replayed["checkpoint_verified"])
            replay_resume = self._tool_text(responses[13])
            self.assertEqual(replay_resume["revision"], resumed["revision"])

            heartbeat = self._tool_text(responses[14])
            self.assertEqual(heartbeat["status"], "heartbeat")
            self.assertTrue(heartbeat["checkpoint_verified"])
            final_resume = self._tool_text(responses[15])
            self.assertEqual(final_resume["revision"], resumed["revision"] + 1)
            self.assertTrue(final_resume["source_fresh"])
            self.assertTrue(final_resume["checkpoint_verified"])
            self.assertTrue(final_resume["lease_valid"])
            self.assertFalse(final_resume["read_only"])
            self.assertIn("error", responses[9])
            self.assertEqual(responses[9]["error"]["code"], -32602)

    @staticmethod
    def _tool_call(request_id: int, name: str, arguments: dict) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

    def _tool_text(self, response: dict) -> dict:
        self.assertNotIn("error", response, response)
        self.assertFalse(response["result"]["isError"], response)
        document = json.loads(response["result"]["content"][0]["text"])
        return document.get("continuity_state", document)


if __name__ == "__main__":
    unittest.main()
