"""M8-05 State MCP tenant and project isolation regressions."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from context_control_plane.authorization_audit import (
    AUTHORIZATION_POLICY_SCHEMA_VERSION,
    InMemoryAuthorizationAuditStore,
    TenantProjectAuthorizer,
    authorization_policy_sha256,
)
from context_control_plane.shared_state_mcp import (
    CLAIM_LIFECYCLE_TOOL,
    REQUEST_SCHEMA_VERSION,
    SharedStateMCPService,
)
from context_control_plane.shared_work_ledger import WorkLedger
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import (
    READ_TOOL,
    RequestContext,
    StateMCPService,
)
from context_control_plane.state_mcp import (
    REQUEST_SCHEMA_VERSION as STATE_REQUEST_SCHEMA_VERSION,
)
from context_control_plane.state_store_capabilities_v2 import (
    SQLiteLocalCoordinatorStateStore,
)
from tests.test_m8_02_shared_state_mcp import _canonical_coordinator_snapshot


class _AllowAuthorizer:
    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return True


class _CountingSQLiteStateStore(SQLiteStateStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.project_reads = 0
        self.event_reads = 0

    def read_project(self, project_id: str) -> dict:
        self.project_reads += 1
        return super().read_project(project_id)

    def read_events(self, project_id: str) -> list[dict]:
        self.event_reads += 1
        return super().read_events(project_id)


def _policy() -> dict:
    return {
        "schema_version": AUTHORIZATION_POLICY_SCHEMA_VERSION,
        "policy_id": "policy-m8-05-state-mcp",
        "policy_revision": 1,
        "issued_at": "2026-08-16T11:00:00+00:00",
        "expires_at": None,
        "projects": [
            {"tenant_id": "tenant-a", "project_id": "project-a", "status": "active"},
            {"tenant_id": "tenant-b", "project_id": "project-b", "status": "active"},
        ],
        "grants": [
            {
                "grant_id": "grant-actor-a",
                "authorization_ref": "authorization://tenant-a/actor-a",
                "subject_ref": "actor-a",
                "tenant_id": "tenant-a",
                "project_ids": ["project-a"],
                "actions": ["state.claim.acquire", "state.read"],
                "not_before": "2026-08-16T11:00:00+00:00",
                "expires_at": None,
                "status": "active",
            }
        ],
    }


def _tenant_authorizer(store: InMemoryAuthorizationAuditStore):
    policy = _policy()
    return TenantProjectAuthorizer(
        policy,
        expected_policy_sha256=authorization_policy_sha256(policy),
        audit_store=store,
        clock=lambda: "2026-08-16T12:00:00+00:00",
    )


def _request(project_id: str) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": "request-shared-across-projects",
        "project_id": project_id,
        "action": "acquire",
        "expected_project_revision": 8,
        "work_id": "work-active",
        "claim_id": "claim-across-projects",
        "requested_ttl_ms": 500,
        "scope_owners": [
            {"scope_kind": "capability", "scope_ref": "capability/a"}
        ],
    }


class M805StateMCPIsolationTests(unittest.TestCase):
    def test_state_mcp_denies_before_lookup_without_disclosing_project_existence(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = _CountingSQLiteStateStore(Path(directory) / "state.sqlite3")
            audit = InMemoryAuthorizationAuditStore()
            service = StateMCPService(
                store,
                authorizer=_tenant_authorizer(audit),
                registry_digest="a" * 64,
                clock=lambda: "2026-08-16T12:00:00+00:00",
                event_id_factory=lambda request_id: f"event-{request_id}",
            )
            context = RequestContext("actor-a", "authorization://tenant-a/actor-a")

            cross_tenant = service.call_tool(
                READ_TOOL,
                {
                    "schema_version": STATE_REQUEST_SCHEMA_VERSION,
                    "request_id": "request-cross-tenant",
                    "project_id": "project-b",
                },
                context=context,
            )
            unknown = service.call_tool(
                READ_TOOL,
                {
                    "schema_version": STATE_REQUEST_SCHEMA_VERSION,
                    "request_id": "request-unknown-project",
                    "project_id": "project-unknown",
                },
                context=context,
            )

        self.assertEqual(cross_tenant["error"], unknown["error"])
        self.assertEqual(cross_tenant["error"]["code"], "permission_denied")
        self.assertEqual(store.project_reads, 0)
        self.assertEqual(store.event_reads, 0)
        reasons = {
            event["reason_code"]
            for event in audit.read_events()
        }
        self.assertEqual(reasons, {"tenant_mismatch", "project_not_registered"})

    def test_shared_state_mcp_denies_before_ledger_mutation(self) -> None:
        scope = {"scope_kind": "capability", "scope_ref": "capability/a"}
        ledger = WorkLedger(
            project_id="project-a",
            project_revision=8,
            works=[
                {
                    "work_id": "work-active",
                    "status": "ready",
                    "identity_key": "work-active",
                    "scope_refs": [scope],
                    "owner_refs": ["actor-a"],
                }
            ],
            max_ttl_ms=1_000,
        )
        before = ledger.snapshot()
        audit = InMemoryAuthorizationAuditStore()
        service = SharedStateMCPService(
            ledger,
            authorizer=_tenant_authorizer(audit),
            clock=lambda: "2026-08-16T12:00:00+00:00",
        )
        context = RequestContext("actor-a", "authorization://tenant-a/actor-a")
        denied_request = _request("project-b")
        denied_request["request_id"] = "request-denied-cross-tenant"

        denied = service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            denied_request,
            context=context,
        )

        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"]["code"], "permission_denied")
        self.assertEqual(ledger.snapshot(), before)
        self.assertEqual(
            audit.read_events(tenant_id="tenant-b")[0]["reason_code"],
            "tenant_mismatch",
        )

    def test_request_identity_is_scoped_by_project_in_shared_service_cache(self) -> None:
        first = _canonical_coordinator_snapshot()
        first["project"]["project_id"] = "project-a"
        second = copy.deepcopy(first)
        second["project"]["project_id"] = "project-b"
        with tempfile.TemporaryDirectory() as directory:
            base = SQLiteStateStore(Path(directory) / "state.sqlite3")
            base.initialize()
            base.create_project(first)
            base.create_project(second)
            coordinator = SQLiteLocalCoordinatorStateStore(base)
            for snapshot in (first, second):
                coordinator.initialize_work_ledger(
                    project_id=snapshot["project"]["project_id"],
                    project_revision=snapshot["project"]["revision"],
                    works=snapshot["works"],
                    max_ttl_ms=1_000,
                )
            service = SharedStateMCPService(
                coordinator,
                authorizer=_AllowAuthorizer(),
                clock=lambda: "2026-08-16T12:00:00+00:00",
            )
            context = RequestContext("actor-a", "authorization-a")

            first_result = service.call_tool(
                CLAIM_LIFECYCLE_TOOL,
                _request("project-a"),
                context=context,
            )
            second_result = service.call_tool(
                CLAIM_LIFECYCLE_TOOL,
                _request("project-b"),
                context=context,
            )

        self.assertTrue(first_result["ok"], first_result["error"])
        self.assertTrue(second_result["ok"], second_result["error"])
        self.assertEqual(first_result["result"]["project_revision"], 9)
        self.assertEqual(second_result["result"]["project_revision"], 9)


if __name__ == "__main__":
    unittest.main()
