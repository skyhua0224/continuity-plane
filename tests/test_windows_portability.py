"""Native Windows has no fcntl and reports 64-bit file IDs."""

from __future__ import annotations

import ast
import errno
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1] / "continuity_plane"
POSIX_ONLY = {"fcntl", "termios", "pwd", "grp", "resource", "posix"}


class ImportPortabilityTests(unittest.TestCase):
    def test_no_module_imports_posix_only_modules_at_top_level(self):
        offenders = []
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module.split(".")[0]]
                offenders += [f"{path.relative_to(PACKAGE)}:{node.lineno} {n}" for n in names if n in POSIX_ONLY]
        self.assertEqual(offenders, [])

    @unittest.skipIf(os.name == "nt", "simulates Windows on POSIX; native runs cover the real path")
    def test_registry_locks_without_fcntl(self):
        # Simulate Windows: fcntl is missing and msvcrt reports one busy attempt.
        # Standard modules are loaded first because they branch on os.name.
        script = r"""
import errno, os, pathlib, shutil, subprocess, sys, tempfile, types
sys.modules["fcntl"] = None
calls = []
fake = types.ModuleType("msvcrt")
fake.LK_NBLCK, fake.LK_UNLCK = 2, 0
def locking(fd, mode, size):
    calls.append(mode)
    if calls.count(fake.LK_NBLCK) == 1 and mode == fake.LK_NBLCK:
        raise OSError(errno.EACCES, "busy")
fake.locking = locking
sys.modules["msvcrt"] = fake
import continuity_plane.release_version
os.name = "nt"
try:
    from continuity_plane import collaboration_registry as registry
finally:
    os.name = "posix"
from pathlib import Path
with registry._locked(Path(sys.argv[1]) / "doc.json"):
    pass
assert calls == [2, 2, 0], calls
print("ok")
"""
        with tempfile.TemporaryDirectory() as root:
            result = subprocess.run(
                [sys.executable, "-c", script, root],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(PACKAGE.parent)},
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), "ok")

        if os.name == "nt":
            self.skipTest("POSIX lock path")
        self.assertNotIn(errno.EBADF, {errno.EACCES, errno.EDEADLOCK})



class JournalIdentityTests(unittest.TestCase):
    def test_unsigned_64_bit_file_ids_are_indexed(self):
        from continuity_plane import journal_index

        real_fstat = os.fstat

        def wide_fstat(fd):
            info = list(real_fstat(fd))
            info[1], info[2] = 2**64 - 7, 2**63 + 11  # st_ino, st_dev
            return os.stat_result(info)

        with tempfile.TemporaryDirectory() as root:
            journal = Path(root) / "changes.jsonl"
            journal.write_text('{"cursor": 1, "event_id": "e1"}\n', encoding="utf-8")
            with mock.patch.object(journal_index.os, "fstat", wide_fstat):
                page = journal_index.read_page(journal, 0, 10)
                self.assertIsNotNone(page)
                self.assertEqual([row["cursor"] for row in page[0]], [1])
                statements = []
                connection = journal_index._open(journal)
                try:
                    # A second refresh recognises the same file instead of rebuilding.
                    connection.set_trace_callback(statements.append)
                    self.assertTrue(journal_index.refresh(connection, journal))
                finally:
                    connection.close()
            self.assertFalse(any("DELETE FROM entries" in s for s in statements))

    def test_index_failure_falls_back_to_journal(self):
        from continuity_plane import journal_index

        with tempfile.TemporaryDirectory() as root:
            journal = Path(root) / "changes.jsonl"
            journal.write_text('{"cursor": 1, "event_id": "e1"}\n', encoding="utf-8")
            with mock.patch.object(journal_index, "refresh", side_effect=OverflowError):
                self.assertIsNone(journal_index.read_page(journal, 0, 10))
                self.assertEqual(journal_index.offset_after(journal, 0), 0)
                self.assertEqual(journal_index.find_event(journal, "e1")["cursor"], 1)


if __name__ == "__main__":
    unittest.main()
