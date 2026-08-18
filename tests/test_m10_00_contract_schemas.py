"""M10-00 strict pilot plan schema and registry tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.self_dogfood_pilot import build_evidence_matrix_receipt
from tests.test_m10_00_self_dogfood_pilot import M1000SelfDogfoodPilotTests


class M1000ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_path = (
            cls.root / "schemas/m10-00/self-dogfood-pilot-plan.schema.json"
        )
        cls.matrix_schema_path = (
            cls.root / "schemas/m10-00/self-dogfood-evidence-matrix.schema.json"
        )
        cls.fault_schema_path = (
            cls.root / "schemas/m10-00/self-dogfood-fault-drill.schema.json"
        )
        cls.release_schema_path = (
            cls.root / "schemas/m10-00/self-dogfood-release-verification.schema.json"
        )

    def plan(self) -> dict:
        fixture = M1000SelfDogfoodPilotTests(
            "test_plan_binds_three_leaves_workers_faults_and_e0_e9"
        )
        fixture.root = self.root
        return fixture.plan()

    def schema(self) -> dict:
        return json.loads(self.schema_path.read_text(encoding="utf-8"))

    def test_runtime_plan_matches_strict_schema(self) -> None:
        Draft202012Validator(
            self.schema(),
            format_checker=FormatChecker(),
        ).validate(self.plan())

    def test_authority_repository_and_nested_fields_are_strict(self) -> None:
        schema = self.schema()
        plan = self.plan()
        mutations = []
        top = {**plan, "state_patch": {}}
        mutations.append(top)
        authority = copy.deepcopy(plan)
        authority["authority"]["state_write_authority"] = True
        mutations.append(authority)
        repository = copy.deepcopy(plan)
        repository["repository_baseline"]["runtime_database"] = "postgresql"
        mutations.append(repository)
        matrix = copy.deepcopy(plan)
        matrix["evidence_matrix"][0]["admission_status"] = "passed"
        mutations.append(matrix)
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                Draft202012Validator(schema).validate(mutation)

    def test_registry_entry_binds_exact_schema_hash(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.self-dogfood-pilot-plan"
        )
        self.assertEqual(
            entry["content_sha256"],
            hashlib.sha256(self.schema_path.read_bytes()).hexdigest(),
        )
        matrix_entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.self-dogfood-evidence-matrix"
        )
        self.assertEqual(
            matrix_entry["content_sha256"],
            hashlib.sha256(self.matrix_schema_path.read_bytes()).hexdigest(),
        )
        fault_entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.self-dogfood-fault-drill"
        )
        self.assertEqual(
            fault_entry["content_sha256"],
            hashlib.sha256(self.fault_schema_path.read_bytes()).hexdigest(),
        )
        release_entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.self-dogfood-release-verification"
        )
        self.assertEqual(
            release_entry["content_sha256"],
            hashlib.sha256(self.release_schema_path.read_bytes()).hexdigest(),
        )

    def test_runtime_evidence_matrix_matches_strict_schema(self) -> None:
        fixture = M1000SelfDogfoodPilotTests(
            "test_plan_binds_three_leaves_workers_faults_and_e0_e9"
        )
        fixture.root = self.root
        plan = fixture.plan()
        receipt = build_evidence_matrix_receipt(
            plan,
            root=self.root,
            observed_at="2026-08-18T00:00:00+08:00",
        )
        Draft202012Validator(
            json.loads(self.matrix_schema_path.read_text(encoding="utf-8")),
            format_checker=FormatChecker(),
        ).validate(receipt)

    def test_committed_fault_drill_matches_strict_schema(self) -> None:
        receipt = json.loads(
            (
                self.root / "experiments/evidence/m10-00-fault-drill-results.json"
            ).read_text(encoding="utf-8")
        )
        Draft202012Validator(
            json.loads(self.fault_schema_path.read_text(encoding="utf-8")),
            format_checker=FormatChecker(),
        ).validate(receipt)

    def test_release_schema_is_strict(self) -> None:
        schema = json.loads(self.release_schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertFalse(schema["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
