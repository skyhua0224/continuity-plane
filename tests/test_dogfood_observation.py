import copy
import hashlib
import unittest
from pathlib import Path

import yaml

from context_control_plane.dogfood_observation import (
    DogfoodObservationError,
    summarize_observations,
    validate_observation_document,
)


class DogfoodObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = (
            Path(__file__).parents[1]
            / "experiments"
            / "dogfood"
            / "observations-2026-08-09.yaml"
        )
        cls.document = yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_baseline_document_is_valid_and_has_all_event_types(self):
        validate_observation_document(self.document)
        event_types = {item["event_type"] for item in self.document["observations"]}

        self.assertEqual(
            event_types,
            {"compaction", "input-routing", "skill-load", "plan-revision", "verification"},
        )

    def test_summary_preserves_safety_veto_metrics(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["compaction_recovery_rate"], 1.0)
        self.assertEqual(summary["stale_decisions_revived"], 0)
        self.assertEqual(summary["unauthorized_task_switches"], 0)
        self.assertEqual(summary["unauthorized_goal_changes"], 0)
        self.assertEqual(summary["verification_failures"], 0)
        self.assertEqual(summary["scope_violations"], 0)

    def test_summary_accounts_for_loaded_skill_bodies(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["skill_body_load_count"], 16)
        self.assertEqual(summary["skill_body_load_bytes"], 185560)
        self.assertEqual(summary["repeated_skill_body_load_bytes"], 73910)

    def test_compactions_without_comparable_bytes_are_only_a_baseline(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["compaction_events"], 5)
        self.assertEqual(summary["input_routing_events"], 13)
        self.assertEqual(summary["trend_status"], "baseline-insufficient-samples")

    def test_recovery_loss_marks_trend_as_regressed(self):
        broken = copy.deepcopy(self.document)
        compaction = next(
            item for item in broken["observations"] if item["event_type"] == "compaction"
        )
        compaction["metrics"]["critical_fields_recovered"] -= 1

        summary = summarize_observations(broken)
        self.assertEqual(summary["trend_status"], "regressed")

    def test_verification_failure_marks_trend_as_regressed(self):
        broken = copy.deepcopy(self.document)
        verification = next(
            item for item in broken["observations"] if item["event_type"] == "verification"
        )
        verification["metrics"]["tests_failed"] = 1

        summary = summarize_observations(broken)
        self.assertEqual(summary["trend_status"], "regressed")

    def test_plan_change_without_authority_is_rejected(self):
        broken = copy.deepcopy(self.document)
        plan = next(
            item for item in broken["observations"] if item["event_type"] == "plan-revision"
        )
        plan["authority"] = "none"

        with self.assertRaises(DogfoodObservationError):
            validate_observation_document(broken)

    def test_input_routing_records_a_parked_idea_without_switching_active_leaf(self):
        document = copy.deepcopy(self.document)
        baseline_summary = summarize_observations(document)
        candidate_summary = "Provider-neutral core with provider adapters."
        document["observations"].append(
            {
                "observation_id": "dogfood-input-routing-test",
                "event_type": "input-routing",
                "observed_at": "2026-08-09T18:30:00+08:00",
                "authority": "explicit-user-idea-label",
                "active_leaf_before": "M1-06",
                "active_leaf_after": "M1-06",
                "input_kind": "idea",
                "route": "capture-and-continue",
                "candidate_id": "idea-provider-adapters-001",
                "candidate_summary": candidate_summary,
                "candidate_summary_sha256": hashlib.sha256(
                    candidate_summary.encode("utf-8")
                ).hexdigest(),
                "metrics": {
                    "messages_observed": 1,
                    "ideas_captured": 1,
                    "unauthorized_task_switches": 0,
                    "return_point_preserved": True,
                    "context_window_used_tokens": None,
                    "context_window_limit_tokens": None,
                    "provider_compaction_signal_available": False,
                    "tool_interruption_observed": False,
                },
            }
        )

        validate_observation_document(document)
        summary = summarize_observations(document)

        self.assertEqual(
            summary["input_routing_events"],
            baseline_summary["input_routing_events"] + 1,
        )
        self.assertEqual(
            summary["ideas_captured"],
            baseline_summary["ideas_captured"] + 1,
        )
        self.assertEqual(summary["unauthorized_task_switches"], 0)

    def test_capture_and_continue_rejects_an_active_leaf_change(self):
        document = copy.deepcopy(self.document)
        candidate_summary = "Provider-neutral core with provider adapters."
        document["observations"].append(
            {
                "observation_id": "dogfood-input-routing-switch-test",
                "event_type": "input-routing",
                "observed_at": "2026-08-09T18:31:00+08:00",
                "authority": "explicit-user-idea-label",
                "active_leaf_before": "M1-06",
                "active_leaf_after": "M4-05",
                "input_kind": "idea",
                "route": "capture-and-continue",
                "candidate_id": "idea-provider-adapters-001",
                "candidate_summary": candidate_summary,
                "candidate_summary_sha256": hashlib.sha256(
                    candidate_summary.encode("utf-8")
                ).hexdigest(),
                "metrics": {
                    "messages_observed": 1,
                    "ideas_captured": 1,
                    "unauthorized_task_switches": 0,
                    "return_point_preserved": True,
                    "context_window_used_tokens": None,
                    "context_window_limit_tokens": None,
                    "provider_compaction_signal_available": False,
                    "tool_interruption_observed": False,
                },
            }
        )

        with self.assertRaises(DogfoodObservationError):
            validate_observation_document(document)

    def test_input_routing_rejects_a_tampered_candidate_summary(self):
        document = copy.deepcopy(self.document)
        document["observations"].append(
            {
                "observation_id": "dogfood-input-routing-summary-test",
                "event_type": "input-routing",
                "observed_at": "2026-08-09T18:32:00+08:00",
                "authority": "explicit-user-idea-label",
                "active_leaf_before": "M1-06",
                "active_leaf_after": "M1-06",
                "input_kind": "idea",
                "route": "capture-and-continue",
                "candidate_id": "idea-provider-adapters-001",
                "candidate_summary": "Tampered summary",
                "candidate_summary_sha256": "a" * 64,
                "metrics": {
                    "messages_observed": 1,
                    "ideas_captured": 1,
                    "unauthorized_task_switches": 0,
                    "return_point_preserved": True,
                    "context_window_used_tokens": None,
                    "context_window_limit_tokens": None,
                    "provider_compaction_signal_available": False,
                    "tool_interruption_observed": False,
                },
            }
        )

        with self.assertRaises(DogfoodObservationError):
            validate_observation_document(document)


if __name__ == "__main__":
    unittest.main()
