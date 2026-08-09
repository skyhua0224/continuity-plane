import copy
import unittest
from pathlib import Path

import yaml

from context_control_plane.dogfood_observation import (
    DogfoodObservationError,
    summarize_observations,
    validate_observation_document,
)


class DeliverySpeedObservationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = (
            Path(__file__).parents[1]
            / "experiments"
            / "dogfood"
            / "observations-2026-08-09.yaml"
        )
        cls.document = yaml.safe_load(path.read_text(encoding="utf-8"))

    @staticmethod
    def _delivery_observation() -> dict:
        return {
            "observation_id": "dogfood-delivery-test",
            "event_type": "delivery",
            "observed_at": "2026-08-09T22:35:20+08:00",
            "authority": "git-merge-and-required-ci-evidence",
            "active_leaf_before": "M1-05",
            "active_leaf_after": "M1-05",
            "work_id": "M0-12",
            "task_class": "governance-ci",
            "measurement_source": "git-merge-proxy",
            "started_at": "2026-08-09T22:09:03+08:00",
            "completed_at": "2026-08-09T22:35:20+08:00",
            "evidence_refs": [
                "git:b578663197780d170201778b7c4412360494fe2b",
                "git:48485aff9dc752064154f24d36a7be60be1dc06f",
                "gitea:run-1021-success",
            ],
            "metrics": {
                "lead_time_seconds": 1577,
                "cycle_time_seconds": 1577,
                "blocked_seconds": 0,
                "rework_seconds": 0,
                "time_to_first_durable_artifact_seconds": 0,
                "rework_events": 2,
                "accepted_artifacts": 1,
                "completion_gates_total": 4,
                "completion_gates_passed": 4,
                "safety_veto_failures": 0,
            },
        }

    def test_delivery_event_is_valid_and_reported_without_overstating_trend(self):
        document = copy.deepcopy(self.document)
        baseline = summarize_observations(document)
        document["observations"].append(self._delivery_observation())

        validate_observation_document(document)
        summary = summarize_observations(document)

        self.assertEqual(summary["delivery_events"], baseline["delivery_events"] + 1)
        self.assertEqual(
            summary["accepted_work_items"], baseline["accepted_work_items"] + 1
        )
        self.assertEqual(summary["delivery_lead_time_p50_seconds"], 1577)
        self.assertEqual(summary["delivery_lead_time_p95_seconds"], 1577)
        self.assertEqual(summary["delivery_trend_status"], "baseline-insufficient-samples")

    def test_delivery_duration_must_match_timestamps(self):
        document = copy.deepcopy(self.document)
        observation = self._delivery_observation()
        observation["metrics"]["lead_time_seconds"] = 10
        document["observations"].append(observation)

        with self.assertRaisesRegex(DogfoodObservationError, "lead time"):
            validate_observation_document(document)

    def test_faster_delivery_cannot_hide_a_safety_veto(self):
        document = copy.deepcopy(self.document)
        baseline = summarize_observations(document)
        observation = self._delivery_observation()
        observation["metrics"]["safety_veto_failures"] = 1
        document["observations"].append(observation)

        summary = summarize_observations(document)
        self.assertEqual(summary["delivery_trend_status"], "regressed")
        self.assertEqual(
            summary["accepted_work_items"], baseline["accepted_work_items"]
        )


if __name__ == "__main__":
    unittest.main()
