from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.build_public_release import build_public_release


class M1100CodexPluginProfileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.core = cls.root / "integrations/codex/continuity-plane"
        cls.state = cls.root / "integrations/codex/continuity-plane-state"

    def test_default_core_has_no_eager_mcp_surface(self) -> None:
        self.assertFalse((self.core / ".mcp.json").exists())
        core_manifest = json.loads(
            (self.core / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(core_manifest["name"], "continuity-plane")
        self.assertTrue((self.core / "hooks/hooks.json").is_file())
        self.assertTrue((self.core / "skills/continuity-plane/SKILL.md").is_file())

    def test_state_tools_are_an_explicit_advanced_plugin(self) -> None:
        state_manifest = json.loads(
            (self.state / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(state_manifest["name"], "continuity-plane-state")
        self.assertTrue((self.state / ".mcp.json").is_file())
        self.assertTrue((self.state / "scripts/continuity-mcp-server.py").is_file())
        self.assertFalse((self.state / "hooks/hooks.json").exists())

    def test_public_marketplace_offers_core_and_state_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            build_public_release(self.root, output, initialize_git=False)
            marketplace = json.loads(
                (output / ".agents/plugins/marketplace.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(
                [plugin["name"] for plugin in marketplace["plugins"]],
                ["continuity-plane", "continuity-plane-state"],
            )
            self.assertFalse(
                (output / "plugins/continuity-plane/.mcp.json").exists()
            )
            self.assertTrue(
                (output / "plugins/continuity-plane-state/.mcp.json").is_file()
            )
            for readme in ("README.md", "README.en.md"):
                text = (output / readme).read_text(encoding="utf-8")
                self.assertIn(
                    "codex plugin add continuity-plane@continuity-plane",
                    text,
                )
                self.assertIn(
                    "codex plugin add continuity-plane-state@continuity-plane",
                    text,
                )


if __name__ == "__main__":
    unittest.main()
