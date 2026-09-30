from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from context_control_plane.code_index import (
    build_code_index,
    default_code_index_path,
    lookup_code_index,
    validate_code_index_receipt,
)
from context_control_plane.cli import main as continuity_main


class M1102CodeIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "test@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Index Test"],
            check=True,
        )
        (self.root / "alpha.py").write_text(
            "class Runtime:\n    pass\n\ndef build_runtime():\n    return Runtime()\n",
            encoding="utf-8",
        )
        (self.root / "beta.ts").write_text(
            "export function verify_runtime(): void {}\n",
            encoding="utf-8",
        )
        (self.root / "ignored.env").write_text("runtime-secret=private\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "alpha.py", "beta.ts"], check=True
        )
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "test: index fixture"],
            check=True,
        )
        self.cache = Path(self.directory.name) / "cache" / "index.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_index_reuses_unchanged_files_and_invalidates_changed_files(self) -> None:
        first = build_code_index(self.root, cache_path=self.cache)
        validate_code_index_receipt(first, root=self.root)
        self.assertEqual(first["cache_status"], "miss")
        self.assertEqual(first["rehashed_files"], 2)
        self.assertEqual(first["reused_files"], 0)

        second = build_code_index(self.root, cache_path=self.cache)
        self.assertEqual(second["cache_status"], "hit")
        self.assertEqual(second["rehashed_files"], 0)
        self.assertEqual(second["reused_files"], 2)

        (self.root / "alpha.py").write_text(
            "class Runtime:\n    pass\n\ndef build_runtime_v2():\n    return Runtime()\n",
            encoding="utf-8",
        )
        third = build_code_index(self.root, cache_path=self.cache)
        self.assertEqual(third["cache_status"], "partial")
        self.assertEqual(third["rehashed_files"], 1)
        self.assertEqual(third["reused_files"], 1)
        self.assertEqual(third["indexed_files"], 2)

    def test_lookup_returns_bounded_symbol_refs_without_source_bodies(self) -> None:
        result = lookup_code_index(
            self.root,
            query="build_runtime",
            cache_path=self.cache,
            max_results=10,
            max_output_bytes=2048,
        )
        validate_code_index_receipt(result, root=self.root)
        self.assertEqual(result["match_count"], 1)
        self.assertEqual(result["matches"][0]["path"], "alpha.py")
        self.assertEqual(result["matches"][0]["name"], "build_runtime")
        self.assertIn("line", result["matches"][0])
        self.assertNotIn("return Runtime", json.dumps(result))
        self.assertNotIn("runtime-secret", json.dumps(result))
        self.assertLessEqual(result["returned_bytes"], 2048)
        self.assertFalse(result["state_write_authority"])
        self.assertFalse(result["memory_authority"])

    def test_default_cache_is_outside_project(self) -> None:
        path = default_code_index_path(self.root)
        self.assertNotIn(self.root, path.parents)

    def test_default_cache_isolated_per_repository(self) -> None:
        other = self.root.parent / "other-repo"
        other.mkdir()
        self.assertNotEqual(default_code_index_path(self.root), default_code_index_path(other))

    def test_unsupported_tracked_files_are_skipped_once(self) -> None:
        (self.root / "binary.dat").write_bytes(b"header\0binary\n")
        subprocess.run(["git", "-C", str(self.root), "add", "binary.dat"], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "test: add binary"],
            check=True,
        )
        first = build_code_index(self.root, cache_path=self.cache)
        self.assertEqual(first["indexed_files"], 2)
        self.assertEqual(first["rehashed_files"], 3)
        second = build_code_index(self.root, cache_path=self.cache)
        self.assertEqual(second["indexed_files"], 2)
        self.assertEqual(second["rehashed_files"], 0)
        self.assertEqual(second["reused_files"], 3)

    def test_cli_exposes_index_and_lookup(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            status = continuity_main(
                [
                    "context",
                    "index",
                    "--root",
                    str(self.root),
                    "--cache-path",
                    str(self.cache),
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["indexed_files"], 2)

        output = StringIO()
        with redirect_stdout(output):
            status = continuity_main(
                [
                    "context",
                    "lookup",
                    "--root",
                    str(self.root),
                    "--cache-path",
                    str(self.cache),
                    "--query",
                    "verify_runtime",
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["matches"][0]["path"], "beta.ts")


if __name__ == "__main__":
    unittest.main()
