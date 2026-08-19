"""Strict provider capability/event and recovery contract registration."""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker

from tests.test_m10_11_provider_continuity_adapter import (
    M1011ProviderContinuityAdapterTests,
)
from tests.test_m10_11_recovery_envelope import M1011RecoveryEnvelopeTests


class M1011ProviderContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas/m10-11"
        cls.schemas = {
            "context.provider-continuity-capability": "provider-continuity-capability.schema.json",
            "context.provider-continuity-event": "provider-continuity-event.schema.json",
            "context.interaction-cursor": "interaction-cursor.schema.json",
            "context.recovery-envelope": "recovery-envelope.schema.json",
        }

    def test_runtime_provider_and_cursor_documents_match_strict_schemas(self) -> None:
        provider = M1011ProviderContinuityAdapterTests(
            "test_explicit_lifecycle_derives_usage_boundary_reads_and_first_action"
        )
        recovery = M1011RecoveryEnvelopeTests(
            "test_resume_emits_checkpoint_bound_bounded_recovery_envelope"
        )
        documents = {
            "context.provider-continuity-capability": provider._manifest(),
            "context.provider-continuity-event": provider._events()[0],
            "context.interaction-cursor": recovery._cursor(),
        }
        for schema_id in documents:
            schema = json.loads(
                (self.schema_dir / self.schemas[schema_id]).read_text(encoding="utf-8")
            )
            with self.subTest(schema_id=schema_id):
                Draft202012Validator.check_schema(schema)
                Draft202012Validator(
                    schema, format_checker=FormatChecker()
                ).validate(documents[schema_id])

    def test_registry_binds_every_m10_11_provider_and_recovery_schema(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {item["schema_id"]: item for item in registry["schemas"]}
        for schema_id, filename in self.schemas.items():
            path = self.schema_dir / filename
            with self.subTest(schema_id=schema_id):
                self.assertIn(schema_id, entries)
                self.assertEqual(entries[schema_id]["artifact_path"], f"schemas/m10-11/{filename}")
                self.assertEqual(
                    entries[schema_id]["content_sha256"],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )


if __name__ == "__main__":
    unittest.main()
