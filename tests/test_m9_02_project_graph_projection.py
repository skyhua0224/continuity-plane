"""M9-02 Project Graph and active Work projection behavior tests."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from pathlib import Path

import yaml

from context_control_plane.external_state_provider import (
    EXTERNAL_READ_TOOL,
    EXTERNAL_REQUEST_SCHEMA_VERSION,
    ExternalStateProjectionProvider,
    HMACExternalStateProjectionSigner,
)
from context_control_plane.idea_continuity_benchmark import build_idea_snapshot
from context_control_plane.project_graph_projection import (
    PROJECT_GRAPH_PROJECTION_SCHEMA_VERSION,
    ProjectGraphProjectionError,
    build_project_graph_projection,
    validate_project_graph_projection,
)
from context_control_plane.shared_state_migration import migrate_typed_state_v5_to_v6
from context_control_plane.state_mcp import RequestContext
from tests.test_m8_02_typed_state_v6_migration import _v5_snapshot


def _resign_projection(
    projection: dict,
    signer: HMACExternalStateProjectionSigner,
) -> None:
    body = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    projection["projection_sha256"] = hashlib.sha256(
        json.dumps(
            body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    projection["signature"] = signer.sign(
        {**body, "projection_sha256": projection["projection_sha256"]}
    )


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


class M902ProjectGraphProjectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        fixture_set = yaml.safe_load(
            (cls.root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.legacy_team = copy.deepcopy(
            next(
                case["document"]
                for case in fixture_set["cases"]
                if case["case_id"] == "multi-worker-disjoint-scopes"
            )
        )
        cls.legacy_duplicate = copy.deepcopy(
            next(
                case["document"]
                for case in fixture_set["cases"]
                if case["case_id"] == "completed-work-overlap-blocked"
            )
        )
        cls.signer = HMACExternalStateProjectionSigner(
            key_id="key-m9-02-test",
            secret=b"m9-02-project-graph-test-key",
        )

    def external_projection(self, snapshot: dict) -> dict:
        provider = ExternalStateProjectionProvider(
            _Source(snapshot),
            provider_id="provider-m9-02-test",
            signer=self.signer,
        )
        response = provider.call_tool(
            EXTERNAL_READ_TOOL,
            {
                "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
                "request_id": "request-m9-02-project-graph",
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": snapshot["project"]["revision"],
            },
            context=RequestContext("actor-reader", "authorization-reader"),
        )
        self.assertTrue(response["ok"], response["error"])
        return response["result"]

    def test_same_revision_projection_contains_graph_active_set_and_ledger(self) -> None:
        snapshot = build_idea_snapshot()
        source = self.external_projection(snapshot)

        projection = build_project_graph_projection(
            source,
            signer=self.signer,
            observed_at="2026-08-17T18:00:00+08:00",
        )

        self.assertEqual(
            projection["schema_version"], PROJECT_GRAPH_PROJECTION_SCHEMA_VERSION
        )
        self.assertEqual(projection["project_id"], snapshot["project"]["project_id"])
        self.assertEqual(projection["state_revision"], snapshot["project"]["revision"])
        self.assertEqual(
            projection["state_schema_version"], snapshot["schema_version"]
        )
        self.assertEqual(projection["state_sha256"], source["state_sha256"])
        self.assertEqual(
            projection["source_projection_sha256"], source["projection_sha256"]
        )
        self.assertEqual(
            {item["work_id"] for item in projection["active_work_set"]},
            set(snapshot["project"]["active_work_ids"]),
        )
        active = projection["active_work_set"][0]
        self.assertEqual(active["claim"]["claim_id"], "claim-active")
        self.assertTrue(active["claim"]["lease_expired_at_observation"])
        self.assertEqual(active["owner_refs"], ["actor-owner"])
        self.assertEqual(projection["primary_work_id"], "work-active")
        self.assertEqual(
            projection["work_ledger"]["work_count"], len(snapshot["works"])
        )
        self.assertEqual(projection["work_ledger"]["active_work_count"], 1)
        self.assertEqual(projection["health"]["cycle_work_ids"], [])
        self.assertEqual(projection["health"]["orphan_work_ids"], [])
        self.assertEqual(
            projection["health"]["expired_active_claim_ids"], ["claim-active"]
        )
        self.assertEqual(projection["health"]["claim_ownership_overlaps"], [])
        self.assertEqual(
            projection["work_ledger"]["capabilities"],
            {"claim_fencing": False, "work_provenance": False},
        )
        self.assertFalse(projection["authority"]["state_write_authority"])
        self.assertEqual(projection["authority"]["provider_authority"], 0)
        validate_project_graph_projection(
            projection,
            source_projection=source,
            signer=self.signer,
        )

    def test_legacy_cycle_and_orphan_are_explicit_health_findings(self) -> None:
        snapshot = copy.deepcopy(self.legacy_team)
        snapshot["works"][0]["parent_work_id"] = snapshot["works"][1]["work_id"]
        snapshot["works"][1]["parent_work_id"] = snapshot["works"][0]["work_id"]
        source = self.external_projection(snapshot)

        projection = build_project_graph_projection(
            source,
            signer=self.signer,
            observed_at="2026-08-09T11:00:00+08:00",
        )

        work_ids = sorted(item["work_id"] for item in snapshot["works"])
        self.assertEqual(projection["health"]["cycle_work_ids"], work_ids)
        self.assertEqual(projection["health"]["orphan_work_ids"], work_ids)
        self.assertEqual(projection["graph"]["root_work_ids"], [])

        orphan_only = copy.deepcopy(self.legacy_team)
        orphan_projection = build_project_graph_projection(
            self.external_projection(orphan_only),
            signer=self.signer,
            observed_at="2026-08-09T11:00:00+08:00",
        )
        self.assertEqual(orphan_projection["health"]["cycle_work_ids"], [])
        self.assertEqual(orphan_projection["health"]["orphan_work_ids"], work_ids)

    def test_expired_experiment_and_scope_overlap_are_visible(self) -> None:
        snapshot = build_idea_snapshot()
        experiment = copy.deepcopy(
            next(item for item in snapshot["works"] if item["work_id"] == "work-target")
        )
        experiment.update(
            {
                "work_id": "experiment-expired",
                "kind": "experiment",
                "title": "Expired experimental branch",
                "status": "ready",
                "parent_work_id": "work-target",
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "experiment-expired"}
                ],
                "return_point_work_id": "work-target",
                "exit_criteria": ["record evidence"],
                "attempt_budget": 1,
                "expires_at": "2026-08-16T00:00:00+08:00",
                "promotion_target_work_id": "work-target",
                "mainline_authority": False,
            }
        )
        snapshot["works"].append(experiment)
        source = self.external_projection(snapshot)
        projection = build_project_graph_projection(
            source,
            signer=self.signer,
            observed_at="2026-08-17T18:00:00+08:00",
        )
        self.assertEqual(
            projection["health"]["expired_branch_work_ids"],
            ["experiment-expired"],
        )
        experiment_node = next(
            item
            for item in projection["graph"]["nodes"]
            if item["work_id"] == "experiment-expired"
        )
        self.assertEqual(experiment_node["return_point_work_id"], "work-target")
        self.assertEqual(
            experiment_node["promotion_target_work_id"], "work-target"
        )
        self.assertEqual(experiment_node["exit_criteria"], ["record evidence"])

        overlap_snapshot = copy.deepcopy(self.legacy_team)
        overlap_snapshot["project"]["active_work_ids"] = ["work-network"]
        overlap_snapshot["project"]["primary_work_id"] = "work-network"
        overlap_snapshot["claims"] = [overlap_snapshot["claims"][0]]
        overlap_snapshot["works"][1]["status"] = "proposed"
        overlap_snapshot["works"][1]["scope_refs"] = copy.deepcopy(
            overlap_snapshot["works"][0]["scope_refs"]
        )
        overlap_snapshot["works"][1]["overlap_candidate_ids"] = ["work-network"]
        overlap_snapshot["works"][1]["dedupe_status"] = "coordinated"
        overlap_snapshot["works"][0]["overlap_candidate_ids"] = ["work-ui"]
        overlap_snapshot["works"][0]["dedupe_status"] = "coordinated"
        overlap_source = self.external_projection(overlap_snapshot)
        overlap_projection = build_project_graph_projection(
            overlap_source,
            signer=self.signer,
            observed_at="2026-08-09T11:00:00+08:00",
        )
        overlaps = overlap_projection["health"]["work_scope_overlap_candidates"]
        self.assertEqual(len(overlaps), 1)
        self.assertEqual(
            {overlaps[0]["left_work_id"], overlaps[0]["right_work_id"]},
            {"work-network", "work-ui"},
        )
        self.assertEqual(
            overlap_projection["health"]["claim_ownership_overlaps"], []
        )
        self.assertEqual(
            overlap_projection["work_ledger"]["overlap_candidate_pair_count"], 1
        )

        duplicate_projection = build_project_graph_projection(
            self.external_projection(self.legacy_duplicate),
            signer=self.signer,
            observed_at="2026-08-09T11:00:00+08:00",
        )
        self.assertEqual(duplicate_projection["primary_work_id"], None)
        self.assertEqual(
            duplicate_projection["work_ledger"]["open_blockers"][0]["blocker_id"],
            "blocker-duplicate",
        )
        self.assertEqual(
            duplicate_projection["work_ledger"]["duplicate_candidates"],
            [
                {
                    "work_id": "work-repeat",
                    "candidate_work_ids": ["work-existing"],
                    "dedupe_status": "blocked",
                }
            ],
        )

    def test_v6_projection_preserves_work_provenance_and_claim_fence(self) -> None:
        snapshot = migrate_typed_state_v5_to_v6(_v5_snapshot())
        active_work = next(
            item
            for item in snapshot["works"]
            if item["work_id"] in snapshot["project"]["active_work_ids"]
        )
        active_work["work_source_ref"] = "source://opaque-thread/work-active"
        active_work["source_revision"] = 7
        active_work["work_identity_sha256"] = "1" * 64
        active_work["dedupe_receipt_sha256"] = "2" * 64
        source = self.external_projection(snapshot)

        projection = build_project_graph_projection(
            source,
            signer=self.signer,
            observed_at="2026-08-14T09:00:00+08:00",
        )

        active = projection["active_work_set"][0]
        self.assertEqual(
            active["work_source_ref"], "source://opaque-thread/work-active"
        )
        self.assertEqual(active["source_revision"], 7)
        self.assertEqual(active["work_identity_sha256"], "1" * 64)
        self.assertEqual(active["dedupe_receipt_sha256"], "2" * 64)
        self.assertEqual(active["claim"]["claim_revision"], 1)
        self.assertEqual(active["claim"]["lease_epoch"], 1)
        self.assertEqual(
            active["claim"]["last_heartbeat_at"], active["claim"]["claimed_at"]
        )
        self.assertFalse(active["claim"]["lease_expired_at_observation"])
        self.assertEqual(
            projection["work_ledger"]["capabilities"],
            {"claim_fencing": True, "work_provenance": True},
        )

    def test_scope_overlap_health_uses_only_declared_candidate_pairs(self) -> None:
        snapshot = copy.deepcopy(self.legacy_team)
        snapshot["project"]["active_work_ids"] = ["work-network"]
        snapshot["project"]["primary_work_id"] = "work-network"
        snapshot["claims"] = [snapshot["claims"][0]]
        other = next(
            item for item in snapshot["works"] if item["work_id"] == "work-ui"
        )
        other["status"] = "proposed"
        other["scope_refs"] = copy.deepcopy(snapshot["works"][0]["scope_refs"])
        other["overlap_candidate_ids"] = []
        other["dedupe_status"] = "clear"

        projection = build_project_graph_projection(
            self.external_projection(snapshot),
            signer=self.signer,
            observed_at="2026-08-09T11:00:00+08:00",
        )

        self.assertEqual(
            projection["health"]["work_scope_overlap_candidates"], []
        )

    def test_projection_rejects_source_larger_than_render_contract(self) -> None:
        snapshot = copy.deepcopy(self.legacy_team)
        proposed = snapshot["works"][1]
        proposed["status"] = "proposed"
        proposed["scope_refs"] = [
            {"scope_kind": "capability", "scope_ref": f"scope-{index:05d}"}
            for index in range(10_001)
        ]
        snapshot["project"]["active_work_ids"] = ["work-network"]
        snapshot["project"]["primary_work_id"] = "work-network"
        snapshot["claims"] = [snapshot["claims"][0]]

        with self.assertRaises(ProjectGraphProjectionError):
            build_project_graph_projection(
                self.external_projection(snapshot),
                signer=self.signer,
                observed_at="2026-08-09T11:00:00+08:00",
            )

    def test_projection_rejects_unbounded_declared_overlap_references(self) -> None:
        snapshot = copy.deepcopy(self.legacy_team)
        template = copy.deepcopy(snapshot["works"][1])
        work_ids = [f"work-{index:03d}" for index in range(225)]
        snapshot["works"] = []
        for index, work_id in enumerate(work_ids):
            work = copy.deepcopy(template)
            work.update(
                {
                    "work_id": work_id,
                    "title": f"Work {index}",
                    "status": "proposed",
                    "parent_work_id": None,
                    "dependency_ids": [],
                    "owner_refs": [],
                    "scope_refs": [
                        {
                            "scope_kind": "capability",
                            "scope_ref": f"scope-{index:03d}",
                        }
                    ],
                    "overlap_candidate_ids": [
                        candidate_id
                        for candidate_id in work_ids
                        if candidate_id != work_id
                    ],
                    "dedupe_status": "candidate",
                    "supersedes_work_id": None,
                    "evidence_ids": [],
                    "blocker_ids": [],
                }
            )
            snapshot["works"].append(work)
        snapshot["project"]["active_work_ids"] = []
        snapshot["project"]["primary_work_id"] = None
        snapshot["project"]["open_blocker_ids"] = []
        snapshot["claims"] = []
        snapshot["blockers"] = []

        with self.assertRaises(ProjectGraphProjectionError):
            build_project_graph_projection(
                self.external_projection(snapshot),
                signer=self.signer,
                observed_at="2026-08-09T11:00:00+08:00",
            )

    def test_projection_rejects_scope_comparisons_above_budget(self) -> None:
        snapshot = copy.deepcopy(self.legacy_team)
        snapshot["project"]["active_work_ids"] = ["work-network"]
        snapshot["project"]["primary_work_id"] = "work-network"
        snapshot["claims"] = [snapshot["claims"][0]]
        left, right = snapshot["works"]
        right["status"] = "proposed"
        for index, work in enumerate((left, right)):
            work["scope_refs"] = [
                {
                    "scope_kind": "capability",
                    "scope_ref": f"work-{index}-scope-{scope_index:03d}",
                }
                for scope_index in range(225)
            ]
        left["overlap_candidate_ids"] = [right["work_id"]]
        right["overlap_candidate_ids"] = [left["work_id"]]
        left["dedupe_status"] = "coordinated"
        right["dedupe_status"] = "coordinated"
        snapshot["claims"][0]["scope_owners"] = [
            copy.deepcopy(left["scope_refs"][0])
        ]

        with self.assertRaises(ProjectGraphProjectionError):
            build_project_graph_projection(
                self.external_projection(snapshot),
                signer=self.signer,
                observed_at="2026-08-09T11:00:00+08:00",
            )

    def test_claim_ownership_conflict_is_rejected_before_projection(self) -> None:
        snapshot = copy.deepcopy(self.legacy_team)
        snapshot["works"][1]["scope_refs"] = copy.deepcopy(
            snapshot["works"][0]["scope_refs"]
        )
        snapshot["claims"][1]["scope_owners"] = copy.deepcopy(
            snapshot["claims"][0]["scope_owners"]
        )
        provider = ExternalStateProjectionProvider(
            _Source(snapshot),
            provider_id="provider-m9-02-conflict",
            signer=self.signer,
        )
        response = provider.call_tool(
            EXTERNAL_READ_TOOL,
            {
                "schema_version": EXTERNAL_REQUEST_SCHEMA_VERSION,
                "request_id": "request-m9-02-conflict",
                "project_id": snapshot["project"]["project_id"],
                "expected_revision": snapshot["project"]["revision"],
            },
            context=RequestContext("actor-reader", "authorization-reader"),
        )

        self.assertFalse(response["ok"])
        self.assertEqual(response["error"]["code"], "source_integrity_error")

    def test_projection_rejects_excessive_aggregate_nested_items(self) -> None:
        snapshot = copy.deepcopy(self.legacy_team)
        template = copy.deepcopy(snapshot["works"][1])
        snapshot["works"] = []
        for work_index in range(3):
            work = copy.deepcopy(template)
            work.update(
                {
                    "work_id": f"work-{work_index}",
                    "title": f"Work {work_index}",
                    "status": "proposed",
                    "parent_work_id": None,
                    "dependency_ids": [],
                    "owner_refs": [
                        f"owner-{work_index}-{index:04d}" for index in range(9000)
                    ],
                    "scope_refs": [
                        {
                            "scope_kind": "capability",
                            "scope_ref": f"scope-{work_index}-{index:04d}",
                        }
                        for index in range(9000)
                    ],
                    "overlap_candidate_ids": [],
                    "dedupe_status": "clear",
                    "supersedes_work_id": None,
                    "evidence_ids": [],
                    "blocker_ids": [],
                }
            )
            snapshot["works"].append(work)
        snapshot["project"]["active_work_ids"] = []
        snapshot["project"]["primary_work_id"] = None
        snapshot["claims"] = []

        with self.assertRaises(ProjectGraphProjectionError):
            build_project_graph_projection(
                self.external_projection(snapshot),
                signer=self.signer,
                observed_at="2026-08-09T11:00:00+08:00",
            )

    def test_tampering_and_invalid_source_fail_closed(self) -> None:
        source = self.external_projection(build_idea_snapshot())
        projection = build_project_graph_projection(
            source,
            signer=self.signer,
            observed_at="2026-08-17T18:00:00+08:00",
        )
        for field, value in (
            ("state_revision", projection["state_revision"] + 1),
            ("state_sha256", "0" * 64),
            ("source_projection_sha256", "0" * 64),
        ):
            forged = copy.deepcopy(projection)
            forged[field] = value
            with self.subTest(field=field), self.assertRaises(
                ProjectGraphProjectionError
            ):
                validate_project_graph_projection(
                    forged,
                    source_projection=source,
                    signer=self.signer,
                )

        coordinated_mutations = (
            lambda item: item["active_work_set"].clear(),
            lambda item: item["graph"]["nodes"].clear(),
            lambda item: item["work_ledger"].__setitem__("active_work_count", 0),
            lambda item: item["health"]["expired_active_claim_ids"].clear(),
        )
        for mutate in coordinated_mutations:
            forged = copy.deepcopy(projection)
            mutate(forged)
            _resign_projection(forged, self.signer)
            with self.assertRaises(ProjectGraphProjectionError):
                validate_project_graph_projection(
                    forged,
                    source_projection=source,
                    signer=self.signer,
                )

        boolean_authority = copy.deepcopy(projection)
        boolean_authority["authority"]["provider_authority"] = False
        _resign_projection(boolean_authority, self.signer)
        with self.assertRaises(ProjectGraphProjectionError):
            validate_project_graph_projection(
                boolean_authority,
                source_projection=source,
                signer=self.signer,
            )

        torn_source = copy.deepcopy(source)
        torn_source["state_revision"] += 1
        with self.assertRaises(ProjectGraphProjectionError):
            build_project_graph_projection(
                torn_source,
                signer=self.signer,
                observed_at="2026-08-17T18:00:00+08:00",
            )
        with self.assertRaises(ProjectGraphProjectionError):
            build_project_graph_projection(
                source,
                signer=self.signer,
                observed_at="not-a-time",
            )


if __name__ == "__main__":
    unittest.main()
