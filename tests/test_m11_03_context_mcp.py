from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_control_plane.code_index import validate_code_index_receipt


class M1103ContextMCPTests(unittest.TestCase):
    root = Path(__file__).resolve().parents[1]

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.project = Path(self.directory.name) / "repo"
        self.project.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.name", "MCP Test"],
            check=True,
        )
        (self.project / "runtime.py").write_text(
            "class Runtime:\n    pass\n\ndef build_runtime():\n    return Runtime()\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(self.project), "add", "runtime.py"], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "commit", "-qm", "test: mcp"],
            check=True,
        )
        self.cache = Path(self.directory.name) / "cache" / "index.json"
        self.server = self.root / "context_control_plane/context_mcp_server.py"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _call(self, requests: list[dict]) -> list[dict]:
        payload = "".join(json.dumps(request) + "\n" for request in requests)
        completed = subprocess.run(
            [sys.executable, str(self.server)],
            cwd="/tmp",
            input=payload,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return [json.loads(line) for line in completed.stdout.splitlines()]

    def test_lists_one_non_blocking_lookup_tool(self) -> None:
        responses = self._call(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ]
        )
        self.assertEqual(responses[0]["result"]["serverInfo"]["name"], "continuity-search")
        tools = responses[1]["result"]["tools"]
        self.assertEqual([item["name"] for item in tools], ["continuity_context_lookup"])
        self.assertFalse(tools[0]["annotations"]["destructiveHint"])
        self.assertFalse(tools[0]["annotations"]["openWorldHint"])
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])

    def test_search_plugin_uses_the_single_tool_entrypoint(self) -> None:
        config = json.loads(
            (self.root / "integrations/codex/continuity-plane-search/.mcp.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            config["mcpServers"]["continuity-search"],
            {"command": "continuity-search-mcp", "args": []},
        )

    def test_lookup_returns_bounded_hash_bound_refs_without_project_writes(self) -> None:
        before = subprocess.run(
            ["git", "-C", str(self.project), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        responses = self._call(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_context_lookup",
                        "arguments": {
                            "root": str(self.project),
                            "query": "build_runtime",
                            "cache_path": str(self.cache),
                            "max_results": 5,
                            "max_output_bytes": 2048,
                        },
                    },
                }
            ]
        )
        result = json.loads(responses[0]["result"]["content"][0]["text"])
        self.assertEqual(responses[0]["result"]["structuredContent"], result)
        validate_code_index_receipt(result, root=self.project)
        self.assertEqual(result["matches"][0]["path"], "runtime.py")
        self.assertEqual(result["matches"][0]["line"], 4)
        self.assertNotIn("return Runtime", json.dumps(result))
        self.assertLessEqual(result["returned_bytes"], 2048)
        after = subprocess.run(
            ["git", "-C", str(self.project), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertEqual(before, after)

    def test_empty_lookup_is_an_explicit_non_blocking_miss(self) -> None:
        responses = self._call(
            [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {
                        "name": "continuity_context_lookup",
                        "arguments": {
                            "root": str(self.project),
                            "query": "not_present_anywhere",
                            "cache_path": str(self.cache),
                            "max_results": 5,
                            "max_output_bytes": 2048,
                        },
                    },
                }
            ]
        )
        result = responses[0]["result"]["structuredContent"]
        self.assertEqual(result, json.loads(responses[0]["result"]["content"][0]["text"]))
        validate_code_index_receipt(result, root=self.project)
        metadata = responses[0]["result"]["_meta"]["continuity"]
        self.assertEqual(metadata["lookup_status"], "empty")
        self.assertEqual(metadata["fallback"], "narrow-search")
        self.assertEqual(result["matches"], [])
        self.assertFalse(responses[0]["result"]["isError"])


if __name__ == "__main__":
    unittest.main()
