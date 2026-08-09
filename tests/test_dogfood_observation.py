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
            {
                "compaction",
                "input-routing",
                "skill-load",
                "plan-revision",
                "verification",
                "delivery",
            },
        )

    def test_summary_preserves_safety_veto_metrics(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["compaction_recovery_rate"], 1.0)
        self.assertEqual(summary["stale_decisions_revived"], 0)
        self.assertEqual(summary["unauthorized_task_switches"], 0)
        self.assertEqual(summary["unauthorized_goal_changes"], 0)
        self.assertEqual(summary["verification_failures"], 7)
        self.assertEqual(summary["scope_violations"], 0)

    def test_summary_accounts_for_loaded_skill_bodies(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["skill_body_load_count"], 54)
        self.assertEqual(summary["skill_body_load_bytes"], 597360)
        self.assertEqual(summary["repeated_skill_body_load_bytes"], 417939)

    def test_visible_compactions_remain_uncomparable_for_cost_metrics(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["compaction_events"], 15)
        self.assertEqual(summary["input_routing_events"], 22)
        self.assertEqual(summary["comparable_compaction_events"], 0)

    def test_post_compaction_interaction_reset_marks_trend_regressed(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["compaction_recovery_rate"], 1.0)
        self.assertEqual(summary["continuation_recovery_rate"], 34 / 36)
        self.assertEqual(summary["already_acknowledged_items_replayed"], 2)
        self.assertEqual(summary["first_post_restore_action_mismatches"], 2)
        self.assertEqual(summary["trend_status"], "regressed")

    def test_current_compaction_resumes_the_exact_ci_red_test(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-009"
        )

        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)

    def test_latest_compaction_resumes_the_exact_governance_red_test(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-010"
        )

        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)

    def test_current_compaction_resumes_the_exact_m2_08_conformance_action(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-011"
        )

        self.assertEqual(latest["active_leaf_after"], "M2-08")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)

    def test_latest_compaction_resumes_the_exact_manifest_red_test(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-012"
        )

        self.assertEqual(latest["active_leaf_after"], "M2-08")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)

    def test_m2_09_compaction_resumes_the_sqlite_lifecycle_red_test(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-013"
        )

        self.assertEqual(latest["active_leaf_before"], "M2-09")
        self.assertEqual(latest["active_leaf_after"], "M2-09")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)

    def test_current_compaction_resumes_m2_09_verification_without_replay(self):
        latest = next(
            (
                item
                for item in self.document["observations"]
                if item["observation_id"] == "dogfood-compaction-014"
            ),
            None,
        )

        self.assertIsNotNone(latest)
        self.assertEqual(latest["active_leaf_before"], "M2-09")
        self.assertEqual(latest["active_leaf_after"], "M2-09")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)

    def test_m2_04_compaction_resumes_governance_red_test_and_rejects_stale_handoff(self):
        latest = next(
            (
                item
                for item in self.document["observations"]
                if item["observation_id"] == "dogfood-compaction-015"
            ),
            None,
        )

        self.assertIsNotNone(latest)
        self.assertEqual(latest["active_leaf_before"], "M2-04")
        self.assertEqual(latest["active_leaf_after"], "M2-04")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)
        self.assertIn(
            "stale multi-Agent handoff",
            "\n".join(latest["evidence"]),
        )

    def test_loaded_database_skill_digest_drift_is_quarantined(self):
        skill_load = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-skill-load-010"
        )
        database_skill = next(
            skill
            for skill in skill_load["metrics"]["skills"]
            if skill["skill_id"] == "database-schema-design"
        )

        self.assertTrue(database_skill["quarantined"])
        self.assertNotEqual(
            database_skill["content_sha256"],
            database_skill["current_content_sha256"],
        )

    def test_latest_compaction_detects_replayed_answer_before_continuation(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-008"
        )

        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)
        self.assertFalse(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 1)

    def test_delivery_speed_is_a_quality_gated_baseline(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["delivery_events"], 1)
        self.assertEqual(summary["accepted_work_items"], 1)
        self.assertEqual(summary["delivery_lead_time_p50_seconds"], 1577)
        self.assertEqual(summary["delivery_lead_time_p95_seconds"], 1577)
        self.assertEqual(summary["delivery_safety_veto_failures"], 0)
        self.assertEqual(summary["delivery_trend_status"], "baseline-insufficient-samples")

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
