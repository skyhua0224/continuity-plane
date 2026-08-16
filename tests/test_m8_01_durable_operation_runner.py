"""M8-01 durable operation runner and idempotent effect recovery."""

from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from typing import Any, ClassVar

from context_control_plane.durable_checkpoint_gate import (
    compose_durable_checkpoint_receipt,
)
from context_control_plane.durable_operation import advance_durable_operation
from context_control_plane.durable_operation_store import SQLiteDurableOperationStore
from context_control_plane.durable_operation_trace import (
    LocalDurableOperationTraceRecorder,
)
from context_control_plane.durable_state_authority import (
    compose_durable_state_receipt,
)
from tests.test_m8_01_durable_operation_store import (
    _continuation_state_for,
    _prepared_operation,
)


class _AuthorityFixture:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-authority-adapter/v1alpha1",
        "adapter_id": "fixture.state-mcp-authority/v1",
        "adapter_version": "1.0.0",
        "authority_interface": "state-mcp",
        "receipt_schema_version": "context.durable-state-receipt/v1alpha1",
        "source_ref": "fixture://m8-01/state-mcp-authority",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(self) -> None:
        self.intent_commits = 0
        self.state_commits = 0

    def commit_intent(self, operation: dict) -> dict:
        self.intent_commits += 1
        return compose_durable_state_receipt(
            operation,
            action="authorize",
            request_id="request-m8-01-authorize",
            request_sha256="5" * 64,
            revision=operation["authority"]["project_revision"] + 1,
            event_head={"sequence_no": 1268, "event_sha256": "6" * 64},
            result_ref=None,
            registry_digest="a" * 64,
            state_response_sha256="7" * 64,
            reconciled=False,
        )

    def commit_state(self, operation: dict) -> dict:
        self.state_commits += 1
        return compose_durable_state_receipt(
            operation,
            action="complete",
            request_id="request-m8-01-complete",
            request_sha256="8" * 64,
            revision=operation["authority"]["project_revision"] + 2,
            event_head={"sequence_no": 1269, "event_sha256": "9" * 64},
            result_ref=operation["result_ref"],
            registry_digest="a" * 64,
            state_response_sha256="b" * 64,
            reconciled=False,
        )


class _SucceededAuthorityFixture(_AuthorityFixture):
    def commit_intent(self, operation: dict) -> dict:
        self.intent_commits += 1
        return compose_durable_state_receipt(
            operation,
            action="authorize",
            request_id="request-m8-01-reconciled-authorize",
            request_sha256="5" * 64,
            revision=operation["authority"]["project_revision"] + 2,
            event_head={"sequence_no": 1269, "event_sha256": "6" * 64},
            effect_status="succeeded",
            result_ref="artifact://sha256/" + "4" * 64,
            registry_digest="a" * 64,
            state_response_sha256="7" * 64,
            reconciled=True,
        )


class _MalformedAuthorityAdapter:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-authority-adapter/v1alpha1",
        "adapter_id": "fixture.malformed-authority/v1",
        "adapter_version": "1.0.0",
        "authority_interface": "state-mcp",
        "receipt_schema_version": "context.durable-state-receipt/v1alpha1",
        "source_ref": "fixture://m8-01/malformed-authority",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def commit_intent(self, operation: dict):
        return "unvalidated-state-reference"

    def commit_state(self, operation: dict):
        return "unvalidated-state-reference"


class _CheckpointGateFixture:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-checkpoint-adapter/v1alpha1",
        "adapter_id": "fixture.checkpoint-gate/v1",
        "adapter_version": "1.0.0",
        "source_ref": "fixture://m8-01/checkpoint-gate",
        "receipt_schema_version": "context.durable-checkpoint-gate/v1alpha1",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def verify(self, operation: dict) -> dict:
        return compose_durable_checkpoint_receipt(
            operation, critical_projection_sha256="c" * 64
        )


class _MalformedCheckpointGate(_CheckpointGateFixture):
    def verify(self, operation: dict):
        return {"execution_gate": "allow"}


