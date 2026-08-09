import copy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from context_control_plane.checkpoint_benchmark import (
    run_checkpoint_benchmark,
    validate_checkpoint_benchmark_receipt,
)


class M206CheckpointBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixtures = yaml.safe_load(
            (
                cls.root / "experiments" / "state" / "m2-01-core-fixtures.yaml"
            ).read_text(encoding="utf-8")
        )
        cls.snapshot = copy.deepcopy(
            next(
                case["document"]
                for case in fixtures["cases"]
                if case["case_id"] == "solo-active-work"
            )
        )

    def test_real_local_path_measures_verified_restore_under_two_seconds(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_checkpoint_benchmark(
                Path(directory),
                copy.deepcopy(self.snapshot),
                samples=12,
            )

        self.assertEqual(result["schema_version"], "context.checkpoint-results/v1alpha1")
        self.assertEqual(result["environment"]["external_services"], 0)
        self.assertEqual(result["environment"]["state_backend"], "context.sqlite")
        self.assertEqual(result["environment"]["artifact_backend"], "local-content-addressed")
        self.assertEqual(result["measurement"]["samples"], 12)
        self.assertGreater(result["measurement"]["manifest_bytes"], 0)
        self.assertGreater(result["measurement"]["snapshot_bytes"], 0)
        self.assertEqual(result["measurement"]["unique_checkpoint_refs"], 1)
        self.assertEqual(result["measurement"]["integrity_failures"], 0)
        self.assertEqual(result["measurement"]["critical_fields_verified"], 18)
        self.assertLess(result["measurement"]["restore_p95_ms"], 2000)
        self.assertTrue(result["acceptance"]["restore_p95_under_2s"])
        self.assertTrue(result["acceptance"]["critical_projection_complete"])

    def test_benchmark_rejects_invalid_sample_counts(self):
        for samples in (0, -1, True, 1.5):
            with self.subTest(samples=samples), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    run_checkpoint_benchmark(
                        Path(directory),
                        copy.deepcopy(self.snapshot),
                        samples=samples,
                    )

    def test_cli_receipt_is_reproducible_and_fails_closed_on_forgery(self):
        observed_at = "2026-08-10T06:30:00+08:00"
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.yaml"
            arguments = [
                "--samples",
                "8",
                "--observed-at",
                observed_at,
                "--output",
                str(output),
            ]
            subprocess.run(
                [
                    sys.executable,
                    str(self.root / "tools" / "run_checkpoint_benchmark.py"),
                    *arguments,
                ],
                cwd=self.root,
                check=True,
            )
            receipt = yaml.safe_load(output.read_text(encoding="utf-8"))

        validate_checkpoint_benchmark_receipt(receipt, root=self.root)
        self.assertEqual(receipt["observed_at"], observed_at)
        self.assertEqual(receipt["generation"]["arguments"], arguments)

        forged = copy.deepcopy(receipt)
        mutations = {
            "manifest_bytes": lambda: forged["measurement"].__setitem__("manifest_bytes", -1),
            "snapshot_bytes": lambda: forged["measurement"].__setitem__("snapshot_bytes", -1),
            "unique refs": lambda: forged["measurement"].__setitem__("unique_checkpoint_refs", 9),
            "integrity": lambda: forged["measurement"].__setitem__("integrity_failures", 1),
            "state backend": lambda: forged["environment"].__setitem__("state_backend", "postgres"),
            "artifact backend": lambda: forged["environment"].__setitem__("artifact_backend", "remote"),
            "command": lambda: forged["generation"].__setitem__("command", "forged"),
            "arguments": lambda: forged["generation"].__setitem__("arguments", ["--samples", "999"]),
            "runtime state": lambda: forged["generation"].__setitem__("writes_runtime_state_to_repository", True),
            "restore p95": lambda: forged["measurement"].__setitem__("restore_p95_ms", 0),
        }
        for label, mutate in mutations.items():
            candidate = copy.deepcopy(receipt)
            mutate = mutations[label]
            mutate.__call__() if candidate is forged else None
            # Apply the same mutation to an isolated receipt so each finding is independent.
            if label == "manifest_bytes": candidate["measurement"]["manifest_bytes"] = -1
            elif label == "snapshot_bytes": candidate["measurement"]["snapshot_bytes"] = -1
            elif label == "unique refs": candidate["measurement"]["unique_checkpoint_refs"] = 9
            elif label == "integrity": candidate["measurement"]["integrity_failures"] = 1
            elif label == "state backend": candidate["environment"]["state_backend"] = "postgres"
            elif label == "artifact backend": candidate["environment"]["artifact_backend"] = "remote"
            elif label == "command": candidate["generation"]["command"] = "forged"
            elif label == "arguments": candidate["generation"]["arguments"] = ["--samples", "999"]
            elif label == "runtime state": candidate["generation"]["writes_runtime_state_to_repository"] = True
            elif label == "restore p95": candidate["measurement"]["restore_p95_ms"] = 0
            with self.assertRaises(ValueError, msg=label):
                validate_checkpoint_benchmark_receipt(candidate, root=self.root)

        cross_field_mutations = {
            "restore acceptance": lambda candidate: candidate["acceptance"].__setitem__(
                "restore_p95_under_2s", False
            ),
            "integrity acceptance": lambda candidate: candidate["acceptance"].__setitem__(
                "missing_or_tampered_artifact_gate", "not-covered"
            ),
            "non-finite latencies": lambda candidate: candidate["measurement"].update(
                {
                    "restore_p50_ms": float("nan"),
                    "restore_p95_ms": float("nan"),
                    "restore_max_ms": float("nan"),
                }
            ),
            "boolean external services": lambda candidate: candidate["acceptance"].__setitem__(
                "external_services", False
            ),
            "generation samples": lambda candidate: candidate["generation"].update(
                {
                    "command": ".venv/bin/python tools/run_checkpoint_benchmark.py --samples 999 --observed-at "
                    + observed_at,
                    "arguments": ["--samples", "999", "--observed-at", observed_at],
                }
            ),
        }
        for label, mutate in cross_field_mutations.items():
            candidate = copy.deepcopy(receipt)
            mutate(candidate)
            with self.subTest(label=label), self.assertRaises(ValueError):
                validate_checkpoint_benchmark_receipt(candidate, root=self.root)

    def test_committed_forty_sample_receipt_has_current_provenance(self):
        receipt_path = (
            self.root
            / "experiments"
            / "state"
            / "m2-06-checkpoint-canary-results.yaml"
        )
        receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))

        validate_checkpoint_benchmark_receipt(receipt, root=self.root)
        self.assertEqual(receipt["measurement"]["samples"], 40)
        self.assertEqual(receipt["measurement"]["critical_fields_verified"], 18)
        self.assertEqual(
            receipt["measurement"]["critical_field_recovery_percent"],
            100.0,
        )
        self.assertLess(receipt["measurement"]["restore_p95_ms"], 2_000)
        self.assertTrue(receipt["acceptance"]["restore_p95_under_2s"])
        self.assertTrue(receipt["acceptance"]["critical_projection_complete"])


if __name__ == "__main__":
    unittest.main()
