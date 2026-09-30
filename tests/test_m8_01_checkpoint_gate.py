"""M8-01 checkpoint restore gate before durable execution."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import tests.test_m8_01_state_mcp_authority as state_fixture
from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.checkpoint import publish_checkpoint
from context_control_plane.sqlite_state_store import SQLiteStateStore
from context_control_plane.state_mcp import RequestContext
from tests.test_m8_01_durable_operation_store import _prepared_operation


class M801CheckpointGateTests(unittest.TestCase):
    def test_published_state_checkpoint_opens_the_bound_operation_gate(self) -> None:
        from context_control_plane.durable_checkpoint_gate import (
            LocalDurableCheckpointGate,
            validate_durable_checkpoint_receipt,
        )

        state_fixture.M801StateMCPAuthorityTests.setUpClass()
        fixture = state_fixture.M801StateMCPAuthorityTests()
        context = RequestContext("actor-second", "authorization-test")
        with tempfile.TemporaryDirectory() as directory:
            state_store = SQLiteStateStore(Path(directory) / "state.sqlite3")
            state_store.initialize()
            state_store.create_project(fixture._ready_snapshot())
            service = fixture._service(state_store)
            claim = fixture._claim(service, context)
            self.assertTrue(claim["ok"], claim["error"])
            read = service.call_tool(
                "context.state.read",
                {
                    "schema_version": "context.state-mcp-request/v1alpha1",
                    "request_id": "request-m8-01-checkpoint-read",
                    "project_id": "project-solo",
                },
                context=context,
            )
            self.assertTrue(read["ok"], read["error"])
            artifact_store = LocalArtifactStore(Path(directory) / "artifacts")
            artifact_store.initialize()
            checkpoint_ref = publish_checkpoint(
                read["result"],
                artifact_store,
                canonical_plan_sha256="e" * 64,
            )
            operation = fixture._operation(
                claim, checkpoint_ref=checkpoint_ref.to_document()
            )
            gate = LocalDurableCheckpointGate(
                artifact_store=artifact_store,
                governance_ref=read["result"]["snapshot"]["project"]["governance_ref"],
                canonical_plan_sha256="e" * 64,
                registry_digest="a" * 64,
            )
            receipt = gate.verify(operation)

        validate_durable_checkpoint_receipt(receipt, operation=operation)
        self.assertEqual(receipt["execution_gate"], "allow")
        self.assertEqual(receipt["checkpoint_ref"], checkpoint_ref.to_document())
        self.assertEqual(receipt["recovery_kind"], "process-restart")
        self.assertIsNone(receipt["postcompact_canary_sha256"])

    def test_postcompact_recovery_requires_a_valid_bound_canary(self) -> None:
        import tests.test_m5_03_postcompact_canary as m503
        from context_control_plane.durable_checkpoint_gate import (
            DurableCheckpointGateError,
            LocalDurableCheckpointGate,
        )
        from context_control_plane.durable_operation import compose_durable_operation

        m503.M503PostCompactCanaryTests.setUpClass()
        canary_case = m503.M503PostCompactCanaryTests(
            methodName="test_canary_is_deterministic_for_identical_restore_inputs"
        )
        canary_case.setUp()
        self.addCleanup(canary_case.doCleanups)
        canary = canary_case._evaluate("pi")
        operation = compose_durable_operation(
            operation_id="operation/m8-01/postcompact",
            project_id="project-solo",
            work_id="work-solo",
            claim_id="claim-solo",
            authority={
                "project_revision": 7,
                "event_head": canary_case.event_head,
            },
            effect={
                "effect_id": "effect/m8-01/postcompact",
                "effect_key": "postcompact:m8-01",
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
            },
            checkpoint_ref=canary_case.checkpoint_ref.to_document(),
            continuation_sha256="d" * 64,
            trace_binding={
                "trace_id": "1" * 32,
                "span_id": "2" * 16,
                "run_id": "run/m8-01/postcompact",
                "correlation_id": "correlation/m8-postcompact",
            },
            observed_at="2026-08-15T00:11:00+08:00",
        )
        arguments = {
            "artifact_store": canary_case.store,
            "governance_ref": canary_case.packet["governance_ref"],
            "canonical_plan_sha256": canary_case.packet[
                "canonical_plan_sha256"
            ],
            "registry_digest": "d" * 64,
            "recovery_kind": "postcompact",
        }
        with self.assertRaisesRegex(DurableCheckpointGateError, "canary"):
            LocalDurableCheckpointGate(**arguments)

        gate = LocalDurableCheckpointGate(
            **arguments,
            postcompact_canary_receipt=canary,
        )
        receipt = gate.verify(operation)
        self.assertEqual(receipt["recovery_kind"], "postcompact")
        self.assertEqual(
            receipt["postcompact_canary_sha256"], canary["canary_sha256"]
        )

    def test_missing_checkpoint_fails_closed(self) -> None:
        from context_control_plane.durable_checkpoint_gate import (
            DurableCheckpointGateError,
            LocalDurableCheckpointGate,
        )

        with tempfile.TemporaryDirectory() as directory:
            store = LocalArtifactStore(Path(directory) / "artifacts")
            store.initialize()
            gate = LocalDurableCheckpointGate(
                artifact_store=store,
                governance_ref="artifact://governance/master@revision-58",
                canonical_plan_sha256="e" * 64,
                registry_digest="f" * 64,
            )
            with self.assertRaisesRegex(DurableCheckpointGateError, "checkpoint"):
                gate.verify(_prepared_operation())


if __name__ == "__main__":
    unittest.main()
