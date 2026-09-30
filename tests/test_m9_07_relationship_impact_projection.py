"""M9-07 read-only Relationship and Impact projection behavior tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

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
from context_control_plane.relationship_impact_projection import (
    RELATIONSHIP_IMPACT_PROJECTION_SCHEMA_VERSION,
    RelationshipImpactProjectionError,
    build_relationship_impact_projection,
    validate_relationship_impact_projection,
)
from context_control_plane.state_mcp import RequestContext


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
                "capabilities": {"adapter_id": "context.test"},
            },
            "error": None,
        }


class M907RelationshipImpactProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-07-test",
            secret=b"m9-07-relationship-impact-test-key",
        )

    def project_graph(self, snapshot: dict) -> dict:
        provider = ExternalStateProjectionProvider(
            _Source(snapshot),
            provider_id="provider-m9-07-test",
            signer=self.signer,
        )
        response = provider.call_tool(
            EXTERNAL_READ_TOOL,
            {
                "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
                "request_id": "request-m9-07-relationship-impact",
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": snapshot["project"]["revision"],
            },
            context=RequestContext("actor-reader", "authorization-reader"),
        )
        self.assertTrue(response["ok"], response["error"])
        return build_project_graph_projection(
            response["result"],
            signer=self.signer,
            observed_at="2026-08-17T18:00:00+08:00",
        )

    def view_request(
        self,
        snapshot: dict,
        **overrides,
    ) -> dict:
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
        request.update(overrides)
        return request

    def resign(self, projection: dict) -> None:
        unsigned = {
            key: value
            for key, value in projection.items()
            if key not in {"projection_sha256", "signature"}
        }
        projection["projection_sha256"] = hashlib.sha256(
            json.dumps(
                unsigned,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        projection["signature"] = self.signer.sign(projection)

    def test_same_revision_focus_is_deterministic_and_read_only(self) -> None:
        snapshot = build_idea_snapshot()
        by_id = {item["work_id"]: item for item in snapshot["works"]}
        by_id["work-target"]["dependency_ids"] = ["work-active"]
        project_graph = self.project_graph(snapshot)
        view_request = self.view_request(
            snapshot,
            focus_node_ids=["work:work-active"],
        )

        projection = build_relationship_impact_projection(
            project_graph,
            view_request=view_request,
            signer=self.signer,
        )

        self.assertEqual(
            projection["schema_version"],
            RELATIONSHIP_IMPACT_PROJECTION_SCHEMA_VERSION,
        )
        self.assertEqual(projection["state_revision"], snapshot["project"]["revision"])
        self.assertEqual(
            projection["project_graph_projection_sha256"],
            project_graph["projection_sha256"],
        )
        self.assertEqual(
            {item["node_id"] for item in projection["nodes"]},
            {"work:campaign", "work:goal", "work:work-active", "work:work-target"},
        )
        self.assertEqual(
            projection["impact"]["direct_dependent_work_ids"], ["work-target"]
        )
        self.assertEqual(projection["impact"]["dependency_work_ids"], [])
        self.assertEqual(projection["authority"]["state_write_authority"], False)
        self.assertEqual(projection["authority"]["controlled_action_authority"], False)
        self.assertEqual(projection["authority"]["approval_authority"], False)
        self.assertEqual(projection["authority"]["completion_authority"], False)
        validate_relationship_impact_projection(
            projection,
            project_graph_projection=project_graph,
            view_request=view_request,
            signer=self.signer,
        )

    def test_verified_code_clue_uses_an_independent_revision_clock(self) -> None:
        snapshot = build_idea_snapshot()
        project_graph = self.project_graph(snapshot)
        receipt_path = (
            self.root / "experiments/retrieval/m6-02-codegraph-verification.json"
        )
        codegraph = json.loads(receipt_path.read_text(encoding="utf-8"))
        view_request = self.view_request(
            snapshot,
            relation_kinds=["parent", "references"],
            node_kinds=["symbol", "work"],
        )

        projection = build_relationship_impact_projection(
            project_graph,
            view_request=view_request,
            signer=self.signer,
            codegraph_receipts=[codegraph],
            codegraph_roots={codegraph["receipt_sha256"]: self.root},
        )

        symbols = [
            node for node in projection["nodes"] if node["node_kind"] == "symbol"
        ]
        code_edges = [
            edge for edge in projection["edges"] if edge["relation"] == "references"
        ]
        self.assertEqual(len(symbols), 2)
        self.assertEqual(len(code_edges), 1)
        self.assertEqual(
            code_edges[0]["authority_class"], "verified-non-authoritative-clue"
        )
        self.assertEqual(
            projection["code_sources"][0]["receipt_sha256"],
            codegraph["receipt_sha256"],
        )
        self.assertEqual(
            projection["code_sources"][0]["index_revisions"],
            ["worktree:m6-codegraph-probe"],
        )
        self.assertIsNone(projection["code_sources"][0]["state_revision_binding"])
        validate_relationship_impact_projection(
            projection,
            project_graph_projection=project_graph,
            view_request=view_request,
            signer=self.signer,
            codegraph_receipts=[codegraph],
            codegraph_roots={codegraph["receipt_sha256"]: self.root},
        )

    def test_invalid_filters_focus_and_capacity_fail_closed(self) -> None:
        snapshot = build_idea_snapshot()
        project_graph = self.project_graph(snapshot)
        invalid_requests = [
            self.view_request(snapshot, expected_state_revision=999),
            self.view_request(snapshot, focus_node_ids=["work:missing"]),
            self.view_request(snapshot, max_depth=9),
            self.view_request(snapshot, max_nodes=1),
            self.view_request(snapshot, relation_kinds=["arbitrary"]),
            {**self.view_request(snapshot), "state_patch": {}},
        ]
        for request in invalid_requests:
            with (
                self.subTest(request=request),
                self.assertRaises(RelationshipImpactProjectionError),
            ):
                build_relationship_impact_projection(
                    project_graph,
                    view_request=request,
                    signer=self.signer,
                )

    def test_rebuild_rejects_resigned_output_tampering(self) -> None:
        snapshot = build_idea_snapshot()
        project_graph = self.project_graph(snapshot)
        request = self.view_request(snapshot)
        projection = build_relationship_impact_projection(
            project_graph,
            view_request=request,
            signer=self.signer,
        )
        projection["nodes"][0]["title"] = "forged"
        self.resign(projection)

        with self.assertRaisesRegex(
            RelationshipImpactProjectionError,
            "does not match signed Project Graph",
        ):
            validate_relationship_impact_projection(
                projection,
                project_graph_projection=project_graph,
                view_request=request,
                signer=self.signer,
            )

    def test_request_set_order_does_not_change_canonical_projection(self) -> None:
        snapshot = build_idea_snapshot()
        project_graph = self.project_graph(snapshot)
        first = self.view_request(
            snapshot,
            relation_kinds=["dependency", "parent"],
        )
        second = self.view_request(
            snapshot,
            relation_kinds=["parent", "dependency"],
        )

        self.assertEqual(
            build_relationship_impact_projection(
                project_graph,
                view_request=first,
                signer=self.signer,
            ),
            build_relationship_impact_projection(
                project_graph,
                view_request=second,
                signer=self.signer,
            ),
        )


if __name__ == "__main__":
    unittest.main()
