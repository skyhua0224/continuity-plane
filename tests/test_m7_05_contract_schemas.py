from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from context_control_plane import affected_test_selection as selection
from context_control_plane import affected_test_selection_benchmark as benchmark


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _reseal(document: dict, field: str) -> None:
    document[field] = hashlib.sha256(
        _canonical({key: value for key, value in document.items() if key != field})
    ).hexdigest()


class M705ContractSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.golden = benchmark.build_affected_test_selection_golden_matrix()
        self.fixture = benchmark.build_affected_test_selection_fixture(
            golden_matrix=self.golden
        )
        self.scenario = next(
            item
            for item in self.golden["scenarios"]
            if item["scenario_id"] == "selection-core-selected"
        )
        self.receipt = benchmark._select_scenario(
            self.fixture, self.scenario, suffix="schema"
        )

    def schema(self, name: str) -> dict:
        path = self.root / f"schemas/m7-05/{name}.schema.json"
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.fail(f"M7-05 schema is missing: {path}")
        Draft202012Validator.check_schema(schema)
        return schema

    def validate_schema(self, name: str, instance: object) -> None:
        Draft202012Validator(
            self.schema(name), format_checker=FormatChecker()
        ).validate(instance)

    def test_all_runtime_contracts_match_strict_schemas(self) -> None:
        instances = {
            "affected-change-set": self.fixture["change_sets"][self.scenario["scenario_id"]],
            "deterministic-derivation-receipt": self.fixture["graphs"]["current"]["derivation"],
            "affected-graph": self.fixture["graphs"]["current"],
            "required-test-inventory": self.fixture["inventory"],
            "affected-test-selection": self.receipt,
            "affected-test-selection-golden": self.golden,
            "affected-test-selection-fixture": self.fixture,
        }
        for name, instance in instances.items():
            with self.subTest(schema=name):
                self.validate_schema(name, instance)

    def test_change_set_path_is_rejected_by_schema_and_runtime(self) -> None:
        change_set = copy.deepcopy(
            self.fixture["change_sets"][self.scenario["scenario_id"]]
        )
        change_set["changed_paths"] = ["/absolute/path.py"]
        _reseal(change_set, "change_set_sha256")
        errors = list(
            Draft202012Validator(
                self.schema("affected-change-set"),
                format_checker=FormatChecker(),
            ).iter_errors(change_set)
        )
        self.assertTrue(errors)
        with self.assertRaises(selection.AffectedTestSelectionError):
            selection.validate_affected_change_set(
                change_set,
                artifact_resolver=benchmark._fixture_artifact_resolver(
                    self.fixture, graph_available=True
                ),
                change_set_resolver=benchmark._fixture_change_set_resolver(
                    self.fixture, scenario_id=self.scenario["scenario_id"]
                ),
                current_context_resolver=benchmark._fixture_context_resolver(
                    self.fixture, graph_key=self.scenario["graph_key"]
                ),
            )

    def test_derivation_dynamic_evidence_is_rejected_by_schema_and_runtime(self) -> None:
        derivation = copy.deepcopy(
            self.fixture["graphs"]["current"]["derivation"]
        )
        derivation["dynamic_edge_artifact_ref"] = None
        derivation["dynamic_edge_artifact_sha256"] = None
        _reseal(derivation, "receipt_sha256")
        errors = list(
            Draft202012Validator(
                self.schema("deterministic-derivation-receipt"),
                format_checker=FormatChecker(),
            ).iter_errors(derivation)
        )
        self.assertTrue(errors)
        with self.assertRaises(selection.AffectedTestSelectionError):
            selection.validate_derivation_receipt(
                derivation,
                artifact_resolver=benchmark._fixture_artifact_resolver(
                    self.fixture, graph_available=True
                ),
                derivation_resolver=benchmark._fixture_derivation_resolver,
                current_context_resolver=benchmark._fixture_context_resolver(
                    self.fixture, graph_key="current"
                ),
            )

    def test_schema_and_runtime_share_array_and_iteration_caps(self) -> None:
        change_schema = self.schema("affected-change-set")
        benchmark_schema = self.schema("affected-test-selection-benchmark")
        self.assertEqual(change_schema["properties"]["changed_paths"]["maxItems"], 1024)
        self.assertEqual(benchmark_schema["properties"]["iterations"]["maximum"], 100000)


if __name__ == "__main__":
    unittest.main()
