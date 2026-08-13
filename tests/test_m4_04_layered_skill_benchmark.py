import copy
import importlib
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


class M404LayeredSkillBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        try:
            cls.api = importlib.import_module(
                "context_control_plane.layered_skill_benchmark"
            )
        except ModuleNotFoundError:
            cls.api = None

    def _require_api(self):
        self.assertIsNotNone(self.api, "layered Skill benchmark module is missing")
        return self.api

    def _receipt(self, samples=40):
        api = self._require_api()
        arguments = [
            "--samples",
            str(samples),
            "--observed-at",
            "2026-08-11T08:00:00+08:00",
            "--output",
            "experiments/state/m4-04-layered-skill-loading-results.yaml",
        ]
        return api.run_layered_skill_benchmark(
            self.root,
            samples=samples,
            observed_at="2026-08-11T08:00:00+08:00",
            arguments=arguments,
        )

    def test_benchmark_measures_deterministic_bounded_composition(self):
        api = self._require_api()
        receipt = self._receipt()

        api.validate_layered_skill_benchmark_receipt(receipt, root=self.root)
        workload = receipt["workload"]
        measurement = receipt["measurement"]
        acceptance = receipt["acceptance"]
        self.assertEqual(workload["layer_content_bytes"], {
            "S0": 1024,
            "S1": 4096,
            "S2": 4096,
            "S3": 16384,
        })
        self.assertEqual(measurement["available_skill_content_bytes"], 25600)
        self.assertEqual(measurement["validated_skill_content_bytes"], 25600)
        self.assertEqual(measurement["selected_skill_content_bytes"], 5120)
        self.assertEqual(measurement["omitted_from_composition_bytes"], 20480)
        self.assertEqual(measurement["content_reduction_basis_points"], 8000)
        self.assertEqual(measurement["baseline_repeated_selected_bytes"], 1024000)
        self.assertEqual(measurement["layered_repeated_selected_bytes"], 204800)
        self.assertEqual(measurement["repeated_selected_bytes_avoided"], 819200)
        self.assertEqual(measurement["unique_receipt_digests"], 1)
        self.assertEqual(len(measurement["receipt_digests"]), 40)
        self.assertEqual(
            set(measurement["receipt_digests"]),
            {measurement["canonical_receipt_sha256"]},
        )
        self.assertEqual(measurement["all_layers_control_loaded_bytes"], 25600)
        self.assertEqual(len(measurement["latency_samples_ms"]), 40)
        self.assertTrue(all(value > 0 for value in measurement["latency_samples_ms"]))
        self.assertLess(measurement["load_p95_ms"], 10.0)
        self.assertTrue(all(acceptance.values()))
        self.assertIn("plan_contract_test_sha256", receipt["provenance"])
        self.assertIn("registry_entries_sha256", receipt["provenance"])
        self.assertNotIn("registry_entry_sha256", receipt["provenance"])
        self.assertTrue(receipt["authority_boundary"]["synthetic_plan_authorizer"])
        self.assertTrue(receipt["authority_boundary"]["frozen_clock"])

    def test_validator_recomputes_latency_and_byte_metrics(self):
        api = self._require_api()
        receipt = self._receipt()

        for mutation in (
            lambda value: value["measurement"].__setitem__(
                "load_p95_ms", value["measurement"]["load_p95_ms"] + 1
            ),
            lambda value: value["measurement"].__setitem__(
                "selected_skill_content_bytes", 1
            ),
            lambda value: value["measurement"].__setitem__(
                "unique_receipt_digests", 2
            ),
            lambda value: value["measurement"].__setitem__(
                "canonical_receipt_sha256", "a" * 64
            ),
        ):
            with self.subTest(mutation=mutation):
                candidate = copy.deepcopy(receipt)
                mutation(candidate)
                with self.assertRaises(ValueError):
                    api.validate_layered_skill_benchmark_receipt(
                        candidate,
                        root=self.root,
                    )

    def test_validator_rejects_stale_or_non_finite_evidence(self):
        api = self._require_api()
        receipt = self._receipt()

        stale = copy.deepcopy(receipt)
        stale["provenance"]["implementation_sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "provenance"):
            api.validate_layered_skill_benchmark_receipt(stale, root=self.root)

        non_finite = copy.deepcopy(receipt)
        non_finite["measurement"]["latency_samples_ms"][0] = math.inf
        with self.assertRaisesRegex(ValueError, "latency"):
            api.validate_layered_skill_benchmark_receipt(
                non_finite,
                root=self.root,
            )

    def test_acceptance_requires_exactly_forty_samples(self):
        api = self._require_api()
        receipt = self._receipt(samples=1)

        with self.assertRaisesRegex(ValueError, "samples"):
            api.validate_layered_skill_benchmark_receipt(receipt, root=self.root)

    def test_validator_rejects_boolean_numeric_metrics(self):
        api = self._require_api()
        receipt = self._receipt()

        for field in (
            "warmup_samples",
            "unique_receipt_digests",
            "load_p50_ms",
            "load_p95_ms",
            "load_max_ms",
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(receipt)
                candidate["measurement"][field] = True
                with self.assertRaises(ValueError):
                    api.validate_layered_skill_benchmark_receipt(
                        candidate,
                        root=self.root,
                    )

    def test_validator_rejects_malformed_environment_identity(self):
        api = self._require_api()
        receipt = self._receipt()

        for field, invalid in (
            ("python_version", None),
            ("python_implementation", []),
            ("platform", False),
            ("machine", {}),
        ):
            with self.subTest(field=field):
                candidate = copy.deepcopy(receipt)
                candidate["environment"][field] = invalid
                with self.assertRaisesRegex(ValueError, "environment"):
                    api.validate_layered_skill_benchmark_receipt(
                        candidate,
                        root=self.root,
                    )

    def test_runner_resolves_default_arguments_and_writes_valid_receipt(self):
        api = self._require_api()
        with tempfile.TemporaryDirectory(prefix="m4-04-benchmark-test-") as directory:
            output = Path(directory) / "result.yaml"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(self.root / "tools" / "run_layered_skill_benchmark.py"),
                    "--output",
                    str(output),
                ],
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = yaml.safe_load(output.read_text(encoding="utf-8"))
            api.validate_layered_skill_benchmark_receipt(receipt, root=self.root)
            self.assertEqual(receipt["measurement"]["samples"], 40)

    def test_versioned_receipt_has_current_provenance(self):
        api = self._require_api()
        receipt_path = (
            self.root
            / "experiments"
            / "state"
            / "m4-04-layered-skill-loading-results.yaml"
        )
        receipt = yaml.safe_load(receipt_path.read_text(encoding="utf-8"))

        api.validate_layered_skill_benchmark_receipt(receipt, root=self.root)

    def test_source_change_during_measurement_is_rejected(self):
        api = self._require_api()
        expected = api._provenance(self.root)
        changed = dict(expected)
        changed["implementation_sha256"] = "a" * 64
        arguments = [
            "--samples",
            "40",
            "--observed-at",
            "2026-08-11T08:00:00+08:00",
            "--output",
            "experiments/state/m4-04-layered-skill-loading-results.yaml",
        ]

        with patch.object(api, "_provenance", side_effect=[expected, changed]):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                api.run_layered_skill_benchmark(
                    self.root,
                    samples=40,
                    observed_at="2026-08-11T08:00:00+08:00",
                    arguments=arguments,
                )


if __name__ == "__main__":
    unittest.main()
