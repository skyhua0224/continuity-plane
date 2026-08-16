"""M8-01 wire immutability and additive request-digest release gates."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path
from typing import ClassVar

import yaml
from jsonschema import Draft202012Validator, ValidationError


class M801WireVersioningTests(unittest.TestCase):
    LEGACY_ARTIFACT_SHA256: ClassVar[dict[str, str]] = {
        "schemas/m2-01/typed-state.schema.json": (
            "7571a96ac34dce044f928312f01ba8885b91d141727e40f4c7b9899fa9009900"
        ),
        "schemas/m3-01/typed-state-v2alpha1.schema.json": (
            "3f5ac85614c5b61ba779fe23660012ac2ee9d5c6474d621c4992463a687268d3"
        ),
        "schemas/m3-05/typed-state-v3alpha1.schema.json": (
            "533626c3c5451fbb0009620e2bc857498fd4321f3b1c65023a193438a1fa9c86"
        ),
        "schemas/m3-07/typed-state-v4alpha1.schema.json": (
            "4b4917ed4c18e6298224cd92accde48e5af7d4c5ab98cd0cf9bf68c85e209f8a"
        ),
        "schemas/m2-05/state-mcp.schema.json": (
            "d80728c35556d18cc2bd8bd1f9783900b171515b143f615b033ea33314cc24ab"
        ),
    }

    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.registry = yaml.safe_load(
            (cls.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        fixture_set = yaml.safe_load(
            (cls.root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.effect_v1 = copy.deepcopy(fixture_set["cases"][0]["document"]["effects"][0])
        cls.effect_v3 = {**copy.deepcopy(cls.effect_v1), "attempt_id": None}

    def registry_entry(self, schema_id: str) -> dict:
        return next(
            entry
            for entry in self.registry["schemas"]
            if entry["schema_id"] == schema_id
        )

    def schema(self, relative_path: str) -> dict:
        path = self.root / relative_path
        self.assertTrue(path.is_file(), f"versioned schema is missing: {relative_path}")
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return schema

    def definition_validator(
        self, schema: dict, definition_name: str
    ) -> Draft202012Validator:
        definition_schema = {
            "$schema": schema["$schema"],
            "$defs": schema["$defs"],
            **schema["$defs"][definition_name],
        }
        Draft202012Validator.check_schema(definition_schema)
        return Draft202012Validator(definition_schema)

    def test_published_wire_artifacts_retain_their_release_hashes(self) -> None:
        for relative_path, expected_sha256 in self.LEGACY_ARTIFACT_SHA256.items():
            with self.subTest(path=relative_path):
                actual = hashlib.sha256(
                    (self.root / relative_path).read_bytes()
                ).hexdigest()
                self.assertEqual(actual, expected_sha256)

    def test_registry_retains_m8_01_typed_state_and_state_mcp_wires(self) -> None:
        typed_state = self.registry_entry("context.typed-state")
        self.assertIn(
            "context.typed-state/v5alpha1",
            typed_state["supported_wire_versions"],
        )
        self.assertIn(
            {
                "from": "context.typed-state/v4alpha1",
                "to": "context.typed-state/v5alpha1",
                "replay_passed": True,
                "idempotent": True,
                "rollback_ref": (
                    "docs/migrations/m8-01-durable-operation-acceptance-2026-08-16.md#rollback"
                ),
            },
            typed_state["migrations"],
        )

        state_mcp = self.registry_entry("context.state-mcp")
        self.assertIn(
            "context.state-mcp/v2alpha1",
            state_mcp["supported_wire_versions"],
        )
        self.assertIn(
            {
                "from": "context.state-mcp/v1alpha1",
                "to": "context.state-mcp/v2alpha1",
                "replay_passed": True,
                "idempotent": True,
                "rollback_ref": (
                    "docs/migrations/m8-01-durable-operation-acceptance-2026-08-16.md#rollback"
                ),
            },
            state_mcp["migrations"],
        )

    def test_legacy_typed_state_effect_wires_reject_the_new_field(self) -> None:
        fixtures = (
            ("schemas/m2-01/typed-state.schema.json", self.effect_v1),
            ("schemas/m3-01/typed-state-v2alpha1.schema.json", self.effect_v1),
            ("schemas/m3-05/typed-state-v3alpha1.schema.json", self.effect_v3),
            ("schemas/m3-07/typed-state-v4alpha1.schema.json", self.effect_v3),
        )
        for relative_path, effect in fixtures:
            with self.subTest(path=relative_path):
                schema = self.schema(relative_path)
                validator = self.definition_validator(schema, "effect")
                validator.validate(effect)
                changed = {**copy.deepcopy(effect), "request_sha256": "a" * 64}
                with self.assertRaises(ValidationError):
                    validator.validate(changed)

    def test_typed_state_v5_requires_a_nullable_request_digest(self) -> None:
        schema = self.schema("schemas/m8-01/typed-state-v5alpha1.schema.json")
        self.assertEqual(
            schema["properties"]["schema_version"]["const"],
            "context.typed-state/v5alpha1",
        )
        definition = schema["$defs"]["effect"]
        self.assertIn("request_sha256", definition["required"])
        validator = self.definition_validator(schema, "effect")
        validator.validate({**copy.deepcopy(self.effect_v3), "request_sha256": None})
        validator.validate(
            {**copy.deepcopy(self.effect_v3), "request_sha256": "a" * 64}
        )
        with self.assertRaises(ValidationError):
            validator.validate(self.effect_v3)

    def test_state_mcp_v2_separates_legacy_and_digest_bound_effect_requests(
        self,
    ) -> None:
        legacy = self.schema("schemas/m2-05/state-mcp.schema.json")
        legacy_effect = legacy["$defs"]["effect_request"]
        self.assertNotIn("request_sha256", legacy_effect["properties"])

        current = self.schema("schemas/m8-01/state-mcp-v2alpha1.schema.json")
        self.assertEqual(current["$defs"]["effect_request_v1"], legacy_effect)
        current_effect = current["$defs"]["effect_request_v2"]
        self.assertEqual(
            current_effect["properties"]["schema_version"]["const"],
            "context.state-mcp-request/v2alpha1",
        )
        self.assertIn("request_sha256", current_effect["required"])
        self.assertFalse(current_effect["additionalProperties"])

    def test_v4_v5_migration_receipt_is_strict_and_registered(self) -> None:
        entry = self.registry_entry("context.typed-state-v4-v5-migration-receipt")
        self.assertEqual(
            entry["current_wire_version"],
            "context.typed-state-v4-v5-migration-receipt/v1alpha1",
        )
        self.assertEqual(
            entry["artifact_path"],
            "schemas/m8-01/typed-state-v4-v5-migration-receipt.schema.json",
        )
        schema = self.schema(entry["artifact_path"])
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        validator = Draft202012Validator(schema)
        receipt = {
            "schema_version": ("context.typed-state-v4-v5-migration-receipt/v1alpha1"),
            "migration_id": "migration-m8-01-v4-v5",
            "project_id": "project-m8-01",
            "direction": "upgrade",
            "from_schema_version": "context.typed-state/v4alpha1",
            "to_schema_version": "context.typed-state/v5alpha1",
            "source_revision": 27,
            "source_event_head": {
                "sequence_no": 27,
                "event_sha256": "a" * 64,
            },
            "source_snapshot_sha256": "b" * 64,
            "target_snapshot_sha256": "c" * 64,
            "algorithm_id": "context.typed-state.effect-request-digest.v4-v5/v1",
            "algorithm_sha256": "d" * 64,
            "registry_digest": "e" * 64,
            "authorization_ref": "authorization://m8-01/state-v5",
            "status": "committed",
            "migrated_at": "2026-08-16T12:30:00+08:00",
            "receipt_sha256": "f" * 64,
        }
        validator.validate(receipt)
        validator.validate({**copy.deepcopy(receipt), "source_event_head": None})
        rollback = {
            **copy.deepcopy(receipt),
            "direction": "rollback",
            "from_schema_version": "context.typed-state/v5alpha1",
            "to_schema_version": "context.typed-state/v4alpha1",
        }
        validator.validate(rollback)

        invalid_receipts = (
            {**copy.deepcopy(receipt), "direction": "rollback"},
            {
                **copy.deepcopy(rollback),
                "from_schema_version": "context.typed-state/v4alpha1",
            },
            {
                **copy.deepcopy(receipt),
                "source_event_head": {"sequence_no": 0, "event_sha256": None},
            },
            {**copy.deepcopy(receipt), "unexpected": True},
        )
        for invalid in invalid_receipts:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                validator.validate(invalid)


if __name__ == "__main__":
    unittest.main()
