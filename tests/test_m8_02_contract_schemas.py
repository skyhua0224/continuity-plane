"""M8-02 typed-state v6 and migration-receipt schema contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.shared_state_migration import (
    build_typed_state_v5_to_v6_migration_receipt,
    migrate_typed_state_v5_to_v6,
)
from tests.test_m8_02_typed_state_v6_migration import _v5_snapshot


class M802ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.source = _v5_snapshot()
        cls.target = migrate_typed_state_v5_to_v6(cls.source)
        cls.receipt = build_typed_state_v5_to_v6_migration_receipt(
            source=cls.source,
            target=cls.target,
            migration_id="migration-m8-02-schema",
            source_event_head={"sequence_no": 40, "event_sha256": "e" * 64},
            registry_digest="f" * 64,
            authorization_ref="authorization://m8-02/schema",
            migrated_at="2026-08-16T12:30:00+08:00",
        )

    @classmethod
    def schema(cls, name: str) -> dict:
        path = cls.root / "schemas" / "m8-02" / f"{name}.schema.json"
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return schema

    @classmethod
    def validate(cls, name: str, instance: object) -> None:
        Draft202012Validator(cls.schema(name), format_checker=FormatChecker()).validate(
            instance
        )

    def test_migration_outputs_match_strict_schemas(self):
        instances = {
            "typed-state-v6alpha1": self.target,
            "typed-state-v5-v6-migration-receipt": self.receipt,
        }
        for name, instance in instances.items():
            with self.subTest(schema=name):
                schema = self.schema(name)
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(schema["properties"]))
                self.validate(name, instance)
                changed = copy.deepcopy(instance)
                changed["unexpected"] = True
                with self.assertRaises(ValidationError):
                    self.validate(name, changed)

    def test_v6_schema_requires_and_types_every_shared_state_field(self):
        paths = (
            ("works", "work_source_ref", 7),
            ("works", "source_revision", -1),
            ("works", "work_identity_sha256", "A" * 64),
            ("works", "dedupe_receipt_sha256", "not-a-digest"),
            ("claims", "claim_revision", -1),
            ("claims", "lease_epoch", True),
            ("claims", "last_heartbeat_at", "not-a-timestamp"),
            ("claims", "closed_at", "2026-08-16T12:00:00"),
            ("claims", "closed_by_ref", ""),
            ("claims", "close_reason", "free-form-reason"),
            ("claims", "reclaimed_from_claim_id", ""),
            ("effects", "lease_epoch", -1),
            ("effects", "dispatch_receipt_sha256", "A" * 64),
            ("effects", "dispatch_started_at", "not-a-timestamp"),
        )
        for collection, field, invalid in paths:
            with self.subTest(collection=collection, field=field):
                missing = copy.deepcopy(self.target)
                del missing[collection][0][field]
                with self.assertRaises(ValidationError):
                    self.validate("typed-state-v6alpha1", missing)
                changed = copy.deepcopy(self.target)
                changed[collection][0][field] = invalid
                with self.assertRaises(ValidationError):
                    self.validate("typed-state-v6alpha1", changed)

    def test_receipt_schema_rejects_wrong_direction_and_invalid_hashes(self):
        changed = copy.deepcopy(self.receipt)
        changed["direction"] = "rollback"
        with self.assertRaises(ValidationError):
            self.validate("typed-state-v5-v6-migration-receipt", changed)

    def test_registry_promotes_v6_v3_capabilities_and_benchmark_contracts(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entries = {entry["schema_id"]: entry for entry in registry["schemas"]}
        expected = {
            "context.typed-state": (
                "context.typed-state/v6alpha1",
                "schemas/m8-02/typed-state-v6alpha1.schema.json",
            ),
            "context.state-mcp": (
                "context.state-mcp/v3alpha1",
                "schemas/m8-02/state-mcp-v3alpha1.schema.json",
            ),
            "context.state-store-capabilities": (
                "context.state-store-capabilities/v2alpha1",
                "schemas/m8-02/state-store-capabilities-v2alpha1.schema.json",
            ),
            "context.typed-state-v5-v6-migration-receipt": (
                "context.typed-state-v5-v6-migration-receipt/v1alpha1",
                "schemas/m8-02/typed-state-v5-v6-migration-receipt.schema.json",
            ),
            "context.shared-work-benchmark": (
                "context.shared-work-benchmark/v1alpha1",
                "schemas/m8-02/shared-work-benchmark.schema.json",
            ),
        }
        for schema_id, (wire_version, relative_path) in expected.items():
            with self.subTest(schema_id=schema_id):
                entry = entries[schema_id]
                artifact = self.root / relative_path
                self.assertEqual(entry["current_wire_version"], wire_version)
                self.assertIn(wire_version, entry["supported_wire_versions"])
                self.assertEqual(entry["artifact_path"], relative_path)
                self.assertEqual(
                    entry["content_sha256"],
                    hashlib.sha256(artifact.read_bytes()).hexdigest(),
                )

        changed = copy.deepcopy(self.receipt)
        changed["receipt_sha256"] = "A" * 64
        with self.assertRaises(ValidationError):
            self.validate("typed-state-v5-v6-migration-receipt", changed)


if __name__ == "__main__":
    unittest.main()
