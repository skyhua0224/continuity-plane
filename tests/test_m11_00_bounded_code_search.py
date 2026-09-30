from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from context_control_plane.bounded_code_search import (
    bounded_git_search,
    validate_bounded_code_search_receipt,
)
from context_control_plane.cli import main as continuity_main


class M1100BoundedCodeSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Search Test"],
            check=True,
        )
        (self.root / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
        (self.root / "alpha.py").write_text(
            "def build_runtime():\n    return 'runtime-slot'\n",
            encoding="utf-8",
        )
        (self.root / "beta.py").write_text(
            "def verify_runtime():\n    return 'runtime-test'\n",
            encoding="utf-8",
        )
        (self.root / "ignored.env").write_text(
            "runtime-secret=private\n", encoding="utf-8"
        )
        subprocess.run(
            ["git", "-C", str(self.root), "add", ".gitignore", "alpha.py", "beta.py"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "test: fixture"],
            check=True,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_search_is_current_tracked_and_output_bounded(self) -> None:
        (self.root / "alpha.py").write_text(
            "def build_runtime():\n    return 'runtime-current'\n",
            encoding="utf-8",
        )

        receipt = bounded_git_search(
            self.root,
            query="runtime",
            max_results=10,
            max_output_bytes=2048,
        )

        validate_bounded_code_search_receipt(receipt, root=self.root)
        self.assertEqual(receipt["match_count"], 4)
        self.assertLessEqual(receipt["returned_bytes"], 2048)
        self.assertIn("runtime-current", json.dumps(receipt))
        self.assertNotIn("runtime-slot", json.dumps(receipt))
        self.assertNotIn("runtime-secret", json.dumps(receipt))
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["memory_authority"])

    def test_small_budget_truncates_without_exceeding_the_limit(self) -> None:
        receipt = bounded_git_search(
            self.root,
            query="runtime",
            max_results=10,
            max_output_bytes=1024,
        )

        validate_bounded_code_search_receipt(receipt, root=self.root)
        self.assertLessEqual(receipt["returned_bytes"], 1024)
        self.assertTrue(receipt["truncated"])

    def test_cli_exposes_context_search(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            status = continuity_main(
                [
                    "context",
                    "search",
                    "--root",
                    str(self.root),
                    "--query",
                    "runtime",
                    "--max-results",
                    "2",
                    "--max-output-bytes",
                    "2048",
                ]
            )

        self.assertEqual(status, 0)
        receipt = json.loads(output.getvalue())
        validate_bounded_code_search_receipt(receipt, root=self.root)
        self.assertEqual(receipt["max_results"], 2)


if __name__ == "__main__":
    unittest.main()
