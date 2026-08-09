import copy
import hashlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from context_control_plane.checkpoint_benchmark import (
    build_checkpoint_benchmark_receipt,
    run_checkpoint_benchmark,
    validate_checkpoint_benchmark_receipt,
)


class M206CheckpointBenchmarkTests(unittest.TestCase):
    historical_receipt_sha256 = (
        "0a793a1088bd38d77c083b745a6ec3444bf4354a9030144d3d1d705d2ec97224"
    )
    provenance_paths = (
        "context_control_plane/checkpoint.py",
        "context_control_plane/checkpoint_benchmark.py",
        "tools/run_checkpoint_benchmark.py",
        "experiments/state/m2-01-core-fixtures.yaml",
        "schemas/registry.yaml",
        "schemas/m2-06/checkpoint-manifest.schema.json",
        "tests/test_m2_06_checkpoint_canary.py",
        "tests/test_m2_06_checkpoint_benchmark.py",
    )

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

    def test_receipt_ignores_unrelated_schema_registry_additions(self):
        observed_at = "2026-08-10T08:30:00+08:00"
        with tempfile.TemporaryDirectory() as benchmark_directory:
            benchmark = run_checkpoint_benchmark(
                Path(benchmark_directory),
                copy.deepcopy(self.snapshot),
                samples=1,
            )
        receipt = build_checkpoint_benchmark_receipt(
            root=self.root,
            benchmark=benchmark,
            observed_at=observed_at,
            arguments=["--samples", "1", "--observed-at", observed_at],
        )

        with tempfile.TemporaryDirectory() as directory:
            isolated_root = Path(directory)
            for relative_path in self.provenance_paths:
                destination = isolated_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / relative_path, destination)
            registry_path = isolated_root / "schemas" / "registry.yaml"
            registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
            registry["schemas"].append(
                {
                    "schema_id": "context.unrelated",
                    "current_semver": "1.0.0-alpha.1",
                    "current_wire_version": "context.unrelated/v1alpha1",
                    "supported_wire_versions": ["context.unrelated/v1alpha1"],
                    "artifact_path": "schemas/unrelated.schema.json",
                    "content_sha256": "a" * 64,
                    "status": "current",
                    "compatibility_mode": "strict-versioned",
                    "migrations": [],
                }
            )
            registry_path.write_text(
                yaml.safe_dump(registry, sort_keys=False),
                encoding="utf-8",
            )

            validate_checkpoint_benchmark_receipt(receipt, root=isolated_root)

    def test_receipt_rejects_checkpoint_registry_entry_changes(self):
        observed_at = "2026-08-10T08:31:00+08:00"
        with tempfile.TemporaryDirectory() as benchmark_directory:
            benchmark = run_checkpoint_benchmark(
                Path(benchmark_directory),
                copy.deepcopy(self.snapshot),
                samples=1,
            )
        receipt = build_checkpoint_benchmark_receipt(
            root=self.root,
            benchmark=benchmark,
            observed_at=observed_at,
            arguments=["--samples", "1", "--observed-at", observed_at],
        )

        with tempfile.TemporaryDirectory() as directory:
            isolated_root = Path(directory)
            for relative_path in self.provenance_paths:
                destination = isolated_root / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(self.root / relative_path, destination)
            registry_path = isolated_root / "schemas" / "registry.yaml"
            registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
            checkpoint_entry = next(
                entry
                for entry in registry["schemas"]
                if entry["schema_id"] == "context.checkpoint-manifest"
            )
            checkpoint_entry["status"] = "deprecated"
            registry_path.write_text(
                yaml.safe_dump(registry, sort_keys=False),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "provenance is stale"):
                validate_checkpoint_benchmark_receipt(receipt, root=isolated_root)

    def test_revalidated_forty_sample_receipt_has_current_provenance(self):
        receipt_path = (
            self.root
            / "experiments"
            / "state"
            / "m2-06-checkpoint-canary-revalidation-2026-08-10.yaml"
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

    def test_original_forty_sample_receipt_remains_immutable_historical_evidence(self):
        receipt_path = (
            self.root
            / "experiments"
            / "state"
            / "m2-06-checkpoint-canary-results.yaml"
        )
        receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
        receipt_bytes = receipt_path.read_bytes()

        self.assertEqual(receipt["observed_at"], "2026-08-10T08:00:00+08:00")
        self.assertEqual(
            receipt["measurement"]["checkpoint_ref"]["digest"],
            "e2e1b95edca212758559a954525b22e138a77bbdc8596e92831214192381d69a",
        )
        self.assertEqual(receipt["measurement"]["restore_p95_ms"], 0.2378)
        self.assertEqual(
            hashlib.sha256(receipt_bytes).hexdigest(),
            self.historical_receipt_sha256,
        )
        validate_checkpoint_benchmark_receipt(
            receipt,
            root=self.root,
            historical_receipt_bytes=receipt_bytes,
            expected_historical_sha256=self.historical_receipt_sha256,
        )

        forged = copy.deepcopy(receipt)
        forged["provenance"] = {
            field: "a" * 64 for field in forged["provenance"]
        }
        forged_bytes = yaml.safe_dump(forged, sort_keys=False).encode("utf-8")
        with self.assertRaisesRegex(ValueError, "historical|digest|trust"):
            validate_checkpoint_benchmark_receipt(
                forged,
                root=self.root,
                historical_receipt_bytes=forged_bytes,
                expected_historical_sha256=self.historical_receipt_sha256,
            )

    def test_revalidated_receipt_is_separate_from_the_original_measurement(self):
        receipt_path = (
            self.root
            / "experiments"
            / "state"
            / "m2-06-checkpoint-canary-revalidation-2026-08-10.yaml"
        )
        self.assertTrue(receipt_path.exists())
        receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))
        validate_checkpoint_benchmark_receipt(receipt, root=self.root)
        self.assertEqual(receipt["measurement"]["samples"], 40)


if __name__ == "__main__":
    unittest.main()
