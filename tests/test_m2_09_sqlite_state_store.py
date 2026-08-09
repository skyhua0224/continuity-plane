import copy
import json
import multiprocessing
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from context_control_plane.state_events import build_state_event
from context_control_plane.sqlite_state_store import (
    SQLITE_APPLICATION_ID,
    SQLITE_SCHEMA_VERSION,
    SQLiteStateStore,
    SQLiteStateStoreError,
)
import context_control_plane.sqlite_state_store as sqlite_state_store_contracts
import context_control_plane.state_store as state_store_contracts
from context_control_plane.state_store import validate_state_store_adapter
from context_control_plane.testing.state_store_conformance import (
    AuthoritativeStateStoreConformanceMixin,
)


def _cross_process_commit_worker(
    database_path,
    event,
    expected,
    barrier,
    outcomes,
):
    store = SQLiteStateStore(database_path)
    try:
        barrier.wait(timeout=10)
        store.commit_event(
            project_id=event["project_id"],
            expected_revision=event["revision_before"],
            event=event,
            expected_snapshot=expected,
        )
    except state_store_contracts.StateStoreConflict:
        outcomes.put("conflict")
    except BaseException as exc:
        outcomes.put(f"error:{type(exc).__name__}:{exc}")
    else:
        outcomes.put("committed")


class _SQLiteFixtureMixin:
    @classmethod
    def setUpClass(cls):
        fixture_path = (
            Path(__file__).parents[1]
            / "experiments"
            / "state"
            / "m2-01-core-fixtures.yaml"
        )
        fixture_set = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
        cases = {case["case_id"]: case["document"] for case in fixture_set["cases"]}
        cls.base_initial = cases["completed-work-overlap-blocked"]

    def make_initial_snapshot(self):
        snapshot = copy.deepcopy(self.base_initial)
        snapshot["project"]["project_id"] = f"project-sqlite-{uuid.uuid4().hex}"
        return snapshot

    def make_candidate(
        self,
        initial,
        suffix,
        *,
        sequence_no=1,
        previous_event_sha256=None,
        event_id=None,
        supersedes_event_id=None,
        event_type="state-transition",
    ):
        expected = copy.deepcopy(initial)
        expected["project"]["revision"] = initial["project"]["revision"] + 1
        expected["project"]["updated_at"] = "2026-08-10T02:30:00+08:00"
        idea = {
            "idea_id": f"idea-{suffix}-{uuid.uuid4().hex}",
            "parent_work_id": "work-repeat",
            "source_ref": f"opaque://sqlite/{suffix}",
            "summary": f"SQLite conformance candidate {suffix}.",
            "status": "parked",
            "return_work_id": "work-repeat",
            "expiry": None,
            "attempt_budget": None,
            "promotion_target": "M2-09",
            "evidence_ids": [],
        }
        expected["ideas"].append(idea)
        event = build_state_event(
            event_id=event_id or f"event-{suffix}-{uuid.uuid4().hex}",
            event_type=event_type,
            project_id=initial["project"]["project_id"],
            sequence_no=sequence_no,
            revision_before=initial["project"]["revision"],
            occurred_at="2026-08-10T02:30:00+08:00",
            actor_ref="actor-sqlite-conformance",
            causation_ref=f"work:{suffix}",
            correlation_ref="conformance:m2-09",
            previous_event_sha256=previous_event_sha256,
            supersedes_event_id=supersedes_event_id,
            changes=[
                {
                    "collection": "ideas",
                    "object_id": idea["idea_id"],
                    "value": idea,
                }
            ],
            project_after=expected["project"],
        )
        return event, expected


