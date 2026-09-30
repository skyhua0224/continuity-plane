"""M5-01 Execution Packet replay and capacity benchmark acceptance."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.execution_packet_benchmark import (
    benchmark_execution_packet,
    validate_execution_packet_benchmark_receipt,
)


class M501ExecutionPacketBenchmarkTests(unittest.TestCase):
    def test_benchmark_is_deterministic_and_within_packet_capacity(self):
        root = Path(__file__).parents[1]
        receipt = benchmark_execution_packet(
            root=root,
            samples=32,
            generated_at="2026-08-14T20:30:00Z",
        )
        self.assertEqual(receipt["successful_samples"], 32)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["canary_failures"], 0)
        self.assertEqual(receipt["state_write_authority_true"], 0)
        self.assertEqual(receipt["capacity_in_bound"], 1)
        self.assertGreaterEqual(receipt["min_packet_bytes"], 4096)
        self.assertLessEqual(receipt["max_packet_bytes"], 12 * 1024)
        validate_execution_packet_benchmark_receipt(receipt, root=root)

    def test_committed_receipt_schema_registry_and_provenance_are_current(self):
        root = Path(__file__).parents[1]
        receipt = json.loads(
            (root / "experiments/routing/m5-01-execution-packet-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_execution_packet_benchmark_receipt(receipt, root=root)
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        for schema_id, path_value in (
            ("context.execution-packet", "schemas/m5-01/execution-packet.schema.json"),
            (
                "context.execution-packet-benchmark",
                "schemas/m5-01/execution-packet-benchmark.schema.json",
            ),
        ):
            entry = next(item for item in registry["schemas"] if item["schema_id"] == schema_id)
            path = root / path_value
            self.assertEqual(entry["artifact_path"], path_value)
            self.assertEqual(entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))
        benchmark_schema = json.loads(
            (root / "schemas/m5-01/execution-packet-benchmark.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(benchmark_schema).validate(receipt)

    def test_validator_rejects_replay_or_capacity_regressions(self):
        root = Path(__file__).parents[1]
        receipt = benchmark_execution_packet(root=root, samples=4)
        for field, value in (
            ("replay_mismatch", 1),
            ("canary_failures", 1),
            ("state_write_authority_true", 1),
            ("capacity_in_bound", 0),
            ("max_packet_bytes", 12 * 1024 + 1),
        ):
            changed = dict(receipt)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_execution_packet_benchmark_receipt(changed, root=root)


if __name__ == "__main__":
    unittest.main()
