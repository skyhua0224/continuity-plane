"""M8-05 strict authorization and audit schema admission."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError

from context_control_plane.authorization_audit import (
    InMemoryAuthorizationAuditStore,
    TenantProjectAuthorizer,
    authorization_policy_sha256,
)
from context_control_plane.authorization_audit_benchmark import (
    benchmark_authorization_isolation,
)
from context_control_plane.state_mcp import RequestContext
from tests.test_m8_05_authorization_audit import NOW, _policy

SCHEMAS = {
    "context.authorization-policy": "authorization-policy.schema.json",
    "context.authorization-audit-event": "authorization-audit-event.schema.json",
    "context.authorization-isolation-benchmark": (
        "authorization-isolation-benchmark.schema.json"
    ),
}


class M805ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas" / "m8-05"
        cls.schemas = {
            schema_id: json.loads((cls.schema_dir / filename).read_text(encoding="utf-8"))
            for schema_id, filename in SCHEMAS.items()
        }
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)

    @staticmethod
    def samples() -> dict[str, dict]:
        policy = _policy()
        store = InMemoryAuthorizationAuditStore()
        authorizer = TenantProjectAuthorizer(
            policy,
            expected_policy_sha256=authorization_policy_sha256(policy),
            audit_store=store,
            clock=lambda: NOW,
        )
        assert authorizer.authorize(
            RequestContext(
                "actor-a", "authorization://tenant-a/actor-a/revision-3"
            ),
            "state.read",
            "project-a",
        )
        return {
            "context.authorization-policy": policy,
            "context.authorization-audit-event": store.read_events()[0],
            "context.authorization-isolation-benchmark": (
                benchmark_authorization_isolation(
                    root=Path(__file__).resolve().parents[1],
                    samples=1,
                    generated_at="2026-08-16T12:30:00+00:00",
                )
            ),
        }

    def test_runtime_documents_match_strict_schemas(self) -> None:
        for schema_id, sample in self.samples().items():
            with self.subTest(schema_id=schema_id):
                Draft202012Validator(self.schemas[schema_id]).validate(sample)

    def test_all_top_level_fields_are_required_and_unknown_fields_are_rejected(
        self,
    ) -> None:
        for schema_id, sample in self.samples().items():
            validator = Draft202012Validator(self.schemas[schema_id])
            self.assertFalse(self.schemas[schema_id]["additionalProperties"])
            self.assertEqual(
                set(self.schemas[schema_id]["required"]),
                set(self.schemas[schema_id]["properties"]),
            )
            extra = {**sample, "unexpected": True}
            with (
                self.subTest(schema_id=schema_id, mutation="extra"),
                self.assertRaises(ValidationError),
            ):
                validator.validate(extra)
            for field in sample:
                missing = copy.deepcopy(sample)
                del missing[field]
                with (
                    self.subTest(schema_id=schema_id, missing=field),
                    self.assertRaises(ValidationError),
                ):
                    validator.validate(missing)

    def test_registry_hashes_match_m8_05_schema_bytes(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entries = {
            item["schema_id"]: item
            for item in registry["schemas"]
            if item["schema_id"] in SCHEMAS
        }
        self.assertEqual(set(entries), set(SCHEMAS))
        for schema_id, filename in SCHEMAS.items():
            path = self.schema_dir / filename
            with self.subTest(schema_id=schema_id):
                if schema_id == "context.authorization-audit-event":
                    current_path = (
                        self.root
                        / "schemas/m9-05/governance-authorization-audit-event.schema.json"
                    )
                    self.assertEqual(
                        entries[schema_id]["current_wire_version"],
                        "context.authorization-audit-event/v2alpha1",
                    )
                    self.assertEqual(
                        entries[schema_id]["supported_wire_versions"],
                        [
                            "context.authorization-audit-event/v1alpha1",
                            "context.authorization-audit-event/v2alpha1",
                        ],
                    )
                    self.assertEqual(
                        entries[schema_id]["artifact_path"],
                        current_path.relative_to(self.root).as_posix(),
                    )
                    self.assertEqual(
                        entries[schema_id]["content_sha256"],
                        hashlib.sha256(current_path.read_bytes()).hexdigest(),
                    )
                    self.assertEqual(
                        hashlib.sha256(path.read_bytes()).hexdigest(),
                        "c303e87dfba8a03ecc5fb0203c2f01c68866c6d2118a95013b9f069c7c1798b4",
                    )
                    continue
                self.assertEqual(
                    entries[schema_id]["current_wire_version"],
                    f"{schema_id}/v1alpha1",
                )
                self.assertEqual(
                    entries[schema_id]["artifact_path"],
                    path.relative_to(self.root).as_posix(),
                )
                self.assertEqual(
                    entries[schema_id]["content_sha256"],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )


if __name__ == "__main__":
    unittest.main()
