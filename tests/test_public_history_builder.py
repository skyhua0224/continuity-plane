from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.build_public_history import build_public_history
from tools.build_public_release import scan_public_release


class PublicHistoryBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_history_preserves_real_public_evolution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            receipt = build_public_history(self.root, output)

            self.assertGreaterEqual(receipt["projected_commit_count"], 20)
            self.assertEqual(scan_public_release(output), [])
            log = subprocess.run(
                ["git", "log", "--reverse", "--format=%an|%ae|%ad|%s", "--date=short"],
                cwd=output,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertNotIn("dev@sky-hua.xyz", log)
            self.assertNotIn("admin@sky-hua.xyz", log)
            self.assertTrue(
                all(
                    line.startswith("SkyHua|skyhua0224@users.noreply.github.com|")
                    for line in log.splitlines()
                )
            )
            for subject in (
                "typed core contract",
                "SQLite embedded backend",
                "immutable checkpoint canary",
                "strict Skill manifest set",
                "bounded execution packet",
                "retrieval and recall contracts",
                "crash-safe local operations",
            ):
                self.assertIn(subject.lower(), log.lower())

    def test_final_tree_matches_release_compiler(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public"
            receipt = build_public_history(self.root, output)

            self.assertEqual(receipt["final_tree_matches_release"], True)
            self.assertTrue((output / "README.md").is_file())
            self.assertTrue((output / "README.en.md").is_file())
            self.assertTrue((output / "continuity_plane/cli.py").is_file())
            self.assertFalse((output / "MASTER.md").exists())
            self.assertFalse((output / "STATUS.md").exists())


if __name__ == "__main__":
    unittest.main()
