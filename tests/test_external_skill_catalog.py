import unittest
from pathlib import Path

import yaml


class ExternalSkillCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        catalog_path = (
            Path(__file__).parents[1] / "profiles" / "skill-catalog.example.yaml"
        )
        cls.catalog = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))

    def test_catalog_entries_are_unique_and_pinned_or_explicitly_dynamic(self):
        entries = self.catalog["entries"]
        ids = [entry["catalog_entry_id"] for entry in entries]

        self.assertEqual(len(ids), len(set(ids)))
        for entry in entries:
            self.assertTrue(entry["source_url"].startswith("https://"))
            self.assertTrue(entry["source_revision"])
            if entry["source_revision"] == "dynamic-index":
                self.assertEqual(entry["trust_tier"], "marketplace")
                self.assertEqual(entry["status"], "candidate")

    def test_metadata_only_catalog_does_not_activate_external_skills(self):
        self.assertEqual(self.catalog["status"], "candidate-catalog")
        self.assertNotIn("active", {entry["status"] for entry in self.catalog["entries"]})

    def test_proprietary_entries_are_quarantined(self):
        proprietary = [
            entry for entry in self.catalog["entries"] if entry["license_ref"] == "Proprietary"
        ]

        self.assertTrue(proprietary)
        self.assertTrue(all(entry["status"] == "quarantined" for entry in proprietary))

    def test_catalog_covers_official_and_market_discovery_sources(self):
        urls = "\n".join(entry["source_url"] for entry in self.catalog["entries"])

        self.assertIn("github.com/openai/", urls)
        self.assertIn("github.com/anthropics/", urls)
        self.assertIn("github.com/github/", urls)
        self.assertIn("skills.sh", urls)


if __name__ == "__main__":
    unittest.main()
