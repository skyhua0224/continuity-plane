import unittest
from pathlib import Path


class StateStorePortabilityPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.master = (cls.root / "MASTER.md").read_text(encoding="utf-8")
        cls.status = (cls.root / "STATUS.md").read_text(encoding="utf-8")
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
        m209 = next(line for line in self.master.splitlines() if "| M2-09 |" in line)
        self.assertIn("✅", m203)
        self.assertIn("✅", m208)
        self.assertIn("🧑‍💻", m209)

    def test_profiles_are_not_user_editions_and_self_dogfood_is_primary(self):
        self.assertIn("capability profiles are not user editions", self.target_state)
        self.assertIn("cohesive monolith", self.target_state)
        self.assertIn("| M10-00 |", self.master)
        self.assertIn("Context Control Plane self-dogfood", self.master)

    def test_m2_04_acceptance_routes_to_m2_05_and_preserves_platform_gates(self):
        spi_acceptance_path = (
            self.root
            / "docs"
            / "migrations"
            / "m2-08-state-store-spi-acceptance-2026-08-10.md"
        )
        sqlite_acceptance_path = (
            self.root
            / "docs"
            / "migrations"
            / "m2-09-sqlite-state-store-acceptance-2026-08-10.md"
        )
        artifact_acceptance_path = (
            self.root
            / "docs"
            / "migrations"
            / "m2-04-artifact-store-acceptance-2026-08-10.md"
        )
        m204 = next(line for line in self.master.splitlines() if "| M2-04 |" in line)
        m205 = next(line for line in self.master.splitlines() if "| M2-05 |" in line)
        m208 = next(line for line in self.master.splitlines() if "| M2-08 |" in line)
        m209 = next(line for line in self.master.splitlines() if "| M2-09 |" in line)

        self.assertTrue(spi_acceptance_path.exists())
        self.assertTrue(sqlite_acceptance_path.exists())
        self.assertTrue(artifact_acceptance_path.exists())
        spi_acceptance = spi_acceptance_path.read_text(encoding="utf-8")
        sqlite_acceptance = sqlite_acceptance_path.read_text(encoding="utf-8")
        artifact_acceptance = artifact_acceptance_path.read_text(encoding="utf-8")
        self.assertIn("✅", m204)
        self.assertIn("🟡", m205)
        self.assertIn("✅", m208)
        self.assertIn("🧑‍💻", m209)
        self.assertIn("High 0, Medium 0", spi_acceptance)
        self.assertIn("Windows and macOS remain blocked", sqlite_acceptance)
        self.assertIn("20/20 passed", artifact_acceptance)
        self.assertIn("context_bytes_reduction_percent", artifact_acceptance)
        self.assertIn("版本：revision 25", self.master)
        self.assertIn("版本：revision 25", self.status)
        self.assertIn("active work | M2-05", self.status)
        self.assertIn("governance authority：`MASTER.md` revision 25", self.target_state)


if __name__ == "__main__":
    unittest.main()
