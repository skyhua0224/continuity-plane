"""M9-01 external State projection strict schema and registry tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from context_control_plane.external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
)
from context_control_plane.external_state_provider_benchmark import (
    benchmark_external_state_projection,
)
from context_control_plane.state_mcp import RequestContext

SCHEMAS = {
    "context.external-state-request": "external-state-request.schema.json",
    "context.external-state-projection": "external-state-projection.schema.json",
    "context.external-state-response": "external-state-response.schema.json",
    "context.external-state-projection-benchmark": "external-state-projection-benchmark.schema.json",
}


class _Source:
    def __init__(self, snapshot: dict) -> None:
        self.snapshot = copy.deepcopy(snapshot)

    def call_tool(
        self,
        tool: str,
        arguments: dict,
        *,
        context: RequestContext,
    ) -> dict:
        return {
            "schema_version": "context.state-mcp-response/v1alpha1",
            "request_id": arguments["request_id"],
            "tool": "context.state.read",
            "ok": True,
            "result": {
                "snapshot": copy.deepcopy(self.snapshot),
                "revision": self.snapshot["project"]["revision"],
                "event_head": None,
                "registry_digest": "a" * 64,
                "capabilities": {"adapter_id": "context.sqlite"},
            },
            "error": None,
        }


class M901ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas/m9-01"
        cls.schemas = {
            schema_id: json.loads((cls.schema_dir / filename).read_text(encoding="utf-8"))
            for schema_id, filename in SCHEMAS.items()
        }
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)
        cls.registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema))
            for schema in cls.schemas.values()
        )
        for relative in (
            "m2-01/typed-state.schema.json",
            "m3-01/typed-state-v2alpha1.schema.json",
            "m3-05/typed-state-v3alpha1.schema.json",
            "m3-07/typed-state-v4alpha1.schema.json",
            "m8-01/typed-state-v5alpha1.schema.json",
            "m8-02/typed-state-v6alpha1.schema.json",
        ):
            schema = json.loads(
                (cls.root / "schemas" / relative).read_text(encoding="utf-8")
            )
            cls.registry = cls.registry.with_resource(
                f"https://context-control-plane.invalid/schemas/{relative}",
                Resource.from_contents(schema),
            )

        fixture_set = yaml.safe_load(
            (cls.root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        snapshot = copy.deepcopy(
            next(
                case["document"]
                for case in fixture_set["cases"]
                if case["case_id"] == "completed-work-overlap-blocked"
            )
        )
        cls.request = {
            "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
            "request_id": "request-m9-01-schema",
            "project_id": snapshot["project"]["project_id"],
            "expected_revision": snapshot["project"]["revision"],
        }
        cls.response = ExternalStateProjectionProvider(
            _Source(snapshot),
            provider_id="provider-docmost-reference",
            signer=HMACExternalStateProjectionSigner(
                key_id="key-m9-01-schema",
                secret=b"m9-01-schema-signing-key-material",
            ),
        ).call_tool(
            EXTERNAL_READ_TOOL,
            cls.request,
            context=RequestContext("actor-reader", "authorization-reader"),
        )
        cls.samples = {
            "context.external-state-request": cls.request,
            "context.external-state-projection": cls.response["result"],
            "context.external-state-response": cls.response,
            "context.external-state-projection-benchmark": benchmark_external_state_projection(
                root=cls.root,
                iterations=2,
                generated_at="2026-08-17T17:00:00+08:00",
            ),
        }

    def validator(self, schema_id: str) -> Draft202012Validator:
        return Draft202012Validator(
            self.schemas[schema_id],
            format_checker=FormatChecker(),
            registry=self.registry,
        )

    def test_runtime_documents_match_strict_schemas(self) -> None:
        for schema_id, sample in self.samples.items():
            with self.subTest(schema_id=schema_id):
                self.validator(schema_id).validate(sample)

    def test_all_top_level_fields_are_required_and_unknown_fields_rejected(self) -> None:
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

    def test_authority_and_revision_mutations_are_rejected(self) -> None:
        projection_validator = self.validator("context.external-state-projection")
        projection = self.samples["context.external-state-projection"]
        mutations = (
            ("state_write_authority", True),
            ("controlled_action_authority", True),
            ("provider_authority", 1),
            ("external_effect_authority", 1),
        )
        for field, value in mutations:
            forged = copy.deepcopy(projection)
            forged[field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                projection_validator.validate(forged)

        request_validator = self.validator("context.external-state-request")
        for value in (True, -1, 1.5):
            forged = {**self.request, "expected_revision": value}
            with self.subTest(expected_revision=value), self.assertRaises(ValidationError):
                request_validator.validate(forged)
        request_validator.validate({**self.request, "expected_revision": None})

    def test_projection_schema_rejects_invalid_typed_snapshots(self) -> None:
        validator = self.validator("context.external-state-projection")
        projection = self.samples["context.external-state-projection"]
        mutations = [
            lambda snapshot: snapshot.pop("works"),
            lambda snapshot: snapshot.__setitem__("unexpected", []),
            lambda snapshot: snapshot["works"][0].__setitem__("status", "unknown"),
        ]
        for mutate in mutations:
            forged = copy.deepcopy(projection)
            mutate(forged["snapshot"])
            with self.assertRaises(ValidationError):
                validator.validate(forged)

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
            self.assertEqual(entry["supported_wire_versions"], [wire_version])
            self.assertEqual(entry["artifact_path"], f"schemas/m9-01/{filename}")
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            self.assertEqual(entry["status"], "current")
            self.assertEqual(entry["compatibility_mode"], "strict-versioned")


if __name__ == "__main__":
    unittest.main()
