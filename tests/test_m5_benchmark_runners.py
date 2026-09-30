"""CLI publication checks for the final M5/M8-04 benchmark receipts."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class BenchmarkRunnerTests(unittest.TestCase):
    def test_all_final_benchmark_runners_publish_valid_receipts(self):
        root = Path(__file__).parents[1]
        runners = (
            (
                "tools/run_idea_return_packet_benchmark.py",
                "context_control_plane.idea_return_packet_benchmark",
                "validate_idea_return_packet_benchmark",
            ),
            (
                "tools/run_durable_continuation_benchmark.py",
                "context_control_plane.durable_continuation_benchmark",
                "validate_durable_continuation_benchmark",
            ),
            (
                "tools/run_context_trace_benchmark.py",
                "context_control_plane.context_trace_benchmark",
                "validate_context_trace_benchmark",
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            for runner, module, validator in runners:
                with self.subTest(runner=runner):
                    output = Path(directory) / Path(runner).name
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(root / runner),
                            "--samples",
                            "4",
                            "--output",
                            str(output),
                        ],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    receipt = json.loads(output.read_text(encoding="utf-8"))
                    validator_args = "r, root=Path.cwd()" if validator == "validate_idea_return_packet_benchmark" else "r"
                    check = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            f"import json; from pathlib import Path; from {module} import {validator}; r=json.load(open({str(output)!r})); {validator}({validator_args})",
                        ],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(check.returncode, 0, check.stderr)
                    self.assertEqual(receipt["samples"], 4)


if __name__ == "__main__":
    unittest.main()
