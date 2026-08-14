"""M5-02 PreCompact delta benchmark acceptance."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.compaction_checkpoint_benchmark import (
    benchmark_compaction_checkpoint,
    validate_compaction_checkpoint_benchmark_receipt,
)


class M502CompactionCheckpointBenchmarkTests(unittest.TestCase):
    def test_benchmark_is_replayable_and_under_precompact_budget(self):
        root = Path(__file__).parents[1]
        receipt = benchmark_compaction_checkpoint(
            root=root, samples=32, generated_at="2026-08-14T20:30:00Z"
        )
        validate_compaction_checkpoint_benchmark_receipt(receipt, root=root)
        self.assertEqual(receipt["successful_samples"], 32)
        self.assertEqual(receipt["delta_replay_mismatch"], 0)
        self.assertEqual(receipt["hook_replay_mismatch"], 0)
        self.assertEqual(receipt["watermark_mismatch"], 0)
        self.assertEqual(receipt["authority_violations"], 0)
        self.assertLess(receipt["p95_ms"], 500)

    def test_committed_receipt_schema_and_provenance_are_strict(self):
        root = Path(__file__).parents[1]
        receipt = json.loads(
            (root / "experiments/routing/m5-02-compaction-checkpoint-results.json").read_text(
                encoding="utf-8"
            )
        )
        validate_compaction_checkpoint_benchmark_receipt(receipt, root=root)
        for path in (
            root / "schemas/m5-02/material-event-delta.schema.json",
            root / "schemas/m5-02/provider-compaction-hook.schema.json",
        ):
            Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))

    def test_validator_rejects_false_veto_claims_and_stale_provenance(self):
        root = Path(__file__).parents[1]
        receipt = benchmark_compaction_checkpoint(root=root, samples=4)
        for field, value in (
            ("delta_replay_mismatch", 1),
            ("hook_replay_mismatch", 1),
            ("watermark_mismatch", 1),
            ("authority_violations", 1),
            ("precompact_p95_gate_passed", False),
        ):
            candidate = copy.deepcopy(receipt)
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_compaction_checkpoint_benchmark_receipt(candidate, root=root)
        stale = copy.deepcopy(receipt)
        stale["provenance"]["implementation_sha256"] = "a" * 64
        with self.assertRaises(ValueError):
            validate_compaction_checkpoint_benchmark_receipt(stale, root=root)


if __name__ == "__main__":
    unittest.main()
