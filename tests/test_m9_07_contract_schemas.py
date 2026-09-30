"""M9-07 strict schema and registry contract tests."""

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
from context_control_plane.relationship_impact_benchmark import (
    benchmark_relationship_impact_projection,
)
from context_control_plane.relationship_impact_projection import (
    build_relationship_impact_projection,
)
from context_control_plane.state_mcp import RequestContext
from tests.test_m9_07_relationship_impact_projection import _Source


class M907ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-07-schema",
            secret=b"m9-07-contract-schema-key",
        )
        cls.request_schema_path = (
            cls.root / "schemas/m9-07/relationship-impact-view-request.schema.json"
        )
        cls.projection_schema_path = (
            cls.root / "schemas/m9-07/relationship-impact-projection.schema.json"
        )
        cls.benchmark_schema_path = (
            cls.root / "schemas/m9-07/relationship-impact-benchmark.schema.json"
        )

    def runtime_documents(self) -> tuple[dict, dict]:
        snapshot = build_idea_snapshot()
        provider = ExternalStateProjectionProvider(
            _Source(snapshot),
            provider_id="provider-m9-07-schema",
            signer=self.signer,
        )
        response = provider.call_tool(
            EXTERNAL_READ_TOOL,
            {
                "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
                "request_id": "request-m9-07-schema",
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": snapshot["project"]["revision"],
            },
            context=RequestContext("actor-reader", "authorization-reader"),
        )
        self.assertTrue(response["ok"], response["error"])
        graph = build_project_graph_projection(
            response["result"],
            signer=self.signer,
            observed_at="2026-08-17T18:00:00+08:00",
        )
        request = {
            "schema_version": "context.relationship-impact-view-request/v1alpha1",
            "project_id": snapshot["project"]["project_id"],
            "expected_state_revision": snapshot["project"]["revision"],
            "focus_node_ids": [],
            "direction": "both",
            "max_depth": 2,
            "relation_kinds": ["dependency", "parent"],
            "node_kinds": ["work"],
            "include_terminal_work": True,
            "max_nodes": 64,
            "max_edges": 128,
        }
        projection = build_relationship_impact_projection(
            graph,
            view_request=request,
            signer=self.signer,
        )
        return request, projection

    def schema(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def validate(self, schema: dict, document: dict) -> None:
        Draft202012Validator(
            schema,
            format_checker=FormatChecker(),
        ).validate(document)

    def test_runtime_documents_match_strict_schemas(self) -> None:
        request, projection = self.runtime_documents()
        self.validate(self.schema(self.request_schema_path), request)
        self.validate(self.schema(self.projection_schema_path), projection)

    def test_top_level_and_authority_contracts_are_strict(self) -> None:
        request, projection = self.runtime_documents()
        request_schema = self.schema(self.request_schema_path)
        projection_schema = self.schema(self.projection_schema_path)
        mutations = [
            (request_schema, {**request, "state_patch": {}}),
            (request_schema, {**request, "max_depth": 9}),
            (projection_schema, {**projection, "state_patch": {}}),
        ]
        authority = copy.deepcopy(projection)
        authority["authority"]["state_write_authority"] = True
        mutations.append((projection_schema, authority))
        truncated = copy.deepcopy(projection)
        truncated["completeness"]["truncated"] = True
        mutations.append((projection_schema, truncated))
        for schema, document in mutations:
            with self.subTest(document=document), self.assertRaises(ValidationError):
                self.validate(schema, document)

    def test_benchmark_runtime_matches_strict_schema(self) -> None:
        receipt = benchmark_relationship_impact_projection(
            root=self.root,
            iterations=1,
            scale_iterations=1,
            generated_at="2026-08-17T23:30:00+08:00",
        )
        self.validate(self.schema(self.benchmark_schema_path), receipt)

    def test_registry_entries_bind_exact_schema_hashes(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {item["schema_id"]: item for item in registry["schemas"]}
        expected = {
            "context.relationship-impact-view-request": self.request_schema_path,
            "context.relationship-impact-projection": self.projection_schema_path,
            "context.relationship-impact-benchmark": self.benchmark_schema_path,
        }
        for schema_id, path in expected.items():
            with self.subTest(schema_id=schema_id):
                entry = entries[schema_id]
                self.assertEqual(
                    entry["content_sha256"],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )


if __name__ == "__main__":
    unittest.main()
