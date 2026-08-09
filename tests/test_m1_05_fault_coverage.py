import copy
import json
import unittest
from pathlib import Path

import yaml

from context_control_plane.fault_coverage import (
    FaultCoverageError,
    summarize_fault_coverage,
    validate_fault_coverage,
)


class M105FaultCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = (
            Path(__file__).parents[1]
            / "experiments"
            / "faults"
            / "e0-e9-coverage-2026-08-09.yaml"
        )
        cls.document = yaml.safe_load(path.read_text(encoding="utf-8"))

    def test_matrix_has_at_least_one_contract_for_every_experiment(self):
        validate_fault_coverage(self.document)
        summary = summarize_fault_coverage(self.document)

        self.assertEqual(summary["experiments_covered"], [f"E{index}" for index in range(10)])
        self.assertEqual(summary["experiment_coverage_rate"], 1.0)
        self.assertGreaterEqual(summary["fixture_count"], 13)

    def test_matrix_covers_required_m1_05_failure_classes(self):
        scenarios = {fixture["scenario"] for fixture in self.document["fixtures"]}

        self.assertTrue(
            {
                "task-switch-without-checkpoint",
                "parallel-ready-work",
                "parallel-path-owner-conflict",
                "issue-backed-ready-work",
                "duplicate-work-detection",
                "stale-skill-digest",
                "sigkill-commit-boundary",
                "provider-503",
                "checkpoint-corruption",
                "concurrent-cas",
            }.issubset(scenarios)
        )

    def test_parallel_ready_work_allows_distinct_claims_and_paths(self):
        fixture = next(
            item for item in self.document["fixtures"] if item["scenario"] == "parallel-ready-work"
        )

        self.assertEqual(fixture["expected"]["gate"], "allow")
        self.assertEqual(len(fixture["initial"]["active_tasks"]), 2)
        self.assertEqual(len(fixture["initial"]["claims"]), 2)
        owned_paths = [path for claim in fixture["initial"]["claims"] for path in claim["paths"]]
        self.assertEqual(len(owned_paths), len(set(owned_paths)))
        self.assertEqual(fixture["expected"]["unauthorized_effects"], 0)

    def test_parallel_path_conflict_is_blocked(self):
        fixture = next(
            item
            for item in self.document["fixtures"]
            if item["scenario"] == "parallel-path-owner-conflict"
        )

        self.assertEqual(fixture["expected"]["gate"], "block")
        self.assertEqual(fixture["expected"]["state_revision"], fixture["initial"]["state_revision"])
        self.assertEqual(fixture["expected"]["unauthorized_effects"], 0)

    def test_issue_backed_work_can_activate_without_copying_the_backlog_to_master(self):
        fixture = next(
            item
            for item in self.document["fixtures"]
            if item["scenario"] == "issue-backed-ready-work"
        )

        self.assertEqual(fixture["expected"]["gate"], "allow")
        self.assertEqual(fixture["injection"]["target"], "issue-provider-revision")
        self.assertEqual(fixture["initial"]["active_tasks"], ["issue-work-42"])
        self.assertEqual(fixture["initial"]["claims"][0]["task_id"], "issue-work-42")

    def test_duplicate_work_is_blocked_before_a_second_effect(self):
        fixture = next(
            item
            for item in self.document["fixtures"]
            if item["scenario"] == "duplicate-work-detection"
        )

        self.assertEqual(fixture["expected"]["gate"], "block")
        self.assertEqual(fixture["injection"]["boundary"], "before-second-claim")
        self.assertEqual(fixture["expected"]["duplicate_effects"], 0)
        self.assertEqual(fixture["expected"]["state_revision"], fixture["initial"]["state_revision"])

    def test_contract_evidence_does_not_claim_runtime_fault_injection(self):
        summary = summarize_fault_coverage(self.document)

        self.assertEqual(summary["contract_fixture_coverage_rate"], 1.0)
        self.assertEqual(summary["runtime_verified_fixture_count"], 0)
        self.assertEqual(summary["runtime_fixture_coverage_rate"], 0.0)
        self.assertEqual(summary["status"], "contract-verified-runtime-deferred")

    def test_fault_coverage_schema_is_versioned_in_the_registry(self):
        root = Path(__file__).parents[1]
        registry = yaml.safe_load((root / "schemas" / "registry.yaml").read_text(encoding="utf-8"))
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.fault-coverage"
        )
        schema = json.loads((root / entry["artifact_path"]).read_text(encoding="utf-8"))

        self.assertEqual(entry["current_wire_version"], "context.fault-coverage/v1alpha1")
        self.assertEqual(schema["properties"]["schema_version"]["const"], entry["current_wire_version"])
        self.assertIn("fixtures", schema["required"])

    def test_validator_rejects_a_missing_experiment(self):
        broken = copy.deepcopy(self.document)
        broken["fixtures"] = [
            fixture for fixture in broken["fixtures"] if fixture["experiment_id"] != "E8"
        ]

        with self.assertRaisesRegex(FaultCoverageError, "E0-E9"):
            validate_fault_coverage(broken)

    def test_validator_rejects_a_veto_fixture_that_allows_bad_effects(self):
        broken = copy.deepcopy(self.document)
        fixture = next(
            item for item in broken["fixtures"] if item["scenario"] == "concurrent-cas"
        )
        fixture["expected"]["duplicate_effects"] = 1

        with self.assertRaisesRegex(FaultCoverageError, "effect safety"):
            validate_fault_coverage(broken)

    def test_validator_rejects_malformed_initial_with_a_contract_error(self):
        broken = copy.deepcopy(self.document)
        fixture = broken["fixtures"][0]
        del fixture["initial"]["state_revision"]

        with self.assertRaisesRegex(FaultCoverageError, "initial fields"):
            validate_fault_coverage(broken)


if __name__ == "__main__":
    unittest.main()
