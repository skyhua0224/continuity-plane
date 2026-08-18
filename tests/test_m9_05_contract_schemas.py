"""M9-05 human governance strict schema and registry tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.human_governance import (
    HumanGovernanceError,
    HumanGovernanceFacade,
    validate_human_governance_request,
)
from tests import test_m9_05_human_governance as fixtures

SCHEMAS = {
    "context.human-governance-request": "human-governance-request.schema.json",
    "context.human-governance-response": "human-governance-response.schema.json",
    "context.authorization-audit-event": (
        "governance-authorization-audit-event.schema.json"
    ),
    "context.human-governance-benchmark": "human-governance-benchmark.schema.json",
}


class M905ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas/m9-05"
        cls.schemas = {
            schema_id: json.loads((cls.schema_dir / filename).read_text(encoding="utf-8"))
            for schema_id, filename in SCHEMAS.items()
        }
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)
        request = fixtures._request(fixtures.ACTIONS[0])
        state = fixtures._StateMCPProbe()
        response = HumanGovernanceFacade(
            state, session_resolver=fixtures._SessionResolver()
        ).submit(request, session_id="session-valid")
        tool, arguments, context = state.calls[0]
        benchmark_module = __import__(
            "context_control_plane.human_governance_benchmark",
            fromlist=["benchmark_human_governance"],
        )
        cls.samples = {
            "context.human-governance-request": request,
            "context.human-governance-response": response,
            "context.authorization-audit-event": (
                state.authorization_receipt(tool, arguments, context=context)
            ),
            "context.human-governance-benchmark": (
                benchmark_module.benchmark_human_governance(
                    root=cls.root,
                    iterations=2,
                    generated_at="2026-08-17T09:00:00+08:00",
                )
            ),
        }

    def validator(self, schema_id: str) -> Draft202012Validator:
        return Draft202012Validator(
            self.schemas[schema_id], format_checker=FormatChecker()
        )

    def test_runtime_documents_match_strict_schemas(self) -> None:
        for schema_id, sample in self.samples.items():
            with self.subTest(schema_id=schema_id):
                self.validator(schema_id).validate(sample)

    def test_top_level_fields_are_required_and_unknown_fields_rejected(self) -> None:
        for schema_id, sample in self.samples.items():
            schema = self.schemas[schema_id]
            validator = self.validator(schema_id)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            with self.subTest(schema_id=schema_id, mutation="extra"), self.assertRaises(
                ValidationError
            ):
                validator.validate({**sample, "unexpected": True})
            for field in sample:
                missing = copy.deepcopy(sample)
                del missing[field]
                with self.subTest(schema_id=schema_id, missing=field), self.assertRaises(
                    ValidationError
                ):
                    validator.validate(missing)

    def test_action_payload_and_zero_authority_are_exact(self) -> None:
        request_validator = self.validator("context.human-governance-request")
        forged = copy.deepcopy(self.samples["context.human-governance-request"])
        forged["action"] = fixtures.ACTIONS[3]
        with self.assertRaises(ValidationError):
            request_validator.validate(forged)

        response_validator = self.validator("context.human-governance-response")
        for field in (
            "state_write_authority",
            "completion_authority",
            "approval_authority",
            "provider_native_authority",
            "external_effect_authority",
        ):
            forged = copy.deepcopy(self.samples["context.human-governance-response"])
            forged["authority"][field] = True
            with self.subTest(field=field), self.assertRaises(ValidationError):
                response_validator.validate(forged)

    def test_request_schema_enforces_the_runtime_evidence_capacity(self) -> None:
        request_validator = self.validator("context.human-governance-request")
        oversized = copy.deepcopy(self.samples["context.human-governance-request"])
        oversized["payload"]["criterion_evidence"] = {
            f"criterion-{criterion}": [
                f"evidence-{criterion}-{evidence}" for evidence in range(64)
            ]
            for criterion in range(17)
        }
        with self.assertRaises(ValidationError):
            request_validator.validate(oversized)

        too_many_criteria = copy.deepcopy(
            self.samples["context.human-governance-request"]
        )
        too_many_criteria["payload"]["criterion_evidence"] = {
            f"criterion-{criterion}": [f"evidence-{criterion}"]
            for criterion in range(17)
        }
        with self.assertRaises(ValidationError):
            request_validator.validate(too_many_criteria)
        with self.assertRaises(HumanGovernanceError):
            validate_human_governance_request(too_many_criteria)

    def test_response_schema_binds_action_tool_and_record_kind(self) -> None:
        validator = self.validator("context.human-governance-response")
        sample = self.samples["context.human-governance-response"]

        wrong_tool = copy.deepcopy(sample)
        wrong_tool["result"]["state_mcp_tool"] = fixtures.ACTIONS[1]
        with self.assertRaises(ValidationError):
            validator.validate(wrong_tool)

        wrong_kind = copy.deepcopy(sample)
        wrong_kind["result"]["record_kind"] = "promotion-approval"
        with self.assertRaises(ValidationError):
            validator.validate(wrong_kind)

    def test_authorization_audit_keeps_one_stable_schema_id(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = [
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.authorization-audit-event"
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(
            entries[0]["supported_wire_versions"],
            [
                "context.authorization-audit-event/v1alpha1",
                "context.authorization-audit-event/v2alpha1",
            ],
        )
        self.assertEqual(
            entries[0]["current_wire_version"],
            "context.authorization-audit-event/v2alpha1",
        )
        self.assertFalse(
            any(
                item["schema_id"]
                == "context.governance-authorization-audit-event"
                for item in registry["schemas"]
            )
        )

    def test_registry_entries_bind_exact_schema_hashes(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {item["schema_id"]: item for item in registry["schemas"]}
        for schema_id, filename in SCHEMAS.items():
            entry = entries[schema_id]
            path = self.schema_dir / filename
            wire_version = self.schemas[schema_id]["properties"]["schema_version"][
                "const"
            ]
            self.assertEqual(entry["current_wire_version"], wire_version)
            expected_wires = [wire_version]
            if schema_id == "context.authorization-audit-event":
                expected_wires = [
                    "context.authorization-audit-event/v1alpha1",
                    "context.authorization-audit-event/v2alpha1",
                ]
            self.assertEqual(entry["supported_wire_versions"], expected_wires)
            self.assertEqual(entry["artifact_path"], f"schemas/m9-05/{filename}")
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            self.assertEqual(entry["status"], "current")
            self.assertEqual(entry["compatibility_mode"], "strict-versioned")


if __name__ == "__main__":
    unittest.main()
