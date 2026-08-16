"""M8-03 provider-neutral workflow orchestration and backend selection."""

from __future__ import annotations

import copy
import hashlib
import json
import unittest
from unittest.mock import patch

from context_control_plane.durable_operation_runner import LocalDurableOperationRunner
from context_control_plane.durable_workflow import (
    DurableWorkflowError,
    activate_workflow_patch,
    append_workflow_step,
    build_workflow_definition,
    continue_workflow_as_new,
    create_workflow_run,
    record_workflow_effect,
    record_workflow_input,
    replay_workflow_chain,
    validate_workflow_replay_receipt,
    validate_workflow_run,
)
from context_control_plane.workflow_orchestration import (
    LocalWorkflowRuntimeAdapter,
    WorkflowOrchestrationError,
    WorkflowRuntimeUnavailable,
    select_workflow_backend,
    select_workflow_runtime,
)


class M803WorkflowOrchestrationTests(unittest.IsolatedAsyncioTestCase):
    class Runtime:
        def __init__(self, backend_id: str, *, distributed: bool) -> None:
            self.calls = 0
            self.capability_manifest = {
                "backend_id": backend_id,
                "execution_scope": "distributed"
                if distributed
                else "authority-instance",
                "distributed_execution": distributed,
                "state_write_authority": False,
                "claim_authority": False,
                "effect_authority": False,
            }

        async def start(
            self,
            prepared: dict,
            *,
            request_id: str,
            request_sha256: str,
            binding_receipt: dict | None = None,
        ) -> dict:
            self.calls += 1
            return {
                "phase": "terminal",
                "operation_id": prepared["operation_id"],
                "request_id": request_id,
                "request_sha256": request_sha256,
                "binding_receipt": binding_receipt,
            }

    @staticmethod
    def authority(revision: int = 27) -> dict:
        return {
            "project_revision": revision,
            "event_head": {
                "sequence_no": revision,
                "event_sha256": "a" * 64,
            },
        }

    @staticmethod
    def result_sha256(step_id: str, input_sha256: str, patches: frozenset[str]) -> str:
        encoded = json.dumps(
            {
                "step_id": step_id,
                "input_sha256": input_sha256,
                "patches": sorted(patches),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def workflow_run(self, *, definition_version: int = 1) -> dict:
        return create_workflow_run(
            project_id="project-m8-03",
            root_work_id="M8-03",
            request_id="request-m8-03-start",
            workflow_type="context.campaign",
            definition_version=definition_version,
            implementation_sha256="b" * 64,
            authority=self.authority(),
            checkpoint_ref="artifact://sha256/" + "c" * 64,
            checkpoint_sha256="c" * 64,
            continuation_sha256="d" * 64,
            started_at="2026-08-16T18:00:00+08:00",
        )

    def test_local_embedded_never_imports_temporal(self) -> None:
        calls: list[str] = []

        def forbidden_loader() -> object:
            calls.append("temporalio")
            raise AssertionError("local profile imported the optional Temporal SDK")

        manifest = select_workflow_backend(
            "local-embedded", temporal_loader=forbidden_loader
        )

        self.assertEqual(calls, [])
        self.assertEqual(manifest["backend_id"], "local-reference")
        self.assertEqual(manifest["profile"], "local-embedded")
        self.assertFalse(manifest["requires_sdk"])
        self.assertFalse(manifest["requires_service"])
        self.assertTrue(manifest["replay"])
        self.assertTrue(manifest["patching"])
        self.assertTrue(manifest["continue_as_new"])
        self.assertFalse(manifest["state_write_authority"])
        self.assertFalse(manifest["provider_native_authority"])

    def test_explicit_temporal_request_fails_closed_without_sdk(self) -> None:
        def missing_loader() -> object:
            raise ModuleNotFoundError("temporalio")

        with self.assertRaisesRegex(
            WorkflowOrchestrationError, "Temporal SDK is unavailable"
        ):
            select_workflow_backend("temporal", temporal_loader=missing_loader)

    def test_workflow_identity_and_history_are_hash_bound(self) -> None:
        first = self.workflow_run()
        replayed = self.workflow_run()
        self.assertEqual(first, replayed)
        self.assertTrue(first["workflow_id"].startswith("wf_"))
        self.assertTrue(first["chain_id"].startswith("wfc_"))
        self.assertTrue(first["run_id"].startswith("wfr_"))
        self.assertEqual(first["generation"], 1)
        self.assertIsNone(first["previous_run_id"])
        self.assertFalse(first["state_write_authority"])
        self.assertFalse(first["effect_dispatch_authority"])
        self.assertFalse(first["provider_native_authority"])
        self.assertEqual(len(first["history"]), 1)
        validate_workflow_run(first)

        second_request = create_workflow_run(
            project_id="project-m8-03",
            root_work_id="M8-03",
            request_id="request-m8-03-second",
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="b" * 64,
            authority=self.authority(),
            checkpoint_ref="artifact://sha256/" + "c" * 64,
            checkpoint_sha256="c" * 64,
            continuation_sha256="d" * 64,
            started_at="2026-08-16T18:00:00+08:00",
        )
        self.assertNotEqual(first["workflow_id"], second_request["workflow_id"])

        tampered = copy.deepcopy(first)
        tampered["authority"]["project_revision"] += 1
        with self.assertRaisesRegex(DurableWorkflowError, "run hash"):
            validate_workflow_run(tampered)

        tampered = copy.deepcopy(first)
        tampered["history"][0]["event_sha256"] = "f" * 64
        with self.assertRaisesRegex(DurableWorkflowError, "event hash"):
            validate_workflow_run(tampered)

    def test_patch_markers_make_old_and_new_history_replay_deterministic(self) -> None:
        definition = build_workflow_definition(
            workflow_type="context.campaign",
            definition_version=2,
            implementation_sha256="e" * 64,
            supported_history_versions=[1, 2],
            patches=[
                {
                    "patch_id": "route-v2",
                    "introduced_in_version": 2,
                    "deprecated_in_version": None,
                    "semantic_sha256": "9" * 64,
                }
            ],
        )
        input_sha256 = "1" * 64

        old = append_workflow_step(
            self.workflow_run(definition_version=1),
            step_id="route",
            input_sha256=input_sha256,
            result_sha256=self.result_sha256("route", input_sha256, frozenset()),
            occurred_at="2026-08-16T18:00:01+08:00",
        )
        old_receipt = replay_workflow_chain(
            [old], definition=definition, step_resolver=self.result_sha256
        )
        self.assertEqual(old_receipt["command_mismatches"], 0)

        new = create_workflow_run(
            project_id="project-m8-03",
            root_work_id="M8-03-new",
            request_id="request-m8-03-new",
            workflow_type="context.campaign",
            definition_version=2,
            implementation_sha256="e" * 64,
            authority=self.authority(),
            checkpoint_ref="artifact://sha256/" + "2" * 64,
            checkpoint_sha256="2" * 64,
            continuation_sha256="3" * 64,
            started_at="2026-08-16T18:01:00+08:00",
        )
        new = activate_workflow_patch(
            new,
            definition=definition,
            patch_id="route-v2",
            occurred_at="2026-08-16T18:01:01+08:00",
        )
        new = append_workflow_step(
            new,
            step_id="route",
            input_sha256=input_sha256,
            result_sha256=self.result_sha256(
                "route", input_sha256, frozenset({"route-v2"})
            ),
            occurred_at="2026-08-16T18:01:02+08:00",
        )
        new_receipt = replay_workflow_chain(
            [new], definition=definition, step_resolver=self.result_sha256
        )
        self.assertEqual(new_receipt["command_mismatches"], 0)
        self.assertEqual(new_receipt["external_calls"], 0)
        self.assertEqual(new_receipt["state_writes"], 0)
        self.assertEqual(new_receipt["effect_calls"], 0)

        incompatible = build_workflow_definition(
            workflow_type="context.campaign",
            definition_version=2,
            implementation_sha256="e" * 64,
            supported_history_versions=[2],
            patches=definition["patches"],
        )
        with self.assertRaisesRegex(DurableWorkflowError, "history version"):
            replay_workflow_chain(
                [old], definition=incompatible, step_resolver=self.result_sha256
            )

        with self.assertRaisesRegex(DurableWorkflowError, "introduced"):
            activate_workflow_patch(
                self.workflow_run(definition_version=1),
                definition=definition,
                patch_id="route-v2",
                occurred_at="2026-08-16T18:02:00+08:00",
            )

        structurally_invalid = copy.deepcopy(definition)
        structurally_invalid["supported_history_versions"] = []
        definition_body = copy.deepcopy(structurally_invalid)
        definition_body.pop("manifest_sha256")
        structurally_invalid["manifest_sha256"] = hashlib.sha256(
            json.dumps(definition_body, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        with self.assertRaisesRegex(DurableWorkflowError, "history versions"):
            replay_workflow_chain(
                [new],
                definition=structurally_invalid,
                step_resolver=self.result_sha256,
            )

    def test_continue_as_new_requires_a_quiescent_effect_boundary(self) -> None:
        active = record_workflow_effect(
            self.workflow_run(),
            effect_id="effect-m8-03",
            effect_key="effect-key-m8-03",
            request_sha256="4" * 64,
            status="started",
            occurred_at="2026-08-16T18:00:01+08:00",
        )
        with self.assertRaisesRegex(DurableWorkflowError, "effect is not settled"):
            continue_workflow_as_new(
                active,
                authority=self.authority(28),
                checkpoint_ref="artifact://sha256/" + "5" * 64,
                checkpoint_sha256="5" * 64,
                continuation_sha256="6" * 64,
                occurred_at="2026-08-16T18:00:02+08:00",
            )

        settled = record_workflow_effect(
            active,
            effect_id="effect-m8-03",
            effect_key="effect-key-m8-03",
            request_sha256="4" * 64,
            status="settled",
            occurred_at="2026-08-16T18:00:02+08:00",
        )
        rollover = continue_workflow_as_new(
            settled,
            authority=self.authority(28),
            checkpoint_ref="artifact://sha256/" + "5" * 64,
            checkpoint_sha256="5" * 64,
            continuation_sha256="6" * 64,
            occurred_at="2026-08-16T18:00:03+08:00",
        )
        closed = rollover["closed_run"]
        next_run = rollover["next_run"]
        receipt = rollover["receipt"]
        self.assertEqual(closed["phase"], "continued-as-new")
        self.assertEqual(next_run["workflow_id"], settled["workflow_id"])
        self.assertEqual(next_run["chain_id"], settled["chain_id"])
        self.assertNotEqual(next_run["run_id"], settled["run_id"])
        self.assertEqual(next_run["generation"], 2)
        self.assertEqual(next_run["previous_run_id"], settled["run_id"])
        self.assertEqual(len(next_run["history"]), 1)
        self.assertEqual(next_run["effects"], closed["effects"])
        self.assertEqual(next_run["effects"], [])
        self.assertEqual(next_run["effect_settlement_count"], 1)
        self.assertTrue(receipt["safe_point"])
        self.assertEqual(receipt["lost_inputs"], 0)
        self.assertEqual(receipt["duplicate_effects"], 0)

        definition = build_workflow_definition(
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="b" * 64,
            supported_history_versions=[1],
            patches=[],
        )
        replay = replay_workflow_chain(
            [closed, next_run],
            definition=definition,
            step_resolver=self.result_sha256,
        )
        self.assertEqual(replay["run_count"], 2)
        self.assertEqual(replay["generation_count"], 2)
        self.assertEqual(
            replay["run_sha256s"], [closed["run_sha256"], next_run["run_sha256"]]
        )
        self.assertEqual(
            replay["final_history_event_sha256"],
            next_run["history"][-1]["event_sha256"],
        )
        self.assertEqual(len(replay["chain_sha256"]), 64)

    def test_continue_as_new_requires_all_inputs_to_be_acknowledged(self) -> None:
        received = record_workflow_input(
            self.workflow_run(),
            input_id="input-m8-03",
            input_sha256="7" * 64,
            status="received",
            occurred_at="2026-08-16T18:00:01+08:00",
        )
        with self.assertRaisesRegex(DurableWorkflowError, "input is not acknowledged"):
            continue_workflow_as_new(
                received,
                authority=self.authority(28),
                checkpoint_ref="artifact://sha256/" + "5" * 64,
                checkpoint_sha256="5" * 64,
                continuation_sha256="6" * 64,
                occurred_at="2026-08-16T18:00:02+08:00",
            )

        acknowledged = record_workflow_input(
            received,
            input_id="input-m8-03",
            input_sha256="7" * 64,
            status="acknowledged",
            occurred_at="2026-08-16T18:00:02+08:00",
        )
        rollover = continue_workflow_as_new(
            acknowledged,
            authority=self.authority(28),
            checkpoint_ref="artifact://sha256/" + "5" * 64,
            checkpoint_sha256="5" * 64,
            continuation_sha256="6" * 64,
            occurred_at="2026-08-16T18:00:03+08:00",
        )
        self.assertEqual(rollover["next_run"]["pending_inputs"], [])
        self.assertEqual(rollover["next_run"]["input_ack_count"], 1)
        self.assertEqual(rollover["receipt"]["input_ack_count"], 1)
        self.assertEqual(rollover["receipt"]["lost_inputs"], 0)

    def test_event_time_order_uses_instants_instead_of_string_order(self) -> None:
        run = create_workflow_run(
            project_id="project-m8-03",
            root_work_id="M8-03-time",
            request_id="request-m8-03-time",
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="b" * 64,
            authority=self.authority(),
            checkpoint_ref="artifact://sha256/" + "c" * 64,
            checkpoint_sha256="c" * 64,
            continuation_sha256="d" * 64,
            started_at="2026-08-16T01:00:00-08:00",
        )
        with self.assertRaisesRegex(DurableWorkflowError, "time moved backwards"):
            append_workflow_step(
                run,
                step_id="route",
                input_sha256="1" * 64,
                result_sha256="2" * 64,
                occurred_at="2026-08-16T08:30:00+00:00",
            )

    def test_event_identity_is_derived_from_event_content(self) -> None:
        run = append_workflow_step(
            self.workflow_run(),
            step_id="route",
            input_sha256="1" * 64,
            result_sha256="2" * 64,
            occurred_at="2026-08-16T18:00:01+08:00",
        )
        forged = copy.deepcopy(run)
        event = forged["history"][-1]
        event["payload"]["result_sha256"] = "3" * 64
        event_body = copy.deepcopy(event)
        event_body.pop("event_sha256")
        event["event_sha256"] = hashlib.sha256(
            json.dumps(event_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        forged["run_sha256"] = ""
        run_body = copy.deepcopy(forged)
        run_body.pop("run_sha256")
        forged["run_sha256"] = hashlib.sha256(
            json.dumps(run_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(DurableWorkflowError, "event identity"):
            validate_workflow_run(forged)

    def test_replay_rejects_time_regression_across_generations(self) -> None:
        rollover = continue_workflow_as_new(
            self.workflow_run(),
            authority=self.authority(28),
            checkpoint_ref="artifact://sha256/" + "5" * 64,
            checkpoint_sha256="5" * 64,
            continuation_sha256="6" * 64,
            occurred_at="2026-08-16T18:00:03+08:00",
        )
        next_run = copy.deepcopy(rollover["next_run"])
        next_run["created_at"] = "2026-08-16T17:59:59+08:00"
        next_run["updated_at"] = "2026-08-16T17:59:59+08:00"
        started = next_run["history"][0]
        started["occurred_at"] = "2026-08-16T17:59:59+08:00"
        event_body = copy.deepcopy(started)
        event_body.pop("event_sha256")
        started["event_sha256"] = hashlib.sha256(
            json.dumps(event_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        next_run["run_sha256"] = ""
        run_body = copy.deepcopy(next_run)
        run_body.pop("run_sha256")
        next_run["run_sha256"] = hashlib.sha256(
            json.dumps(run_body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        definition = build_workflow_definition(
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="b" * 64,
            supported_history_versions=[1],
            patches=[],
        )
        with self.assertRaisesRegex(DurableWorkflowError, "time moved backwards"):
            replay_workflow_chain(
                [rollover["closed_run"], next_run],
                definition=definition,
                step_resolver=self.result_sha256,
            )

    def test_pending_input_and_effect_sets_are_bounded(self) -> None:
        run = self.workflow_run()
        for index in range(128):
            run = record_workflow_input(
                run,
                input_id=f"input-{index:03d}",
                input_sha256=hashlib.sha256(f"input-{index}".encode()).hexdigest(),
                status="received",
                occurred_at=f"2026-08-16T18:{index // 60:02d}:{index % 60:02d}+08:00",
            )
        with self.assertRaisesRegex(DurableWorkflowError, "pending input limit"):
            record_workflow_input(
                run,
                input_id="input-overflow",
                input_sha256="9" * 64,
                status="received",
                occurred_at="2026-08-16T18:02:08+08:00",
            )

        run = self.workflow_run()
        for index in range(128):
            run = record_workflow_effect(
                run,
                effect_id=f"effect-{index:03d}",
                effect_key=f"effect-key-{index:03d}",
                request_sha256=hashlib.sha256(f"effect-{index}".encode()).hexdigest(),
                status="started",
                occurred_at=f"2026-08-16T18:{index // 60:02d}:{index % 60:02d}+08:00",
            )
        with self.assertRaisesRegex(DurableWorkflowError, "effect limit"):
            record_workflow_effect(
                run,
                effect_id="effect-overflow",
                effect_key="effect-key-overflow",
                request_sha256="9" * 64,
                status="started",
                occurred_at="2026-08-16T18:02:08+08:00",
            )

    def test_replay_receipt_validator_rejects_semantic_tamper(self) -> None:
        definition = build_workflow_definition(
            workflow_type="context.campaign",
            definition_version=1,
            implementation_sha256="b" * 64,
            supported_history_versions=[1],
            patches=[],
        )
        runs = [self.workflow_run()]
        receipt = replay_workflow_chain(
            runs, definition=definition, step_resolver=self.result_sha256
        )
        validate_workflow_replay_receipt(
            receipt,
            runs=runs,
            definition=definition,
            step_resolver=self.result_sha256,
        )
        forged = copy.deepcopy(receipt)
        forged["history_event_count"] += 1
        body = copy.deepcopy(forged)
        body.pop("receipt_sha256")
        forged["receipt_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with self.assertRaisesRegex(DurableWorkflowError, "replay receipt"):
            validate_workflow_replay_receipt(
                forged,
                runs=runs,
                definition=definition,
                step_resolver=self.result_sha256,
            )

    async def test_local_runtime_adapter_executes_the_m8_01_runner(self) -> None:
        runner = object.__new__(LocalDurableOperationRunner)
        terminal = {"operation_id": "operation-m8-03", "phase": "terminal"}
        with patch.object(
            LocalDurableOperationRunner, "run", return_value=terminal
        ) as run:
            runtime = LocalWorkflowRuntimeAdapter(runner)
            selected = select_workflow_runtime(local_runtime=runtime)
            result = await selected.start(
                {"operation_id": "operation-m8-03"},
                request_id="request-local-runner",
                request_sha256="8" * 64,
            )
        self.assertEqual(result, terminal)
        run.assert_called_once_with({"operation_id": "operation-m8-03"})

    async def test_runtime_fallback_is_allowed_only_before_remote_binding(self) -> None:
        local = self.Runtime("local-reference", distributed=False)

        def unavailable_factory() -> M803WorkflowOrchestrationTests.Runtime:
            raise WorkflowRuntimeUnavailable(
                "sdk_unavailable", remote_execution_possible=False
            )

        selected = select_workflow_runtime(
            local_runtime=local,
            temporal_factory=unavailable_factory,
            mode="prefer-temporal",
        )
        self.assertEqual(selected.receipt["selected_backend_id"], "local-reference")
        self.assertEqual(
            selected.receipt["selection_reason"], "fallback-before-binding"
        )
        self.assertFalse(selected.receipt["distributed_execution"])
        result = await selected.start(
            {"operation_id": "operation-m8-03"},
            request_id="request-local-start",
            request_sha256="8" * 64,
        )
        self.assertEqual(result["phase"], "terminal")

        def ambiguous_factory() -> M803WorkflowOrchestrationTests.Runtime:
            raise WorkflowRuntimeUnavailable(
                "start_outcome_unknown", remote_execution_possible=True
            )

        with self.assertRaisesRegex(
            WorkflowRuntimeUnavailable, "start_outcome_unknown"
        ):
            select_workflow_runtime(
                local_runtime=local,
                temporal_factory=ambiguous_factory,
                mode="prefer-temporal",
            )
        self.assertEqual(local.calls, 1)

    def test_required_temporal_never_falls_back(self) -> None:
        local = self.Runtime("local-reference", distributed=False)

        def unavailable_factory() -> M803WorkflowOrchestrationTests.Runtime:
            raise WorkflowRuntimeUnavailable(
                "sdk_unavailable", remote_execution_possible=False
            )

        with self.assertRaisesRegex(WorkflowRuntimeUnavailable, "sdk_unavailable"):
            select_workflow_runtime(
                local_runtime=local,
                temporal_factory=unavailable_factory,
                mode="require-temporal",
            )
        self.assertEqual(local.calls, 0)


if __name__ == "__main__":
    unittest.main()
