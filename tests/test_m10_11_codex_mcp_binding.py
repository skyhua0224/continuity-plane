"""Codex MCP project/actor/claim binding for write-capable Continuity tools."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


class M1011CodexMCPBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.server = (
            cls.root
            / "integrations/codex/continuity-plane/scripts/continuity-mcp-server.py"
        )

    def _run(self, requests: list[dict], *, read_only: bool = False) -> tuple[list[dict], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            project = temp / "project"
            project.mkdir()
            (project / ".continuity").mkdir()
            (project / ".continuity/project.yaml").write_text("project: test\n")
            bin_dir = temp / "bin"
            bin_dir.mkdir()
            calls = temp / "calls.jsonl"
            binary = bin_dir / "continuity"
            envelope = {
                "schema_version": "context.recovery-envelope/v1alpha1",
                "project_id": "project-test",
                "revision": 7,
                "active_work": {"work_id": "work-active"},
                "claim": {
                    "claim_id": "claim-active",
                    "actor_ref": "actor-bound",
                },
                "read_only": read_only,
            }
            binary.write_text(
                "#!/bin/sh\n"
                'printf \'%s\\n\' "$*" >> "$MCP_BINDING_CALLS"\n'
                'if [ "$1" = "resume" ]; then printf \'%s\\n\' "$MCP_BINDING_ENVELOPE"; fi\n',
                encoding="utf-8",
            )
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            completed = subprocess.run(
                ["python3", str(self.server)],
                cwd=project,
                input="\n".join(json.dumps(item) for item in requests) + "\n",
                text=True,
                capture_output=True,
                env={
                    **os.environ,
                    "PATH": f"{bin_dir}:{os.environ['PATH']}",
                    "MCP_BINDING_CALLS": str(calls),
                    "MCP_BINDING_ENVELOPE": json.dumps(envelope),
                },
                check=True,
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            call_lines = calls.read_text().splitlines() if calls.exists() else []
            return responses, call_lines

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


if __name__ == "__main__":
    unittest.main()
