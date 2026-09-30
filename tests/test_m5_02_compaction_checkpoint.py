"""M5-02 material-event PreCompact delta and provider shadow adapters."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.compaction_checkpoint import (
    CompactionCheckpointError,
    DeepSeekCompactionHookAdapter,
    PiCompactionHookAdapter,
    build_material_event_delta,
    canonical_material_event_delta_bytes,
    publish_material_event_delta,
    validate_material_event_delta,
)
from context_control_plane.execution_packet_benchmark import benchmark_fixture


class M502CompactionCheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        fixture = benchmark_fixture(cls.root)
        cls.snapshot = copy.deepcopy(fixture["snapshot"])
        cls.packet = fixture["packet"]

    def _event(self, sequence_no: int, revision_before: int, revision_after: int, event_id: str):
        event = {
            "schema_version": "context.state-event/v4alpha1",
            "event_id": event_id,
            "event_type": "state-transition",
            "project_id": self.snapshot["project"]["project_id"],
            "sequence_no": sequence_no,
            "revision_before": revision_before,
            "revision_after": revision_after,
            "occurred_at": "2026-08-14T20:00:00+08:00",
            "actor_ref": "actor://executor",
            "causation_ref": "run:m5-02",
            "correlation_ref": "task:m5-02",
            "previous_event_sha256": None if sequence_no == 1 else "a" * 64,
            "supersedes_event_id": None,
            "changes": [],
            "project_after": copy.deepcopy(self.snapshot["project"]),
            "task_transition": None,
            "experiment_transition": None,
        }
        event["event_sha256"] = hashlib.sha256(
            json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return event

    def _delta(self):
        return build_material_event_delta(
            snapshot=self.snapshot,
            base_checkpoint_ref=None,
            base_revision=self.snapshot["project"]["revision"],
            base_event_head=None,
            events=[self._event(1, 7, 7, "event-m5-02")],
            execution_packet=self.packet,
            observed_at="2026-08-14T20:00:00+08:00",
        )

    def test_delta_is_strict_registered_and_preserves_authority_fields(self):
        delta = self._delta()
        validate_material_event_delta(delta)
        schema = json.loads(
            (self.root / "schemas/m5-02/material-event-delta.schema.json").read_text(
                encoding="utf-8"
            )
        )
        Draft202012Validator(schema).validate(delta)
        self.assertEqual(delta["project_revision"], 7)
        self.assertEqual(delta["task_revision"], self.packet["active_leaf"]["revision"])
        self.assertEqual(delta["effect_high_watermark"], self.packet["continuation_cursor"]["reserved_effect_ids"].__len__())
        self.assertFalse(delta["state_write_authority"])
        self.assertFalse(delta["provider_native_authority"])

    def test_delta_replay_is_deterministic_and_publication_is_content_addressed(self):
        first = self._delta()
        second = self._delta()
        self.assertEqual(canonical_material_event_delta_bytes(first), canonical_material_event_delta_bytes(second))
        with tempfile.TemporaryDirectory() as directory:
            store = LocalArtifactStore(Path(directory) / "artifacts")
            store.initialize()
            first_ref = publish_material_event_delta(first, store)
            second_ref = publish_material_event_delta(second, store)
            self.assertEqual(first_ref, second_ref)

    def test_delta_rejects_stale_base_and_event_gap_or_duplicate(self):
        from context_control_plane.compaction_checkpoint import MaterialEventDeltaError

        with self.assertRaisesRegex(MaterialEventDeltaError, "base revision"):
            build_material_event_delta(
                snapshot=self.snapshot,
                base_checkpoint_ref=None,
                base_revision=6,
                base_event_head=None,
                events=[self._event(1, 7, 7, "event-m5-02")],
                execution_packet=self.packet,
                observed_at="2026-08-14T20:00:00+08:00",
            )
        invalid = self._delta()
        invalid["material_events"].append(copy.deepcopy(invalid["material_events"][0]))
        with self.assertRaisesRegex(CompactionCheckpointError, "unique"):
            validate_material_event_delta(invalid)

    def test_pi_and_deepseek_shadow_hooks_preserve_authority_without_provider_writes(self):
        delta = self._delta()
        hook_schema = json.loads(
            (self.root / "schemas/m5-02/provider-compaction-hook.schema.json").read_text(
                encoding="utf-8"
            )
        )
        for adapter, metadata in (
            (PiCompactionHookAdapter(), {"cut_point": 12, "split_turn": True, "usage_tokens": 2048}),
            (DeepSeekCompactionHookAdapter(), {"checkpoint_kind": "semantic", "checkpoint_sequence": 4}),
        ):
            receipt = adapter.capture(delta, metadata=metadata, observed_at="2026-08-14T20:00:00+08:00")
            self.assertEqual(receipt["task_revision"], delta["task_revision"])
            self.assertEqual(receipt["effect_high_watermark"], delta["effect_high_watermark"])
            self.assertFalse(receipt["provider_native_authority"])
            self.assertFalse(receipt["state_write_authority"])
            Draft202012Validator(hook_schema).validate(receipt)

    def test_provider_metadata_cannot_override_authority_or_unbounded_payload(self):
        delta = self._delta()
        adapter = PiCompactionHookAdapter()
        with self.assertRaisesRegex(CompactionCheckpointError, "metadata"):
            adapter.capture(
                delta,
                metadata={"cut_point": 12, "split_turn": True, "usage_tokens": 2048, "state_write_authority": True},
                observed_at="2026-08-14T20:00:00+08:00",
            )
        with self.assertRaisesRegex(CompactionCheckpointError, "metadata"):
            adapter.capture(
                delta,
                metadata={"cut_point": 12, "split_turn": True, "usage_tokens": 2048, "summary": "x" * 10000},
                observed_at="2026-08-14T20:00:00+08:00",
            )


if __name__ == "__main__":
    unittest.main()
