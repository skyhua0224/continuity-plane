"""M5-08 local durable continuation replay benchmark."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.durable_continuation import evaluate_durable_recovery
from context_control_plane.durable_continuation_benchmark import (
    benchmark_durable_continuation,
    durable_continuation_fixture,
    validate_durable_continuation_benchmark,
)


class M508DurableContinuationBenchmarkTests(unittest.TestCase):
    def test_committed_receipt_and_strict_schemas_are_current(self):
        root = Path(__file__).parents[1]
        fixture = durable_continuation_fixture()
        recovery = evaluate_durable_recovery(
            trusted_state=copy.deepcopy(fixture["state"]),
            restored_state=copy.deepcopy(fixture["state"]),
            trusted_authority=copy.deepcopy(fixture["authority"]),
            proposed_first_action=fixture["proposed_first_action"],
            response_input_id=None,
            requested_effect_id=fixture["requested_effect_id"],
            replay_requested=False,
            recovery_reads=copy.deepcopy(fixture["recovery_reads"]),
            recovery_budget_bytes=fixture["recovery_budget_bytes"],
            observed_at="2026-08-15T04:00:00+08:00",
        )
        receipt = json.loads(
            (root / "experiments/routing/m5-08-durable-continuation-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_durable_continuation_benchmark(receipt)
        documents = (
            ("schemas/m5-08/durable-continuation.schema.json", fixture["state"]),
            ("schemas/m5-08/durable-continuation-recovery.schema.json", recovery),
            ("schemas/m5-08/durable-continuation-benchmark.schema.json", receipt),
        )
        for relative, instance in documents:
            with self.subTest(schema=relative):
                schema = json.loads((root / relative).read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(schema).validate(instance)
                changed = copy.deepcopy(instance)
                changed["unexpected"] = True
                self.assertFalse(Draft202012Validator(schema).is_valid(changed))
        self.assertEqual(
            receipt["implementation_sha256"],
            hashlib.sha256(
                (root / "context_control_plane/durable_continuation.py").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            receipt["benchmark_sha256"],
            hashlib.sha256(
                (root / "context_control_plane/durable_continuation_benchmark.py").read_bytes()
            ).hexdigest(),
        )
        self.assertEqual(
            receipt["fixture_sha256"],
            hashlib.sha256(
                json.dumps(
                    fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        )

    def test_fixture_covers_pi_oracle_and_provider_neutral_authority(self):
        fixture = durable_continuation_fixture()
        state = fixture["state"]
        self.assertEqual(state["phase"], "effect-in-flight")
        self.assertEqual(state["reserved_effects"][0]["replay_policy"], "never")
        self.assertEqual(state["response_mode"], "continue-silently")
        self.assertFalse(state["state_write_authority"])
        self.assertFalse(state["provider_native_authority"])
        self.assertLessEqual(fixture["recovery_bytes"], fixture["recovery_budget_bytes"])

    def test_replay_and_fault_benchmark_meets_completion_gate(self):
        receipt = benchmark_durable_continuation(samples=32)
        validate_durable_continuation_benchmark(receipt)
        self.assertEqual(receipt["successful_samples"], 32)
        self.assertEqual(receipt["replay_mismatch"], 0)
        self.assertEqual(receipt["first_action_mismatches"], 0)
        self.assertEqual(receipt["acknowledged_input_replays"], 0)
        self.assertEqual(receipt["continuation_field_mismatches"], 0)
        self.assertEqual(receipt["continuation_fields_recovered"], 320)
        self.assertEqual(receipt["continuation_fields_total"], 320)
        self.assertEqual(receipt["fault_rejections"], receipt["fault_samples"])
        self.assertTrue(all(value == 0 for value in receipt["faults"].values()))
        self.assertLessEqual(receipt["max_recovery_bytes"], receipt["recovery_budget_bytes"])
        self.assertEqual(receipt["authority_violations"], 0)
        self.assertEqual(receipt["external_services"], 0)

    def test_benchmark_validator_rejects_false_success(self):
        receipt = benchmark_durable_continuation(samples=4)
        for field, value in (
            ("replay_mismatch", 1),
            ("first_action_mismatches", 1),
            ("acknowledged_input_replays", 1),
            ("continuation_field_mismatches", 1),
            ("authority_violations", 1),
            ("external_services", 1),
        ):
            changed = copy.deepcopy(receipt)
            changed[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_durable_continuation_benchmark(changed)


if __name__ == "__main__":
    unittest.main()
