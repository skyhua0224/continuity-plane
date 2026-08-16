"""M8-02 provider-neutral State MCP v3 shared-state boundary."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from context_control_plane.shared_state_mcp import (
    CLAIM_LIFECYCLE_TOOL,
    EFFECT_DISPATCH_TOOL,
    REQUEST_SCHEMA_VERSION,
    RESPONSE_SCHEMA_VERSION,
    SharedStateMCPService,
    shared_state_mcp_tool_definitions,
)
from context_control_plane.shared_state_migration import migrate_typed_state_v5_to_v6
from context_control_plane.shared_work_ledger import WorkLedger
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import RequestContext
from context_control_plane.state_store_capabilities_v2 import (
    SQLiteLocalCoordinatorStateStore,
)
from tests.test_m8_02_typed_state_v6_migration import _v5_snapshot

BASE_TIME = "2026-08-16T10:00:00+00:00"


def _scope(ref: str = "capability/a") -> dict[str, str]:
    return {"scope_kind": "capability", "scope_ref": ref}


def _ledger() -> WorkLedger:
    return WorkLedger(
        project_id="project-m8-02",
        project_revision=7,
        works=[
            {
                "work_id": "work-a",
                "status": "ready",
                "identity_key": "identity-a",
                "scope_refs": [_scope()],
            }
        ],
        max_ttl_ms=1_000,
    )


def _canonical_coordinator_snapshot() -> dict:
    snapshot = migrate_typed_state_v5_to_v6(_v5_snapshot())
    snapshot["project"]["project_id"] = "project-m8-02"
    snapshot["project"]["revision"] = 8
    work = next(item for item in snapshot["works"] if item["work_id"] == "work-active")
    work["status"] = "ready"
    work["owner_refs"] = ["actor-a"]
    work["scope_refs"] = [_scope()]
    snapshot["project"]["active_work_ids"] = []
    snapshot["project"]["primary_work_id"] = None
    snapshot["claims"] = []
    snapshot["effects"] = []
    snapshot["project"]["effect_high_watermark"] = 0
    return snapshot


class _Clock:
    def __init__(self, value: str = BASE_TIME) -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


class _RecordingAuthorizer:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[RequestContext, str, str]] = []

    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        self.calls.append((context, action, project_id))
        return self.allowed


def _acquire_request(
    *, request_id: str = "request-acquire", requested_ttl_ms: int = 500
) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "project_id": "project-m8-02",
        "action": "acquire",
        "expected_project_revision": 7,
        "work_id": "work-a",
        "claim_id": "claim-a",
        "requested_ttl_ms": requested_ttl_ms,
        "scope_owners": [_scope()],
    }


def _dispatch_request(
    *, request_id: str = "request-dispatch", expected_project_revision: int = 8
) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "project_id": "project-m8-02",
        "effect_id": "effect-a",
        "effect_key": "effect-key-a",
        "request_sha256": "a" * 64,
        "claim_id": "claim-a",
        "work_id": "work-a",
        "expected_project_revision": expected_project_revision,
        "expected_claim_revision": 1,
        "lease_epoch": 1,
        "fence": 1,
        "operation": "write-artifact",
        "scope_ref": _scope(),
    }


class M802SharedStateMCPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.authorizer = _RecordingAuthorizer()
        self.ledger = _ledger()
        self.service = SharedStateMCPService(
            self.ledger,
            authorizer=self.authorizer,
            clock=self.clock,
        )
        self.context = RequestContext("actor-a", "authorization-a")

    def test_acquire_uses_trusted_identity_clock_and_action_authorization(self):
        response = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            _acquire_request(),
            context=self.context,
        )

        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["schema_version"], RESPONSE_SCHEMA_VERSION)
        claim = response["result"]["claim"]
        self.assertEqual(claim["actor_ref"], "actor-a")
        self.assertEqual(claim["claimed_at"], BASE_TIME)
        self.assertEqual(claim["lease_expires_at"], "2026-08-16T10:00:00.500000+00:00")
        self.assertEqual(
            self.authorizer.calls,
            [(self.context, "state.claim.acquire", "project-m8-02")],
        )

    def test_client_time_and_absolute_expiry_are_rejected_without_mutation(self):
        for field, value in (
            ("observed_at", BASE_TIME),
            ("lease_expires_at", "2026-08-16T10:00:00.500000+00:00"),
            ("actor_ref", "forged-actor"),
        ):
            with self.subTest(field=field):
                request = {**_acquire_request(), field: value}
                before = self.ledger.snapshot()

                response = self.service.call_tool(
                    CLAIM_LIFECYCLE_TOOL,
                    request,
                    context=self.context,
                )

                self.assertFalse(response["ok"])
                self.assertEqual(response["error"]["code"], "invalid_request")
                self.assertEqual(response["error"]["reason"], "unexpected_fields")
                self.assertEqual(self.ledger.snapshot(), before)

    def test_exact_replay_is_idempotent_and_changed_payload_conflicts(self):
        request = _acquire_request()
        first = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL, request, context=self.context
        )
        after_first = self.ledger.snapshot()

        replay = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL, copy.deepcopy(request), context=self.context
        )
        changed = {**request, "requested_ttl_ms": 600}
        conflict = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL, changed, context=self.context
        )

        self.assertEqual(replay, first)
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "conflict")
        self.assertEqual(conflict["error"]["reason"], "request_id_reused")
        self.assertEqual(self.ledger.snapshot(), after_first)

    def test_denial_and_ledger_rejection_leave_state_unchanged(self):
        denied_ledger = _ledger()
        denied_service = SharedStateMCPService(
            denied_ledger,
            authorizer=_RecordingAuthorizer(allowed=False),
            clock=self.clock,
        )
        before = denied_ledger.snapshot()
        denied = denied_service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            _acquire_request(),
            context=self.context,
        )
        self.assertEqual(denied["error"]["code"], "permission_denied")
        self.assertEqual(denied_ledger.snapshot(), before)

        before = self.ledger.snapshot()
        rejected = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            _acquire_request(requested_ttl_ms=1_001),
            context=self.context,
        )
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["error"]["code"], "claim_rejected")
        self.assertEqual(rejected["error"]["reason"], "ttl_out_of_bounds")
        self.assertEqual(self.ledger.snapshot(), before)

    def test_each_lifecycle_action_has_distinct_authorization_action(self):
        acquire = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL, _acquire_request(), context=self.context
        )
        self.assertTrue(acquire["ok"], acquire["error"])
        claim = acquire["result"]["claim"]

        requests = [
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-heartbeat",
                "project_id": "project-m8-02",
                "action": "heartbeat",
                "expected_project_revision": 8,
                "claim_id": "claim-a",
                "expected_claim_revision": claim["claim_revision"],
                "lease_epoch": claim["lease_epoch"],
                "fence": claim["lease_epoch"],
                "requested_ttl_ms": 600,
            },
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-release",
                "project_id": "project-m8-02",
                "action": "release",
                "expected_project_revision": 9,
                "claim_id": "claim-a",
                "expected_claim_revision": 2,
                "lease_epoch": claim["lease_epoch"],
                "fence": claim["lease_epoch"],
            },
        ]
        for request in requests:
            response = self.service.call_tool(
                CLAIM_LIFECYCLE_TOOL, request, context=self.context
            )
            self.assertTrue(response["ok"], response["error"])

        observed = [call[1] for call in self.authorizer.calls]
        self.assertEqual(
            observed,
            [
                "state.claim.acquire",
                "state.claim.heartbeat",
                "state.claim.release",
            ],
        )

    def test_effect_dispatch_persists_start_and_uses_trusted_clock(self):
        acquired = self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL, _acquire_request(), context=self.context
        )
        self.assertTrue(acquired["ok"], acquired["error"])
        self.clock.value = "2026-08-16T10:00:00.100000+00:00"

        response = self.service.call_tool(
            EFFECT_DISPATCH_TOOL,
            _dispatch_request(),
            context=self.context,
        )

        self.assertTrue(response["ok"], response["error"])
        self.assertEqual(response["result"]["status"], "accepted")
        self.assertEqual(
            response["result"]["effect"]["dispatch_started_at"], self.clock.value
        )
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot["project_revision"], 9)
        self.assertEqual(snapshot["effects"][0]["status"], "started")
        self.assertEqual(
            self.authorizer.calls[-1][1],
            "state.effect.dispatch",
        )

    def test_effect_dispatch_replay_is_idempotent_and_drift_conflicts(self):
        self.service.call_tool(
            CLAIM_LIFECYCLE_TOOL, _acquire_request(), context=self.context
        )
        request = _dispatch_request()
        first = self.service.call_tool(
            EFFECT_DISPATCH_TOOL, request, context=self.context
        )
        after_first = self.ledger.snapshot()
        replay = self.service.call_tool(
            EFFECT_DISPATCH_TOOL, copy.deepcopy(request), context=self.context
        )
        changed = {**request, "operation": "write-other-artifact"}
        conflict = self.service.call_tool(
            EFFECT_DISPATCH_TOOL, changed, context=self.context
        )

        self.assertEqual(replay, first)
        self.assertEqual(self.ledger.snapshot(), after_first)
        self.assertEqual(conflict["error"]["code"], "conflict")
        self.assertEqual(conflict["error"]["reason"], "request_id_reused")

    def test_tool_definitions_publish_strict_action_union(self):
        definitions = {
            item["name"]: item for item in shared_state_mcp_tool_definitions()
        }
        self.assertEqual(set(definitions), {CLAIM_LIFECYCLE_TOOL, EFFECT_DISPATCH_TOOL})
        lifecycle = definitions[CLAIM_LIFECYCLE_TOOL]["inputSchema"]
        actions = {
            branch["properties"]["action"]["const"] for branch in lifecycle["oneOf"]
        }
        self.assertEqual(
            actions, {"acquire", "heartbeat", "release", "revoke", "expire", "reclaim"}
        )
        for branch in lifecycle["oneOf"]:
            self.assertFalse(branch["additionalProperties"])
            self.assertEqual(set(branch["required"]), set(branch["properties"]))
            self.assertNotIn("observed_at", branch["properties"])
            self.assertNotIn("lease_expires_at", branch["properties"])

    def test_sqlite_coordinator_replays_exact_request_after_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            base = SQLiteStateStore(database)
            base.initialize()
            source = _canonical_coordinator_snapshot()
            base.create_project(source)
            coordinator = SQLiteLocalCoordinatorStateStore(base)
            coordinator.initialize_work_ledger(
                project_id=source["project"]["project_id"],
                project_revision=source["project"]["revision"],
                works=source["works"],
                max_ttl_ms=1_000,
            )
            first_service = SharedStateMCPService(
                coordinator,
                authorizer=self.authorizer,
                clock=_Clock(BASE_TIME),
            )
            request = {
                **_acquire_request(),
                "expected_project_revision": source["project"]["revision"],
                "work_id": "work-active",
            }

            first = first_service.call_tool(
                CLAIM_LIFECYCLE_TOOL, request, context=self.context
            )
            reopened = SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database))
            replay_service = SharedStateMCPService(
                reopened,
                authorizer=self.authorizer,
                clock=_Clock("2026-08-16T10:00:00.250000+00:00"),
            )
            replay = replay_service.call_tool(
                CLAIM_LIFECYCLE_TOOL, copy.deepcopy(request), context=self.context
            )
            conflict = SharedStateMCPService(
                SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database)),
                authorizer=self.authorizer,
                clock=_Clock("2026-08-16T10:00:00.300000+00:00"),
            ).call_tool(
                CLAIM_LIFECYCLE_TOOL,
                {**request, "requested_ttl_ms": 600},
                context=self.context,
            )
            cross_action = SharedStateMCPService(
                SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database)),
                authorizer=self.authorizer,
                clock=_Clock("2026-08-16T10:00:00.350000+00:00"),
            ).call_tool(
                CLAIM_LIFECYCLE_TOOL,
                {
                    "schema_version": REQUEST_SCHEMA_VERSION,
                    "request_id": request["request_id"],
                    "project_id": request["project_id"],
                    "action": "heartbeat",
                    "expected_project_revision": source["project"]["revision"] + 1,
                    "claim_id": "claim-a",
                    "expected_claim_revision": 1,
                    "lease_epoch": 1,
                    "fence": 1,
                    "requested_ttl_ms": 500,
                },
                context=self.context,
            )

            self.assertTrue(first["ok"], first["error"])
            self.assertEqual(replay, first)
            self.assertEqual(conflict["error"]["code"], "conflict")
            self.assertEqual(conflict["error"]["reason"], "request_id_reused")
            self.assertEqual(cross_action["error"]["code"], "conflict")
            self.assertEqual(cross_action["error"]["reason"], "request_id_reused")
            self.assertEqual(
                reopened.read_work_ledger(source["project"]["project_id"])[
                    "project_revision"
                ],
                source["project"]["revision"] + 1,
            )

    def test_sqlite_coordinator_persists_effect_dispatch_across_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            base = SQLiteStateStore(database)
            base.initialize()
            source = _canonical_coordinator_snapshot()
            base.create_project(source)
            coordinator = SQLiteLocalCoordinatorStateStore(base)
            coordinator.initialize_work_ledger(
                project_id=source["project"]["project_id"],
                project_revision=source["project"]["revision"],
                works=source["works"],
                max_ttl_ms=1_000,
            )
            service = SharedStateMCPService(
                coordinator,
                authorizer=self.authorizer,
                clock=_Clock(BASE_TIME),
            )
            acquired = service.call_tool(
                CLAIM_LIFECYCLE_TOOL,
                {
                    **_acquire_request(),
                    "expected_project_revision": source["project"]["revision"],
                    "work_id": "work-active",
                },
                context=self.context,
            )
            self.assertTrue(acquired["ok"], acquired["error"])
            service = SharedStateMCPService(
                SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database)),
                authorizer=self.authorizer,
                clock=_Clock("2026-08-16T10:00:00.100000+00:00"),
            )

            first = service.call_tool(
                EFFECT_DISPATCH_TOOL,
                {
                    **_dispatch_request(),
                    "expected_project_revision": source["project"]["revision"] + 1,
                    "work_id": "work-active",
                },
                context=self.context,
            )
            replay = SharedStateMCPService(
                SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database)),
                authorizer=self.authorizer,
                clock=_Clock("2026-08-16T10:00:00.200000+00:00"),
            ).call_tool(
                EFFECT_DISPATCH_TOOL,
                {
                    **_dispatch_request(),
                    "expected_project_revision": source["project"]["revision"] + 1,
                    "work_id": "work-active",
                },
                context=self.context,
            )

            self.assertTrue(first["ok"], first["error"])
            self.assertEqual(replay, first)
            snapshot = coordinator.read_work_ledger(source["project"]["project_id"])
            self.assertEqual(
                snapshot["project_revision"], source["project"]["revision"] + 2
            )
            self.assertEqual(len(snapshot["effects"]), 1)


class M802SharedStateMCPSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "m8-02"
            / "state-mcp-v3alpha1.schema.json"
        )
        cls.schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def test_schema_accepts_every_request_action_and_both_response_states(self):
        lifecycle_requests = [
            _acquire_request(),
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-heartbeat",
                "project_id": "project-m8-02",
                "action": "heartbeat",
                "expected_project_revision": 8,
                "claim_id": "claim-a",
                "expected_claim_revision": 1,
                "lease_epoch": 1,
                "fence": 1,
                "requested_ttl_ms": 500,
            },
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-release",
                "project_id": "project-m8-02",
                "action": "release",
                "expected_project_revision": 8,
                "claim_id": "claim-a",
                "expected_claim_revision": 1,
                "lease_epoch": 1,
                "fence": 1,
            },
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-revoke",
                "project_id": "project-m8-02",
                "action": "revoke",
                "expected_project_revision": 8,
                "claim_id": "claim-a",
                "expected_claim_revision": 1,
                "lease_epoch": 1,
                "fence": 1,
                "reason": "operator requested revocation",
            },
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-expire",
                "project_id": "project-m8-02",
                "action": "expire",
                "expected_project_revision": 8,
                "claim_id": "claim-a",
                "expected_claim_revision": 1,
                "lease_epoch": 1,
                "fence": 1,
            },
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-reclaim",
                "project_id": "project-m8-02",
                "action": "reclaim",
                "expected_project_revision": 8,
                "old_claim_id": "claim-a",
                "new_claim_id": "claim-b",
                "expected_claim_revision": 1,
                "lease_epoch": 1,
                "fence": 1,
                "requested_ttl_ms": 500,
                "scope_owners": [_scope()],
            },
        ]
        success = {
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "request_id": "request-acquire",
            "tool": CLAIM_LIFECYCLE_TOOL,
            "ok": True,
            "result": {
                "schema_version": "context.work-claim-receipt/v1alpha1",
                "operation": "acquire",
                "status": "accepted",
                "project_revision": 8,
                "claim": {"claim_id": "claim-a"},
                "receipt": {
                    "receipt_sha256": "a" * 64,
                    "fence": 1,
                    "claim": {"claim_id": "claim-a"},
                },
                "transition": {
                    "schema_version": "context.work-claim-transition/v1alpha1",
                    "operation": "acquire",
                    "project_id": "project-m8-02",
                    "project_revision_before": 7,
                    "project_revision_after": 8,
                    "actor_ref": "actor-a",
                    "observed_at": BASE_TIME,
                    "previous_transition_sha256": None,
                    "changes": {},
                    "transition_id": "transition-a",
                    "transition_sha256": "b" * 64,
                },
            },
            "error": None,
        }
        error = {
            "schema_version": RESPONSE_SCHEMA_VERSION,
            "request_id": "request-acquire",
            "tool": CLAIM_LIFECYCLE_TOOL,
            "ok": False,
            "result": None,
            "error": {"code": "claim_rejected", "reason": "ttl_out_of_bounds"},
        }
        for document in (*lifecycle_requests, _dispatch_request(), success, error):
            with self.subTest(document=document):
                self.validator.validate(document)

    def test_schema_rejects_client_time_unknown_action_and_response_contradiction(self):
        with self.assertRaises(ValidationError):
            self.validator.validate({**_acquire_request(), "observed_at": BASE_TIME})
        with self.assertRaises(ValidationError):
            self.validator.validate({**_acquire_request(), "action": "steal"})
        with self.assertRaises(ValidationError):
            self.validator.validate(
                {
                    "schema_version": RESPONSE_SCHEMA_VERSION,
                    "request_id": "request-bad",
                    "tool": CLAIM_LIFECYCLE_TOOL,
                    "ok": True,
                    "result": {"status": "accepted"},
                    "error": {"code": "conflict", "reason": "contradictory"},
                }
            )


if __name__ == "__main__":
    unittest.main()
