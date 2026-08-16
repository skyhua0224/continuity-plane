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
            "anthropic-claude-code-agents": {
                "content_sha256": "ae17a0b90a1b7944030191b31f5d254d8f74d8794faee620f94d9314d2e002bb"
            },
            "anthropic-claude-code-agent-teams": {
                "content_sha256": "c079ade28c18a4f2a48c4e9d47d7e58c09dab1354edc7e16919db40340094449"
            },
            "anthropic-claude-code-subagents": {
                "content_sha256": "2f08d529620dba34468ea6a0b660843f4b7bbe17f50874f11294c1ba6e45c5f8"
            },
            "anthropic-claude-code-advisor": {
                "content_sha256": "14b381a1728b52eeca7445c089cd649a5ca05f20feb95ee8c8708c7971ebdc71"
            },
            "anthropic-claude-code-worktrees": {
                "content_sha256": "c5a85623acef35889ca086313de4ddd3383c9b6ab4b04ff7e0dcfa583c41075d"
            },
        }
        entries = {entry["reference_id"]: entry for entry in self.catalog["entries"]}

        for reference_id, fields in expected.items():
            with self.subTest(reference_id=reference_id):
                self.assertIn(reference_id, entries)
                self.assertEqual(
                    entries[reference_id]["content_sha256"],
                    fields["content_sha256"],
                )
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

    def test_catalog_tracks_deepseek_cordis_and_pi_as_bounded_candidates(self):
        entries = {entry["reference_id"]: entry for entry in self.catalog["entries"]}

        expected = {
            "deepseek-ai-deepseek-harness": {
                "source_revision": "47f943859bef60e4160492346772ded9b24f765a",
                "source_tree": "f904efab9ef435201d6ba4da88a34d6366568272",
                "content_sha256": "0b86f44cf564c8fefa279134c21a6e5f9bb933520ef25c7ac360c42437e381be",
                "license_ref": "MIT",
                "validity": "verified-current",
            },
            "cordiverse-spatiotemporal-composability-paper": {
                "source_revision": "948a07b369c62adb3b12e102458be5c18dfb69b9",
                "source_tree": "9843926bd597bf184536fe9b2961bcc77f245bb6",
                "content_sha256": "4d48478dc0b6222d9f74d7db10ee776449b1209eb112632336544d32a49db97f",
                "license_ref": "unknown",
                "validity": "candidate",
            },
            "earendil-pi-compaction": {
                "source_revision": "retrieved-2026-08-14",
                "content_sha256": "af529b36af20560837631b3c4c3681ee5d409d849474a380860690b08d448bc4",
                "license_ref": "source-terms",
                "validity": "verified-current",
            },
            "earendil-pi-agent-harness": {
                "source_revision": "9d2ec7ffabe927bfad2214c1cee25b6632a78dcf",
                "source_tree": "9108e8903e1ba009dac694ff8ad6289b8673b1eb",
                "article_source_revision": "47610217098d9ba8f22d223fa7c1413f9f5fd759",
                "content_sha256": "1a33d95a34a4cc2a23f787b3a62acb3e6bc6b538187991aab08da94a82e387b0",
                "license_ref": "MIT",
                "validity": "verified-current",
            },
        }

        for reference_id, fields in expected.items():
            with self.subTest(reference_id=reference_id):
                self.assertIn(reference_id, entries)
                for field, value in fields.items():
                    self.assertEqual(entries[reference_id][field], value)
                self.assertEqual(entries[reference_id]["adoption_status"], "candidate")

    def test_catalog_pins_the_m8_03_temporal_sdk_release(self):
        entries = {entry["reference_id"]: entry for entry in self.catalog["entries"]}

        temporal = entries["temporalio-python-sdk-1-31"]
        self.assertEqual(temporal["source_revision"], "84b519e0ff407b049da88ac7d1711f110494ff4d")
        self.assertEqual(temporal["source_tree"], "6ca7d581e9e0bea3f19a0e1bf5f3a5ef9fec6d21")
        self.assertEqual(
            temporal["content_sha256"],
            "44fcd507cce70c1fd4210edcb554c9b0275b849bfe8ac9867aa0af7975f16435",
        )
        self.assertEqual(temporal["license_ref"], "MIT")
        self.assertEqual(temporal["validity"], "verified-current")
        self.assertEqual(temporal["adoption_status"], "candidate")
        self.assertIn("M8-03", temporal["supports"])


if __name__ == "__main__":
    unittest.main()
