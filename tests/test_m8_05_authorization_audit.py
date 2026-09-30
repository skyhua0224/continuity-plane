"""M8-05 actor, tenant, project authorization and audit contracts."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from context_control_plane.authorization_audit import (
    AUTHORIZATION_POLICY_SCHEMA_VERSION,
    AuthorizationAuditError,
    InMemoryAuthorizationAuditStore,
    SQLiteAuthorizationAuditStore,
    TenantProjectAuthorizer,
    authorization_policy_sha256,
    replay_authorization_audit_events,
    validate_authorization_audit_event,
    validate_authorization_policy,
)
from context_control_plane.state_mcp import RequestContext

NOW = "2026-08-16T12:00:00+00:00"


def _policy() -> dict:
    return {
        "schema_version": AUTHORIZATION_POLICY_SCHEMA_VERSION,
        "policy_id": "policy-m8-05",
        "policy_revision": 3,
        "issued_at": "2026-08-16T11:00:00+00:00",
        "expires_at": None,
        "projects": [
            {
                "tenant_id": "tenant-a",
                "project_id": "project-a",
                "status": "active",
            },
            {
                "tenant_id": "tenant-a",
                "project_id": "project-a-secondary",
                "status": "active",
            },
            {
                "tenant_id": "tenant-b",
                "project_id": "project-b",
                "status": "active",
            },
        ],
        "grants": [
            {
                "grant_id": "grant-actor-a",
                "authorization_ref": "authorization://tenant-a/actor-a/revision-3",
                "subject_ref": "actor-a",
                "tenant_id": "tenant-a",
                "project_ids": ["project-a"],
                "actions": ["state.claim.acquire", "state.read"],
                "not_before": "2026-08-16T11:00:00+00:00",
                "expires_at": "2026-08-16T13:00:00+00:00",
                "status": "active",
            }
        ],
    }


def _context(
    *,
    subject_ref: str = "actor-a",
    authorization_ref: str = "authorization://tenant-a/actor-a/revision-3",
) -> RequestContext:
    return RequestContext(subject_ref, authorization_ref)


class _FailingAuditStore:
    def append(self, event: dict) -> dict:
        raise OSError("audit storage unavailable")


class M805AuthorizationAuditTests(unittest.TestCase):
    def test_authorizer_requires_an_exact_trusted_policy_digest(self) -> None:
        policy = _policy()
        with self.assertRaisesRegex(TypeError, "expected_policy_sha256"):
            TenantProjectAuthorizer(
                policy,
                audit_store=InMemoryAuthorizationAuditStore(),
                clock=lambda: NOW,
            )
        with self.assertRaisesRegex(AuthorizationAuditError, "policy digest"):
            TenantProjectAuthorizer(
                policy,
                expected_policy_sha256="0" * 64,
                audit_store=InMemoryAuthorizationAuditStore(),
                clock=lambda: NOW,
            )

    def test_exact_grant_allows_and_appends_a_hash_chained_audit_event(self) -> None:
        policy = validate_authorization_policy(_policy())
        store = InMemoryAuthorizationAuditStore()
        authorizer = TenantProjectAuthorizer(
            policy,
            expected_policy_sha256=authorization_policy_sha256(policy),
            audit_store=store,
            clock=lambda: NOW,
        )

        self.assertTrue(
            authorizer.authorize(_context(), "state.claim.acquire", "project-a")
        )

        events = store.read_events(tenant_id="tenant-a", project_id="project-a")
        self.assertEqual(len(events), 1)
        event = events[0]
        validate_authorization_audit_event(event)
        self.assertEqual(event["decision"], "allow")
        self.assertEqual(event["reason_code"], "grant_matched")
        self.assertEqual(event["subject_ref"], "actor-a")
        self.assertEqual(event["tenant_id"], "tenant-a")
        self.assertEqual(event["project_id"], "project-a")
        self.assertEqual(event["policy_revision"], 3)
        self.assertIsNone(event["previous_event_sha256"])
        replayed = replay_authorization_audit_events(events)
        self.assertEqual(replayed["event_count"], 1)
        self.assertEqual(replayed["event_head_sha256"], event["event_sha256"])

    def test_wrong_actor_action_project_tenant_and_stale_grant_are_denied(self) -> None:
        cases = [
            (
                "subject_mismatch",
                _context(subject_ref="actor-other"),
                "state.read",
                "project-a",
                NOW,
            ),
            (
                "grant_not_found",
                _context(authorization_ref="authorization://unknown/grant"),
                "state.read",
                "project-a",
                NOW,
            ),
            (
                "action_not_granted",
                _context(),
                "state.effect.authorize",
                "project-a",
                NOW,
            ),
            (
                "project_not_granted",
                _context(),
                "state.read",
                "project-a-secondary",
                NOW,
            ),
            (
                "tenant_mismatch",
                _context(),
                "state.read",
                "project-b",
                NOW,
            ),
            (
                "project_not_registered",
                _context(),
                "state.read",
                "project-unknown",
                NOW,
            ),
            (
                "grant_expired",
                _context(),
                "state.read",
                "project-a",
                "2026-08-16T13:00:00.000001+00:00",
            ),
        ]

        for reason, context, action, project_id, observed_at in cases:
            with self.subTest(reason=reason):
                store = InMemoryAuthorizationAuditStore()
                authorizer = TenantProjectAuthorizer(
                    _policy(),
                    expected_policy_sha256=authorization_policy_sha256(_policy()),
                    audit_store=store,
                    clock=lambda value=observed_at: value,
                )

                self.assertFalse(authorizer.authorize(context, action, project_id))

                events = store.read_events()
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["decision"], "deny")
                self.assertEqual(events[0]["reason_code"], reason)

    def test_policy_cannot_authorize_before_its_issue_time(self) -> None:
        policy = _policy()
        policy["grants"][0]["not_before"] = "2026-08-16T10:00:00+00:00"
        store = InMemoryAuthorizationAuditStore()
        authorizer = TenantProjectAuthorizer(
            policy,
            expected_policy_sha256=authorization_policy_sha256(policy),
            audit_store=store,
            clock=lambda: "2026-08-16T10:30:00+00:00",
        )

        self.assertFalse(authorizer.authorize(_context(), "state.read", "project-a"))
        self.assertEqual(store.read_events()[0]["reason_code"], "policy_not_active")

    def test_audit_write_failure_fails_closed(self) -> None:
        authorizer = TenantProjectAuthorizer(
            _policy(),
            expected_policy_sha256=authorization_policy_sha256(_policy()),
            audit_store=_FailingAuditStore(),
            clock=lambda: NOW,
        )

        self.assertFalse(authorizer.authorize(_context(), "state.read", "project-a"))
        self.assertEqual(authorizer.audit_write_failures, 1)

    def test_policy_rejects_duplicate_credentials_and_cross_tenant_grants(self) -> None:
        duplicate = _policy()
        duplicate["grants"].append(copy.deepcopy(duplicate["grants"][0]))
        duplicate["grants"][1]["grant_id"] = "grant-actor-a-duplicate"
        with self.assertRaisesRegex(AuthorizationAuditError, "authorization_ref"):
            validate_authorization_policy(duplicate)

        cross_tenant = _policy()
        cross_tenant["grants"][0]["project_ids"].append("project-b")
        with self.assertRaisesRegex(AuthorizationAuditError, "tenant"):
            validate_authorization_policy(cross_tenant)

    def test_sqlite_audit_survives_restart_and_rejects_chain_tamper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "authorization-audit.sqlite3"
            store = SQLiteAuthorizationAuditStore(path)
            store.initialize()
            authorizer = TenantProjectAuthorizer(
                _policy(),
                expected_policy_sha256=authorization_policy_sha256(_policy()),
                audit_store=store,
                clock=lambda: NOW,
            )
            self.assertTrue(authorizer.authorize(_context(), "state.read", "project-a"))
            self.assertFalse(
                authorizer.authorize(
                    _context(), "state.read", "project-a-secondary"
                )
            )

            reopened = SQLiteAuthorizationAuditStore(path)
            events = reopened.read_events()

        replay = replay_authorization_audit_events(events)
        self.assertEqual(replay["event_count"], 2)
        self.assertEqual(events[1]["previous_event_sha256"], events[0]["event_sha256"])

        tampered = copy.deepcopy(events)
        tampered[0]["decision"] = "deny"
        with self.assertRaisesRegex(AuthorizationAuditError, "hash"):
            replay_authorization_audit_events(tampered)

    def test_each_tenant_has_an_independent_sequence_and_hash_head(self) -> None:
        store = InMemoryAuthorizationAuditStore()
        authorizer = TenantProjectAuthorizer(
            _policy(),
            expected_policy_sha256=authorization_policy_sha256(_policy()),
            audit_store=store,
            clock=lambda: NOW,
        )

        self.assertTrue(authorizer.authorize(_context(), "state.read", "project-a"))
        self.assertFalse(authorizer.authorize(_context(), "state.read", "project-b"))

        tenant_a = store.read_events(tenant_id="tenant-a")
        tenant_b = store.read_events(tenant_id="tenant-b")
        self.assertEqual([event["sequence_no"] for event in tenant_a], [1])
        self.assertEqual([event["sequence_no"] for event in tenant_b], [1])
        self.assertIsNone(tenant_a[0]["previous_event_sha256"])
        self.assertIsNone(tenant_b[0]["previous_event_sha256"])

    def test_tenant_audit_time_cannot_regress(self) -> None:
        observed_at = [NOW]
        policy = _policy()
        store = InMemoryAuthorizationAuditStore()
        authorizer = TenantProjectAuthorizer(
            policy,
            expected_policy_sha256=authorization_policy_sha256(policy),
            audit_store=store,
            clock=lambda: observed_at[0],
        )
        self.assertTrue(authorizer.authorize(_context(), "state.read", "project-a"))
        observed_at[0] = "2026-08-16T11:59:59+00:00"

        self.assertFalse(authorizer.authorize(_context(), "state.read", "project-a"))
        self.assertEqual(authorizer.audit_write_failures, 1)
        self.assertEqual(len(store.read_events(tenant_id="tenant-a")), 1)

    def test_replay_rejects_rehashed_tenant_time_regression(self) -> None:
        policy = _policy()
        store = InMemoryAuthorizationAuditStore()
        authorizer = TenantProjectAuthorizer(
            policy,
            expected_policy_sha256=authorization_policy_sha256(policy),
            audit_store=store,
            clock=lambda: NOW,
        )
        self.assertTrue(authorizer.authorize(_context(), "state.read", "project-a"))
        self.assertTrue(authorizer.authorize(_context(), "state.read", "project-a"))
        forged = store.read_events(tenant_id="tenant-a")
        forged[1]["observed_at"] = "2026-08-16T11:59:59+00:00"
        forged[1]["event_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in forged[1].items()
                    if key != "event_sha256"
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

        with self.assertRaisesRegex(AuthorizationAuditError, "regressed"):
            replay_authorization_audit_events(forged)

    def test_sqlite_tenant_audit_time_cannot_regress(self) -> None:
        observed_at = [NOW]
        policy = _policy()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteAuthorizationAuditStore(
                Path(directory) / "authorization-audit.sqlite3"
            )
            store.initialize()
            authorizer = TenantProjectAuthorizer(
                policy,
                expected_policy_sha256=authorization_policy_sha256(policy),
                audit_store=store,
                clock=lambda: observed_at[0],
            )
            self.assertTrue(
                authorizer.authorize(_context(), "state.read", "project-a")
            )
            observed_at[0] = "2026-08-16T11:59:59+00:00"

            self.assertFalse(
                authorizer.authorize(_context(), "state.read", "project-a")
            )
            self.assertEqual(authorizer.audit_write_failures, 1)
            self.assertEqual(len(store.read_events(tenant_id="tenant-a")), 1)

    def test_sqlite_rows_are_database_enforced_append_only(self) -> None:
        policy = _policy()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "authorization-audit.sqlite3"
            store = SQLiteAuthorizationAuditStore(path)
            store.initialize()
            authorizer = TenantProjectAuthorizer(
                policy,
                expected_policy_sha256=authorization_policy_sha256(policy),
                audit_store=store,
                clock=lambda: NOW,
            )
            self.assertTrue(
                authorizer.authorize(_context(), "state.read", "project-a")
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute(
                        "UPDATE authorization_audit_events SET project_id = ?",
                        ("project-forged",),
                    )
                with self.assertRaisesRegex(sqlite3.DatabaseError, "append-only"):
                    connection.execute("DELETE FROM authorization_audit_events")

    def test_sqlite_store_closes_every_connection(self) -> None:
        class TrackingConnection(sqlite3.Connection):
            def __init__(self, *args, **kwargs) -> None:
                super().__init__(*args, **kwargs)
                self.was_closed = False

            def close(self) -> None:
                self.was_closed = True
                super().close()

        class TrackingStore(SQLiteAuthorizationAuditStore):
            def __init__(self, path: Path) -> None:
                super().__init__(path)
                self.connections: list[TrackingConnection] = []

            def _connect(self) -> sqlite3.Connection:
                connection = sqlite3.connect(self.path, factory=TrackingConnection)
                connection.row_factory = sqlite3.Row
                self.connections.append(connection)
                return connection

        policy = _policy()
        with tempfile.TemporaryDirectory() as directory:
            store = TrackingStore(Path(directory) / "authorization-audit.sqlite3")
            store.initialize()
            authorizer = TenantProjectAuthorizer(
                policy,
                expected_policy_sha256=authorization_policy_sha256(policy),
                audit_store=store,
                clock=lambda: NOW,
            )
            self.assertTrue(
                authorizer.authorize(_context(), "state.read", "project-a")
            )
            store.read_events(tenant_id="tenant-a")

            self.assertTrue(store.connections)
            self.assertTrue(all(item.was_closed for item in store.connections))


if __name__ == "__main__":
    unittest.main()