class M209SQLiteStateStoreLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "state.sqlite3"

    def test_manifest_declares_only_proven_local_capabilities(self):
        manifest = validate_state_store_adapter(SQLiteStateStore(self.database_path))

        self.assertEqual(manifest.adapter_id, "context.sqlite")
        self.assertEqual(manifest.authority_mode, "local")
        self.assertEqual(
            manifest.operations,
            ("create_project", "read_project", "read_events", "commit_event"),
        )
        self.assertFalse(manifest.shared_authority)
        self.assertTrue(manifest.offline_write)
        self.assertFalse(manifest.unique_claim)
        self.assertTrue(manifest.multi_writer)
        self.assertEqual(manifest.lease_clock, "none")
        self.assertEqual(manifest.artifact_scope, "none")
        self.assertTrue(manifest.expected_revision)
        self.assertFalse(manifest.migration_source)
        self.assertFalse(manifest.migration_target)

    def test_initialize_creates_a_local_database_without_an_external_service(self):
        store = SQLiteStateStore(self.database_path)

        store.initialize()

        self.assertTrue(self.database_path.is_file())
        with closing(sqlite3.connect(self.database_path)) as connection:
            objects = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_schema "
                    "WHERE name IN ('projects', 'state_events', "
                    "'state_events_no_update', 'state_events_no_delete')"
                )
            }
        self.assertEqual(
            objects,
            {
                ("table", "projects"),
                ("table", "state_events"),
                ("trigger", "state_events_no_update"),
                ("trigger", "state_events_no_delete"),
            },
        )

    def test_initialize_is_idempotent_and_rejects_an_unknown_newer_schema(self):
        store = SQLiteStateStore(self.database_path)
        store.initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            before = connection.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            self.assertEqual(
                connection.execute("PRAGMA application_id").fetchone()[0],
                SQLITE_APPLICATION_ID,
            )
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                SQLITE_SCHEMA_VERSION,
            )

        store.initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            after = connection.execute(
                "SELECT type, name, sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            ).fetchall()
            connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION + 1}")
        self.assertEqual(after, before)

        with self.assertRaisesRegex(SQLiteStateStoreError, "newer schema"):
            store.initialize()

    def test_every_adapter_connection_uses_the_durability_pragmas(self):
        store = SQLiteStateStore(self.database_path, busy_timeout_ms=375)
        store.initialize()

        with store._connect() as connection:
            pragmas = {
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
                "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            }

        self.assertEqual(pragmas["journal_mode"].lower(), "wal")
        self.assertEqual(pragmas["foreign_keys"], 1)
        self.assertEqual(pragmas["busy_timeout"], 375)
        self.assertEqual(pragmas["synchronous"], 2)

    def test_initialize_fails_closed_when_wal_mode_is_unavailable(self):
        with self.assertRaisesRegex(
            sqlite_state_store_contracts.SQLiteStateIntegrityError,
            "WAL",
        ):
            SQLiteStateStore(":memory:").initialize()

    def test_initialize_does_not_claim_an_unrelated_unversioned_database(self):
        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            connection.execute("CREATE TABLE unrelated_data (value TEXT)")

        with self.assertRaisesRegex(SQLiteStateStoreError, "unclaimed"):
            SQLiteStateStore(self.database_path).initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name = 'unrelated_data'"
                ).fetchone()
            )

    def test_initialize_rejects_nonempty_unowned_database_with_empty_schema(self):
        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            connection.execute("CREATE TABLE discarded_data (value BLOB)")
            connection.execute(
                "INSERT INTO discarded_data VALUES (zeroblob(65536))"
            )
            connection.execute("DROP TABLE discarded_data")
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
                ).fetchone()[0],
                0,
            )
            self.assertGreater(connection.execute("PRAGMA freelist_count").fetchone()[0], 0)
        self.assertGreater(self.database_path.stat().st_size, 0)

        with self.assertRaisesRegex(SQLiteStateStoreError, "unclaimed"):
            SQLiteStateStore(self.database_path).initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_rejected_unowned_database_preserves_journal_mode(self):
        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            connection.execute("PRAGMA journal_mode = DELETE")
            connection.execute("CREATE TABLE unrelated_data (value TEXT)")
            before = connection.execute("PRAGMA journal_mode").fetchone()[0]

        with self.assertRaisesRegex(SQLiteStateStoreError, "unclaimed"):
            SQLiteStateStore(self.database_path).initialize()

        with closing(sqlite3.connect(self.database_path)) as connection:
            after = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(after, before)

    def test_busy_errors_have_backend_neutral_and_sqlite_boundaries(self):
        self.assertTrue(hasattr(state_store_contracts, "StateStoreBusy"))
        self.assertTrue(hasattr(sqlite_state_store_contracts, "SQLiteStateBusy"))
        generic_busy = state_store_contracts.StateStoreBusy
        sqlite_busy = sqlite_state_store_contracts.SQLiteStateBusy
        self.assertTrue(issubclass(sqlite_busy, generic_busy))
        self.assertTrue(issubclass(sqlite_busy, SQLiteStateStoreError))

    def test_lock_timeout_is_normalized_and_write_recovers_after_release(self):
        store = SQLiteStateStore(self.database_path, busy_timeout_ms=25)
        store.initialize()
        fixture = _SQLiteFixtureMixin()
        _SQLiteFixtureMixin.setUpClass()
        first = fixture.make_initial_snapshot()
        store.create_project(first)
        second = fixture.make_initial_snapshot()

        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as blocker:
            blocker.execute("PRAGMA journal_mode = WAL")
            blocker.execute("BEGIN IMMEDIATE")
            with self.assertRaises(state_store_contracts.StateStoreBusy):
                store.create_project(second)
            blocker.execute("ROLLBACK")

        store.create_project(second)
        self.assertEqual(
            store.read_project(second["project"]["project_id"])["project"][
                "project_id"
            ],
            second["project"]["project_id"],
        )


