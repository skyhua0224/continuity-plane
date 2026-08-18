"""M9-01 external State projection measured acceptance tests."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_control_plane.external_state_provider_benchmark import (
    ExternalStateProjectionBenchmarkError,
    benchmark_external_state_projection,
    validate_external_state_projection_benchmark,
)
from tools import run_external_state_projection_benchmark


def _resign(receipt: dict) -> None:
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    receipt["receipt_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class M901ExternalStateProjectionBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_benchmark_proves_consistency_and_fail_closed_gates(self) -> None:
        receipt = benchmark_external_state_projection(
            root=self.root,
            iterations=20,
            generated_at="2026-08-17T17:00:00+08:00",
        )

        validate_external_state_projection_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["gate"]["status"], "passed")
        self.assertEqual(receipt["gate"]["failed_gates"], [])
        self.assertEqual(receipt["results"]["same_revision_matches"], 20)
        self.assertEqual(receipt["results"]["state_digest_matches"], 20)
        self.assertEqual(receipt["results"]["signed_projection_matches"], 20)
        self.assertEqual(receipt["results"]["stale_view_rejections"], 20)
        self.assertEqual(receipt["results"]["unauthorized_rejections"], 20)
        self.assertEqual(receipt["results"]["torn_source_rejections"], 20)
        self.assertEqual(receipt["results"]["unauthorized_state_reads"], 0)
        self.assertEqual(receipt["results"]["authority_violations"], 0)
        self.assertEqual(receipt["results"]["provider_invocations"], 0)
        self.assertEqual(receipt["results"]["external_services"], 0)

    def test_validator_rejects_forged_results_latency_and_provenance(self) -> None:
        receipt = benchmark_external_state_projection(
            root=self.root,
            iterations=5,
            generated_at="2026-08-17T17:00:00+08:00",
        )
        mutations = [
            ("same revision", lambda item: item["results"].__setitem__("same_revision_matches", 4)),
            ("count overflow", lambda item: item["results"].__setitem__("state_digest_matches", 6)),
            ("unauthorized read", lambda item: item["results"].__setitem__("unauthorized_state_reads", 1)),
            ("authority", lambda item: item["results"].__setitem__("authority_violations", 1)),
            ("provider", lambda item: item["results"].__setitem__("provider_invocations", 1)),
            ("external", lambda item: item["results"].__setitem__("external_services", 1)),
            ("negative latency", lambda item: item["latency_ms"].__setitem__("p95", -1.0)),
            ("infinite latency", lambda item: item["latency_ms"].__setitem__("max", float("inf"))),
            ("boolean count", lambda item: item["results"].__setitem__("authority_violations", False)),
            ("boolean rate", lambda item: item["results"].__setitem__("same_revision_rate", True)),
            ("boolean threshold", lambda item: item["thresholds"].__setitem__("authority_violations_max", False)),
            ("loose timestamp", lambda item: item.__setitem__("generated_at", " 2026-08-17T17:00:00+08:00")),
            ("provenance", lambda item: item["provenance"].__setitem__("implementation_sha256", "0" * 64)),
        ]
        for label, mutate in mutations:
            forged = copy.deepcopy(receipt)
            mutate(forged)
            _resign(forged)
            with self.subTest(label=label), self.assertRaises(
                ExternalStateProjectionBenchmarkError
            ):
                validate_external_state_projection_benchmark(forged, root=self.root)

    def test_committed_receipt_is_independently_valid(self) -> None:
        path = self.root / "experiments/evidence/m9-01-external-state-projection-results.json"
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.skipTest("M9-01 measured receipt has not been generated")
        validate_external_state_projection_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["parameters"]["iterations"], 1000)
        self.assertEqual(receipt["gate"]["status"], "passed")

    def test_invalid_iteration_count_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory():
            for value in (True, 0, 1001):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    benchmark_external_state_projection(
                        root=self.root,
                        iterations=value,
                        generated_at="2026-08-17T17:00:00+08:00",
                    )

    def test_runner_writes_a_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = run_external_state_projection_benchmark.main(
                [
                    "--root",
                    str(self.root),
                    "--iterations",
                    "3",
                    "--generated-at",
                    "2026-08-17T17:00:00+08:00",
                    "--output",
                    str(output),
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        self.assertEqual(receipt["parameters"]["iterations"], 3)
        validate_external_state_projection_benchmark(receipt, root=self.root)

    def test_runner_is_directly_executable_from_outside_repository(self) -> None:
        script = self.root / "tools/run_external_state_projection_benchmark.py"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--root",
                    str(self.root),
                    "--iterations",
                    "2",
                    "--output",
                    str(output),
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("same_revision=2/2", result.stdout)


if __name__ == "__main__":
    unittest.main()
