"""M8-01 append-only trace binding for durable operation revisions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from context_control_plane.context_trace import validate_context_trace_chain
from context_control_plane.durable_operation_runner import LocalDurableOperationRunner
from context_control_plane.durable_operation_store import SQLiteDurableOperationStore
from tests.test_m8_01_durable_operation_runner import (
    _AuthorityFixture,
    _CheckpointGateFixture,
    _IdempotentEffectFixture,
)
from tests.test_m8_01_durable_operation_store import (
    _continuation_state_for,
    _prepared_operation,
)


class M801DurableOperationTraceTests(unittest.TestCase):
    def test_two_recorders_share_one_exactly_once_trace_chain(self) -> None:
        from context_control_plane.durable_operation_trace import (
            LocalDurableOperationTraceRecorder,
        )

        source = {
            "kind": "durable_operation",
            "provider": "local",
            "adapter": "context.durable-runner/v1",
            "source_ref": "component://context.durable-runner",
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "trace.jsonl"
            first = LocalDurableOperationTraceRecorder(
                output_path=output, source=source
            )
            second = LocalDurableOperationTraceRecorder(
                output_path=output, source=source
            )
            operation = _prepared_operation()
            first._emitter_for(operation)
            second._emitter_for(operation)

            first.record(operation)
            duplicate = second.record(operation)
            reopened = LocalDurableOperationTraceRecorder(
                output_path=output, source=source
            )
            reopened.record(operation)
            events = reopened.events

        self.assertEqual(duplicate["event_id"], events[0]["event_id"])
        self.assertEqual(len(events), 1)
        validate_context_trace_chain(events)

    def test_runner_records_every_persisted_revision_once(self) -> None:
        from context_control_plane.durable_operation_trace import (
            LocalDurableOperationTraceRecorder,
        )

        source = {
            "kind": "durable_operation",
            "provider": "local",
            "adapter": "context.durable-runner/v1",
            "source_ref": "component://context.durable-runner",
        }
        with tempfile.TemporaryDirectory() as directory:
            operation_store = SQLiteDurableOperationStore(
                Path(directory) / "operation.sqlite3"
            )
            operation_store.initialize()
            recorder = LocalDurableOperationTraceRecorder(
                output_path=Path(directory) / "trace.jsonl",
                source=source,
            )
            runner = LocalDurableOperationRunner(
                store=operation_store,
                effect_adapter=_IdempotentEffectFixture(),
                authority_adapter=_AuthorityFixture(),
                checkpoint_gate=_CheckpointGateFixture(),
                trace_recorder=recorder,
                continuation_state=_continuation_state_for,
                clock=iter(
                    f"2026-08-16T10:00:0{index}+08:00"
                    for index in range(1, 8)
                ).__next__,
            )
            prepared = _prepared_operation()
            terminal = runner.run(prepared)
            replay = runner.run(prepared)
            history = operation_store.read_history(prepared["operation_id"])
            events = recorder.events

        self.assertEqual(replay, terminal)
        self.assertEqual(len(history), 6)
        self.assertEqual(len(events), 6)
        self.assertEqual(
            [event["attributes"]["record_sha256"] for event in events],
            [record["record_sha256"] for record in history],
        )
        validate_context_trace_chain(events)

    def test_same_operation_revision_is_recorded_once_with_configured_source(self) -> None:
        from context_control_plane.durable_operation_trace import (
            LocalDurableOperationTraceRecorder,
        )

        source = {
            "kind": "durable_operation",
            "provider": "local",
            "adapter": "context.durable-runner/v1",
            "source_ref": "component://context.durable-runner",
        }
        with tempfile.TemporaryDirectory() as directory:
            recorder = LocalDurableOperationTraceRecorder(
                output_path=Path(directory) / "trace.jsonl",
                source=source,
            )
            operation = _prepared_operation()
            first = recorder.record(operation)
            duplicate = recorder.record(operation)
            events = recorder.events

        validate_context_trace_chain(events)
        self.assertEqual(first, duplicate)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], source)
        self.assertEqual(events[0]["operation_id"], operation["operation_id"])
        self.assertEqual(events[0]["attributes"]["record_sha256"], operation["record_sha256"])
        self.assertEqual(events[0]["attributes"]["phase"], "prepared")


if __name__ == "__main__":
    unittest.main()
