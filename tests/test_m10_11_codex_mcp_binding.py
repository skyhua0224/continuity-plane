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
            / "integrations/codex/continuity-plane/scripts/continuity-mcp-server.py"
        )
        cls.plugin = cls.root / "integrations/codex/continuity-plane"

    def test_plugin_mcp_config_starts_from_plugin_root_and_handshakes(self) -> None:
        config = json.loads((self.plugin / ".mcp.json").read_text(encoding="utf-8"))
        server = config["mcpServers"]["continuity"]
        self.assertEqual(server, {"command": "continuity-mcp", "args": []})
        command = [server["command"], *server["args"]]
        with tempfile.TemporaryDirectory() as directory:
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
                check=False,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        response = json.loads(completed.stdout)
        self.assertEqual(response["id"], 1)
        self.assertEqual(response["result"]["serverInfo"]["name"], "continuity")

    def test_packaged_and_plugin_mcp_servers_have_the_same_contract(self) -> None:
        packaged = self.root / "context_control_plane/codex_mcp_server.py"
        plugin_server = self.plugin / "scripts/continuity-mcp-server.py"
        self.assertEqual(packaged.read_bytes(), plugin_server.read_bytes())

    def _run(
        self,
        requests: list[dict],
        *,
        read_only: bool = False,
        idle: bool = False,
        start_from_plugin_cache: bool = False,
        checkpoint_failure: bool = False,
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
                "next_action": "activate-next-work" if idle else "continue-active-work",
            }
            binary.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$MCP_BINDING_CALLS"\n'
                'if [ "$1" = "resume" ]; then printf \'%s\\n\' "$MCP_BINDING_ENVELOPE"; fi\n'
                'if [ "$1" = "checkpoint" ] && [ "$2" = "create" ] && '
                '[ "$MCP_CHECKPOINT_FAIL" = "1" ]; then '
                'printf \'checkpoint refresh failed\\n\' >&2; exit 9; fi\n',
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
                    "MCP_BINDING_CALLS": str(calls),
                    "MCP_BINDING_ENVELOPE": json.dumps(envelope),
                    "MCP_CHECKPOINT_FAIL": "1" if checkpoint_failure else "0",
                },
                check=True,
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            call_lines = calls.read_text().splitlines() if calls.exists() else []
            return responses, call_lines

    def test_plugin_cache_process_binds_on_resume_then_rejects_other_root(self) -> None:
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

        responses, calls = self._run(requests, start_from_plugin_cache=True)

        self.assertIn("error", responses[0])
        self.assertIn("continuity_resume", responses[0]["error"]["message"])
        self.assertNotIn("error", responses[1])
        self.assertNotIn("error", responses[2])
        self.assertIn("error", responses[3])
        self.assertIn("project", responses[3]["error"]["message"].lower())
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

    def test_transition_tool_is_exposed_and_executes_as_one_cli_operation(self) -> None:
        requests = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
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
        self.assertNotIn("error", responses[1])
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

        responses, calls = self._run([request])

        self.assertNotIn("error", responses[0])
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

        responses, calls = self._run([request], checkpoint_failure=True)

        self.assertFalse(responses[0]["result"]["isError"])
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
                    "name": "continuity_work_complete",
                    "arguments": {
                        "root": "/tmp/other-project",
                        "work_id": "work-active",
                        "claim_id": "claim-active",
                        "actor_ref": "actor-bound",
                        "evidence_files": ["evidence.json"],
                    },
                },
            },
            {
                "jsonrpc": "2.0",
                "id": 3,
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
        self.assertIn("error", responses[1])
        self.assertIn("project", responses[1]["error"]["message"].lower())
        self.assertIn("error", responses[2])
        self.assertIn("binding", responses[2]["error"]["message"].lower())
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
        responses, calls = self._run([request])
        self.assertNotIn("error", responses[0])
        self.assertEqual(sum("work recover heartbeat" in line for line in calls), 1)

        denied, denied_calls = self._run([request], read_only=True)
        self.assertIn("error", denied[0])
        self.assertIn("read-only", denied[0]["error"]["message"].lower())
        self.assertEqual(sum("work recover heartbeat" in line for line in denied_calls), 0)

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
        return json.loads(response["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