def _trace_recorder(directory: str) -> LocalDurableOperationTraceRecorder:
    return LocalDurableOperationTraceRecorder(
        output_path=Path(directory) / "trace.jsonl",
        source={
            "kind": "durable_operation",
            "provider": "local",
            "adapter": "context.durable-runner/v1",
            "source_ref": "component://context.durable-runner",
        },
    )


class _IdempotentEffectFixture:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-effect-adapter/v1alpha1",
        "adapter_id": "fixture.idempotent-effect/v1",
        "adapter_version": "1.0.0",
        "implementation_sha256": "9" * 64,
        "source_ref": "fixture://m8-01/idempotent-effect",
        "idempotency_modes": ["effect-key"],
        "status_lookups": ["none", "supported"],
        "replay_policies": ["never", "safe"],
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(self) -> None:
        self.results: dict[str, dict[str, str]] = {}
        self.physical_effects = 0

    def lookup(self, effect_key: str, request_sha256: str) -> dict:
        return self.results.get(effect_key, {"status": "absent"})

    def apply(self, effect_key: str, request_sha256: str) -> dict:
        existing = self.results.get(effect_key)
        if existing is not None:
            return existing
        self.physical_effects += 1
        result = {
            "status": "settled",
            "request_sha256": request_sha256,
            "result_ref": "artifact://sha256/" + "4" * 64,
            "settlement_ref": "settlement://fixture/" + effect_key,
        }
        self.results[effect_key] = result
        return result


class _AbsentEffectFixture:
    capability_manifest = _IdempotentEffectFixture.capability_manifest

    def __init__(self) -> None:
        self.lookup_calls = 0
        self.apply_calls = 0

    def lookup(self, effect_key: str, request_sha256: str) -> dict:
        self.lookup_calls += 1
        return {"status": "absent"}

    def apply(self, effect_key: str, request_sha256: str) -> dict:
        self.apply_calls += 1
        return {
            "status": "settled",
            "request_sha256": request_sha256,
            "result_ref": "artifact://sha256/" + "4" * 64,
            "settlement_ref": "settlement://unexpected-apply",
        }


class _SettledLookupEffectFixture:
    capability_manifest = _IdempotentEffectFixture.capability_manifest

    def __init__(self, request_sha256: str) -> None:
        self.lookup_calls = 0
        self.apply_calls = 0
        self.result = {
            "status": "settled",
            "request_sha256": request_sha256,
            "result_ref": "artifact://sha256/" + "4" * 64,
            "settlement_ref": "settlement://verified-existing",
        }

    def lookup(self, effect_key: str, request_sha256: str) -> dict:
        self.lookup_calls += 1
        return self.result

    def apply(self, effect_key: str, request_sha256: str) -> dict:
        self.apply_calls += 1
        return self.result


class _NonIdempotentEffectFixture:
    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-effect-adapter/v1alpha1",
        "adapter_id": "fixture.idempotent-effect/v1",
        "adapter_version": "1.0.0",
        "implementation_sha256": "8" * 64,
        "source_ref": "fixture://m8-01/non-idempotent-effect",
        "idempotency_modes": ["none"],
        "status_lookups": ["none"],
        "replay_policies": ["never"],
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(self) -> None:
        self.semantic_effects = 0
        self._lock = Lock()

    def lookup(self, effect_key: str, request_sha256: str) -> dict:
        return {"status": "absent"}

    def apply(self, effect_key: str, request_sha256: str) -> dict:
        with self._lock:
            self.semantic_effects += 1
        return {
            "status": "settled",
            "request_sha256": request_sha256,
            "result_ref": "artifact://sha256/" + "4" * 64,
            "settlement_ref": "settlement://non-idempotent/" + effect_key,
        }


class _DispatchBarrierStore(SQLiteDurableOperationStore):
    def __init__(self, database_path: Path, barrier: Barrier) -> None:
        super().__init__(database_path)
        self._dispatch_barrier = barrier

    def append_transition_owned(
        self, operation: dict, *, expected_record_sha256: str
    ) -> dict:
        if operation["phase"] == "effect-in-flight":
            self._dispatch_barrier.wait(timeout=5)
        return super().append_transition_owned(
            operation, expected_record_sha256=expected_record_sha256
        )


