"""M8-01 local durable operation journal."""

from __future__ import annotations

import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

from context_control_plane.durable_continuation import compose_durable_continuation
from context_control_plane.durable_operation import (
    advance_durable_operation,
    compose_durable_operation,
)

_CONTINUATION_PHASE = {
    "prepared": "prepared",
    "intent-committed": "intent-committed",
    "effect-in-flight": "effect-in-flight",
    "outcome-unknown": "effect-in-flight",
    "effect-settled": "effect-settled",
    "response-committed": "response-committed",
    "terminal": "terminal",
}
_CONTINUATION_ACTIONS = {
    "prepared": ("operation-created", "commit-intent"),
    "intent-committed": ("effect-intent-committed", "dispatch-effect"),
    "effect-in-flight": ("effect-started", "verify-effect"),
    "effect-settled": ("effect-settled", "commit-state"),
    "response-committed": ("state-response-committed", "finalize"),
    "terminal": ("operation-terminal", None),
}
_CONTINUATION_EFFECT_STATUS = {
    "prepared": "reserved",
    "intent-committed": "reserved",
    "effect-in-flight": "started",
    "effect-settled": "settled",
    "response-committed": "settled",
    "terminal": "settled",
}


def _continuation_state_for(operation: dict, requested_phase: str) -> dict:
    phase = _CONTINUATION_PHASE[requested_phase]
    last_action, next_action = _CONTINUATION_ACTIONS[phase]
    return compose_durable_continuation(
        operation_id=operation["operation_id"],
        project_id=operation["project_id"],
        project_revision=operation["authority"]["project_revision"],
        task_id=operation["work_id"],
        task_revision=1,
        event_head=operation["authority"]["event_head"],
        phase=phase,
        last_durable_action=last_action,
        next_action=next_action,
        acknowledged_input_ids=[],
        reserved_effects=[
            {
                "effect_id": operation["effect"]["effect_id"],
                "replay_policy": operation["effect"]["replay_policy"],
                "status": _CONTINUATION_EFFECT_STATUS[phase],
            }
        ],
        response_mode="terminal" if phase == "terminal" else "continue-silently",
    )


def _prepared_operation(
    *,
    operation_id: str = "operation/m8-01/store",
    effect_id: str = "effect/m8-01/store",
    claim_id: str = "claim/m8-01/store",
    replay_policy: str = "never",
    idempotency_mode: str = "effect-key",
    status_lookup: str = "none",
) -> dict:
    arguments = {
        "operation_id": operation_id,
        "project_id": "project-context-control-plane",
        "work_id": "M8-01",
        "claim_id": claim_id,
        "authority": {
            "project_revision": 58,
            "event_head": {"sequence_no": 1267, "event_sha256": "a" * 64},
        },
        "effect": {
            "effect_id": effect_id,
            "effect_key": "external-write:m8-01:store",
            "operation": "external-write",
            "scope_ref": {
                "scope_kind": "effect",
                "scope_ref": "durable-operation-store",
            },
            "adapter_id": "fixture.idempotent-effect/v1",
            "request_sha256": "b" * 64,
            "replay_policy": replay_policy,
            "idempotency_mode": idempotency_mode,
            "status_lookup": status_lookup,
        },
        "checkpoint_ref": {
            "schema_version": "context.artifact-ref/v1alpha1",
            "digest_algorithm": "sha-256",
            "digest": "c" * 64,
            "size_bytes": 4096,
            "artifact_uri": "artifact://sha256/" + "c" * 64,
        },
        "continuation_sha256": "d" * 64,
        "trace_binding": {
            "trace_id": "1" * 32,
            "span_id": "2" * 16,
            "run_id": "run/" + operation_id,
            "correlation_id": "correlation/m8-01",
        },
        "observed_at": "2026-08-16T10:00:00+08:00",
    }
    draft = compose_durable_operation(**arguments)
    arguments["continuation_sha256"] = _continuation_state_for(
        draft, "prepared"
    )["state_sha256"]
    return compose_durable_operation(**arguments)


