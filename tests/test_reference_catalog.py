import re
import unittest
from pathlib import Path

import yaml


class ReferenceCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog_path = (
            Path(__file__).parents[1] / "profiles" / "reference-catalog.example.yaml"
        )
        cls.catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))

    def test_entries_have_stable_identity_snapshot_and_refresh_triggers(self):
        entries = self.catalog["entries"]
        ids = [entry["reference_id"] for entry in entries]

        self.assertEqual(len(ids), len(set(ids)))
        for entry in entries:
            self.assertTrue(entry["canonical_url"].startswith("https://"))
            self.assertTrue(entry["retrieval_url"].startswith("https://"))
            self.assertTrue(entry["source_revision"])
            self.assertRegex(entry["content_sha256"], re.compile(r"^[0-9a-f]{64}$"))
            self.assertGreater(entry["refresh_cadence_days"], 0)
            self.assertIn("before-bearing-use", entry["refresh_triggers"])
            self.assertIn("hash-change", entry["refresh_triggers"])

    def test_proxy_rendered_sources_remain_candidates(self):
        proxy_entries = [
            entry
            for entry in self.catalog["entries"]
            if entry["acquisition"] == "proxy-render"
        ]

        self.assertTrue(proxy_entries)
        self.assertTrue(
            all(entry["validity"] == "candidate" for entry in proxy_entries)
        )

    def test_catalog_does_not_auto_adopt_references(self):
        self.assertEqual(self.catalog["status"], "candidate-catalog")
        self.assertNotIn(
            "adopted", {entry["adoption_status"] for entry in self.catalog["entries"]}
        )

    def test_catalog_covers_codex_and_claude_harness_sources(self):
        urls = "\n".join(entry["canonical_url"] for entry in self.catalog["entries"])

        self.assertIn("developers.openai.com/codex", urls)
        self.assertIn("openai.com/index/harness-engineering", urls)
        self.assertIn("anthropic.com/engineering/effective-harnesses", urls)
        self.assertIn("code.claude.com/docs", urls)

    def test_catalog_covers_current_claude_parallel_agent_surfaces(self):
        expected = {
            "anthropic-claude-code-agents": "ae17a0b90a1b7944030191b31f5d254d8f74d8794faee620f94d9314d2e002bb",
            "anthropic-claude-code-agent-teams": "c079ade28c18a4f2a48c4e9d47d7e58c09dab1354edc7e16919db40340094449",
            "anthropic-claude-code-subagents": "2f08d529620dba34468ea6a0b660843f4b7bbe17f50874f11294c1ba6e45c5f8",
            "anthropic-claude-code-advisor": "14b381a1728b52eeca7445c089cd649a5ca05f20feb95ee8c8708c7971ebdc71",
            "anthropic-claude-code-worktrees": "c5a85623acef35889ca086313de4ddd3383c9b6ab4b04ff7e0dcfa583c41075d",
        }
        entries = {entry["reference_id"]: entry for entry in self.catalog["entries"]}

        for reference_id, content_sha256 in expected.items():
            with self.subTest(reference_id=reference_id):
                self.assertIn(reference_id, entries)
                self.assertEqual(entries[reference_id]["content_sha256"], content_sha256)
                self.assertEqual(entries[reference_id]["acquisition"], "direct-official")
                self.assertEqual(entries[reference_id]["validity"], "verified-current")
                self.assertEqual(entries[reference_id]["adoption_status"], "candidate")

    def test_catalog_tracks_the_selected_docmost_fork_branch_as_candidate(self):
        entries = {entry["reference_id"]: entry for entry in self.catalog["entries"]}

        docmost = entries["yundi339-docmost-native-database-fusion"]
        self.assertEqual(
            docmost["canonical_url"], "https://github.com/Yundi339/docmost"
        )
        self.assertEqual(
            docmost["source_revision"],
            "dcf85124087a11ccb24daad5c4801e528208db9d",
        )
        self.assertEqual(docmost["source_branch"], "feat/native-database-fusion")
        self.assertEqual(docmost["validity"], "candidate")
        self.assertEqual(docmost["adoption_status"], "candidate")


if __name__ == "__main__":
    unittest.main()
