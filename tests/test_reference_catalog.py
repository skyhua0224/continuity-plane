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


if __name__ == "__main__":
    unittest.main()