class M801DurableOperationStoreTests(unittest.TestCase):
    def test_sigkill_inside_store_transactions_rolls_back_to_a_valid_boundary(
        self,
    ) -> None:
        if not hasattr(signal, "SIGKILL"):
            self.skipTest("platform has no SIGKILL")
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreNotFound,
            SQLiteDurableOperationStore,
        )

        prepared = _prepared_operation()
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256="e" * 64,
            intent_ref="state-event://effect-authorized/1",
        )
        script = """
import os
import signal
import sys
from pathlib import Path
from context_control_plane.durable_operation import advance_durable_operation
from context_control_plane.durable_operation_store import SQLiteDurableOperationStore
from tests.test_m8_01_durable_operation_store import _prepared_operation

path = Path(sys.argv[1])
point = sys.argv[2]
prepared = _prepared_operation()
if point != "after-schema-ddl":
    base = SQLiteDurableOperationStore(path)
    base.initialize()
    if point == "before-transition-commit":
        base.create_operation(prepared)

def fault(observed):
    if observed == point:
        os.kill(os.getpid(), signal.SIGKILL)

store = SQLiteDurableOperationStore(path, fault_hook=fault)
if point == "after-schema-ddl":
    store.initialize()
elif point == "before-create-commit":
    store.create_operation(prepared)
else:
    intent = advance_durable_operation(
        prepared,
        phase="intent-committed",
        observed_at="2026-08-16T10:00:01+08:00",
        continuation_sha256="e" * 64,
        intent_ref="state-event://effect-authorized/1",
    )
    store.append_transition(intent, expected_record_sha256=prepared["record_sha256"])
"""
        for point in (
            "after-schema-ddl",
            "before-create-commit",
            "before-transition-commit",
        ):
            with self.subTest(point=point), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "durable-operation.sqlite3"
                killed = subprocess.run(
                    [sys.executable, "-c", script, str(path), point],
                    cwd=Path(__file__).parents[1],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)

                recovered = SQLiteDurableOperationStore(path)
                recovered.initialize()
                if point == "after-schema-ddl":
                    with closing(sqlite3.connect(path)) as connection:
                        metadata_count = connection.execute(
                            "SELECT COUNT(*) FROM durable_operation_metadata"
                        ).fetchone()[0]
                    self.assertEqual(metadata_count, 1)
                elif point == "before-create-commit":
                    with self.assertRaises(DurableOperationStoreNotFound):
                        recovered.read_operation(prepared["operation_id"])
                    recovered.create_operation(prepared)
                else:
                    self.assertEqual(recovered.read_history(prepared["operation_id"]), [prepared])
                    recovered.append_transition(
                        intent,
                        expected_record_sha256=prepared["record_sha256"],
                    )

    def test_concurrent_project_effect_key_creation_has_one_winner(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreConflict,
            SQLiteDurableOperationStore,
        )

        first = _prepared_operation()
        second = _prepared_operation(
            operation_id="operation/m8-01/concurrent",
            effect_id="effect/m8-01/concurrent",
            claim_id="claim/m8-01/concurrent",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            SQLiteDurableOperationStore(path).initialize()
            barrier = Barrier(2)

            def create(operation: dict) -> tuple[str, str]:
                worker = SQLiteDurableOperationStore(path)
                barrier.wait(timeout=5)
                try:
                    worker.create_operation(operation)
                    return "created", operation["operation_id"]
                except DurableOperationStoreConflict:
                    return "conflict", operation["operation_id"]
                finally:
                    worker.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(create, (first, second)))
            with closing(sqlite3.connect(path)) as connection:
                head_count = connection.execute(
                    "SELECT COUNT(*) FROM durable_operation_heads"
                ).fetchone()[0]
                record_count = connection.execute(
                    "SELECT COUNT(*) FROM durable_operation_records"
                ).fetchone()[0]

        self.assertEqual(sorted(outcome for outcome, _ in outcomes), ["conflict", "created"])
        self.assertEqual(head_count, 1)
        self.assertEqual(record_count, 1)

    def test_project_effect_key_is_reserved_by_only_one_operation(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreConflict,
            SQLiteDurableOperationStore,
        )

        first = _prepared_operation()
        second = _prepared_operation(
            operation_id="operation/m8-01/competing",
            effect_id="effect/m8-01/competing",
            claim_id="claim/m8-01/competing",
        )
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteDurableOperationStore(
                Path(directory) / "durable-operation.sqlite3"
            )
            store.initialize()
            store.create_operation(first)
            with self.assertRaisesRegex(DurableOperationStoreConflict, "effect key"):
                store.create_operation(second)

    def test_history_survives_reopen_with_an_append_only_hash_chain(self) -> None:
        from context_control_plane.durable_operation_store import (
            SQLiteDurableOperationStore,
        )

        prepared = _prepared_operation()
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256="e" * 64,
            intent_ref="state-event://effect-authorized/1",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            store = SQLiteDurableOperationStore(path)
            store.initialize()
            self.assertEqual(store.create_operation(prepared), prepared)
            self.assertEqual(
                store.append_transition(
                    intent,
                    expected_record_sha256=prepared["record_sha256"],
                ),
                intent,
            )
            store.close()

            reopened = SQLiteDurableOperationStore(path)
            reopened.initialize()
            self.assertEqual(reopened.read_operation(prepared["operation_id"]), intent)
            self.assertEqual(
                reopened.read_history(prepared["operation_id"]), [prepared, intent]
            )
            reopened.close()

    def test_same_transition_is_idempotent_and_competing_transition_conflicts(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreConflict,
            SQLiteDurableOperationStore,
        )

        prepared = _prepared_operation()
        intent = advance_durable_operation(
            prepared,
            phase="intent-committed",
            observed_at="2026-08-16T10:00:01+08:00",
            continuation_sha256="e" * 64,
            intent_ref="state-event://effect-authorized/1",
        )
        started = advance_durable_operation(
            intent,
            phase="effect-in-flight",
            observed_at="2026-08-16T10:00:02+08:00",
            continuation_sha256="f" * 64,
            start_ref="attempt://effect/m8-01/store/1",
        )
        quarantined = advance_durable_operation(
            intent,
            phase="quarantined",
            observed_at="2026-08-16T10:00:02+08:00",
            continuation_sha256="9" * 64,
            reconciliation_reason="competing-worker-quarantine",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            store = SQLiteDurableOperationStore(path)
            store.initialize()
            store.create_operation(prepared)
            store.append_transition(
                intent,
                expected_record_sha256=prepared["record_sha256"],
            )
            duplicate = store.append_transition(
                intent,
                expected_record_sha256=prepared["record_sha256"],
            )
            self.assertEqual(duplicate, intent)

            def append(candidate: dict) -> str:
                worker = SQLiteDurableOperationStore(path)
                worker.initialize()
                try:
                    worker.append_transition(
                        candidate,
                        expected_record_sha256=intent["record_sha256"],
                    )
                    return "committed"
                except DurableOperationStoreConflict:
                    return "conflict"
                finally:
                    worker.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(append, (started, quarantined)))
            history = store.read_history(prepared["operation_id"])

        self.assertEqual(sorted(outcomes), ["committed", "conflict"])
        self.assertEqual(len(history), 3)
        self.assertIn(history[-1]["phase"], {"effect-in-flight", "quarantined"})

    def test_history_is_sql_append_only_and_tampering_fails_closed(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreIntegrityError,
            SQLiteDurableOperationStore,
        )

        prepared = _prepared_operation()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            store = SQLiteDurableOperationStore(path)
            store.initialize()
            store.create_operation(prepared)
            with closing(sqlite3.connect(path)) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(
                        "UPDATE durable_operation_records SET record_json = '{}'"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute("DELETE FROM durable_operation_records")
                connection.execute("DROP TRIGGER durable_operation_records_no_update")
                connection.execute(
                    "UPDATE durable_operation_records SET record_json = '{}'"
                )
                connection.commit()
            with self.assertRaises(DurableOperationStoreIntegrityError):
                store.read_operation(prepared["operation_id"])

    def test_head_effect_identity_is_sql_immutable(self) -> None:
        from context_control_plane.durable_operation_store import (
            SQLiteDurableOperationStore,
        )

        prepared = _prepared_operation()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            store = SQLiteDurableOperationStore(path)
            store.initialize()
            store.create_operation(prepared)
            with (
                closing(sqlite3.connect(path)) as connection,
                self.assertRaisesRegex(sqlite3.IntegrityError, "identity"),
            ):
                    connection.execute(
                        "UPDATE durable_operation_heads SET effect_key = ?",
                        ("external-write:m8-01:forged",),
                    )

    def test_head_identity_tampering_fails_closed_when_read(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreIntegrityError,
            SQLiteDurableOperationStore,
        )

        prepared = _prepared_operation()
        competing = _prepared_operation(
            operation_id="operation/m8-01/tampered-head",
            effect_id="effect/m8-01/tampered-head",
            claim_id="claim/m8-01/tampered-head",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            store = SQLiteDurableOperationStore(path)
            store.initialize()
            store.create_operation(prepared)
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "DROP TRIGGER durable_operation_heads_identity_no_update"
                )
                connection.execute(
                    "UPDATE durable_operation_heads SET effect_key = ?",
                    ("external-write:m8-01:forged",),
                )
                connection.commit()

            with self.assertRaisesRegex(
                DurableOperationStoreIntegrityError, "head identity"
            ):
                store.create_operation(competing)
            with self.assertRaisesRegex(
                DurableOperationStoreIntegrityError, "head identity"
            ):
                store.read_operation(prepared["operation_id"])

    def test_initialize_persists_the_expected_schema_fingerprint(self) -> None:
        from context_control_plane.durable_operation_store import (
            SQLiteDurableOperationStore,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            SQLiteDurableOperationStore(path).initialize()
            with closing(sqlite3.connect(path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(durable_operation_metadata)"
                    )
                }
                fingerprint = (
                    connection.execute(
                        "SELECT schema_fingerprint FROM durable_operation_metadata "
                        "WHERE singleton = 1"
                    ).fetchone()[0]
                    if "schema_fingerprint" in columns
                    else None
                )

        self.assertIn("schema_fingerprint", columns)
        self.assertRegex(fingerprint or "", r"^[0-9a-f]{64}$")

    def test_initialize_rejects_schema_fingerprint_drift(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreIntegrityError,
            SQLiteDurableOperationStore,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            SQLiteDurableOperationStore(path).initialize()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "ALTER TABLE durable_operation_heads ADD COLUMN injected TEXT"
                )
                connection.commit()

            with self.assertRaisesRegex(
                DurableOperationStoreIntegrityError, "schema fingerprint"
            ):
                SQLiteDurableOperationStore(path).initialize()

    def test_initialize_rejects_a_missing_append_only_trigger(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreIntegrityError,
            SQLiteDurableOperationStore,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            SQLiteDurableOperationStore(path).initialize()
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("DROP TRIGGER durable_operation_records_no_update")
                connection.commit()

            with self.assertRaisesRegex(
                DurableOperationStoreIntegrityError, "append-only trigger"
            ):
                SQLiteDurableOperationStore(path).initialize()

    def test_initialize_rejects_a_tampered_append_only_trigger(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreIntegrityError,
            SQLiteDurableOperationStore,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            SQLiteDurableOperationStore(path).initialize()
            with closing(sqlite3.connect(path)) as connection:
                connection.executescript(
                    """
                    DROP TRIGGER durable_operation_records_no_update;
                    CREATE TRIGGER durable_operation_records_no_update
                    BEFORE UPDATE ON durable_operation_records
                    BEGIN
                        SELECT 1;
                    END;
                    """
                )

            with self.assertRaisesRegex(
                DurableOperationStoreIntegrityError, "append-only trigger"
            ):
                SQLiteDurableOperationStore(path).initialize()

    def test_initialize_rejects_a_failed_sqlite_quick_check(self) -> None:
        from context_control_plane.durable_operation_store import (
            DurableOperationStoreIntegrityError,
            SQLiteDurableOperationStore,
        )

        prepared = _prepared_operation()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "durable-operation.sqlite3"
            store = SQLiteDurableOperationStore(path)
            store.initialize()
            store.create_operation(prepared)
            with closing(sqlite3.connect(path)) as connection:
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            with path.open("r+b") as database:
                database.seek(page_size + 5)
                original = database.read(1)
                database.seek(page_size + 5)
                database.write(bytes([original[0] ^ 0xFF]))

            with self.assertRaisesRegex(
                DurableOperationStoreIntegrityError, "quick check"
            ):
                SQLiteDurableOperationStore(path).initialize()


if __name__ == "__main__":
    unittest.main()
