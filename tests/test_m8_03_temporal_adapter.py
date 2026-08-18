"""M8-03 lazy optional Temporal adapter mapping."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path

from context_control_plane.durable_workflow import (
    DurableWorkflowError,
    activate_workflow_patch,
    build_workflow_definition,
    continue_workflow_as_new,
    create_workflow_run,
)
from context_control_plane.shared_state_mcp import (
    CLAIM_LIFECYCLE_TOOL,
    EFFECT_DISPATCH_TOOL,
    REQUEST_SCHEMA_VERSION,
    SharedStateMCPService,
)
from context_control_plane.shared_work_ledger import WorkLedger
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import RequestContext
from context_control_plane.state_store_capabilities_v2 import (
    SQLiteLocalCoordinatorStateStore,
)
from context_control_plane.temporal_workflow_adapter import (
    MAX_TEMPORAL_PAYLOAD_BYTES,
    TemporalWorkflowAdapter,
    bind_temporal_payload_receipt,
    build_workflow_backend_binding,
    invoke_temporal_continue_as_new,
    resolve_temporal_patch,
    validate_workflow_run_receipt,
    workflow_backend_binding_intent,
)
from context_control_plane.workflow_orchestration import WorkflowRuntimeUnavailable
from tests.test_m8_02_shared_state_mcp import _canonical_coordinator_snapshot


class _SDK:
    __version__ = "1.31.0"

    class common:
        class WorkflowIDConflictPolicy:
            USE_EXISTING = "use-existing"

        class WorkflowIDReusePolicy:
            REJECT_DUPLICATE = "reject-duplicate"


class _Handle:
    id = "wf_provider"
    run_id = None
    result_run_id = "provider-run-1"
    first_execution_run_id = "provider-run-1"


class _Client:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.namespace = "context-control-plane"
        self.calls: list[dict] = []

    async def start_workflow(
        self,
        workflow: str,
        payload: dict,
        *,
        id: str,
        task_queue: str,
        id_conflict_policy: str,
        id_reuse_policy: str,
    ) -> _Handle:
        self.calls.append(
            {
                "workflow": workflow,
                "payload": payload,
                "id": id,
                "task_queue": task_queue,
                "id_conflict_policy": id_conflict_policy,
                "id_reuse_policy": id_reuse_policy,
            }
        )
        if self.fail:
            raise TimeoutError("start result was not observed")
        handle = _Handle()
        handle.id = id
        return handle


class _WorkflowAPI:
    def __init__(self) -> None:
        self.payload: dict | None = None
        self.patch_result = True
        self.deprecated_patch_ids: list[str] = []

    def continue_as_new(self, payload: dict) -> None:
        self.payload = payload
        raise _ContinueAsNew()

    def patched(self, patch_id: str) -> bool:
        self.patch_id = patch_id
        return self.patch_result

    def deprecate_patch(self, patch_id: str) -> None:
        self.deprecated_patch_ids.append(patch_id)


class _ContinueAsNew(RuntimeError):
    pass


class _Authorizer:
    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return True


class M803TemporalAdapterTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def workflow_run() -> dict:
        return create_workflow_run(
            project_id="project-m8-03",
            root_work_id="M8-03",
            request_id="request-m8-03",
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="a" * 64,
            authority={
                "project_revision": 27,
                "event_head": {"sequence_no": 27, "event_sha256": "b" * 64},
            },
            checkpoint_ref="artifact://sha256/" + "c" * 64,
            checkpoint_sha256="c" * 64,
            continuation_sha256="d" * 64,
            started_at="2026-08-16T19:00:00+08:00",
        )

    @staticmethod
    def adapter(client: _Client, *, state_mcp_resolver=None) -> TemporalWorkflowAdapter:
        return TemporalWorkflowAdapter(
            client=client,
            namespace="context-control-plane",
            task_queue="context-m8-03",
            workflow_name="context.control-plane.campaign",
            adapter_version="1.0.0",
            implementation_sha256="a" * 64,
            worker_deployment="context-control-plane",
            worker_build_id="m8-03-v1",
            sdk_loader=lambda: _SDK(),
            state_mcp_resolver=state_mcp_resolver,
        )

    @staticmethod
    def state_mcp_authority(run: dict) -> tuple[dict, object]:
        scope = {"scope_kind": "capability", "scope_ref": "workflow-backend"}
        ledger = WorkLedger(
            project_id=run["project_id"],
            project_revision=run["authority"]["project_revision"] - 1,
            works=[
                {
                    "work_id": run["root_work_id"],
                    "status": "ready",
                    "identity_key": "m8-03-temporal-binding",
                    "scope_refs": [scope],
                }
            ],
            max_ttl_ms=10_000,
        )
        service = SharedStateMCPService(
            ledger,
            authorizer=_Authorizer(),
            clock=lambda: "2026-08-16T11:00:00+00:00",
        )
        context = RequestContext("actor-m8-03", "authorization-m8-03")
        acquired = service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-m8-03-claim",
                "project_id": run["project_id"],
                "action": "acquire",
                "expected_project_revision": run["authority"]["project_revision"] - 1,
                "work_id": run["root_work_id"],
                "claim_id": "claim-m8-03-binding",
                "requested_ttl_ms": 5_000,
                "scope_owners": [scope],
            },
            context=context,
        )
        assert acquired["ok"], acquired
        intent = workflow_backend_binding_intent(run, backend_id="temporal")
        dispatched = service.call_tool(
            EFFECT_DISPATCH_TOOL,
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-m8-03-binding",
                "project_id": run["project_id"],
                "effect_id": intent["effect_id"],
                "effect_key": intent["effect_key"],
                "request_sha256": intent["request_sha256"],
                "claim_id": "claim-m8-03-binding",
                "work_id": run["root_work_id"],
                "expected_project_revision": run["authority"]["project_revision"],
                "expected_claim_revision": 1,
                "lease_epoch": 1,
                "fence": 1,
                "operation": intent["operation"],
                "scope_ref": scope,
            },
            context=context,
        )
        assert dispatched["ok"], dispatched
        return dispatched["result"], service.resolve_committed_effect_dispatch

    @classmethod
    def backend_authority(cls, run: dict) -> tuple[dict, object]:
        result, resolver = cls.state_mcp_authority(run)
        binding = build_workflow_backend_binding(
            run,
            backend_id="temporal",
            state_mcp_result=result,
            state_mcp_resolver=resolver,
        )
        return binding, resolver

    async def test_start_maps_stable_identity_without_copying_history(self) -> None:
        client = _Client()
        run = self.workflow_run()
        binding, resolver = self.backend_authority(run)
        adapter = self.adapter(client, state_mcp_resolver=resolver)
        receipt = await adapter.start(
            run,
            request_id="request-temporal-start",
            request_sha256="f" * 64,
            binding_receipt=binding,
        )

        self.assertEqual(len(client.calls), 1)
        call = client.calls[0]
        self.assertEqual(call["id"], run["workflow_id"])
        self.assertEqual(call["task_queue"], "context-m8-03")
        self.assertEqual(call["id_conflict_policy"], "use-existing")
        self.assertEqual(call["id_reuse_policy"], "reject-duplicate")
        self.assertNotIn("history", call["payload"])
        self.assertEqual(call["payload"]["run_sha256"], run["run_sha256"])
        self.assertEqual(receipt["workflow_id"], run["workflow_id"])
        self.assertEqual(receipt["provider_run_id"], "provider-run-1")
        self.assertEqual(receipt["first_execution_run_id"], "provider-run-1")
        self.assertTrue(receipt["backend_binding_committed"])
        self.assertEqual(
            receipt["backend_binding_receipt_sha256"], binding["receipt_sha256"]
        )
        self.assertFalse(receipt["state_write_authority"])
        self.assertFalse(receipt["effect_dispatch_authority"])
        self.assertFalse(receipt["provider_native_authority"])
        self.assertTrue(adapter.capability_manifest["patching"])
        self.assertTrue(adapter.capability_manifest["worker_versioning_supported"])
        self.assertFalse(adapter.capability_manifest["worker_versioning_configured"])
        self.assertNotEqual(
            adapter.capability_manifest["worker_build_id"],
            adapter.capability_manifest["patch_contract"],
        )

    async def test_ambiguous_start_is_never_reported_as_safe_fallback(self) -> None:
        run = self.workflow_run()
        binding, resolver = self.backend_authority(run)
        adapter = self.adapter(_Client(fail=True), state_mcp_resolver=resolver)
        with self.assertRaisesRegex(
            WorkflowRuntimeUnavailable, "start_outcome_unknown"
        ) as raised:
            await adapter.start(
                run,
                request_id="request-temporal-start",
                request_sha256="f" * 64,
                binding_receipt=binding,
            )
        self.assertTrue(raised.exception.remote_execution_possible)

    async def test_start_requires_a_committed_state_mcp_backend_binding(self) -> None:
        client = _Client()
        adapter = self.adapter(client)
        run = self.workflow_run()

        with self.assertRaisesRegex(DurableWorkflowError, "backend binding"):
            await adapter.start(
                run,
                request_id="request-temporal-start",
                request_sha256="f" * 64,
                binding_receipt=None,
            )
        self.assertEqual(client.calls, [])

        binding, _ = self.backend_authority(run)
        with self.assertRaisesRegex(DurableWorkflowError, "trusted State MCP"):
            await adapter.start(
                run,
                request_id="request-temporal-start",
                request_sha256="f" * 64,
                binding_receipt=binding,
            )
        self.assertEqual(client.calls, [])

    async def test_backend_binding_resolver_survives_state_mcp_restart(self) -> None:
        source = _canonical_coordinator_snapshot()
        run = create_workflow_run(
            project_id=source["project"]["project_id"],
            root_work_id="work-active",
            request_id="request-m8-03-persistent",
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="a" * 64,
            authority={
                "project_revision": source["project"]["revision"] + 1,
                "event_head": {"sequence_no": 1, "event_sha256": "b" * 64},
            },
            checkpoint_ref="artifact://sha256/" + "c" * 64,
            checkpoint_sha256="c" * 64,
            continuation_sha256="d" * 64,
            started_at="2026-08-16T19:00:00+08:00",
        )
        scope = {"scope_kind": "capability", "scope_ref": "workflow-backend"}
        active_work = next(
            item for item in source["works"] if item["work_id"] == "work-active"
        )
        active_work["scope_refs"] = [scope]
        context = RequestContext("actor-a", "authorization-m8-03")
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            base = SQLiteStateStore(database)
            base.initialize()
            base.create_project(source)
            coordinator = SQLiteLocalCoordinatorStateStore(base)
            coordinator.initialize_work_ledger(
                project_id=source["project"]["project_id"],
                project_revision=source["project"]["revision"],
                works=source["works"],
                max_ttl_ms=10_000,
            )
            service = SharedStateMCPService(
                coordinator,
                authorizer=_Authorizer(),
                clock=lambda: "2026-08-16T11:00:00+00:00",
            )
            acquired = service.call_tool(
                CLAIM_LIFECYCLE_TOOL,
                {
                    "schema_version": REQUEST_SCHEMA_VERSION,
                    "request_id": "request-m8-03-persistent-claim",
                    "project_id": run["project_id"],
                    "action": "acquire",
                    "expected_project_revision": source["project"]["revision"],
                    "work_id": run["root_work_id"],
                    "claim_id": "claim-m8-03-persistent",
                    "requested_ttl_ms": 5_000,
                    "scope_owners": [scope],
                },
                context=context,
            )
            self.assertTrue(acquired["ok"], acquired["error"])
            intent = workflow_backend_binding_intent(run, backend_id="temporal")
            dispatched = service.call_tool(
                EFFECT_DISPATCH_TOOL,
                {
                    "schema_version": REQUEST_SCHEMA_VERSION,
                    "request_id": "request-m8-03-persistent-binding",
                    "project_id": run["project_id"],
                    "effect_id": intent["effect_id"],
                    "effect_key": intent["effect_key"],
                    "request_sha256": intent["request_sha256"],
                    "claim_id": "claim-m8-03-persistent",
                    "work_id": run["root_work_id"],
                    "expected_project_revision": run["authority"]["project_revision"],
                    "expected_claim_revision": 1,
                    "lease_epoch": 1,
                    "fence": 1,
                    "operation": intent["operation"],
                    "scope_ref": scope,
                },
                context=context,
            )
            self.assertTrue(dispatched["ok"], dispatched["error"])
            restarted = SharedStateMCPService(
                SQLiteLocalCoordinatorStateStore(SQLiteStateStore(database)),
                authorizer=_Authorizer(),
                clock=lambda: "2026-08-16T11:00:01+00:00",
            )

            binding = build_workflow_backend_binding(
                run,
                backend_id="temporal",
                state_mcp_result=dispatched["result"],
                state_mcp_resolver=restarted.resolve_committed_effect_dispatch,
            )

        self.assertEqual(
            binding["state_mcp_receipt_sha256"],
            dispatched["result"]["receipt"]["receipt_sha256"],
        )

    async def test_start_rechecks_binding_against_the_trusted_state_mcp(self) -> None:
        run = self.workflow_run()
        binding, resolver = self.backend_authority(run)
        forged = copy.deepcopy(binding)
        forged["state_mcp_receipt"]["claim_id"] = "claim-forged"
        forged["state_mcp_receipt"]["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in forged["state_mcp_receipt"].items()
                    if key != "receipt_sha256"
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        forged["state_mcp_receipt_sha256"] = forged["state_mcp_receipt"][
            "receipt_sha256"
        ]
        forged["binding_id"] = "wfb_" + hashlib.sha256(
            json.dumps(
                [
                    run["workflow_id"],
                    run["run_id"],
                    "temporal",
                    forged["state_mcp_receipt_sha256"],
                ],
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:32]
        forged["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in forged.items()
                    if key != "receipt_sha256"
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        client = _Client()
        with self.assertRaisesRegex(DurableWorkflowError, "State MCP authority"):
            await self.adapter(client, state_mcp_resolver=resolver).start(
                run,
                request_id="request-temporal-start",
                request_sha256="f" * 64,
                binding_receipt=forged,
            )
        self.assertEqual(client.calls, [])

    async def test_binding_rejects_a_forged_state_mcp_dispatch_receipt(self) -> None:
        run = self.workflow_run()
        intent = workflow_backend_binding_intent(run, backend_id="temporal")
        forged = {
            "schema_version": "context.effect-dispatch-receipt/v1alpha1",
            "request_id": "request-forged",
            "project_id": run["project_id"],
            "project_revision": run["authority"]["project_revision"] + 1,
            "work_id": run["root_work_id"],
            "claim_id": "claim-forged",
            "claim_revision": 1,
            "lease_epoch": 1,
            "fence": 1,
            "effect_id": intent["effect_id"],
            "effect_key": intent["effect_key"],
            "request_sha256": "0" * 64,
            "operation": intent["operation"],
            "scope_ref": {
                "scope_kind": "capability",
                "scope_ref": "workflow-backend",
            },
            "dispatch_started_at": "2026-08-16T19:00:01+08:00",
            "receipt_sha256": "9" * 64,
        }
        with self.assertRaisesRegex(DurableWorkflowError, "State MCP"):
            build_workflow_backend_binding(
                run,
                backend_id="temporal",
                state_mcp_result=forged,
                state_mcp_resolver=lambda project_id, request_id: None,
            )

        valid_result, resolver = self.state_mcp_authority(run)
        forged_result = copy.deepcopy(valid_result)
        forged_result["receipt"]["request_sha256"] = "0" * 64
        forged_result["receipt"]["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in forged_result["receipt"].items()
                    if key != "receipt_sha256"
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        forged_result["effect"]["request_sha256"] = "0" * 64
        forged_result["effect"]["dispatch_receipt_sha256"] = forged_result["receipt"][
            "receipt_sha256"
        ]
        forged_result["transition"]["changes"]["effect_after"] = copy.deepcopy(
            forged_result["effect"]
        )
        transition_body = {
            key: value
            for key, value in forged_result["transition"].items()
            if key not in {"transition_id", "transition_sha256"}
        }
        forged_result["transition"]["transition_sha256"] = hashlib.sha256(
            json.dumps(transition_body, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        with self.assertRaisesRegex(DurableWorkflowError, "does not match workflow"):
            build_workflow_backend_binding(
                run,
                backend_id="temporal",
                state_mcp_result=forged_result,
                state_mcp_resolver=resolver,
            )

    async def test_backend_binding_rejects_derived_identity_and_time_tamper(
        self,
    ) -> None:
        run = self.workflow_run()
        binding, resolver = self.backend_authority(run)
        for field, value in (
            ("binding_id", "wfb_" + "0" * 32),
            ("committed_at", "2026-08-16T11:00:01+00:00"),
        ):
            with self.subTest(field=field):
                forged = copy.deepcopy(binding)
                forged[field] = value
                forged["receipt_sha256"] = hashlib.sha256(
                    json.dumps(
                        {
                            key: item
                            for key, item in forged.items()
                            if key != "receipt_sha256"
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                client = _Client()

                with self.assertRaisesRegex(DurableWorkflowError, "backend binding"):
                    await self.adapter(
                        client, state_mcp_resolver=resolver
                    ).start(
                        run,
                        request_id="request-temporal-start",
                        request_sha256="f" * 64,
                        binding_receipt=forged,
                    )
                self.assertEqual(client.calls, [])

    async def test_direct_temporal_start_only_accepts_first_generation(self) -> None:
        rollover = continue_workflow_as_new(
            self.workflow_run(),
            authority={
                "project_revision": 28,
                "event_head": {"sequence_no": 28, "event_sha256": "1" * 64},
            },
            checkpoint_ref="artifact://sha256/" + "2" * 64,
            checkpoint_sha256="2" * 64,
            continuation_sha256="3" * 64,
            occurred_at="2026-08-16T19:00:01+08:00",
        )
        run = rollover["next_run"]
        client = _Client()
        binding, resolver = self.backend_authority(run)
        with self.assertRaisesRegex(DurableWorkflowError, "first generation"):
            await self.adapter(client, state_mcp_resolver=resolver).start(
                run,
                request_id="request-temporal-generation-two",
                request_sha256="f" * 64,
                binding_receipt=binding,
            )
        self.assertEqual(client.calls, [])

    async def test_payload_limit_includes_the_binding_receipt_field(self) -> None:
        field = "backend_binding_receipt_sha256"
        payload = {"padding": ""}
        bound = {**payload, field: "f" * 64}
        overhead = len(
            json.dumps(
                bound,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        payload["padding"] = "x" * (MAX_TEMPORAL_PAYLOAD_BYTES - overhead + 1)
        with self.assertRaisesRegex(DurableWorkflowError, "payload limit"):
            bind_temporal_payload_receipt(
                payload,
                receipt_field=field,
                receipt_sha256="f" * 64,
            )

    async def test_start_rejects_worker_implementation_drift_before_remote_call(
        self,
    ) -> None:
        client = _Client()
        adapter = TemporalWorkflowAdapter(
            client=client,
            namespace="context-control-plane",
            task_queue="context-m8-03",
            workflow_name="context.control-plane.campaign",
            adapter_version="1.0.0",
            implementation_sha256="e" * 64,
            worker_deployment="context-control-plane",
            worker_build_id="m8-03-v1",
            sdk_loader=lambda: _SDK(),
        )
        run = self.workflow_run()
        binding, resolver = self.backend_authority(run)
        adapter.state_mcp_resolver = resolver

        with self.assertRaisesRegex(DurableWorkflowError, "implementation"):
            await adapter.start(
                run,
                request_id="request-temporal-start",
                request_sha256="f" * 64,
                binding_receipt=binding,
            )
        self.assertEqual(client.calls, [])

    async def test_continue_as_new_uses_the_safe_rollover_receipt(self) -> None:
        rollover = continue_workflow_as_new(
            self.workflow_run(),
            authority={
                "project_revision": 28,
                "event_head": {"sequence_no": 28, "event_sha256": "1" * 64},
            },
            checkpoint_ref="artifact://sha256/" + "2" * 64,
            checkpoint_sha256="2" * 64,
            continuation_sha256="3" * 64,
            occurred_at="2026-08-16T19:00:01+08:00",
        )
        workflow_api = _WorkflowAPI()

        with self.assertRaises(_ContinueAsNew):
            invoke_temporal_continue_as_new(
                rollover["receipt"],
                closed_run=rollover["closed_run"],
                next_run=rollover["next_run"],
                workflow_api=workflow_api,
            )

        self.assertIsNotNone(workflow_api.payload)
        assert workflow_api.payload is not None
        self.assertNotIn("history", workflow_api.payload)
        self.assertEqual(workflow_api.payload["generation"], 2)
        self.assertEqual(
            workflow_api.payload["previous_run_event_sha256"],
            rollover["receipt"]["continued_from_event_sha256"],
        )

    async def test_continue_as_new_rejects_a_forged_predecessor_receipt(self) -> None:
        rollover = continue_workflow_as_new(
            self.workflow_run(),
            authority={
                "project_revision": 28,
                "event_head": {"sequence_no": 28, "event_sha256": "1" * 64},
            },
            checkpoint_ref="artifact://sha256/" + "2" * 64,
            checkpoint_sha256="2" * 64,
            continuation_sha256="3" * 64,
            occurred_at="2026-08-16T19:00:01+08:00",
        )
        forged = dict(rollover["receipt"])
        forged["from_run_id"] = "wfr_forged"
        forged["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in forged.items()
                    if key != "receipt_sha256"
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(DurableWorkflowError, "run chain"):
            invoke_temporal_continue_as_new(
                forged,
                closed_run=rollover["closed_run"],
                next_run=rollover["next_run"],
                workflow_api=_WorkflowAPI(),
            )

    async def test_patch_bridge_matches_temporal_history_decision_to_core_marker(
        self,
    ) -> None:
        run = self.workflow_run()
        definition = build_workflow_definition(
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="a" * 64,
            supported_history_versions=[1],
            patches=[
                {
                    "patch_id": "route-v1",
                    "introduced_in_version": 1,
                    "deprecated_in_version": None,
                    "semantic_sha256": "8" * 64,
                }
            ],
        )
        run = activate_workflow_patch(
            run,
            definition=definition,
            patch_id="route-v1",
            occurred_at="2026-08-16T19:00:01+08:00",
        )
        workflow_api = _WorkflowAPI()

        self.assertTrue(
            resolve_temporal_patch(
                run,
                definition=definition,
                patch_id="route-v1",
                workflow_api=workflow_api,
            )
        )
        self.assertEqual(workflow_api.patch_id, "route-v1")

        workflow_api.patch_result = False
        with self.assertRaisesRegex(DurableWorkflowError, "patch decision"):
            resolve_temporal_patch(
                run,
                definition=definition,
                patch_id="route-v1",
                workflow_api=workflow_api,
            )

    async def test_patch_bridge_replays_old_history_and_deprecates_marker(self) -> None:
        definition = build_workflow_definition(
            workflow_type="context.campaign",
            definition_version=3,
            implementation_sha256="a" * 64,
            supported_history_versions=[1, 2, 3],
            patches=[
                {
                    "patch_id": "route-v2",
                    "introduced_in_version": 2,
                    "deprecated_in_version": 3,
                    "semantic_sha256": "8" * 64,
                }
            ],
        )
        old_api = _WorkflowAPI()
        old_api.patch_result = False
        self.assertFalse(
            resolve_temporal_patch(
                self.workflow_run(),
                definition=definition,
                patch_id="route-v2",
                workflow_api=old_api,
            )
        )

        current = create_workflow_run(
            project_id="project-m8-03",
            root_work_id="M8-03-deprecated",
            request_id="request-m8-03-deprecated",
            workflow_type="context.campaign",
            definition_version=3,
            implementation_sha256="a" * 64,
            authority={
                "project_revision": 27,
                "event_head": {"sequence_no": 27, "event_sha256": "b" * 64},
            },
            checkpoint_ref="artifact://sha256/" + "c" * 64,
            checkpoint_sha256="c" * 64,
            continuation_sha256="d" * 64,
            started_at="2026-08-16T19:00:00+08:00",
        )
        current_api = _WorkflowAPI()
        self.assertTrue(
            resolve_temporal_patch(
                current,
                definition=definition,
                patch_id="route-v2",
                workflow_api=current_api,
            )
        )
        self.assertEqual(current_api.deprecated_patch_ids, ["route-v2"])

    async def test_workflow_run_receipt_validator_rejects_semantic_tamper(self) -> None:
        run = self.workflow_run()
        binding, resolver = self.backend_authority(run)
        adapter = self.adapter(_Client(), state_mcp_resolver=resolver)
        receipt = await adapter.start(
            run,
            request_id="request-temporal-start",
            request_sha256="f" * 64,
            binding_receipt=binding,
        )
        validation_arguments = {
            "run": run,
            "binding_receipt": binding,
            "request_id": "request-temporal-start",
            "request_sha256": "f" * 64,
            "adapter_manifest": adapter.capability_manifest,
            "provider_run_id": "provider-run-1",
            "first_execution_run_id": "provider-run-1",
        }
        validate_workflow_run_receipt(receipt, **validation_arguments)
        forged = dict(receipt)
        forged["generation"] = 2
        forged["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in forged.items()
                    if key != "receipt_sha256"
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(DurableWorkflowError, "does not match run"):
            validate_workflow_run_receipt(forged, **validation_arguments)

        forged = dict(receipt)
        forged["request_id"] = "request-forged"
        forged["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in forged.items()
                    if key != "receipt_sha256"
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        with self.assertRaisesRegex(DurableWorkflowError, "does not match request"):
            validate_workflow_run_receipt(forged, **validation_arguments)

        forged = dict(receipt)
        forged["implementation_sha256"] = "e" * 64
        forged["receipt_sha256"] = hashlib.sha256(
            json.dumps(
                {
                    key: value
                    for key, value in forged.items()
                    if key != "receipt_sha256"
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        forged_arguments = copy.deepcopy(validation_arguments)
        forged_arguments["adapter_manifest"]["implementation_sha256"] = "e" * 64
        with self.assertRaisesRegex(DurableWorkflowError, "implementation"):
            validate_workflow_run_receipt(forged, **forged_arguments)

    async def test_adapter_rejects_client_namespace_mismatch(self) -> None:
        client = _Client()
        client.namespace = "other-namespace"
        with self.assertRaisesRegex(DurableWorkflowError, "namespace"):
            self.adapter(client)

    async def test_adapter_fails_closed_when_optional_sdk_is_missing(self) -> None:
        def missing_sdk() -> object:
            raise ModuleNotFoundError("temporalio")

        with self.assertRaisesRegex(WorkflowRuntimeUnavailable, "sdk_unavailable"):
            TemporalWorkflowAdapter(
                client=_Client(),
                namespace="context-control-plane",
                task_queue="context-m8-03",
                workflow_name="context.control-plane.campaign",
                adapter_version="1.0.0",
                implementation_sha256="e" * 64,
                worker_deployment="context-control-plane",
                worker_build_id="m8-03-v1",
                sdk_loader=missing_sdk,
            )

    async def test_real_temporal_sdk_start_surface_when_available(self) -> None:
        try:
            temporalio = importlib.import_module("temporalio")
        except ModuleNotFoundError:
            self.skipTest("optional temporalio SDK is not installed")
        self.assertEqual(temporalio.__version__, "1.31.0")
        client = _Client()
        adapter = TemporalWorkflowAdapter(
            client=client,
            namespace="context-control-plane",
            task_queue="context-m8-03",
            workflow_name="context.control-plane.campaign",
            adapter_version="1.0.0",
            implementation_sha256="a" * 64,
            worker_deployment="context-control-plane",
            worker_build_id="m8-03-v1",
            sdk_loader=lambda: temporalio,
            state_mcp_resolver=None,
        )
        run = self.workflow_run()
        binding, resolver = self.backend_authority(run)
        adapter.state_mcp_resolver = resolver

        await adapter.start(
            run,
            request_id="request-temporal-real-sdk",
            request_sha256="f" * 64,
            binding_receipt=binding,
        )

        call = client.calls[0]
        self.assertEqual(
            call["id_conflict_policy"],
            temporalio.common.WorkflowIDConflictPolicy.USE_EXISTING,
        )
        self.assertEqual(
            call["id_reuse_policy"],
            temporalio.common.WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )


if __name__ == "__main__":
    unittest.main()
