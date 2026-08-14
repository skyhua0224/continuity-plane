"""M5-06 Idea return packet quantitative acceptance."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.idea_return_packet_benchmark import (
    benchmark_idea_return_packet,
    benchmark_idea_return_packet_fixture,
    validate_idea_return_packet_benchmark,
)
from context_control_plane.idea_return_packet import (
    build_return_context_migration_receipt,
)


class M506IdeaReturnPacketBenchmarkTests(unittest.TestCase):
    def test_committed_receipt_and_strict_schemas_are_current(self):
        root = Path(__file__).parents[1]
        receipt = json.loads(
            (root / "experiments/routing/m5-06-idea-return-packet-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_idea_return_packet_benchmark(receipt, root=root)
        fixture = benchmark_idea_return_packet_fixture()
        migration = build_return_context_migration_receipt(
            migration_id="migration-schema-canary",
            project_id=fixture["snapshot"]["project"]["project_id"],
            return_work_id="work-active",
            from_project_revision=9,
            to_project_revision=11,
            from_canonical_plan_sha256="1" * 64,
            to_canonical_plan_sha256="3" * 64,
            from_registry_digest="2" * 64,
            to_registry_digest="4" * 64,
            authority_event_ref="event://state-mcp/schema-canary",
            authorization_ref="authorization://schema-canary",
        )
        instances = {
            "schemas/m5-06/idea-return-packet.schema.json": fixture["packet"],
            "schemas/m5-06/idea-return-checkpoint-binding.schema.json": fixture[
                "checkpoint_binding"
            ],
            "schemas/m5-06/return-context-migration.schema.json": migration,
            "schemas/m5-06/idea-return-packet-benchmark.schema.json": receipt,
        }
        for relative, instance in instances.items():
            with self.subTest(schema=relative):
                schema = json.loads((root / relative).read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(instance)

    def test_benchmark_recovers_return_point_without_copying_idea_body(self):
        root = Path(__file__).parents[1]
        receipt = benchmark_idea_return_packet(root=root, samples=100)
        validate_idea_return_packet_benchmark(receipt, root=root)
        self.assertEqual(receipt["successful_samples"], 100)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["original_task_recovery_mismatch"], 0)
        self.assertEqual(receipt["return_point_recovery_mismatch"], 0)
        self.assertEqual(receipt["idea_ref_recovery_mismatch"], 0)
        self.assertEqual(receipt["idea_body_copy_violations"], 0)
        self.assertEqual(receipt["candidate_authority_violations"], 0)
        self.assertEqual(receipt["fault_rejections"], receipt["fault_samples"])
        self.assertEqual(receipt["task_recovery_millionths"], 1_000_000)
        self.assertEqual(receipt["return_point_recovery_millionths"], 1_000_000)
        self.assertEqual(receipt["external_services"], 0)

    def test_fixture_keeps_historical_and_current_authority_distinct(self):
        fixture = benchmark_idea_return_packet_fixture()
        self.assertLess(
            fixture["checkpoint_binding"]["project_revision"],
            fixture["snapshot"]["project"]["revision"],
        )
        self.assertLess(
            fixture["checkpoint_binding"]["return_work_revision"],
            fixture["packet"]["active_leaf"]["revision"],
        )
        self.assertEqual(
            set(fixture["packet"]["idea_refs"][0]),
            {"idea_id", "status", "return_work_id", "authority"},
        )

    def test_benchmark_validator_rejects_false_success_and_stale_provenance(self):
        root = Path(__file__).parents[1]
        receipt = benchmark_idea_return_packet(root=root, samples=8)
        for field, value in (
            ("original_task_recovery_mismatch", 1),
            ("return_point_recovery_mismatch", 1),
            ("idea_body_copy_violations", 1),
            ("candidate_authority_violations", 1),
            ("fault_rejections", receipt["fault_rejections"] - 1),
            ("external_services", 1),
        ):
            changed = copy.deepcopy(receipt)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_idea_return_packet_benchmark(changed, root=root)
        stale = copy.deepcopy(receipt)
        stale["implementation_sha256"] = "a" * 64
        with self.assertRaises(ValueError):
            validate_idea_return_packet_benchmark(stale, root=root)


if __name__ == "__main__":
    unittest.main()
