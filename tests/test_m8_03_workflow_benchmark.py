"""M8-03 workflow chain replay and rollover benchmark."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from context_control_plane.workflow_orchestration_benchmark import (
    WorkflowOrchestrationBenchmarkError,
    _chain_link_failure_count,
    _nondeterminism_failure_count,
    benchmark_workflow_orchestration,
    validate_workflow_orchestration_benchmark,
)


class M803WorkflowBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_three_generation_replay_and_fault_matrix_is_lossless(self) -> None:
        receipt = benchmark_workflow_orchestration(
            root=self.root,
            samples=40,
            generated_at="2026-08-16T20:00:00+08:00",
        )
        validate_workflow_orchestration_benchmark(receipt, root=self.root)

        self.assertEqual(receipt["successful_chains"], 40)
        self.assertEqual(receipt["run_count"], 120)
        self.assertEqual(receipt["rollover_count"], 80)
        self.assertEqual(receipt["fault_attempts"], 280)
        self.assertEqual(receipt["fault_rejections"], 280)
        self.assertEqual(receipt["metrics"]["replay_mismatches"], 0)
        self.assertEqual(receipt["metrics"]["lost_inputs"], 0)
        self.assertEqual(receipt["metrics"]["duplicate_effects"], 0)
        self.assertEqual(receipt["metrics"]["authority_violations"], 0)
        self.assertEqual(receipt["provider_invocations"], 0)
        self.assertEqual(receipt["external_services"], 0)
        self.assertFalse(receipt["shared_authority_claim"])
        self.assertEqual(
            receipt["temporal_sdk_reference"]["worker_replayer"], "unavailable"
        )
        self.assertGreater(receipt["latency_ms"]["p95"], 0)
        self.assertLess(receipt["latency_ms"]["p95"], 50)

    def test_chain_and_determinism_metrics_are_computed(self) -> None:
        receipt = benchmark_workflow_orchestration(
            root=self.root,
            samples=1,
            generated_at="2026-08-16T20:00:00+08:00",
        )
        self.assertEqual(receipt["metrics"]["chain_link_failures"], 0)
        self.assertEqual(receipt["metrics"]["nondeterminism_failures"], 0)
        first = {"receipt_sha256": "a" * 64}
        self.assertEqual(_nondeterminism_failure_count(first, copy.deepcopy(first)), 0)
        self.assertEqual(
            _nondeterminism_failure_count(first, {"receipt_sha256": "b" * 64}), 1
        )
        broken_chain = [
            {
                "generation": 1,
                "run_id": "run-1",
                "previous_run_id": None,
                "previous_run_event_sha256": None,
                "history": [{"event_sha256": "a" * 64}],
            },
            {
                "generation": 2,
                "run_id": "run-2",
                "previous_run_id": "wrong",
                "previous_run_event_sha256": "b" * 64,
                "history": [{"event_sha256": "c" * 64}],
            },
        ]
        self.assertEqual(_chain_link_failure_count(broken_chain), 2)

    def test_receipt_rejects_forged_success_and_stale_provenance(self) -> None:
        receipt = benchmark_workflow_orchestration(
            root=self.root,
            samples=2,
            generated_at="2026-08-16T20:00:00+08:00",
        )
        forged = copy.deepcopy(receipt)
        forged["fault_rejections"] -= 1
        with self.assertRaises(WorkflowOrchestrationBenchmarkError):
            validate_workflow_orchestration_benchmark(forged, root=self.root)

        forged = copy.deepcopy(receipt)
        forged["provenance"]["durable_workflow_sha256"] = "0" * 64
        body = copy.deepcopy(forged)
        body.pop("receipt_sha256")
        forged["receipt_sha256"] = (
            __import__("hashlib")
            .sha256(
                json.dumps(
                    body,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            .hexdigest()
        )
        with self.assertRaisesRegex(WorkflowOrchestrationBenchmarkError, "provenance"):
            validate_workflow_orchestration_benchmark(forged, root=self.root)

    def test_registry_provenance_is_bounded_to_m8_03_contracts(self) -> None:
        receipt = benchmark_workflow_orchestration(
            root=self.root,
            samples=1,
            generated_at="2026-08-16T20:00:00+08:00",
        )
        provenance_paths = (
            "context_control_plane/durable_workflow.py",
            "context_control_plane/workflow_orchestration.py",
            "context_control_plane/temporal_workflow_adapter.py",
            "context_control_plane/workflow_orchestration_benchmark.py",
            "schemas/registry.yaml",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in provenance_paths:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.root / relative, destination)

            registry_path = root / "schemas/registry.yaml"
            registry = registry_path.read_text(encoding="utf-8")
            registry_path.write_text(
                registry.replace("status: current", "status: deprecated", 1),
                encoding="utf-8",
            )
            validate_workflow_orchestration_benchmark(receipt, root=root)

            registry = registry_path.read_text(encoding="utf-8")
            registry_path.write_text(
                registry.replace(
                    "10944d6ceaaa85e5cef36c62c5430e6ec1373e10f89794b0aaedf4e43e68d929",
                    "0" * 64,
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                WorkflowOrchestrationBenchmarkError, "provenance"
            ):
                validate_workflow_orchestration_benchmark(receipt, root=root)

    def test_published_receipt_matches_current_implementation(self) -> None:
        path = self.root / "experiments/evidence/m8-03-workflow-results.json"
        receipt = json.loads(path.read_text(encoding="utf-8"))
        validate_workflow_orchestration_benchmark(receipt, root=self.root)
        self.assertEqual(receipt["samples"], 1000)
        self.assertEqual(receipt["successful_chains"], 1000)

    def test_cli_atomically_writes_a_current_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "receipt.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(self.root / "tools/run_workflow_orchestration_benchmark.py"),
                    "--samples",
                    "2",
                    "--generated-at",
                    "2026-08-16T20:00:00+08:00",
                    "--output",
                    str(output),
                ],
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(output.read_text(encoding="utf-8"))
            validate_workflow_orchestration_benchmark(receipt, root=self.root)
            self.assertFalse(list(output.parent.glob(f".{output.name}.*")))


if __name__ == "__main__":
    unittest.main()
