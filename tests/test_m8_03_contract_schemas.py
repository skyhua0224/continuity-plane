"""M8-03 strict workflow and optional Temporal adapter schema contracts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import unittest
from pathlib import Path
from typing import Any, ClassVar

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from context_control_plane.durable_workflow import (
    activate_workflow_patch,
    append_workflow_step,
    build_workflow_definition,
    continue_workflow_as_new,
    create_workflow_run,
    record_workflow_effect,
    record_workflow_input,
    replay_workflow_chain,
)
from context_control_plane.shared_state_mcp import (
    CLAIM_LIFECYCLE_TOOL,
    EFFECT_DISPATCH_TOOL,
    REQUEST_SCHEMA_VERSION,
    SharedStateMCPService,
)
from context_control_plane.shared_work_ledger import WorkLedger
from context_control_plane.state_mcp import RequestContext
from context_control_plane.temporal_workflow_adapter import (
    TemporalWorkflowAdapter,
    build_workflow_backend_binding,
    invoke_temporal_continue_as_new,
    workflow_backend_binding_intent,
)
from context_control_plane.workflow_orchestration import (
    select_workflow_backend,
    select_workflow_runtime,
)
from context_control_plane.workflow_orchestration_benchmark import (
    benchmark_workflow_orchestration,
)

SCHEMA_WIRES = {
    "workflow-run": "context.workflow-run/v1alpha1",
    "workflow-history-event": "context.workflow-history-event/v1alpha1",
    "workflow-definition": "context.workflow-definition/v1alpha1",
    "workflow-replay-receipt": "context.workflow-replay-receipt/v1alpha1",
    "workflow-rollover-receipt": "context.workflow-rollover-receipt/v1alpha1",
    "workflow-backend-capabilities": ("context.workflow-backend-capabilities/v1alpha1"),
    "workflow-runtime-selection": "context.workflow-runtime-selection/v1alpha1",
    "workflow-adapter-manifest": "context.workflow-adapter-manifest/v1alpha1",
    "temporal-workflow-input": "context.temporal-workflow-input/v1alpha1",
    "workflow-run-receipt": "context.workflow-run-receipt/v1alpha1",
    "workflow-backend-binding": "context.workflow-backend-binding/v1alpha1",
    "workflow-orchestration-benchmark": (
        "context.workflow-orchestration-benchmark/v1alpha1"
    ),
}


class _SDK:
    __version__ = "1.31.0"

    class common:
        class WorkflowIDConflictPolicy:
            USE_EXISTING = "use-existing"

        class WorkflowIDReusePolicy:
            REJECT_DUPLICATE = "reject-duplicate"


class _Handle:
    id = ""
    result_run_id = "provider-run-m8-03"
    first_execution_run_id = "provider-first-run-m8-03"


class _Client:
    def __init__(self) -> None:
        self.namespace = "context-control-plane"
        self.calls: list[dict[str, Any]] = []

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
        handle = _Handle()
        handle.id = id
        return handle


class _WorkflowAPI:
    def __init__(self) -> None:
        self.payload: dict[str, Any] | None = None

    def continue_as_new(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        raise _ContinueAsNew()


class _ContinueAsNew(RuntimeError):
    pass


class _LocalRuntime:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "backend_id": "local-reference",
        "execution_scope": "authority-instance",
        "distributed_execution": False,
        "state_write_authority": False,
        "claim_authority": False,
        "effect_authority": False,
    }

    async def start(
        self,
        prepared: dict[str, Any],
        *,
        request_id: str,
        request_sha256: str,
        binding_receipt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return prepared


class _Authorizer:
    def authorize(self, context: RequestContext, action: str, project_id: str) -> bool:
        return True


class M803ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.authority = {
            "project_revision": 60,
            "event_head": {"sequence_no": 600, "event_sha256": "a" * 64},
        }
        cls.definition = build_workflow_definition(
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="b" * 64,
            supported_history_versions=[1],
            patches=[
                {
                    "patch_id": "route-v2",
                    "introduced_in_version": 1,
                    "deprecated_in_version": None,
                    "semantic_sha256": "6" * 64,
                }
            ],
        )
        run = create_workflow_run(
            project_id="project-m8-03",
            root_work_id="M8-03",
            request_id="request-m8-03-schema",
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="b" * 64,
            authority=cls.authority,
            checkpoint_ref="artifact://sha256/" + "c" * 64,
            checkpoint_sha256="c" * 64,
            continuation_sha256="d" * 64,
            started_at="2026-08-16T20:00:00+08:00",
        )
        run = activate_workflow_patch(
            run,
            definition=cls.definition,
            patch_id="route-v2",
            occurred_at="2026-08-16T20:00:01+08:00",
        )
        run = record_workflow_input(
            run,
            input_id="input-m8-03",
            input_sha256="7" * 64,
            status="received",
            occurred_at="2026-08-16T20:00:02+08:00",
        )
        run = record_workflow_effect(
            run,
            effect_id="effect-m8-03",
            effect_key="effect-key-m8-03",
            request_sha256="e" * 64,
            status="started",
            occurred_at="2026-08-16T20:00:03+08:00",
        )
        cls.active_run = run
        run = record_workflow_input(
            run,
            input_id="input-m8-03",
            input_sha256="7" * 64,
            status="acknowledged",
            occurred_at="2026-08-16T20:00:04+08:00",
        )
        run = record_workflow_effect(
            run,
            effect_id="effect-m8-03",
            effect_key="effect-key-m8-03",
            request_sha256="e" * 64,
            status="settled",
            occurred_at="2026-08-16T20:00:05+08:00",
        )
        result_sha256 = cls._result_sha256("route", "f" * 64, frozenset({"route-v2"}))
        run = append_workflow_step(
            run,
            step_id="route",
            input_sha256="f" * 64,
            result_sha256=result_sha256,
            occurred_at="2026-08-16T20:00:06+08:00",
        )
        cls.rollover = continue_workflow_as_new(
            run,
            authority={
                "project_revision": 61,
                "event_head": {
                    "sequence_no": 601,
                    "event_sha256": "1" * 64,
                },
            },
            checkpoint_ref="artifact://sha256/" + "2" * 64,
            checkpoint_sha256="2" * 64,
            continuation_sha256="3" * 64,
            occurred_at="2026-08-16T20:00:07+08:00",
        )
        cls.replay_receipt = replay_workflow_chain(
            [cls.rollover["closed_run"], cls.rollover["next_run"]],
            definition=cls.definition,
            step_resolver=cls._result_sha256,
        )
        cls.local_capabilities = select_workflow_backend("local-embedded")
        cls.temporal_capabilities = select_workflow_backend(
            "temporal", temporal_loader=lambda: _SDK()
        )
        cls.runtime_selection = select_workflow_runtime(
            local_runtime=_LocalRuntime(), mode="local"
        ).receipt

        cls.client = _Client()
        cls.adapter = TemporalWorkflowAdapter(
            client=cls.client,
            namespace="context-control-plane",
            task_queue="context-m8-03",
            workflow_name="context.control-plane.campaign",
            adapter_version="1.0.0",
            implementation_sha256="b" * 64,
            worker_deployment="context-control-plane",
            worker_build_id="m8-03-v1",
            sdk_loader=lambda: _SDK(),
            state_mcp_resolver=None,
        )
        scope = {"scope_kind": "capability", "scope_ref": "workflow-backend"}
        ledger = WorkLedger(
            project_id=cls.active_run["project_id"],
            project_revision=cls.active_run["authority"]["project_revision"] - 1,
            works=[
                {
                    "work_id": cls.active_run["root_work_id"],
                    "status": "ready",
                    "identity_key": "m8-03-schema-binding",
                    "scope_refs": [scope],
                }
            ],
            max_ttl_ms=10_000,
        )
        service = SharedStateMCPService(
            ledger,
            authorizer=_Authorizer(),
            clock=lambda: "2026-08-16T12:00:04+00:00",
        )
        context = RequestContext("actor-m8-03", "authorization-m8-03")
        claim = service.call_tool(
            CLAIM_LIFECYCLE_TOOL,
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-m8-03-schema-claim",
                "project_id": cls.active_run["project_id"],
                "action": "acquire",
                "expected_project_revision": 59,
                "work_id": cls.active_run["root_work_id"],
                "claim_id": "claim-m8-03-schema",
                "requested_ttl_ms": 5_000,
                "scope_owners": [scope],
            },
            context=context,
        )
        assert claim["ok"], claim
        intent = workflow_backend_binding_intent(cls.active_run, backend_id="temporal")
        dispatch = service.call_tool(
            EFFECT_DISPATCH_TOOL,
            {
                "schema_version": REQUEST_SCHEMA_VERSION,
                "request_id": "request-m8-03-schema-binding",
                "project_id": cls.active_run["project_id"],
                "effect_id": intent["effect_id"],
                "effect_key": intent["effect_key"],
                "request_sha256": intent["request_sha256"],
                "claim_id": "claim-m8-03-schema",
                "work_id": cls.active_run["root_work_id"],
                "expected_project_revision": 60,
                "expected_claim_revision": 1,
                "lease_epoch": 1,
                "fence": 1,
                "operation": intent["operation"],
                "scope_ref": scope,
            },
            context=context,
        )
        assert dispatch["ok"], dispatch
        cls.backend_binding = build_workflow_backend_binding(
            cls.active_run,
            backend_id="temporal",
            state_mcp_result=dispatch["result"],
            state_mcp_resolver=service.resolve_committed_effect_dispatch,
        )
        cls.adapter.state_mcp_resolver = service.resolve_committed_effect_dispatch
        cls.run_receipt = asyncio.run(
            cls.adapter.start(
                cls.active_run,
                request_id="request-temporal-schema",
                request_sha256="5" * 64,
                binding_receipt=cls.backend_binding,
            )
        )
        cls.start_input = cls.client.calls[0]["payload"]
        workflow_api = _WorkflowAPI()
        try:
            invoke_temporal_continue_as_new(
                cls.rollover["receipt"],
                closed_run=cls.rollover["closed_run"],
                next_run=cls.rollover["next_run"],
                workflow_api=workflow_api,
            )
        except _ContinueAsNew:
            pass
        assert workflow_api.payload is not None
        cls.continue_input = workflow_api.payload
        cls.benchmark_receipt = benchmark_workflow_orchestration(
            root=cls.root,
            samples=2,
            generated_at="2026-08-16T20:00:00+08:00",
        )

    @staticmethod
    def _result_sha256(step_id: str, input_sha256: str, patches: frozenset[str]) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "step_id": step_id,
                    "input_sha256": input_sha256,
                    "patches": sorted(patches),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def instances(cls) -> dict[str, list[dict[str, Any]]]:
        return {
            "workflow-run": [
                cls.rollover["closed_run"],
                cls.rollover["next_run"],
            ],
            "workflow-history-event": cls.rollover["closed_run"]["history"],
            "workflow-definition": [cls.definition],
            "workflow-replay-receipt": [cls.replay_receipt],
            "workflow-rollover-receipt": [cls.rollover["receipt"]],
            "workflow-backend-capabilities": [
                cls.local_capabilities,
                cls.temporal_capabilities,
            ],
            "workflow-runtime-selection": [cls.runtime_selection],
            "workflow-adapter-manifest": [cls.adapter.capability_manifest],
            "temporal-workflow-input": [cls.start_input, cls.continue_input],
            "workflow-run-receipt": [cls.run_receipt],
            "workflow-backend-binding": [cls.backend_binding],
            "workflow-orchestration-benchmark": [cls.benchmark_receipt],
        }

    @classmethod
    def schema(cls, name: str) -> dict[str, Any]:
        path = cls.root / "schemas" / "m8-03" / f"{name}.schema.json"
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            cls.fail(cls, f"M8-03 schema is missing: {path}")
        Draft202012Validator.check_schema(schema)
        return schema

    @classmethod
    def validate(cls, name: str, instance: object) -> None:
        Draft202012Validator(cls.schema(name), format_checker=FormatChecker()).validate(
            instance
        )

    def test_runtime_outputs_match_all_strict_schemas(self) -> None:
        self.assertEqual(set(self.instances()), set(SCHEMA_WIRES))
        for name, instances in self.instances().items():
            with self.subTest(schema=name):
                schema = self.schema(name)
                if name == "temporal-workflow-input":
                    for branch in schema["oneOf"]:
                        concrete = schema["$defs"][branch["$ref"].rsplit("/", 1)[-1]]
                        self.assertFalse(concrete["additionalProperties"])
                        self.assertEqual(
                            set(concrete["required"]), set(concrete["properties"])
                        )
                else:
                    self.assertFalse(schema["additionalProperties"])
                    self.assertEqual(set(schema["required"]), set(schema["properties"]))
                self._assert_nested_objects_are_strict(schema)
                for instance in instances:
                    self.validate(name, instance)

    def test_all_top_level_schemas_reject_unknown_fields(self) -> None:
        for name, instances in self.instances().items():
            with self.subTest(schema=name):
                changed = copy.deepcopy(instances[0])
                changed["unexpected"] = True
                with self.assertRaises(ValidationError):
                    self.validate(name, changed)

    def test_all_top_level_schemas_require_every_field(self) -> None:
        for name, instances in self.instances().items():
            for index, instance in enumerate(instances):
                for field in instance:
                    with self.subTest(schema=name, instance=index, field=field):
                        changed = copy.deepcopy(instance)
                        del changed[field]
                        with self.assertRaises(ValidationError):
                            self.validate(name, changed)

    def test_authority_flags_cannot_be_elevated(self) -> None:
        fields = (
            "state_write_authority",
            "effect_dispatch_authority",
            "provider_native_authority",
            "claim_authority",
            "effect_authority",
        )
        exercised: set[str] = set()
        for name, instances in self.instances().items():
            for field in fields:
                if field not in instances[0]:
                    continue
                exercised.add(field)
                with self.subTest(schema=name, field=field):
                    changed = copy.deepcopy(instances[0])
                    changed[field] = True
                    with self.assertRaises(ValidationError):
                        self.validate(name, changed)
        self.assertEqual(exercised, set(fields))

    def test_wire_ids_and_event_payloads_are_closed(self) -> None:
        for name, wire in SCHEMA_WIRES.items():
            with self.subTest(schema=name):
                changed = copy.deepcopy(self.instances()[name][0])
                changed["schema_version"] = wire + ".future"
                with self.assertRaises(ValidationError):
                    self.validate(name, changed)

        for source in (self.rollover["closed_run"], self.active_run):
            for original in source["history"]:
                with self.subTest(event_type=original["event_type"]):
                    event = copy.deepcopy(original)
                    event["payload"]["unexpected"] = True
                    with self.assertRaises(ValidationError):
                        self.validate("workflow-history-event", event)

    def test_event_types_cannot_be_rebound_to_other_registered_payloads(self) -> None:
        events = self.rollover["closed_run"]["history"]
        for index, original in enumerate(events):
            replacement = events[(index + 1) % len(events)]["payload"]
            with self.subTest(event_type=original["event_type"]):
                event = copy.deepcopy(original)
                event["payload"] = copy.deepcopy(replacement)
                with self.assertRaises(ValidationError):
                    self.validate("workflow-history-event", event)

                run = copy.deepcopy(self.rollover["closed_run"])
                run["history"][index]["payload"] = copy.deepcopy(replacement)
                with self.assertRaises(ValidationError):
                    self.validate("workflow-run", run)

    def test_identifiers_hashes_timestamps_and_enums_are_bounded(self) -> None:
        mutations = (
            ("workflow-run", "workflow_id", "bad id"),
            ("workflow-run", "run_sha256", "A" * 64),
            ("workflow-run", "created_at", "2026-08-16T20:00:00"),
            ("workflow-run", "phase", "completed"),
            ("workflow-history-event", "event_type", "unregistered"),
            ("workflow-definition", "implementation_sha256", "not-a-hash"),
            ("workflow-backend-capabilities", "profile", "automatic"),
            ("workflow-runtime-selection", "selection_reason", "unknown"),
            ("workflow-adapter-manifest", "execution_scope", "global"),
            ("temporal-workflow-input", "input_ack_count", -1),
            ("workflow-run-receipt", "receipt_sha256", "F" * 64),
            ("workflow-backend-binding", "status", "pending"),
            ("workflow-orchestration-benchmark", "samples", 0),
        )
        for name, field, invalid in mutations:
            with self.subTest(schema=name, field=field):
                changed = copy.deepcopy(self.instances()[name][0])
                changed[field] = invalid
                with self.assertRaises(ValidationError):
                    self.validate(name, changed)

    def test_registry_admits_every_m8_03_schema_by_content_hash(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entries = {entry["schema_id"]: entry for entry in registry["schemas"]}
        for name, wire in SCHEMA_WIRES.items():
            schema_id = wire.split("/", maxsplit=1)[0]
            relative_path = f"schemas/m8-03/{name}.schema.json"
            with self.subTest(schema_id=schema_id):
                entry = entries[schema_id]
                artifact = self.root / relative_path
                self.assertEqual(entry["current_semver"], "1.0.0-alpha.1")
                self.assertEqual(entry["current_wire_version"], wire)
                self.assertEqual(entry["supported_wire_versions"], [wire])
                self.assertEqual(entry["artifact_path"], relative_path)
                self.assertEqual(entry["compatibility_mode"], "strict-versioned")
                self.assertEqual(entry["migrations"], [])
                self.assertEqual(
                    entry["content_sha256"],
                    hashlib.sha256(artifact.read_bytes()).hexdigest(),
                )

    def _assert_nested_objects_are_strict(self, node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                self.assertIs(
                    node.get("additionalProperties"),
                    False,
                    msg=f"open object schema: {node}",
                )
                self.assertEqual(
                    set(node.get("required", [])),
                    set(node.get("properties", {})),
                    msg=f"object fields are not fully required: {node}",
                )
            for value in node.values():
                self._assert_nested_objects_are_strict(value)
        elif isinstance(node, list):
            for value in node:
                self._assert_nested_objects_are_strict(value)


if __name__ == "__main__":
    unittest.main()
