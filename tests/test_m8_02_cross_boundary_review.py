"""M8-02 cross-boundary safety regressions found during integration review."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from context_control_plane.shared_state_mcp import (
    CLAIM_LIFECYCLE_TOOL,
    REQUEST_SCHEMA_VERSION,
    SharedStateMCPService,
)
from context_control_plane.shared_state_migration import (
    migrate_typed_state_v5_to_v6,
)
from context_control_plane.shared_work_ledger import (
    ClaimLifecycleError,
    WorkLedger,
)
from context_control_plane.sqlite_state_store import (
    SQLiteStateConflict,
    SQLiteStateStore,
)
from context_control_plane.sqlite_work_ledger import SQLiteWorkLedgerStore
from context_control_plane.state_events import replay_state_events
from context_control_plane.state_mcp import RequestContext
from context_control_plane.state_store_capabilities_v2 import (
    SQLiteLocalCoordinatorStateStore,
)
from tests.test_m8_02_typed_state_v6_migration import _v5_snapshot


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


def _unclaimed_v6_snapshot() -> dict:
    snapshot = migrate_typed_state_v5_to_v6(_v5_snapshot())
    active = next(
        item for item in snapshot["works"] if item["work_id"] == "work-active"
    )
    active["status"] = "ready"
    snapshot["project"]["active_work_ids"] = []
    snapshot["project"]["primary_work_id"] = None
    snapshot["project"]["effect_high_watermark"] = 0
    snapshot["claims"] = []
    snapshot["effects"] = []
    return snapshot


class M802CrossBoundaryReviewTests(unittest.TestCase):
    def test_state_mcp_rejects_the_standalone_sqlite_fixture_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            standalone = SQLiteWorkLedgerStore(Path(directory) / "ledger.sqlite3")

            with self.assertRaisesRegex(TypeError, "authority contract"):
                SharedStateMCPService(
                    standalone,
                    authorizer=_AllowAuthorizer(),
                    clock=lambda: "2026-08-14T01:00:00+00:00",
                )

    def test_event_replay_accepts_a_migrated_v6_checkpoint(self):
        migrated = migrate_typed_state_v5_to_v6(_v5_snapshot())

        restored = replay_state_events(migrated, [])

        self.assertEqual(restored, migrated)

    def test_sqlite_ledger_initialization_rejects_a_noncanonical_revision(self):
        canonical = migrate_typed_state_v5_to_v6(_v5_snapshot())
        project_id = canonical["project"]["project_id"]
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(canonical)
            coordinator = SQLiteLocalCoordinatorStateStore(store)

            with self.assertRaises(SQLiteStateConflict):
                coordinator.initialize_work_ledger(
                    project_id=project_id,
                    project_revision=canonical["project"]["revision"] + 1,
                    works=copy.deepcopy(canonical["works"]),
                    max_ttl_ms=1_000,
                )

    def test_lifecycle_request_id_cannot_change_action_after_restart(self):
        canonical = _unclaimed_v6_snapshot()
        project_id = canonical["project"]["project_id"]
        initial_revision = canonical["project"]["revision"]
        work = next(
            copy.deepcopy(item)
            for item in canonical["works"]
            if item["work_id"] == "work-target"
        )
        scope = copy.deepcopy(work["scope_refs"][0])
        context = RequestContext("actor-owner", "authorization-review")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = SQLiteStateStore(database)
            store.initialize()
            store.create_project(canonical)
            coordinator = SQLiteLocalCoordinatorStateStore(store)
            coordinator.initialize_work_ledger(
                project_id=project_id,
                project_revision=initial_revision,
                works=copy.deepcopy(canonical["works"]),
                max_ttl_ms=1_000,
            )
            acquire = {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-shared-lifecycle",
                "project_id": project_id,
                "action": "acquire",
                "expected_project_revision": initial_revision,
                "work_id": work["work_id"],
                "claim_id": "claim-review",
                "requested_ttl_ms": 500,
                "scope_owners": [scope],
            }
            first = SharedStateMCPService(
                coordinator,
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-14T01:00:00+00:00",
            ).call_tool(CLAIM_LIFECYCLE_TOOL, acquire, context=context)
            self.assertTrue(first["ok"], first["error"])

            heartbeat = {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": acquire["request_id"],
                "project_id": project_id,
                "action": "heartbeat",
                "expected_project_revision": initial_revision + 1,
                "claim_id": "claim-review",
                "expected_claim_revision": 1,
                "lease_epoch": 1,
                "fence": 1,
                "requested_ttl_ms": 500,
            }
            replay_service = SharedStateMCPService(
                SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database)),
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-14T01:00:00.100000+00:00",
            )

            changed_intent = replay_service.call_tool(
                CLAIM_LIFECYCLE_TOOL,
                heartbeat,
                context=context,
            )

            self.assertFalse(changed_intent["ok"])
            self.assertEqual(changed_intent["error"]["code"], "conflict")
            self.assertEqual(changed_intent["error"]["reason"], "request_id_reused")

    def test_canonical_work_identity_blocks_a_second_disjoint_claim(self):
        identity = "d" * 64
        ledger = WorkLedger(
            project_id="project-review",
            project_revision=7,
            works=[
                {
                    "work_id": "work-one",
                    "status": "ready",
                    "work_identity_sha256": identity,
                    "scope_refs": [{"scope_kind": "capability", "scope_ref": "one"}],
                },
                {
                    "work_id": "work-two",
                    "status": "ready",
                    "work_identity_sha256": identity,
                    "scope_refs": [{"scope_kind": "capability", "scope_ref": "two"}],
                },
            ],
            max_ttl_ms=1_000,
        )
        first = ledger.acquire_claim(
            work_id="work-one",
            actor_ref="actor-one",
            expected_project_revision=7,
            observed_at="2026-08-16T10:00:00+00:00",
            requested_ttl_ms=500,
            claim_id="claim-one",
            scope_owners=[{"scope_kind": "capability", "scope_ref": "one"}],
        )

        with self.assertRaisesRegex(ClaimLifecycleError, "duplicate_work_identity"):
            ledger.acquire_claim(
                work_id="work-two",
                actor_ref="actor-two",
                expected_project_revision=first["claim"]["expected_project_revision"],
                observed_at="2026-08-16T10:00:00.100000+00:00",
                requested_ttl_ms=500,
                claim_id="claim-two",
                scope_owners=[{"scope_kind": "capability", "scope_ref": "two"}],
            )

    def test_v5_migration_does_not_leave_active_claims_without_a_fence(self):
        migrated = migrate_typed_state_v5_to_v6(_v5_snapshot())

        unfenced_active_claims = [
            claim["claim_id"]
            for claim in migrated["claims"]
            if claim["status"] == "active"
            and (claim["claim_revision"] < 1 or claim["lease_epoch"] < 1)
        ]

        self.assertEqual(unfenced_active_claims, [])


if __name__ == "__main__":
    unittest.main()
