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

        self.assertEqual(summary["skill_body_load_count"], 83)
        self.assertEqual(summary["skill_body_load_bytes"], 860439)
        self.assertEqual(summary["repeated_skill_body_load_bytes"], 681018)

    def test_visible_compactions_remain_uncomparable_for_cost_metrics(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["compaction_events"], 24)
        self.assertEqual(summary["input_routing_events"], 24)
        self.assertEqual(summary["comparable_compaction_events"], 0)

    def test_post_compaction_interaction_reset_marks_trend_regressed(self):
        summary = summarize_observations(self.document)

        self.assertEqual(summary["compaction_recovery_rate"], 1.0)
        self.assertEqual(summary["continuation_recovery_rate"], 70 / 72)
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

    def test_m2_05_compaction_resumes_the_exact_review_red_test(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-016"
        )

        self.assertEqual(latest["active_leaf_before"], "M2-05")
        self.assertEqual(latest["active_leaf_after"], "M2-05")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)

    def test_m2_06_compaction_resumes_from_the_m2_05_pr_cursor(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-017"
        )

        self.assertEqual(latest["active_leaf_before"], "M2-06")
        self.assertEqual(latest["active_leaf_after"], "M2-06")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)
        self.assertIn(
            "M2-05 PR status",
            "\n".join(latest["evidence"]),
        )

    def test_m2_06_review_compaction_resumes_the_governance_ref_red_test(self):
        latest = next(
            item
            for item in self.document["observations"]
            if item["observation_id"] == "dogfood-compaction-018"
        )

        self.assertEqual(latest["active_leaf_before"], "M2-06")
        self.assertEqual(latest["active_leaf_after"], "M2-06")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)
        self.assertIn(
            "governance-ref red test",
            "\n".join(latest["evidence"]),
        )

    def test_m2_07_compaction_resumes_the_runtime_profile_red_test(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        self.assertIn("dogfood-compaction-019", observations)
        self.assertIn("dogfood-skill-load-025", observations)
        latest = observations["dogfood-compaction-019"]

        self.assertEqual(latest["active_leaf_before"], "M2-07")
        self.assertEqual(latest["active_leaf_after"], "M2-07")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)
        self.assertIn(
            "runtime_capability_profile red test",
            "\n".join(latest["evidence"]),
        )

        skill_load = observations["dogfood-skill-load-025"]
        self.assertEqual(skill_load["active_leaf_before"], "M2-07")
        self.assertEqual(skill_load["metrics"]["body_load_count"], 3)
        self.assertEqual(skill_load["metrics"]["total_body_bytes"], 27216)
        self.assertEqual(skill_load["metrics"]["repeated_body_load_bytes"], 27216)

    def test_review_compaction_resumes_the_exact_m2_07_acceptance_cursor(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        latest = observations["dogfood-compaction-020"]

        self.assertEqual(latest["active_leaf_before"], "M2-07")
        self.assertEqual(latest["active_leaf_after"], "M2-07")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["continuation_fields_recovered"], 4)
        self.assertIn(
            "proposal_revision",
            "\n".join(latest["evidence"]),
        )

    def test_m2_07_delivery_compaction_resumes_the_final_review_cursor(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        latest = observations["dogfood-compaction-021"]

        self.assertEqual(latest["active_leaf_before"], "M4-01")
        self.assertEqual(latest["active_leaf_after"], "M4-01")
        self.assertTrue(latest["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(latest["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(latest["metrics"]["unauthorized_task_switches"], 0)
        self.assertIn("final High/Medium review", "\n".join(latest["evidence"]))

        skill_load = observations["dogfood-skill-load-027"]
        self.assertEqual(skill_load["active_leaf_before"], "M4-01")
        self.assertEqual(skill_load["metrics"]["body_load_count"], 3)
        self.assertEqual(skill_load["metrics"]["total_body_bytes"], 27216)
        self.assertEqual(skill_load["metrics"]["repeated_body_load_bytes"], 27216)

    def test_multi_agent_reviewer_compaction_keeps_global_active_leaf(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        reviewer = observations["dogfood-compaction-022"]

        self.assertEqual(reviewer["active_leaf_before"], "M4-01")
        self.assertEqual(reviewer["active_leaf_after"], "M4-01")
        self.assertTrue(reviewer["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(reviewer["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(reviewer["metrics"]["unauthorized_task_switches"], 0)
        self.assertIn("read-only reviewer role", "\n".join(reviewer["evidence"]))

        skill_load = observations["dogfood-skill-load-028"]
        self.assertEqual(skill_load["active_leaf_before"], "M4-01")
        self.assertEqual(skill_load["metrics"]["body_load_count"], 2)
        self.assertEqual(skill_load["metrics"]["total_body_bytes"], 17349)
        self.assertEqual(skill_load["metrics"]["repeated_body_load_bytes"], 17349)

    def test_m4_design_reviewer_compaction_preserves_the_delivery_gate(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        reviewer = observations["dogfood-compaction-023"]

        self.assertEqual(reviewer["active_leaf_before"], "M4-01")
        self.assertEqual(reviewer["active_leaf_after"], "M4-01")
        self.assertTrue(reviewer["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(reviewer["metrics"]["already_acknowledged_items_replayed"], 0)
        self.assertEqual(reviewer["metrics"]["unauthorized_task_switches"], 0)
        self.assertIn("M2-07 delivery gate", "\n".join(reviewer["evidence"]))

        recovery_skills = observations["dogfood-skill-load-029"]
        self.assertEqual(recovery_skills["metrics"]["body_load_count"], 4)
        self.assertEqual(recovery_skills["metrics"]["total_body_bytes"], 32851)
        self.assertEqual(
            recovery_skills["metrics"]["repeated_body_load_bytes"], 32851
        )

        delivery_skills = observations["dogfood-skill-load-030"]
        self.assertEqual(delivery_skills["metrics"]["body_load_count"], 2)
        self.assertEqual(delivery_skills["metrics"]["total_body_bytes"], 22367)
        self.assertEqual(
            delivery_skills["metrics"]["repeated_body_load_bytes"], 22367
        )

    def test_m4_01_compaction_and_status_query_resume_the_exact_red_test_cursor(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        compaction = observations["dogfood-compaction-024"]

        self.assertEqual(compaction["active_leaf_before"], "M4-01")
        self.assertEqual(compaction["active_leaf_after"], "M4-01")
        self.assertTrue(compaction["metrics"]["first_post_restore_action_matched"])
        self.assertEqual(
            compaction["metrics"]["already_acknowledged_items_replayed"],
            0,
        )
        self.assertEqual(compaction["metrics"]["unauthorized_task_switches"], 0)
        self.assertIn(
            "M4-01 design correction test cursor",
            "\n".join(compaction["evidence"]),
        )

        status_query = observations["dogfood-input-routing-024"]
        self.assertEqual(status_query["input_kind"], "status-query")
        self.assertEqual(status_query["route"], "continue")
        self.assertEqual(status_query["active_leaf_after"], "M4-01")
        self.assertEqual(
            status_query["metrics"]["unauthorized_task_switches"],
            0,
        )

        skill_load = observations["dogfood-skill-load-031"]
        self.assertEqual(skill_load["active_leaf_before"], "M4-01")
        self.assertEqual(skill_load["metrics"]["body_load_count"], 3)
        self.assertEqual(skill_load["metrics"]["total_body_bytes"], 27216)
        self.assertEqual(skill_load["metrics"]["repeated_body_load_bytes"], 27216)

    def test_m2_07_acceptance_advances_to_the_ready_skill_manifest_leaf(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        self.assertIn("dogfood-plan-revision-024", observations)
        self.assertIn("dogfood-verification-023", observations)

        revision = observations["dogfood-plan-revision-024"]
        self.assertEqual(revision["active_leaf_before"], "M2-07")
        self.assertEqual(revision["active_leaf_after"], "M4-01")
        self.assertEqual(revision["metrics"]["master_revision_before"], 27)
        self.assertEqual(revision["metrics"]["master_revision_after"], 28)
        self.assertEqual(revision["metrics"]["tasks_completed"], ["M2-07"])
        self.assertEqual(revision["metrics"]["tasks_activated"], ["M4-01"])

        verification = observations["dogfood-verification-023"]
        self.assertEqual(verification["active_leaf_before"], "M2-07")
        self.assertEqual(verification["active_leaf_after"], "M4-01")
        self.assertEqual(verification["metrics"]["tests_run"], 388)
        self.assertEqual(verification["metrics"]["tests_failed"], 0)
        self.assertEqual(verification["metrics"]["checks_failed"], 0)

    def test_final_m2_07_verification_preserves_the_corrected_contract(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        verification = observations["dogfood-verification-024"]

        self.assertEqual(verification["active_leaf_before"], "M4-01")
        self.assertEqual(verification["active_leaf_after"], "M4-01")
        self.assertEqual(verification["metrics"]["tests_run"], 394)
        self.assertEqual(verification["metrics"]["tests_failed"], 0)
        self.assertEqual(verification["metrics"]["checks_failed"], 0)
        self.assertEqual(verification["metrics"]["scope_violations"], 0)
        self.assertIn(
            "review:m2-07-high-0-medium-0-2026-08-10",
            verification["evidence_refs"],
        )

    def test_m4_01_acceptance_advances_to_compiled_skill_packets(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        revision = observations["dogfood-plan-revision-025"]
        verification = observations["dogfood-verification-025"]

        self.assertEqual(revision["active_leaf_before"], "M4-01")
        self.assertEqual(revision["active_leaf_after"], "M4-02")
        self.assertEqual(revision["metrics"]["master_revision_before"], 28)
        self.assertEqual(revision["metrics"]["master_revision_after"], 29)
        self.assertEqual(revision["metrics"]["tasks_completed"], ["M4-01"])
        self.assertEqual(revision["metrics"]["tasks_activated"], ["M4-02"])

        self.assertEqual(verification["active_leaf_before"], "M4-01")
        self.assertEqual(verification["active_leaf_after"], "M4-02")
        self.assertEqual(verification["metrics"]["tests_run"], 430)
        self.assertEqual(verification["metrics"]["tests_failed"], 0)
        self.assertEqual(verification["metrics"]["checks_failed"], 0)
        self.assertIn(
            "review:m4-01-high-0-medium-0-2026-08-10",
            verification["evidence_refs"],
        )

    def test_m4_02_acceptance_advances_to_skill_drift_quarantine(self):
        observations = {
            item["observation_id"]: item for item in self.document["observations"]
        }
        revision = observations["dogfood-plan-revision-026"]
        verification = observations["dogfood-verification-026"]

        self.assertEqual(revision["active_leaf_before"], "M4-02")
        self.assertEqual(revision["active_leaf_after"], "M4-03")
        self.assertEqual(revision["metrics"]["master_revision_before"], 29)
        self.assertEqual(revision["metrics"]["master_revision_after"], 30)
        self.assertEqual(revision["metrics"]["tasks_completed"], ["M4-02"])
        self.assertEqual(revision["metrics"]["tasks_activated"], ["M4-03"])

        self.assertEqual(verification["active_leaf_before"], "M4-02")
        self.assertEqual(verification["active_leaf_after"], "M4-03")
        self.assertEqual(verification["metrics"]["tests_run"], 448)
        self.assertEqual(verification["metrics"]["tests_failed"], 0)
        self.assertEqual(verification["metrics"]["checks_failed"], 0)
        self.assertEqual(
            verification["metrics"]["static_metadata_bytes_reduction_percent"],
            72.666,
        )
        self.assertIn(
            "review:m4-02-high-0-medium-0-2026-08-11",
            verification["evidence_refs"],
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
