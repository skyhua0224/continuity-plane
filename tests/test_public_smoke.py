from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from continuity_plane.cli import main


class PublicSmokeTests(unittest.TestCase):
    def test_init_verify_and_doctor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(StringIO()):
                self.assertEqual(
                    main(["init", "--root", str(root), "--project-id", "sample-app"]),
                    0,
                )
                self.assertEqual(main(["verify", "--root", str(root)]), 0)
                self.assertEqual(main(["doctor", "--root", str(root)]), 0)
            self.assertTrue((root / ".continuity/state.sqlite3").is_file())
            self.assertTrue((root / ".continuity/MASTER.en.md").is_file())
            self.assertTrue((root / ".continuity/STATUS.en.md").is_file())

    def test_state_show_reads_initialized_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with redirect_stdout(StringIO()):
                main(["init", "--root", str(root), "--project-id", "sample-app"])
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(main(["state", "show", "--root", str(root)]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result["project_id"], "sample-app")
            self.assertEqual(result["revision"], 1)


if __name__ == "__main__":
    unittest.main()
