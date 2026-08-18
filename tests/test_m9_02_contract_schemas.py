"""M9-02 Project Graph projection strict schema and registry tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
)
from context_control_plane.idea_continuity_benchmark import build_idea_snapshot
from context_control_plane.project_graph_projection import (
    build_project_graph_projection,
)
from context_control_plane.project_graph_projection_benchmark import (
    benchmark_project_graph_projection,
)
from context_control_plane.state_mcp import RequestContext

SCHEMAS = {
    "context.project-graph-projection": "project-graph-projection.schema.json",
    "context.project-graph-projection-benchmark": (
        "project-graph-projection-benchmark.schema.json"
    ),
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
                "capabilities": {"adapter_id": "context.schema-test"},
            },
            "error": None,
        }


class M902ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas/m9-02"
        cls.schemas = {
            schema_id: json.loads(
                (cls.schema_dir / filename).read_text(encoding="utf-8")
            )
            for schema_id, filename in SCHEMAS.items()
        }
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)

        snapshot = build_idea_snapshot()
        cls.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-02-schema",
            secret=b"m9-02-project-graph-schema-key",
        )
        response = ExternalStateProjectionProvider(
            _Source(snapshot),
            provider_id="provider-m9-02-schema",
            signer=cls.signer,
        ).call_tool(
            EXTERNAL_READ_TOOL,
            {
                "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
                "request_id": "request-m9-02-schema",
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": snapshot["project"]["revision"],
            },
            context=RequestContext("actor-schema", "authorization-schema"),
        )
        cls.samples = {
            "context.project-graph-projection": build_project_graph_projection(
                response["result"],
                signer=cls.signer,
                observed_at="2026-08-17T20:00:00+08:00",
            ),
            "context.project-graph-projection-benchmark": (
                benchmark_project_graph_projection(
                    root=cls.root,
                    iterations=2,
                    generated_at="2026-08-17T20:00:00+08:00",
                )
            ),
        }

    def validator(self, schema_id: str) -> Draft202012Validator:
        return Draft202012Validator(
            self.schemas[schema_id],
            format_checker=FormatChecker(),
        )

    def test_runtime_documents_match_strict_schemas(self) -> None:
        for schema_id, sample in self.samples.items():
            with self.subTest(schema_id=schema_id):
                self.validator(schema_id).validate(sample)

    def test_top_level_and_nested_contracts_are_strict(self) -> None:
        for schema_id, sample in self.samples.items():
            schema = self.schemas[schema_id]
            validator = self.validator(schema_id)
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            with self.subTest(schema_id=schema_id), self.assertRaises(
                ValidationError
            ):
                validator.validate({**sample, "unexpected": True})
            for field in sample:
                missing = copy.deepcopy(sample)
                del missing[field]
                with self.subTest(schema_id=schema_id, field=field), self.assertRaises(
                    ValidationError
                ):
                    validator.validate(missing)

        projection = copy.deepcopy(
            self.samples["context.project-graph-projection"]
        )
        nested = (
            projection["graph"]["nodes"][0],
            projection["active_work_set"][0],
            projection["active_work_set"][0]["claim"],
            projection["work_ledger"],
            projection["work_ledger"]["capabilities"],
            projection["health"],
            projection["authority"],
        )
        validator = self.validator("context.project-graph-projection")
        for item in nested:
            forged = copy.deepcopy(projection)
            target = next(
                candidate
                for candidate in (
                    forged["graph"]["nodes"][0],
                    forged["active_work_set"][0],
                    forged["active_work_set"][0]["claim"],
                    forged["work_ledger"],
                    forged["work_ledger"]["capabilities"],
                    forged["health"],
                    forged["authority"],
                )
                if set(candidate) == set(item)
            )
            target["unexpected"] = True
            with self.assertRaises(ValidationError):
                validator.validate(forged)

    def test_authority_and_health_types_are_exact(self) -> None:
        projection = self.samples["context.project-graph-projection"]
        validator = self.validator("context.project-graph-projection")
        for field, value in (
            ("state_write_authority", True),
            ("controlled_action_authority", True),
            ("provider_authority", False),
            ("external_effect_authority", 1),
        ):
            forged = copy.deepcopy(projection)
            forged["authority"][field] = value
            with self.subTest(field=field), self.assertRaises(ValidationError):
                validator.validate(forged)

        forged = copy.deepcopy(projection)
        forged["health"]["snapshot_age_ms"] = True
        with self.assertRaises(ValidationError):
            validator.validate(forged)

        forged = copy.deepcopy(projection)
        forged["health"]["claim_ownership_overlaps"] = [
            {
                "left_claim_id": "claim-left",
                "left_work_id": "work-left",
                "right_claim_id": "claim-right",
                "right_work_id": "work-right",
                "left_scope": {
                    "scope_kind": "capability",
                    "scope_ref": "shared",
                },
                "right_scope": {
                    "scope_kind": "capability",
                    "scope_ref": "shared",
                },
            }
        ]
        with self.assertRaises(ValidationError):
            validator.validate(forged)

        benchmark = copy.deepcopy(
            self.samples["context.project-graph-projection-benchmark"]
        )
        benchmark["gate"] = {
            "status": "failed",
            "failed_gates": ["same-revision"],
        }
        with self.assertRaises(ValidationError):
            self.validator(
                "context.project-graph-projection-benchmark"
            ).validate(benchmark)

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
            self.assertEqual(entry["artifact_path"], f"schemas/m9-02/{filename}")
            self.assertEqual(
                entry["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            self.assertEqual(entry["status"], "current")
            self.assertEqual(entry["compatibility_mode"], "strict-versioned")


if __name__ == "__main__":
    unittest.main()
