import copy
import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.document_lifecycle import (
    DocumentLifecycleError,
    prepare_document_lifecycle_validation_snapshot,
    validate_document_control_manifest,
)
from context_control_plane.document_lifecycle_benchmark import (
    DocumentLifecycleBenchmarkError,
    benchmark_document_lifecycle,
    load_document_lifecycle_benchmark_config,
    measure_document_lifecycle_validator,
    validate_document_lifecycle_benchmark_receipt,
)
from tools import run_document_lifecycle_benchmark


class M010DocumentLifecycleBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.manifest = yaml.safe_load(
            (cls.root / "profiles" / "document-control-manifest.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.baseline_ref = "ed0873319b94f0a5cbac6b8a9815e9837f3ef2e1"
        cls.baseline_status = subprocess.run(
            ["git", "show", f"{cls.baseline_ref}:STATUS.md"],
            cwd=cls.root,
            check=True,
            capture_output=True,
        ).stdout
        cls.baseline_master = subprocess.run(
            ["git", "show", f"{cls.baseline_ref}:MASTER.md"],
            cwd=cls.root,
            check=True,
            capture_output=True,
        ).stdout

    def _benchmark(self, samples: int = 40) -> dict:
        return benchmark_document_lifecycle(
            self.root,
            self.manifest,
            baseline_ref=self.baseline_ref,
            baseline_status=self.baseline_status,
            baseline_master=self.baseline_master,
            generated_at="2026-08-12T00:00:00Z",
            samples=samples,
        )

    def test_benchmark_quantifies_capacity_reduction_and_complete_recovery(self):
        receipt = self._benchmark()
        metrics = receipt["metrics"]

        self.assertEqual(metrics["baseline_status_utf8_bytes"], 15_884)
        self.assertLessEqual(metrics["current_status_utf8_bytes"], 12 * 1024)
        self.assertGreater(metrics["status_bytes_reduction_percent"], 75)
        self.assertGreater(metrics["baseline_master_max_section_utf8_bytes"], 24 * 1024)
        self.assertLessEqual(
            metrics["current_master_max_section_utf8_bytes"], 24 * 1024
        )
        self.assertGreater(metrics["master_max_section_reduction_percent"], 60)
        self.assertEqual(metrics["recovery_fields_recovered"], 5)
        self.assertEqual(metrics["recovery_fields_total"], 5)
        self.assertNotIn("validator_latency_samples_ms", metrics)
        self.assertNotIn("validator_p50_ms", metrics)
        self.assertNotIn("validator_p95_ms", metrics)
        self.assertNotIn("validator_max_ms", metrics)

        live = measure_document_lifecycle_validator(
            self.root, self.manifest, samples=40
        )
        self.assertEqual(live["validator_samples"], 40)
        self.assertEqual(live["validator_failures"], 0)
        self.assertLess(live["validator_p95_ms"], 100)

    def test_benchmark_rejects_non_positive_sample_count(self):
        with self.assertRaisesRegex(DocumentLifecycleBenchmarkError, "samples"):
            self._benchmark(samples=0)

    def test_benchmark_requires_at_least_forty_samples(self):
        with self.assertRaisesRegex(DocumentLifecycleBenchmarkError, "at least 40"):
            self._benchmark(samples=39)

    def test_benchmark_requires_exactly_forty_samples(self):
        with self.assertRaisesRegex(DocumentLifecycleBenchmarkError, "exactly 40"):
            self._benchmark(samples=41)

    def test_benchmark_rejects_a_caller_selected_baseline(self):
        reachable_refs = subprocess.run(
            ["git", "rev-list", "HEAD"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        alternate_ref = next(
            revision for revision in reachable_refs if revision != self.baseline_ref
        )
        alternate_status = subprocess.run(
            ["git", "show", f"{alternate_ref}:STATUS.md"],
            cwd=self.root,
            check=True,
            capture_output=True,
        ).stdout
        alternate_master = subprocess.run(
            ["git", "show", f"{alternate_ref}:MASTER.md"],
            cwd=self.root,
            check=True,
            capture_output=True,
        ).stdout

        with self.assertRaisesRegex(
            DocumentLifecycleBenchmarkError, "governed baseline"
        ):
            benchmark_document_lifecycle(
                self.root,
                self.manifest,
                baseline_ref=alternate_ref,
                baseline_status=alternate_status,
                baseline_master=alternate_master,
                generated_at="2026-08-12T00:00:00Z",
                samples=40,
            )

    def test_live_measurement_reuses_one_preverified_lineage_snapshot(self):
        with mock.patch(
            "context_control_plane.document_lifecycle._validate_committed_lineage",
            wraps=__import__(
                "context_control_plane.document_lifecycle", fromlist=["unused"]
            )._validate_committed_lineage,
        ) as validate_lineage:
            live = measure_document_lifecycle_validator(
                self.root, self.manifest, samples=40
            )

        self.assertEqual(live["validator_failures"], 0)
        self.assertEqual(validate_lineage.call_count, 1)

    def test_live_measurement_reuses_one_preverified_supersedes_snapshot(self):
        expected_supersedes_checks = sum(
            document["document_revision"] > 1 for document in self.manifest["documents"]
        )
        self.assertGreater(expected_supersedes_checks, 0)

        with mock.patch(
            "context_control_plane.document_lifecycle._validate_supersedes_provenance",
            wraps=__import__(
                "context_control_plane.document_lifecycle", fromlist=["unused"]
            )._validate_supersedes_provenance,
        ) as validate_supersedes:
            live = measure_document_lifecycle_validator(
                self.root, self.manifest, samples=40
            )

        self.assertEqual(live["validator_failures"], 0)
        self.assertEqual(validate_supersedes.call_count, expected_supersedes_checks)

    def test_preverified_snapshot_rejects_manifest_mutation(self):
        snapshot = prepare_document_lifecycle_validation_snapshot(
            self.root, self.manifest
        )
        mutated = copy.deepcopy(self.manifest)
        mutated["generated_at"] = "2026-08-12T00:00:01Z"

        with self.assertRaisesRegex(DocumentLifecycleError, "snapshot manifest"):
            validate_document_control_manifest(
                self.root, mutated, lineage_snapshot=snapshot
            )

    def test_benchmark_config_rejects_unknown_fields(self):
        config_path = (
            self.root / "profiles" / "document-lifecycle-benchmark-config.yaml"
        )
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        forged = copy.deepcopy(config)
        forged["baseline_override"] = "HEAD"

        with self.assertRaisesRegex(DocumentLifecycleBenchmarkError, "config fields"):
            load_document_lifecycle_benchmark_config(self.root, document=forged)

    def test_benchmark_config_rejects_a_changed_p95_gate(self):
        config_path = (
            self.root / "profiles" / "document-lifecycle-benchmark-config.yaml"
        )
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        forged = copy.deepcopy(config)
        forged["validator_p95_limit_ms"] = 1_000

        with self.assertRaisesRegex(
            DocumentLifecycleBenchmarkError, "p95 limit must be exactly 100"
        ):
            load_document_lifecycle_benchmark_config(self.root, document=forged)

    def test_benchmark_config_baseline_must_be_a_reachable_commit(self):
        config_path = (
            self.root / "profiles" / "document-lifecycle-benchmark-config.yaml"
        )
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        forged = copy.deepcopy(config)
        forged["baseline_git_ref"] = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        with self.assertRaisesRegex(
            DocumentLifecycleBenchmarkError, "reachable commit"
        ):
            load_document_lifecycle_benchmark_config(self.root, document=forged)

    def test_benchmark_runner_does_not_allow_baseline_or_sample_overrides(self):
        script = self.root / "tools" / "run_document_lifecycle_benchmark.py"
        for arguments in (
            ["--baseline-ref", self.baseline_ref],
            ["--samples", "41"],
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(script), *arguments],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    check=False,
                )

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unrecognized arguments", result.stderr)

    def test_receipt_validator_recomputes_static_metrics_and_provenance(self):
        receipt = self._benchmark()
        validate_document_lifecycle_benchmark_receipt(
            self.root,
            self.manifest,
            receipt,
            baseline_ref=self.baseline_ref,
            baseline_status=self.baseline_status,
            baseline_master=self.baseline_master,
        )

        for mutation in (
            lambda value: value["metrics"].__setitem__("current_status_utf8_bytes", 1),
            lambda value: value["provenance"].__setitem__(
                "implementation_sha256", "0" * 64
            ),
            lambda value: value["provenance"].__setitem__("baseline_git_ref", "0" * 40),
            lambda value: value["metrics"].__setitem__("recovery_fields_recovered", 4),
        ):
            with self.subTest(mutation=mutation):
                forged = copy.deepcopy(receipt)
                mutation(forged)
                with self.assertRaises(DocumentLifecycleBenchmarkError):
                    validate_document_lifecycle_benchmark_receipt(
                        self.root,
                        self.manifest,
                        forged,
                        baseline_ref=self.baseline_ref,
                        baseline_status=self.baseline_status,
                        baseline_master=self.baseline_master,
                    )

    def test_receipt_validator_enforces_the_p95_latency_gate(self):
        receipt = self._benchmark()

        with (
            mock.patch(
                "context_control_plane.document_lifecycle_benchmark."
                "measure_document_lifecycle_validator",
                return_value={
                    "validator_samples": 40,
                    "validator_failures": 0,
                    "validator_p50_ms": 100.0,
                    "validator_p95_ms": 100.0,
                    "validator_max_ms": 100.0,
                },
            ),
            self.assertRaisesRegex(
                DocumentLifecycleBenchmarkError, "p95 must be less than 100 ms"
            ),
        ):
            validate_document_lifecycle_benchmark_receipt(
                self.root,
                self.manifest,
                receipt,
                baseline_ref=self.baseline_ref,
                baseline_status=self.baseline_status,
                baseline_master=self.baseline_master,
            )

    def test_runner_reuses_the_gated_live_measurement_for_output(self):
        receipt = self._benchmark()
        live = {
            "validator_samples": 40,
            "validator_failures": 0,
            "validator_p50_ms": 10.0,
            "validator_p95_ms": 20.0,
            "validator_max_ms": 25.0,
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.yaml"
            with (
                mock.patch.object(
                    sys,
                    "argv",
                    [
                        "run_document_lifecycle_benchmark.py",
                        "--root",
                        str(self.root),
                        "--output",
                        str(output),
                    ],
                ),
                mock.patch.object(
                    run_document_lifecycle_benchmark,
                    "load_document_lifecycle_benchmark_config",
                    return_value={
                        "baseline_git_ref": self.baseline_ref,
                        "validator_samples": 40,
                    },
                ),
                mock.patch.object(
                    run_document_lifecycle_benchmark,
                    "load_document_lifecycle_benchmark_baseline",
                    return_value=(self.baseline_status, self.baseline_master),
                ),
                mock.patch.object(
                    run_document_lifecycle_benchmark,
                    "benchmark_document_lifecycle",
                    return_value=receipt,
                ),
                mock.patch.object(
                    run_document_lifecycle_benchmark,
                    "validate_document_lifecycle_benchmark_receipt",
                    return_value=live,
                ),
                mock.patch.object(
                    run_document_lifecycle_benchmark,
                    "measure_document_lifecycle_validator",
                    create=True,
                ) as duplicate_measure,
            ):
                self.assertEqual(run_document_lifecycle_benchmark.main(), 0)

        duplicate_measure.assert_not_called()

    def test_receipt_validator_rejects_historical_timing_claims(self):
        receipt = self._benchmark()
        receipt["metrics"].update(
            {
                "validator_latency_samples_ms": [1.0] * 40,
                "validator_p50_ms": 1.0,
                "validator_p95_ms": 1.0,
                "validator_max_ms": 1.0,
            }
        )

        with self.assertRaisesRegex(DocumentLifecycleBenchmarkError, "metrics fields"):
            validate_document_lifecycle_benchmark_receipt(
                self.root,
                self.manifest,
                receipt,
                baseline_ref=self.baseline_ref,
                baseline_status=self.baseline_status,
                baseline_master=self.baseline_master,
            )

    def test_receipt_runtime_rejects_unknown_fields_and_invalid_timestamp(self):
        receipt = self._benchmark()
        mutations = (
            ("receipt fields", lambda value: value.__setitem__("override", True)),
            (
                "metrics fields",
                lambda value: value["metrics"].__setitem__("override", True),
            ),
            (
                "generated_at must be RFC3339",
                lambda value: value.__setitem__("generated_at", "not-a-timestamp"),
            ),
        )
        for message, mutation in mutations:
            with self.subTest(message=message):
                forged = copy.deepcopy(receipt)
                mutation(forged)
                with self.assertRaisesRegex(DocumentLifecycleBenchmarkError, message):
                    validate_document_lifecycle_benchmark_receipt(
                        self.root,
                        self.manifest,
                        forged,
                        baseline_ref=self.baseline_ref,
                        baseline_status=self.baseline_status,
                        baseline_master=self.baseline_master,
                    )

    def test_committed_receipt_is_strict_registered_and_current(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.document-lifecycle-benchmark"
        )
        schema_path = self.root / entry["artifact_path"]
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            entry["content_sha256"],
        )
        receipt = yaml.safe_load(
            (
                self.root
                / "experiments"
                / "state"
                / "m0-10-document-lifecycle-results.yaml"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(receipt)), [])
        metric_properties = schema["properties"]["metrics"]["properties"]
        self.assertNotIn("validator_latency_samples_ms", metric_properties)
        self.assertNotIn("validator_p95_ms", metric_properties)
        validate_document_lifecycle_benchmark_receipt(
            self.root,
            self.manifest,
            receipt,
            baseline_ref=self.baseline_ref,
            baseline_status=self.baseline_status,
            baseline_master=self.baseline_master,
        )

    def test_acceptance_records_the_final_independent_review(self):
        acceptance = (
            self.root
            / "docs"
            / "migrations"
            / "m0-10-document-lifecycle-acceptance-2026-08-12.md"
        ).read_text(encoding="utf-8")

        self.assertIn("状态：verified", acceptance)
        self.assertIn("High 0", acceptance)
        self.assertIn("Medium 0", acceptance)

    def test_benchmark_config_is_strict_registered_and_current(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.document-lifecycle-benchmark-config"
        )
        schema_path = self.root / entry["artifact_path"]
        schema = yaml.safe_load(schema_path.read_text(encoding="utf-8"))
        config = load_document_lifecycle_benchmark_config(self.root)

        self.assertEqual(
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            entry["content_sha256"],
        )
        self.assertEqual(list(Draft202012Validator(schema).iter_errors(config)), [])
        self.assertEqual(config["baseline_git_ref"], self.baseline_ref)
        self.assertEqual(config["validator_samples"], 40)
        self.assertEqual(config["validator_p95_limit_ms"], 100)


if __name__ == "__main__":
    unittest.main()