class M801DurableOperationRunnerTests(unittest.TestCase):
    def test_reconciled_succeeded_never_effect_is_not_dispatched_again(self) -> None:
        from context_control_plane.durable_operation_runner import (
            LocalDurableOperationRunner,
        )

        prepared = _prepared_operation(idempotency_mode="none")
        effect_adapter = _NonIdempotentEffectFixture()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(Path(directory) / "operation.sqlite3")
            store.initialize()
            terminal = LocalDurableOperationRunner(
                store=store,
                effect_adapter=effect_adapter,
                authority_adapter=_SucceededAuthorityFixture(),
                checkpoint_gate=_CheckpointGateFixture(),
                trace_recorder=_trace_recorder(directory),
                continuation_state=_continuation_state_for,
                clock=iter(
                    f"2026-08-16T10:00:0{index}+08:00"
                    for index in range(1, 8)
                ).__next__,
            ).run(prepared)

        self.assertEqual(terminal["phase"], "terminal")
        self.assertEqual(terminal["result_ref"], "artifact://sha256/" + "4" * 64)
        self.assertEqual(effect_adapter.semantic_effects, 0)

    def test_concurrent_runners_have_one_never_effect_dispatch_owner(self) -> None:
        from context_control_plane.durable_operation_runner import (
            LocalDurableOperationRunner,
        )

        prepared = _prepared_operation(idempotency_mode="none")
        effect_adapter = _NonIdempotentEffectFixture()
        barrier = Barrier(2)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "operation.sqlite3"
            SQLiteDurableOperationStore(path).initialize()

            def run(worker: int) -> str:
                from context_control_plane.durable_operation_store import (
                    DurableOperationStoreConflict,
                )

                trace_directory = Path(directory) / f"trace-{worker}"
                trace_directory.mkdir()
                runner = LocalDurableOperationRunner(
                    store=_DispatchBarrierStore(path, barrier),
                    effect_adapter=effect_adapter,
                    authority_adapter=_AuthorityFixture(),
                    checkpoint_gate=_CheckpointGateFixture(),
                    trace_recorder=_trace_recorder(str(trace_directory)),
                    continuation_state=_continuation_state_for,
                    clock=lambda: "2026-08-16T10:00:01+08:00",
                )
                try:
                    return runner.run(prepared)["phase"]
                except DurableOperationStoreConflict:
                    return "cas-conflict"

            with ThreadPoolExecutor(max_workers=2) as executor:
                phases = list(executor.map(run, range(2)))

        self.assertEqual(effect_adapter.semantic_effects, 1)
        self.assertEqual(sorted(phases), ["cas-conflict", "terminal"])

    def test_unvalidated_checkpoint_result_is_rejected_before_authority(self) -> None:
        from context_control_plane.durable_operation_runner import (
            DurableOperationRunnerError,
            LocalDurableOperationRunner,
        )

        authority = _AuthorityFixture()
        effect_adapter = _AbsentEffectFixture()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(Path(directory) / "operation.sqlite3")
            store.initialize()
            runner = LocalDurableOperationRunner(
                store=store,
                effect_adapter=effect_adapter,
                authority_adapter=authority,
                checkpoint_gate=_MalformedCheckpointGate(),
                trace_recorder=_trace_recorder(directory),
                continuation_state=_continuation_state_for,
                clock=lambda: "2026-08-16T10:00:01+08:00",
            )
            with self.assertRaisesRegex(DurableOperationRunnerError, "checkpoint"):
                runner.run(_prepared_operation())

        self.assertEqual(authority.intent_commits, 0)
        self.assertEqual(effect_adapter.apply_calls, 0)

    def test_unvalidated_authority_result_is_rejected_before_effect(self) -> None:
        from context_control_plane.durable_operation_runner import (
            DurableOperationRunnerError,
            LocalDurableOperationRunner,
        )

        effect_adapter = _AbsentEffectFixture()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(Path(directory) / "operation.sqlite3")
            store.initialize()
            runner = LocalDurableOperationRunner(
                store=store,
                effect_adapter=effect_adapter,
                authority_adapter=_MalformedAuthorityAdapter(),
                checkpoint_gate=_CheckpointGateFixture(),
                trace_recorder=_trace_recorder(directory),
                continuation_state=_continuation_state_for,
                clock=lambda: "2026-08-16T10:00:01+08:00",
            )
            with self.assertRaisesRegex(DurableOperationRunnerError, "authority"):
                runner.run(_prepared_operation())

        self.assertEqual(effect_adapter.apply_calls, 0)

    def test_effect_adapter_identity_mismatch_is_rejected_before_intent(self) -> None:
        from context_control_plane.durable_operation_runner import (
            DurableOperationRunnerError,
            LocalDurableOperationRunner,
        )

        authority = _AuthorityFixture()
        effect_adapter = _AbsentEffectFixture()
        effect_adapter.capability_manifest = {
            **effect_adapter.capability_manifest,
            "adapter_id": "fixture.other-effect/v1",
        }
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(Path(directory) / "operation.sqlite3")
            store.initialize()
            runner = LocalDurableOperationRunner(
                store=store,
                effect_adapter=effect_adapter,
                authority_adapter=authority,
                checkpoint_gate=_CheckpointGateFixture(),
                trace_recorder=_trace_recorder(directory),
                continuation_state=_continuation_state_for,
                clock=lambda: "2026-08-16T10:00:01+08:00",
            )
            with self.assertRaisesRegex(DurableOperationRunnerError, "adapter"):
                runner.run(_prepared_operation())

        self.assertEqual(authority.intent_commits, 0)
        self.assertEqual(effect_adapter.apply_calls, 0)

    def test_unbound_continuation_is_rejected_before_intent_or_effect(self) -> None:
        from context_control_plane.durable_operation_runner import (
            DurableOperationRunnerError,
            LocalDurableOperationRunner,
        )

        authority = _AuthorityFixture()
        effect_adapter = _AbsentEffectFixture()
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(Path(directory) / "operation.sqlite3")
            store.initialize()
            runner = LocalDurableOperationRunner(
                store=store,
                effect_adapter=effect_adapter,
                authority_adapter=authority,
                checkpoint_gate=_CheckpointGateFixture(),
                trace_recorder=_trace_recorder(directory),
                continuation_state=lambda operation, phase: {
                    "schema_version": "context.durable-continuation/v1alpha1",
                    "state_sha256": operation["continuation_sha256"],
                },
                clock=lambda: "2026-08-16T10:00:01+08:00",
            )
            with self.assertRaisesRegex(DurableOperationRunnerError, "continuation"):
                runner.run(_prepared_operation())

        self.assertEqual(authority.intent_commits, 0)
        self.assertEqual(effect_adapter.apply_calls, 0)

    def test_safe_unknown_effect_resumes_from_verified_settlement(self) -> None:
        from context_control_plane.durable_operation_runner import (
            LocalDurableOperationRunner,
        )

        prepared = _prepared_operation(
            replay_policy="safe",
            status_lookup="supported",
        )
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256=_continuation_state_for(
                prepared, "intent-committed"
            )["state_sha256"],
            intent_ref="state-event://effect-authorized/1",
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T10:00:02+08:00",
            continuation_sha256=_continuation_state_for(
                intent, "effect-in-flight"
            )["state_sha256"],
            start_ref="attempt://effect/m8-01/store/1",
        )
        unknown = advance_durable_operation(
            started,
            phase="outcome-unknown",
            observed_at="2026-08-16T10:00:03+08:00",
            continuation_sha256=_continuation_state_for(
                started, "outcome-unknown"
            )["state_sha256"],
            reconciliation_reason="process-ended-before-settlement",
        )
        effect_adapter = _SettledLookupEffectFixture(
            prepared["effect"]["request_sha256"]
        )
        authority = _AuthorityFixture()

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(Path(directory) / "operation.sqlite3")
            store.initialize()
            store.create_operation(prepared)
            for current, advanced in ((prepared, intent), (intent, started), (started, unknown)):
                store.append_transition(
                    advanced,
                    expected_record_sha256=current["record_sha256"],
                )
            runner = LocalDurableOperationRunner(
                store=store,
                effect_adapter=effect_adapter,
                authority_adapter=authority,
                checkpoint_gate=_CheckpointGateFixture(),
                trace_recorder=_trace_recorder(directory),
                continuation_state=_continuation_state_for,
                clock=lambda: "2026-08-16T10:00:04+08:00",
            )
            recovered = runner.run(prepared)

        self.assertEqual(recovered["phase"], "terminal")
        self.assertEqual(effect_adapter.lookup_calls, 1)
        self.assertEqual(effect_adapter.apply_calls, 0)
        self.assertEqual(authority.intent_commits, 0)
        self.assertEqual(authority.state_commits, 1)

    def test_recovered_never_effect_does_not_reapply_when_lookup_is_absent(self) -> None:
        from context_control_plane.durable_operation_runner import (
            LocalDurableOperationRunner,
        )

        prepared = _prepared_operation(
            replay_policy="never",
            status_lookup="supported",
        )
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256=_continuation_state_for(
                prepared, "intent-committed"
            )["state_sha256"],
            intent_ref="state-event://effect-authorized/1",
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T10:00:02+08:00",
            continuation_sha256=_continuation_state_for(
                intent, "effect-in-flight"
            )["state_sha256"],
            start_ref="attempt://effect/m8-01/store/1",
        )
        effect_adapter = _AbsentEffectFixture()

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(Path(directory) / "operation.sqlite3")
            store.initialize()
            store.create_operation(prepared)
            store.append_transition(
                intent,
                expected_record_sha256=prepared["record_sha256"],
            )
            store.append_transition(
                started,
                expected_record_sha256=intent["record_sha256"],
            )
            runner = LocalDurableOperationRunner(
                store=store,
                effect_adapter=effect_adapter,
                authority_adapter=_AuthorityFixture(),
                checkpoint_gate=_CheckpointGateFixture(),
                trace_recorder=_trace_recorder(directory),
                continuation_state=_continuation_state_for,
                clock=lambda: "2026-08-16T10:00:03+08:00",
            )
            recovered = runner.run(prepared)

        self.assertEqual(recovered["phase"], "outcome-unknown")
        self.assertEqual(effect_adapter.lookup_calls, 1)
        self.assertEqual(effect_adapter.apply_calls, 0)

    def test_runner_completes_once_and_terminal_retry_is_a_noop(self) -> None:
        from context_control_plane.durable_operation_runner import (
            LocalDurableOperationRunner,
        )

        authority = _AuthorityFixture()
        effect_adapter = _IdempotentEffectFixture()
        ticks = iter(
            f"2026-08-16T10:00:0{index}+08:00" for index in range(1, 8)
        )

        def continuation(operation: dict, phase: str) -> dict:
            return _continuation_state_for(operation, phase)

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(Path(directory) / "operation.sqlite3")
            store.initialize()
            runner = LocalDurableOperationRunner(
                store=store,
                effect_adapter=effect_adapter,
                authority_adapter=authority,
                checkpoint_gate=_CheckpointGateFixture(),
                trace_recorder=_trace_recorder(directory),
                continuation_state=continuation,
                clock=lambda: next(ticks),
            )
            terminal = runner.run(_prepared_operation())
            replay = runner.run(_prepared_operation())
            history = store.read_history(terminal["operation_id"])

        self.assertEqual(terminal["phase"], "terminal")
        self.assertEqual(replay, terminal)
        self.assertEqual([item["phase"] for item in history], [
            "prepared",
            "intent-committed",
            "effect-in-flight",
            "effect-settled",
            "response-committed",
            "terminal",
        ])
        self.assertEqual(effect_adapter.physical_effects, 1)
        self.assertEqual(authority.intent_commits, 1)
        self.assertEqual(authority.state_commits, 1)


if __name__ == "__main__":
    unittest.main()
