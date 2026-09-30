"""M9-04 Context Health measured acceptance tests."""

from __future__ import annotations

import copy
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_control_plane.context_health_benchmark import (
    ContextHealthBenchmarkError,
    _EffectProbe,
    benchmark_context_health_projection,
    validate_context_health_benchmark,
)
from context_control_plane.provider_skill_adapter import ProviderSkillAdapter
from tools import run_context_health_benchmark


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()


def _resign(receipt: dict[str, object]) -> None:
    receipt["receipt_sha256"] = hashlib.sha256(
        _canonical(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
    ).hexdigest()


class M904ContextHealthBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_small_benchmark_measures_all_veto_gates(self) -> None:
        receipt = benchmark_context_health_projection(
            root=self.root,
            iterations=3,
            generated_at="2026-08-17T23:00:00+08:00",
        )

        validate_context_health_benchmark(receipt, root=self.root)

        for field in (
            "same_revision_matches",
            "source_binding_matches",
            "context_binding_matches",
            "stale_assertion_visibility_matches",
            "harness_failure_visibility_matches",
            "replay_failure_visibility_matches",
            "unavailable_measurement_honesty_matches",
            "tamper_rejections",
            "expired_claim_rejections",
            "reference_conflict_rejections",
            "terminal_digest_rejections",
            "partial_accounting_honesty_matches",
            "read_budget_classification_matches",
            "acknowledged_input_replay_classification_matches",
            "metric_evidence_rejections",
            "canary_tamper_rejections",
            "effect_watermark_rejections",
        ):
            self.assertEqual(receipt["results"][field], 3)
        self.assertTrue(receipt["results"]["capacity_rejected"])
        self.assertEqual(receipt["results"]["capacity_cases_total"], 11)
        self.assertEqual(
            receipt["results"]["capacity_cases_rejected"],
            receipt["results"]["capacity_cases_total"],
        )
        self.assertEqual(receipt["parameters"]["scale_trace_events"], 3500)
        self.assertEqual(receipt["parameters"]["scale_iterations"], 3)
        self.assertEqual(receipt["results"]["false_healthy_claims"], 0)
        self.assertEqual(receipt["results"]["false_current_assertions"], 0)
        self.assertEqual(receipt["results"]["authority_violations"], 0)
        self.assertEqual(receipt["results"]["provider_invocations"], 0)
        self.assertEqual(receipt["results"]["external_services"], 0)
        self.assertEqual(
            set(receipt["provenance"]),
            {
                "implementation_sha256",
                "benchmark_sha256",
                "fixture_sha256",
                "schema_sha256",
                "metric_evidence_implementation_sha256",
                "metric_evidence_schema_sha256",
                "projection_sha256",
            },
        )
        self.assertEqual(receipt["gate"], {"status": "passed", "failed_gates": []})
        self.assertEqual(len(receipt["latency_samples_ms"]), 3)
        self.assertEqual(len(receipt["scale_latency_samples_ms"]), 3)
        self.assertLess(receipt["scale_latency_ms"]["p95"], 500.0)
        self.assertEqual(
            receipt["attestation"],
            {
                "mode": "local-unattested",
                "measurement_authenticity": False,
                "zero_count_source": "isolated-effect-probe",
                "provider_probe_targets": [
                    "provider-skill-adapter",
                    "recall-provider",
                    "reviewer-adapter",
                ],
                "external_probe_targets": [
                    "socket.connect",
                    "socket.create_connection",
                    "subprocess.Popen",
                ],
            },
        )

    def test_effect_probe_counts_provider_calls_and_blocks_network(self) -> None:
        probe = _EffectProbe()
        adapter = ProviderSkillAdapter("codex", "codex-skill-directory/v1")
        with probe:
            with self.assertRaises(TypeError):
                adapter.compose()  # type: ignore[call-arg]
            with self.assertRaises(ContextHealthBenchmarkError):
                socket.create_connection(("example.invalid", 443))

        self.assertEqual(probe.provider_invocations, 1)
        self.assertEqual(probe.external_services, 1)

    def test_validator_rejects_forged_results_latency_and_provenance(self) -> None:
        receipt = benchmark_context_health_projection(
            root=self.root,
            iterations=2,
            generated_at="2026-08-17T23:00:00+08:00",
        )
        mutations = [
            lambda item: item["results"].__setitem__("same_revision_matches", 1),
            lambda item: item["results"].__setitem__("false_healthy_claims", 1),
            lambda item: item["results"].__setitem__("capacity_rejected", False),
            lambda item: item["results"].__setitem__("capacity_cases_rejected", 10),
            lambda item: item["latency_ms"].__setitem__("p95", -1.0),
            lambda item: item["latency_ms"].__setitem__("max", float("inf")),
            lambda item: item["provenance"].__setitem__(
                "implementation_sha256", "0" * 64
            ),
            lambda item: item.__setitem__(
                "generated_at", "2026-99-99T23:00:00+08:00"
            ),
        ]
        for mutate in mutations:
            forged = copy.deepcopy(receipt)
            mutate(forged)
            _resign(forged)
            with self.assertRaises(ContextHealthBenchmarkError):
                validate_context_health_benchmark(forged, root=self.root)

    def test_validator_rejects_consistently_derived_failed_gate(self) -> None:
        receipt = benchmark_context_health_projection(
            root=self.root,
            iterations=2,
            generated_at="2026-08-17T23:00:00+08:00",
        )
        receipt["results"]["same_revision_matches"] = 1
        receipt["results"]["same_revision_rate"] = 0.5
        receipt["gate"] = {"status": "failed", "failed_gates": ["same-revision"]}
        _resign(receipt)

        with self.assertRaises(ContextHealthBenchmarkError):
            validate_context_health_benchmark(receipt, root=self.root)

    def test_committed_receipt_is_source_bound_and_valid(self) -> None:
        path = self.root / "experiments/evidence/m9-04-context-health-results.json"
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.skipTest("M9-04 measured receipt has not been generated")
        validate_context_health_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["parameters"]["iterations"], 1000)
        self.assertEqual(receipt["gate"]["status"], "passed")

    def test_invalid_iteration_count_is_rejected(self) -> None:
        for value in (True, 0, 1001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                benchmark_context_health_projection(
                    root=self.root,
                    iterations=value,
                    generated_at="2026-08-17T23:00:00+08:00",
                )

    def test_runner_writes_valid_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = run_context_health_benchmark.main(
                [
                    "--root",
                    str(self.root),
                    "--iterations",
                    "2",
                    "--generated-at",
                    "2026-08-17T23:00:00+08:00",
                    "--output",
                    str(output),
                ]
            )
            receipt = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(result, 0)
        validate_context_health_benchmark(receipt, root=self.root)

    def test_runner_executes_from_its_file_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.root / "tools/run_context_health_benchmark.py"),
                    "--root",
                    str(self.root),
                    "--iterations",
                    "1",
                    "--generated-at",
                    "2026-08-17T23:00:00+08:00",
                    "--output",
                    str(output),
                ],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
