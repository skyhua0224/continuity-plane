"""M5-07 automated dogfood observation emitter and coverage gates."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from context_control_plane.context_trace import LocalContextTraceEmitter
from context_control_plane.dogfood_emitter import (
    DogfoodCoverageError,
    DogfoodObservationEmitter,
    assess_dogfood_coverage,
    canonical_dogfood_observation_bytes,
    require_dogfood_coverage_gate,
    validate_dogfood_observation,
)


class M507DogfoodEmitterTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.trace = LocalContextTraceEmitter(
            binding={
                "project_id": "project-context-control-plane",
                "state_revision": 54,
                "active_work_id": "M5-07",
                "trace_id": "1" * 32,
                "span_id": "2" * 16,
                "run_id": "run-m5-07",
                "operation_id": "operation-m5-campaign",
                "correlation_id": "campaign-M5",
            },
            source={"kind": "local-emitter", "component": "context.dogfood"},
            output_path=Path(directory.name) / "trace.jsonl",
        )
        self.emitter = DogfoodObservationEmitter(self.trace)

    def _emit(self, *, event_kind: str, subject_id: str, **overrides):
        values = {
            "event_kind": event_kind,
            "subject_id": subject_id,
            "active_work_before": "M5-07",
            "active_work_after": "M5-07",
            "return_point_work_id": "M5-07",
            "route": None,
            "interrupted": False,
            "candidate_only": False,
            "acknowledged_input_replayed": False,
            "first_action_match": True,
            "target_revision": 54,
            "canary_sequence": None,
            "first_side_effect_sequence": None,
            "provider_metrics_status": "unavailable",
            "evidence_refs": [f"run://m5-07/{subject_id}"],
            "observed_at": "2026-08-15T05:00:00+08:00",
        }
        values.update(overrides)
        return self.emitter.emit(**values)

    def _complete_observations(self):
        return [
            self._emit(event_kind="input-routing", subject_id="ingress-1", route="continue"),
            self._emit(
                event_kind="input-routing", subject_id="idea-1", route="capture-and-continue",
                candidate_only=True,
            ),
            self._emit(
                event_kind="compaction", subject_id="compaction-1", canary_sequence=20,
                first_side_effect_sequence=21,
            ),
            self._emit(event_kind="skill-selection", subject_id="skill-selection-1"),
            self._emit(event_kind="plan-revision", subject_id="revision-54"),
            self._emit(event_kind="agent-dispatch", subject_id="dispatch-1"),
            self._emit(event_kind="agent-handoff", subject_id="dispatch-1"),
            self._emit(event_kind="delivery", subject_id="delivery-1"),
        ]

    def _expectations(self):
        return {
            "eligible_ingress_ids": ["ingress-1", "idea-1"],
            "visible_compaction_ids": ["compaction-1"],
            "skill_selection_ids": ["skill-selection-1"],
            "target_revisions": [54],
            "agent_dispatch_ids": ["dispatch-1"],
            "delivery_ids": ["delivery-1"],
        }

    def test_emitter_records_every_required_kind_through_context_trace(self):
        observations = self._complete_observations()
        for observation in observations:
            validate_dogfood_observation(observation)
            self.assertFalse(observation["state_write_authority"])
            self.assertFalse(observation["provider_native_authority"])
            self.assertTrue(observation["trace_event_name"].startswith("context.dogfood."))
        self.assertEqual(len(self.trace.events), len(observations))

    def test_candidate_idea_does_not_switch_active_work_or_gain_authority(self):
        observation = self._emit(
            event_kind="input-routing",
            subject_id="idea-2",
            route="capture-and-continue",
            candidate_only=True,
        )
        self.assertEqual(observation["active_work_before"], observation["active_work_after"])
        self.assertEqual(observation["return_point_work_id"], "M5-07")
        self.assertFalse(observation["state_write_authority"])

    def test_complete_denominators_and_pre_effect_canary_pass_at_full_coverage(self):
        receipt = assess_dogfood_coverage(
            self._complete_observations(), expectations=self._expectations()
        )
        require_dogfood_coverage_gate(receipt)
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual(receipt["overall_coverage_millionths"], 1_000_000)
        self.assertTrue(all(value == 1_000_000 for value in receipt["coverage_millionths"].values()))
        self.assertEqual(receipt["veto_failures"], [])

    def test_missing_handoff_or_late_canary_is_regressed(self):
        observations = self._complete_observations()
        without_handoff = [item for item in observations if item["event_kind"] != "agent-handoff"]
        receipt = assess_dogfood_coverage(without_handoff, expectations=self._expectations())
        with self.assertRaises(DogfoodCoverageError):
            require_dogfood_coverage_gate(receipt)
        self.assertEqual(receipt["status"], "regressed")

        late = self._complete_observations()
        compaction = next(item for item in late if item["event_kind"] == "compaction")
        changed = dict(compaction)
        changed["canary_sequence"] = changed["first_side_effect_sequence"]
        late[late.index(compaction)] = changed
        receipt = assess_dogfood_coverage(late, expectations=self._expectations())
        with self.assertRaises(DogfoodCoverageError):
            require_dogfood_coverage_gate(receipt)

    def test_observation_is_deterministic_and_digest_bound(self):
        first = self._emit(event_kind="delivery", subject_id="delivery-deterministic")
        validate_dogfood_observation(first)
        self.assertEqual(canonical_dogfood_observation_bytes(first), canonical_dogfood_observation_bytes(first))
        changed = dict(first)
        changed["active_work_after"] = "M5-08"
        with self.assertRaises(ValueError):
            validate_dogfood_observation(changed)


if __name__ == "__main__":
    unittest.main()
