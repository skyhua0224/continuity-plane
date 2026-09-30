from __future__ import annotations

import copy
import unittest

from context_control_plane import affected_test_selection as selection
from context_control_plane import affected_test_selection_benchmark as benchmark


class M705AffectedTestSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.golden = benchmark.build_affected_test_selection_golden_matrix()
        self.fixture = benchmark.build_affected_test_selection_fixture(
            golden_matrix=self.golden
        )

    def scenario(self, scenario_id: str) -> tuple[dict, dict]:
        scenario = next(
            item
            for item in self.golden["scenarios"]
            if item["scenario_id"] == scenario_id
        )
        return scenario, benchmark._select_scenario(
            self.fixture, scenario, suffix="test"
        )

    def test_provider_neutral_trusted_api_exists(self) -> None:
        for name in (
            "build_affected_change_set",
            "build_affected_graph",
            "build_derivation_receipt",
            "build_test_inventory",
            "select_affected_tests",
            "validate_affected_test_selection_receipt",
        ):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(selection, name, None)))

    def test_dependency_closure_and_required_anchors_are_selected(self) -> None:
        scenario, receipt = self.scenario("selection-core-selected")
        self.assertEqual(receipt["selection_mode"], "selected")
        self.assertEqual(receipt["changed_paths"], scenario["changed_paths"])
        self.assertEqual(
            receipt["selected_test_ids"],
            ["m7-05-contract", "python-compile", "ruff-selection"],
        )
        self.assertEqual(receipt["fallback_reasons"], [])
        self.assertEqual(receipt["external_service_calls"], 0)
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["completion_authority"])

    def test_golden_fault_matrix_falls_back_without_partial_selection(self) -> None:
        for scenario in self.golden["scenarios"]:
            with self.subTest(scenario=scenario["scenario_id"]):
                _, receipt = self.scenario(scenario["scenario_id"])
                self.assertEqual(receipt["selection_mode"], scenario["expected_selection_mode"])
                self.assertEqual(
                    receipt["selected_test_ids"], scenario["expected_test_ids"]
                )
                self.assertEqual(
                    receipt["fallback_reasons"], scenario["expected_fallback_reasons"]
                )
                if receipt["selection_mode"] == "full-validation":
                    self.assertEqual(receipt["affected_node_ids"], [])

    def test_graph_semantic_mutation_is_rejected_after_resealing(self) -> None:
        scenario, receipt = self.scenario("selection-core-selected")
        del scenario
        forged_graph = copy.deepcopy(self.fixture["graphs"]["current"])
        for node in forged_graph["nodes"]:
            node["depends_on_node_ids"] = []
        forged_graph["graph_sha256"] = selection._digest(
            forged_graph, "graph_sha256"
        )
        with self.assertRaises(selection.AffectedTestSelectionError):
            benchmark._select_scenario(
                {
                    **self.fixture,
                    "graphs": {**self.fixture["graphs"], "current": forged_graph},
                },
                next(
                    item
                    for item in self.golden["scenarios"]
                    if item["scenario_id"] == "selection-core-selected"
                ),
                suffix="forged",
            )
        self.assertEqual(receipt["selection_mode"], "selected")

    def test_inventory_derivation_is_required_for_full_scope(self) -> None:
        scenario, _ = self.scenario("selection-core-selected")
        forged_inventory = copy.deepcopy(self.fixture["inventory"])
        forged_inventory["tests"] = [
            test
            for test in forged_inventory["tests"]
            if test["test_id"] != "m7-02-unit"
        ]
        forged_inventory["inventory_sha256"] = selection._digest(
            forged_inventory, "inventory_sha256"
        )
        with self.assertRaises(selection.AffectedTestSelectionError):
            selection.select_affected_tests(
                selection_id="selection/forged-inventory",
                work_id="M7-05",
                project_revision=self.fixture["project_revision"],
                profile=self.fixture["profile"],
                inventory=forged_inventory,
                graph=self.fixture["graphs"]["current"],
                change_set=self.fixture["change_sets"][scenario["scenario_id"]],
                evaluated_at=self.fixture["evaluated_at"],
                artifact_resolver=benchmark._fixture_artifact_resolver(
                    self.fixture, graph_available=True
                ),
                change_set_resolver=benchmark._fixture_change_set_resolver(
                    self.fixture, scenario_id=scenario["scenario_id"]
                ),
                derivation_resolver=benchmark._fixture_derivation_resolver,
                current_context_resolver=benchmark._fixture_context_resolver(
                    self.fixture, graph_key="current"
                ),
            )

    def test_receipt_replay_rejects_self_sealed_test_removal(self) -> None:
        scenario, receipt = self.scenario("selection-core-selected")
        forged = copy.deepcopy(receipt)
        forged["selected_test_ids"] = ["python-compile", "ruff-selection"]
        forged["selected_estimated_wall_time_ms"] = 200
        forged["selected_input_bytes"] = 2_000_000
        forged["wall_time_reduction_basis_points"] = 6666
        forged["input_bytes_reduction_basis_points"] = 6666
        forged["receipt_sha256"] = selection._digest(forged, "receipt_sha256")
        with self.assertRaises(selection.AffectedTestSelectionError):
            selection.validate_affected_test_selection_receipt(
                forged,
                profile=self.fixture["profile"],
                inventory=self.fixture["inventory"],
                graph=self.fixture["graphs"]["current"],
                change_set=self.fixture["change_sets"][scenario["scenario_id"]],
                artifact_resolver=benchmark._fixture_artifact_resolver(
                    self.fixture, graph_available=True
                ),
                change_set_resolver=benchmark._fixture_change_set_resolver(
                    self.fixture, scenario_id=scenario["scenario_id"]
                ),
                derivation_resolver=benchmark._fixture_derivation_resolver,
                current_context_resolver=benchmark._fixture_context_resolver(
                    self.fixture, graph_key="current"
                ),
            )


if __name__ == "__main__":
    unittest.main()