class M209SQLiteStateStoreConformanceTests(
    _SQLiteFixtureMixin,
    AuthoritativeStateStoreConformanceMixin,
    unittest.TestCase,
):
    def make_store(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        store = SQLiteStateStore(
            Path(temporary_directory.name) / "conformance.sqlite3"
        )
        store.initialize()
        return store


class M209SQLiteStateStoreDurabilityTests(
    _SQLiteFixtureMixin,
    unittest.TestCase,
):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "durable.sqlite3"
        self.store = SQLiteStateStore(self.database_path)
        self.store.initialize()

    def _create_committed_project(self, suffix):
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, suffix)
        self.store.create_project(initial)
        self.store.commit_event(
            project_id=initial["project"]["project_id"],
            expected_revision=initial["project"]["revision"],
            event=event,
            expected_snapshot=expected,
        )
        return initial, event, expected

    def test_two_connections_make_one_commit_and_one_explicit_conflict(self):
        stores = (
            SQLiteStateStore(self.database_path),
            SQLiteStateStore(self.database_path),
        )
        for store in stores:
            store.initialize()

        for attempt in range(8):
            with self.subTest(attempt=attempt):
                initial = self.make_initial_snapshot()
                self.store.create_project(initial)
                candidates = (
                    self.make_candidate(initial, f"writer-a-{attempt}"),
                    self.make_candidate(initial, f"writer-b-{attempt}"),
                )
                barrier = threading.Barrier(2)

                def commit(index):
                    event, expected = candidates[index]
                    barrier.wait()
                    try:
                        stores[index].commit_event(
                            project_id=initial["project"]["project_id"],
                            expected_revision=initial["project"]["revision"],
                            event=event,
                            expected_snapshot=expected,
                        )
                    except state_store_contracts.StateStoreConflict:
                        return "conflict"
                    return "committed"

                with ThreadPoolExecutor(max_workers=2) as executor:
                    outcomes = list(executor.map(commit, (0, 1)))

                self.assertEqual(sorted(outcomes), ["committed", "conflict"])
                restored = self.store.read_project(
                    initial["project"]["project_id"]
                )
                events = self.store.read_events(initial["project"]["project_id"])
                self.assertEqual(restored["project"]["revision"], 15)
                self.assertEqual(len(events), 1)

    def test_event_rows_reject_raw_update_and_delete(self):
        initial, event, expected = self._create_committed_project("append-only")
        project_id = initial["project"]["project_id"]

        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            for statement in (
                "UPDATE state_events SET envelope = '{}' WHERE project_id = ?",
                "DELETE FROM state_events WHERE project_id = ?",
            ):
                with self.subTest(statement=statement):
                    with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                        connection.execute(statement, (project_id,))

        self.assertEqual(self.store.read_events(project_id), [event])
        self.assertEqual(self.store.read_project(project_id), expected)

    def test_event_identity_is_unique_within_each_project_stream(self):
        first_initial = self.make_initial_snapshot()
        second_initial = self.make_initial_snapshot()
        shared_event_id = f"event-shared-{uuid.uuid4().hex}"
        first_event, first_expected = self.make_candidate(
            first_initial,
            "project-scope-first",
            event_id=shared_event_id,
        )
        second_event, second_expected = self.make_candidate(
            second_initial,
            "project-scope-second",
            event_id=shared_event_id,
        )
        self.store.create_project(first_initial)
        self.store.create_project(second_initial)

        self.store.commit_event(
            project_id=first_event["project_id"],
            expected_revision=first_event["revision_before"],
            event=first_event,
            expected_snapshot=first_expected,
        )
        self.store.commit_event(
            project_id=second_event["project_id"],
            expected_revision=second_event["revision_before"],
            event=second_event,
            expected_snapshot=second_expected,
        )

        self.assertEqual(self.store.read_events(first_event["project_id"]), [first_event])
        self.assertEqual(self.store.read_events(second_event["project_id"]), [second_event])

    def test_project_read_rejects_event_head_drift(self):
        initial, _event, _expected = self._create_committed_project("head-drift")
        project_id = initial["project"]["project_id"]
        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            connection.execute(
                "UPDATE projects SET last_sequence = last_sequence + 1 "
                "WHERE project_id = ?",
                (project_id,),
            )

        with self.assertRaisesRegex(
            sqlite_state_store_contracts.SQLiteStateIntegrityError,
            "head",
        ):
            self.store.read_project(project_id)

    def test_event_read_rejects_column_and_envelope_drift(self):
        initial, _event, _expected = self._create_committed_project("column-drift")
        project_id = initial["project"]["project_id"]
        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            connection.execute("DROP TRIGGER state_events_no_update")
            connection.execute(
                "UPDATE state_events SET occurred_at = '2026-08-10T09:00:00+08:00' "
                "WHERE project_id = ?",
                (project_id,),
            )

        with self.assertRaisesRegex(
            sqlite_state_store_contracts.SQLiteStateIntegrityError,
            "envelope",
        ):
            self.store.read_events(project_id)

    def test_event_read_rejects_a_revision_chain_that_skips_a_revision(self):
        initial = self.make_initial_snapshot()
        first, after_first = self.make_candidate(initial, "revision-chain-first")
        second, after_second = self.make_candidate(
            after_first,
            "revision-chain-second",
            sequence_no=2,
            previous_event_sha256=first["event_sha256"],
        )
        project_id = initial["project"]["project_id"]
        self.store.create_project(initial)
        self.store.commit_event(
            project_id=project_id,
            expected_revision=initial["project"]["revision"],
            event=first,
            expected_snapshot=after_first,
        )
        self.store.commit_event(
            project_id=project_id,
            expected_revision=after_first["project"]["revision"],
            event=second,
            expected_snapshot=after_second,
        )
        forged_project_after = copy.deepcopy(second["project_after"])
        forged_project_after["revision"] = second["revision_after"] + 1
        forged = build_state_event(
            event_id=second["event_id"],
            event_type=second["event_type"],
            project_id=project_id,
            sequence_no=2,
            revision_before=second["revision_after"],
            occurred_at=second["occurred_at"],
            actor_ref=second["actor_ref"],
            causation_ref=second["causation_ref"],
            correlation_ref=second["correlation_ref"],
            previous_event_sha256=first["event_sha256"],
            supersedes_event_id=second["supersedes_event_id"],
            changes=second["changes"],
            project_after=forged_project_after,
        )
        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            connection.execute("DROP TRIGGER state_events_no_update")
            connection.execute(
                """
                UPDATE state_events
                SET revision_before = ?, revision_after = ?, event_sha256 = ?,
                    envelope = ?
                WHERE project_id = ? AND sequence_no = 2
                """,
                (
                    forged["revision_before"],
                    forged["revision_after"],
                    forged["event_sha256"],
                    json.dumps(forged, sort_keys=True, separators=(",", ":")),
                    project_id,
                ),
            )
            connection.execute(
                "UPDATE projects SET revision = ?, last_event_sha256 = ? "
                "WHERE project_id = ?",
                (forged["revision_after"], forged["event_sha256"], project_id),
            )

        with self.assertRaisesRegex(
            sqlite_state_store_contracts.SQLiteStateIntegrityError,
            "revision chain",
        ):
            self.store.read_events(project_id)

    def test_initialize_rejects_missing_schema_objects_at_current_version(self):
        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            connection.execute("DROP TRIGGER state_events_no_delete")

        with self.assertRaisesRegex(SQLiteStateStoreError, "schema"):
            self.store.initialize()

    def test_initialize_rejects_a_same_name_noop_append_only_trigger(self):
        with closing(
            sqlite3.connect(self.database_path, isolation_level=None)
        ) as connection:
            connection.execute("DROP TRIGGER state_events_no_delete")
            connection.execute(
                "CREATE TRIGGER state_events_no_delete "
                "BEFORE DELETE ON state_events BEGIN SELECT 1; END"
            )

        with self.assertRaisesRegex(SQLiteStateStoreError, "schema"):
            self.store.initialize()

    def _run_crashing_commit(self, point, event, expected):
        event_path = Path(self.temporary_directory.name) / f"{point}-event.json"
        snapshot_path = Path(self.temporary_directory.name) / f"{point}-snapshot.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        snapshot_path.write_text(json.dumps(expected), encoding="utf-8")
        child = """
import json
import os
import sys
from pathlib import Path
from context_control_plane.sqlite_state_store import SQLiteStateStore

database_path, point, event_path, snapshot_path = sys.argv[1:]
event = json.loads(Path(event_path).read_text(encoding="utf-8"))
snapshot = json.loads(Path(snapshot_path).read_text(encoding="utf-8"))

def fault_hook(observed):
    if observed == point:
        os._exit(91)

store = SQLiteStateStore(database_path, fault_hook=fault_hook)
store.commit_event(
    project_id=event["project_id"],
    expected_revision=event["revision_before"],
    event=event,
    expected_snapshot=snapshot,
)
"""
        return subprocess.run(
            [
                sys.executable,
                "-c",
                child,
                str(self.database_path),
                point,
                str(event_path),
                str(snapshot_path),
            ],
            cwd=Path(__file__).parents[1],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_process_death_after_event_insert_rolls_back_the_transaction(self):
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, "crash-before-commit")
        self.store.create_project(initial)

        result = self._run_crashing_commit("after_event_insert", event, expected)

        self.assertEqual(result.returncode, 91, result.stderr)
        recovered = SQLiteStateStore(self.database_path)
        recovered.initialize()
        self.assertEqual(recovered.read_project(event["project_id"]), initial)
        self.assertEqual(recovered.read_events(event["project_id"]), [])

    def test_process_death_after_commit_recovers_exactly_one_event(self):
        initial = self.make_initial_snapshot()
        event, expected = self.make_candidate(initial, "crash-after-commit")
        self.store.create_project(initial)

        result = self._run_crashing_commit("after_commit", event, expected)

        self.assertEqual(result.returncode, 91, result.stderr)
        recovered = SQLiteStateStore(self.database_path)
        recovered.initialize()
        self.assertEqual(recovered.read_project(event["project_id"]), expected)
        self.assertEqual(recovered.read_events(event["project_id"]), [event])
        with self.assertRaises(state_store_contracts.StateStoreConflict):
            recovered.commit_event(
                project_id=event["project_id"],
                expected_revision=event["revision_before"],
                event=event,
                expected_snapshot=expected,
            )
        self.assertEqual(recovered.read_events(event["project_id"]), [event])

    def test_physical_header_corruption_is_a_typed_integrity_error(self):
        initial = self.make_initial_snapshot()
        self.store.create_project(initial)
        with self.database_path.open("r+b") as database:
            database.write(b"corrupted-header")

        with self.assertRaises(state_store_contracts.StateStoreIntegrityError):
            SQLiteStateStore(self.database_path).initialize()

    def test_non_header_btree_page_corruption_is_a_typed_integrity_error(self):
        initial = self.make_initial_snapshot()
        self.store.create_project(initial)
        with self.store._connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            root_page = connection.execute(
                "SELECT rootpage FROM sqlite_schema WHERE name = 'projects'"
            ).fetchone()[0]
        self.assertGreater(root_page, 1)
        with self.database_path.open("r+b") as database:
            database.seek((root_page - 1) * page_size)
            database.write(b"\x00" * 16)

        with self.assertRaises(state_store_contracts.StateStoreIntegrityError):
            SQLiteStateStore(self.database_path).initialize()

    def test_two_spawned_processes_make_one_commit_and_one_conflict(self):
        initial = self.make_initial_snapshot()
        self.store.create_project(initial)
        candidates = (
            self.make_candidate(initial, "process-a"),
            self.make_candidate(initial, "process-b"),
        )
        context = multiprocessing.get_context("spawn")
        barrier = context.Barrier(2)
        outcomes = context.Queue()
        processes = [
            context.Process(
                target=_cross_process_commit_worker,
                args=(
                    self.database_path,
                    event,
                    expected,
                    barrier,
                    outcomes,
                ),
            )
            for event, expected in candidates
        ]

        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
        results = sorted(outcomes.get(timeout=2) for _ in processes)
        for process in processes:
            self.assertFalse(process.is_alive())
            self.assertEqual(process.exitcode, 0)
        outcomes.close()
        outcomes.join_thread()

        self.assertEqual(results, ["committed", "conflict"])
        self.assertEqual(len(self.store.read_events(initial["project"]["project_id"])), 1)

    def test_unicode_path_can_checkpoint_rename_reopen_and_delete(self):
        unicode_path = (
            Path(self.temporary_directory.name)
            / "state directory"
            / "context-\u72b6\u6001.sqlite3"
        )
        unicode_path.parent.mkdir()
        store = SQLiteStateStore(unicode_path)
        store.initialize()
        initial = self.make_initial_snapshot()
        store.create_project(initial)
        with store._connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        renamed = unicode_path.with_name("renamed-\u72b6\u6001.sqlite3")
        unicode_path.rename(renamed)
        reopened = SQLiteStateStore(renamed)
        reopened.initialize()
        self.assertEqual(
            reopened.read_project(initial["project"]["project_id"]),
            initial,
        )
        renamed.unlink()
        self.assertFalse(renamed.exists())
        for suffix in ("-wal", "-shm"):
            self.assertFalse(Path(f"{unicode_path}{suffix}").exists())
            self.assertFalse(Path(f"{renamed}{suffix}").exists())


if __name__ == "__main__":
    unittest.main()
