import unittest
from pathlib import Path


class StateStorePortabilityPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.master = (cls.root / "MASTER.md").read_text(encoding="utf-8")
        cls.target_state = (
            cls.root / "docs" / "architecture" / "target-state.md"
        ).read_text(encoding="utf-8")
        assessment_path = (
            cls.root
            / "docs"
            / "research"
            / "state-store-portability-assessment-2026-08-10.md"
        )
        cls.assessment_exists = assessment_path.exists()
        cls.assessment = (
            assessment_path.read_text(encoding="utf-8")
            if cls.assessment_exists
            else ""
        )

    def test_plan_separates_storage_contract_embedded_backend_and_forge_sync(self):
        self.assertIn("| M2-08 |", self.master)
        self.assertIn("| M2-09 |", self.master)
        self.assertIn("| M8-08 |", self.master)
        self.assertIn("| M10-09 |", self.master)

    def test_default_profiles_do_not_require_postgresql_or_containers(self):
        for profile in (
            "local-embedded",
            "forge-coordinated",
            "local-coordinator",
            "shared-strong",
        ):
            self.assertIn(profile, self.target_state)

        local_profile = next(
            line
            for line in self.target_state.splitlines()
            if "`local-embedded`" in line
        )
        self.assertIn("SQLite", local_profile)
        self.assertIn("0", local_profile)
        self.assertNotIn("PostgreSQL", local_profile)
        self.assertNotIn("Docker", local_profile)

    def test_postgresql_is_optional_and_degraded_coordination_is_explicit(self):
        self.assertTrue(self.assessment_exists)
        self.assertIn("optional shared backend", self.assessment)
        self.assertIn("offline", self.assessment)
        self.assertIn("force-with-lease", self.assessment)
        self.assertIn("32.39 MiB", self.assessment)
        self.assertIn("120 MB", self.assessment)

    def test_postgresql_acceptance_routes_to_the_backend_neutral_spi(self):
        acceptance_path = (
            self.root
            / "docs"
            / "migrations"
            / "m2-03-postgresql-state-store-acceptance-2026-08-10.md"
        )

        self.assertTrue(acceptance_path.exists())
        m203 = next(line for line in self.master.splitlines() if "| M2-03 |" in line)
        m208 = next(line for line in self.master.splitlines() if "| M2-08 |" in line)
        self.assertIn("✅", m203)
        self.assertIn("🟡", m208)

    def test_profiles_are_not_user_editions_and_self_dogfood_is_primary(self):
        self.assertIn("capability profiles are not user editions", self.target_state)
        self.assertIn("cohesive monolith", self.target_state)
        self.assertIn("| M10-00 |", self.master)
        self.assertIn("Context Control Plane self-dogfood", self.master)


if __name__ == "__main__":
    unittest.main()
