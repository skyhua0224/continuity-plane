from __future__ import annotations

import json
import os
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

from tools.build_public_release import build_public_release


class M1100CodexPluginProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.core = cls.root / "integrations/codex/continuity-plane"
        cls.state = cls.root / "integrations/codex/continuity-plane-state"
        cls.search = cls.root / "integrations/codex/continuity-plane-search"

    def test_default_core_has_no_eager_mcp_surface(self) -> None:
        self.assertFalse((self.core / ".mcp.json").exists())
        core_manifest = json.loads(
            (self.core / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(core_manifest["name"], "continuity-plane")
        self.assertTrue((self.core / "hooks/hooks.json").is_file())
        self.assertNotIn("skills", core_manifest)
        tombstone = self.core / "skills/continuity-plane/SKILL.md"
        self.assertTrue(tombstone.is_file())
        self.assertIn("status: superseded", tombstone.read_text(encoding="utf-8"))

    def test_bounded_search_is_an_explicit_optional_plugin(self) -> None:
        search_manifest = json.loads(
            (self.search / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(search_manifest["name"], "continuity-plane-search")
        self.assertTrue((self.search / ".mcp.json").is_file())
        self.assertNotIn("skills", search_manifest)
        self.assertEqual(search_manifest["mcpServers"], "./.mcp.json")
        hooks = json.loads(
            (self.core / "hooks/hooks.json").read_text(encoding="utf-8")
        )["hooks"]
        self.assertEqual(
            set(hooks),
            {
                "SessionStart",
                "PreCompact",
                "PostCompact",
                "PreToolUse",
                "PostToolUse",
                "UserPromptSubmit",
            },
        )

    def test_state_tools_are_an_explicit_advanced_plugin(self) -> None:
        state_manifest = json.loads(
            (self.state / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state_manifest["name"], "continuity-plane-state")
        self.assertTrue((self.state / ".mcp.json").is_file())
        self.assertTrue((self.state / "scripts/continuity-mcp-server.py").is_file())
        self.assertNotIn("skills", state_manifest)
        self.assertFalse((self.state / "hooks/hooks.json").exists())

    def test_state_launcher_runs_from_a_clean_working_directory(self) -> None:
        script = self.state / "scripts/continuity-mcp-server.py"
        source = script.read_text(encoding="utf-8")
        self.assertIn("from context_control_plane.codex_mcp_server import main", source)
        self.assertIn("sys.path", source)
        request = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05"},
            }
        ) + "\n"
        completed = subprocess.run(
            [sys.executable, str(script)],
            cwd="/tmp",
            input=request,
            capture_output=True,
            text=True,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ""},
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["result"]["serverInfo"]["name"], "continuity")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            build_public_release(self.root, output, initialize_git=False)
            public_script = output / "plugins/continuity-plane-state/scripts/continuity-mcp-server.py"
            generated = subprocess.run(
                [sys.executable, str(public_script)],
                cwd="/tmp",
                input=request,
                capture_output=True,
                text=True,
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": ""},
                timeout=10,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            self.assertEqual(
                json.loads(generated.stdout)["result"]["serverInfo"]["name"],
                "continuity",
            )
            self.assertTrue(
                (output / "continuity_plane/codex_hook_launcher.py").is_file()
            )
            public_pyproject = (output / "pyproject.toml").read_text(encoding="utf-8")
            self.assertIn(
                "continuity-codex-hook = \"continuity_plane.codex_hook_launcher:main\"",
                public_pyproject,
            )

    def test_public_marketplace_offers_core_and_state_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            build_public_release(self.root, output, initialize_git=False)
            marketplace = json.loads(
                (output / ".agents/plugins/marketplace.json").read_text(
                    encoding="utf-8"
                )
            )
            advisory = output / "plugins/continuity-plane/scripts/continuity-advisory-hook.py"
            self.assertTrue(advisory.is_file(), "the public plugin must ship its registered entry")
            project = Path(directory) / "project"
            (project / ".continuity").mkdir(parents=True)
            (project / ".continuity/project.yaml").write_text("project_id: sample\n")
            data = Path(directory) / "data"
            for event in ("PreToolUse", "PostToolUse"):
                result = subprocess.run(
                    [sys.executable, str(output / "continuity_plane/codex_hook_launcher.py"), str(advisory)],
                    input=json.dumps({"cwd": str(project), "session_id": "public-test", "hook_event_name": event,
                                      "tool_name": "Bash", "tool_input": {"command": "git push origin main"}}),
                    env={**os.environ, "PLUGIN_DATA": str(data), "CONTINUITY_EFFECT_POLICY": "strict"},
                    cwd=project, capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, "")
            observations = next((data / "live-events").glob("*.jsonl")).read_text().splitlines()
            self.assertEqual(len(observations), 2)

            self.assertEqual(
                [plugin["name"] for plugin in marketplace["plugins"]],
                [
                    "continuity-plane",
                    "continuity-plane-search",
                    "continuity-plane-state",
                ],
            )
            self.assertFalse(
                (output / "plugins/continuity-plane/.mcp.json").exists()
            )
            self.assertTrue(
                (output / "plugins/continuity-plane-state/.mcp.json").is_file()
            )
            self.assertTrue(
                (output / "plugins/continuity-plane-search/.mcp.json").is_file()
            )
            self.assertFalse(
                (output / "plugins/continuity-plane-search/skills").exists()
            )
            self.assertFalse(
                (output / "plugins/continuity-plane-state/skills").exists()
            )
            for readme in ("README.md", "README.en.md"):
                text = (output / readme).read_text(encoding="utf-8")
                self.assertIn(
                    "codex plugin add continuity-plane@continuity-plane",
                    text,
                )
                self.assertIn(
                    "codex plugin add continuity-plane-search@continuity-plane",
                    text,
                )
                self.assertIn(
                    "codex plugin add continuity-plane-state@continuity-plane",
                    text,
                )


if __name__ == "__main__":
    unittest.main()
