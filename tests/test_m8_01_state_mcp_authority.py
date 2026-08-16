"""M8-01 durable authority through the real local State MCP boundary."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from context_control_plane.durable_continuation import compose_durable_continuation
from context_control_plane.durable_operation import (
    advance_durable_operation,
    compose_durable_operation,
)
from context_control_plane.durable_state_migration import (
    migrate_typed_state_v4_to_v5,
    rollback_typed_state_v5_to_v4,
)
from context_control_plane.idea_review import migrate_typed_state_v3_to_v4
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import (
    RequestContext,
    StateMCPService,
    state_mcp_tool_definitions,
)
from context_control_plane.typed_state_migration import migrate_v2alpha1_to_v3alpha1


class _AllowAuthorizer:
    def authorize(self, context, action, project_id):
        return True


class M801StateMCPAuthorityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        root = Path(__file__).parents[1]
        fixture_set = yaml.safe_load(
            (root / "experiments/state/m2-01-core-fixtures.yaml").read_text(
                encoding="utf-8"
            )
        )
        cls.snapshot = copy.deepcopy(
            next(
                case["document"]
                for case in fixture_set["cases"]
                if case["case_id"] == "solo-active-work"
            )
        )

    def _ready_snapshot(self) -> dict:
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["schema_version"] = "context.typed-state/v2alpha1"
        snapshot["project"]["updated_at"] = "2026-08-10T04:00:00+08:00"
        snapshot["project"]["active_work_ids"] = []
        snapshot["project"]["primary_work_id"] = None
        snapshot["project"]["open_blocker_ids"] = []
        snapshot["blockers"] = []
        work = next(item for item in snapshot["works"] if item["work_id"] == "work-solo")
        work.update(
            {
                "status": "ready",
                "parent_work_id": "goal-m8-01",
                "owner_refs": ["actor-second"],
                "dedupe_status": "clear",
                "overlap_candidate_ids": [],
                "blocker_ids": [],
                "return_point_work_id": None,
                "exit_criteria": [],
                "attempt_budget": None,
                "expires_at": None,
                "promotion_target_work_id": None,
                "mainline_authority": True,
            }
        )
        work["scope_refs"] = [
            {"scope_kind": "file", "scope_ref": "repo://control-plane/src/core.py"}
        ]
        common = {
            "status": "ready",
            "dependency_ids": [],
            "owner_refs": ["actor-owner"],
            "overlap_candidate_ids": [],
            "dedupe_status": "clear",
            "supersedes_work_id": None,
            "evidence_ids": [],
            "blocker_ids": [],
            "revision": 1,
            "return_point_work_id": None,
            "exit_criteria": [],
            "attempt_budget": None,
            "expires_at": None,
            "promotion_target_work_id": None,
            "mainline_authority": True,
        }
        snapshot["works"] = [
            {
                **common,
                "work_id": "campaign-m8",
                "kind": "campaign",
                "title": "M8 campaign",
                "parent_work_id": None,
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "m8/campaign"}
                ],
            },
            {
                **common,
                "work_id": "goal-m8-01",
                "kind": "goal",
                "title": "M8-01 goal",
                "parent_work_id": "campaign-m8",
                "scope_refs": [
                    {"scope_kind": "capability", "scope_ref": "m8/durable"}
                ],
            },
            work,
        ]
        snapshot["claims"] = []
        snapshot["effects"] = []
        v3 = migrate_v2alpha1_to_v3alpha1(snapshot)
        v4 = migrate_typed_state_v3_to_v4(
            v3, migrated_at="2026-08-10T04:00:00+08:00"
        )
        return migrate_typed_state_v4_to_v5(v4)

    @staticmethod
    def _service(store: SQLiteStateStore) -> StateMCPService:
        return StateMCPService(
            store,
            authorizer=_AllowAuthorizer(),
            registry_digest="a" * 64,
            clock=lambda: "2026-08-10T05:00:00+08:00",
            event_id_factory=lambda request_id: f"event-m8-01-{request_id}",
        )

    @staticmethod
    def _claim(service: StateMCPService, context: RequestContext) -> dict:
        return service.call_tool(
            "context.state.claim",
            {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": "request-m8-01-claim",
                "project_id": "project-solo",
                "expected_revision": 7,
                "work_id": "work-solo",
                "claim_id": "claim-m8-01",
                "scope_owners": [
                    {
                        "scope_kind": "file",
                        "scope_ref": "repo://control-plane/src/core.py",
                    }
                ],
                "lease_expires_at": "2026-08-10T06:00:00+08:00",
                "causation_ref": "work:M8-01",
                "correlation_ref": "campaign:M8",
            },
            context=context,
        )

    @staticmethod
    def _operation(
        claim_response: dict, *, checkpoint_ref: dict | None = None
    ) -> dict:
        authority = {
            "project_revision": claim_response["result"]["revision"],
            "event_head": claim_response["result"]["event_head"],
        }
        effect = {
            "effect_id": "effect-m8-01-state-mcp",
            "effect_key": "effect-key-m8-01-state-mcp",
            "operation": "write-artifact",
            "scope_ref": {
                "scope_kind": "file",
                "scope_ref": "repo://control-plane/src/core.py",
            },
            "adapter_id": "fixture.idempotent-effect/v1",
            "request_sha256": "b" * 64,
            "replay_policy": "safe",
            "idempotency_mode": "effect-key",
            "status_lookup": "supported",
        }
        draft = compose_durable_operation(
            operation_id="operation/m8-01/state-mcp",
            project_id="project-solo",
            work_id="work-solo",
            claim_id="claim-m8-01",
            authority=authority,
            effect=effect,
            checkpoint_ref=checkpoint_ref
            or {
                "schema_version": "context.artifact-ref/v1alpha1",
                "digest_algorithm": "sha-256",
                "digest": "c" * 64,
                "size_bytes": 4096,
                "artifact_uri": "artifact://sha256/" + "c" * 64,
            },
            continuation_sha256="d" * 64,
            trace_binding={
                "trace_id": "1" * 32,
                "span_id": "2" * 16,
                "run_id": "run/m8-01/state-mcp",
                "correlation_id": "correlation/m8",
            },
            observed_at="2026-08-10T05:00:00+08:00",
        )
        continuation = compose_durable_continuation(
            operation_id=draft["operation_id"],
            project_id=draft["project_id"],
            project_revision=authority["project_revision"],
            task_id=draft["work_id"],
            task_revision=1,
            event_head=authority["event_head"],
            phase="prepared",
            last_durable_action="operation-created",
            next_action="commit-intent",
            acknowledged_input_ids=[],
            reserved_effects=[
                {
                    "effect_id": effect["effect_id"],
                    "replay_policy": "safe",
                    "status": "reserved",
                }
            ],
            response_mode="continue-silently",
        )
        return compose_durable_operation(
            **{
                **{
                    key: value
                    for key, value in draft.items()
                    if key
                    in {
                        "operation_id",
                        "project_id",
                        "work_id",
                        "claim_id",
                        "authority",
                        "effect",
                        "checkpoint_ref",
                        "trace_binding",
                    }
                },
                "continuation_sha256": continuation["state_sha256"],
                "observed_at": draft["created_at"],
            }
        )

    def test_effect_tool_preserves_v1_and_requires_digest_in_v2(self) -> None:
        definition = next(
            item
            for item in state_mcp_tool_definitions()
            if item["name"] == "context.state.effect"
        )["inputSchema"]

        self.assertEqual(len(definition["oneOf"]), 2)
        v1, v2 = definition["oneOf"]
        self.assertEqual(
            v1["properties"]["schema_version"]["const"],
            "context.state-mcp-request/v1alpha1",
        )
        self.assertNotIn("request_sha256", v1["properties"])
        self.assertEqual(
            v2["properties"]["schema_version"]["const"],
            "context.state-mcp-request/v2alpha1",
        )
        self.assertIn("request_sha256", v2["required"])

    def test_effect_v2_requires_digest_and_typed_state_v5(self) -> None:
        context = RequestContext("actor-second", "authorization-test")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(
                rollback_typed_state_v5_to_v4(self._ready_snapshot())
            )
            service = self._service(store)
            claim = self._claim(service, context)
            self.assertTrue(claim["ok"], claim["error"])
            operation = self._operation(claim)
            request = {
                "schema_version": "context.state-mcp-request/v2alpha1",
                "request_id": "request-m8-01-effect-v2",
                "project_id": operation["project_id"],
                "expected_revision": claim["result"]["revision"],
                "action": "authorize",
                "effect_id": operation["effect"]["effect_id"],
                "effect_key": operation["effect"]["effect_key"],
                "work_id": operation["work_id"],
                "claim_id": operation["claim_id"],
                "operation": operation["effect"]["operation"],
                "scope_ref": operation["effect"]["scope_ref"],
                "result_ref": None,
                "evidence_ids": [],
                "causation_ref": "work:M8-01",
                "correlation_ref": "campaign:M8",
            }

            missing_digest = service.call_tool(
                "context.state.effect", request, context=context
            )
            self.assertEqual(missing_digest["error"]["code"], "invalid_request")

            request["request_sha256"] = operation["effect"]["request_sha256"]
            migration_required = service.call_tool(
                "context.state.effect", request, context=context
            )
            self.assertEqual(migration_required["error"]["code"], "unsupported")
            self.assertIn("typed state v5", migration_required["error"]["message"])

            legacy_with_digest = copy.deepcopy(request)
            legacy_with_digest["schema_version"] = (
                "context.state-mcp-request/v1alpha1"
            )
            invalid_legacy = service.call_tool(
                "context.state.effect", legacy_with_digest, context=context
            )
            self.assertEqual(invalid_legacy["error"]["code"], "invalid_request")

    def test_durable_authority_emits_the_digest_bound_v2_request(self) -> None:
        from context_control_plane.durable_state_authority import (
            StateMCPDurableAuthorityAdapter,
        )

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            adapter = StateMCPDurableAuthorityAdapter(
                self._service(store),
                context=RequestContext("actor-second", "authorization-test"),
            )
            request = adapter._request(
                self._operation(
                    {
                        "result": {
                            "revision": 8,
                            "event_head": {
                                "sequence_no": 1,
                                "event_sha256": "a" * 64,
                            },
                        }
                    }
                ),
                action="authorize",
                expected_revision=8,
            )

        self.assertEqual(
            request["schema_version"], "context.state-mcp-request/v2alpha1"
        )
        self.assertEqual(request["request_sha256"], "b" * 64)
        self.assertEqual(
            adapter.capability_manifest["adapter_id"], "context.state-mcp/v2"
        )
        self.assertEqual(
            adapter.capability_manifest["adapter_version"], "1.1.0-alpha.1"
        )

    def test_authorize_and_complete_reconcile_after_service_restart(self) -> None:
        from context_control_plane.durable_state_authority import (
            StateMCPDurableAuthorityAdapter,
            validate_durable_state_receipt,
        )

        context = RequestContext("actor-second", "authorization-test")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(self._ready_snapshot())
            service = self._service(store)
            claim = self._claim(service, context)
            self.assertTrue(claim["ok"], claim["error"])
            prepared = self._operation(claim)

            adapter = StateMCPDurableAuthorityAdapter(service, context=context)
            authorized = adapter.commit_intent(prepared)
            validate_durable_state_receipt(
                authorized, operation=prepared, expected_action="authorize"
            )

            restarted = StateMCPDurableAuthorityAdapter(
                self._service(store), context=context
            )
            reconciled_authorize = restarted.commit_intent(prepared)
            validate_durable_state_receipt(
                reconciled_authorize,
                operation=prepared,
                expected_action="authorize",
            )

            intent = advance_durable_operation(
                prepared,
                phase="intent-committed",
                observed_at="2026-08-10T05:00:01+08:00",
                continuation_sha256="e" * 64,
                intent_ref="state-receipt://sha256/" + authorized["receipt_sha256"],
            )
            started = advance_durable_operation(
                intent,
                phase="effect-in-flight",
                observed_at="2026-08-10T05:00:02+08:00",
                continuation_sha256="f" * 64,
                start_ref="attempt://effect-m8-01-state-mcp/1",
            )
            settled = advance_durable_operation(
                started,
                phase="effect-settled",
                observed_at="2026-08-10T05:00:03+08:00",
                continuation_sha256="7" * 64,
                settlement_ref="settlement://m8-01/state-mcp",
                result_ref="artifact://sha256/" + "4" * 64,
            )
            completed = restarted.commit_state(settled)
            validate_durable_state_receipt(
                completed, operation=settled, expected_action="complete"
            )
            reconciled_complete = StateMCPDurableAuthorityAdapter(
                self._service(store), context=context
            ).commit_state(settled)
            validate_durable_state_receipt(
                reconciled_complete,
                operation=settled,
                expected_action="complete",
            )
            snapshot = store.read_project("project-solo")
            events = store.read_events("project-solo")

        effect = next(
            item
            for item in snapshot["effects"]
            if item["effect_id"] == "effect-m8-01-state-mcp"
        )
        self.assertEqual(effect["status"], "succeeded")
        self.assertEqual(snapshot["project"]["revision"], 10)
        self.assertEqual(len(events), 3)
        self.assertFalse(authorized["reconciled"])
        self.assertTrue(reconciled_authorize["reconciled"])
        self.assertFalse(completed["reconciled"])
        self.assertTrue(reconciled_complete["reconciled"])

    def test_reconciliation_rejects_the_same_effect_identity_with_new_payload(self) -> None:
        from context_control_plane.durable_state_authority import (
            DurableStateAuthorityError,
            StateMCPDurableAuthorityAdapter,
        )

        context = RequestContext("actor-second", "authorization-test")
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            store.initialize()
            store.create_project(self._ready_snapshot())
            service = self._service(store)
            claim = self._claim(service, context)
            prepared = self._operation(claim)
            StateMCPDurableAuthorityAdapter(service, context=context).commit_intent(
                prepared
            )
            changed_effect = copy.deepcopy(prepared["effect"])
            changed_effect["request_sha256"] = "1" * 64
            changed = compose_durable_operation(
                operation_id=prepared["operation_id"],
                project_id=prepared["project_id"],
                work_id=prepared["work_id"],
                claim_id=prepared["claim_id"],
                authority=prepared["authority"],
                effect=changed_effect,
                checkpoint_ref=prepared["checkpoint_ref"],
                continuation_sha256=prepared["continuation_sha256"],
                trace_binding=prepared["trace_binding"],
                observed_at=prepared["created_at"],
            )

            restarted = StateMCPDurableAuthorityAdapter(
                self._service(store), context=context
            )
            with self.assertRaises(DurableStateAuthorityError):
                restarted.commit_intent(changed)

            snapshot = store.read_project("project-solo")

        effect = next(
            item
            for item in snapshot["effects"]
            if item["effect_id"] == prepared["effect"]["effect_id"]
        )
        self.assertEqual(effect["request_sha256"], "b" * 64)


if __name__ == "__main__":
    unittest.main()
