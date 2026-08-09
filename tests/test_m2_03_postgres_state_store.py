import copy
import os
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import yaml

from context_control_plane.postgres_state_store import (
    PostgresStateConflict,
    PostgresStateIntegrityError,
    PostgresStateNotFound,
    PostgresStateStore,
)
from context_control_plane.state_events import build_state_event


@unittest.skipUnless(
    os.environ.get("CONTEXT_TEST_POSTGRES_DSN"),
    "CONTEXT_TEST_POSTGRES_DSN is required for PostgreSQL integration tests",
)
class M203PostgresStateStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = os.environ["CONTEXT_TEST_POSTGRES_DSN"]
        fixture_path = (
            Path(__file__).parents[1]
            / "experiments"
            / "state"
            / "m2-01-core-fixtures.yaml"
        )
        fixture_set = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
        cases = {case["case_id"]: case["document"] for case in fixture_set["cases"]}
        cls.base_initial = cases["completed-work-overlap-blocked"]
        cls.store = PostgresStateStore(cls.dsn)
        cls.store.initialize()

    def _initial_snapshot(self) -> dict:
        snapshot = copy.deepcopy(self.base_initial)
        snapshot["project"]["project_id"] = f"project-cas-{uuid.uuid4().hex}"
        return snapshot

    def _candidate(
        self,
        initial: dict,
        suffix: str,
        *,
        sequence_no: int = 1,
        previous_event_sha256: str | None = None,
        event_id: str | None = None,
    ) -> tuple[dict, dict]:
        expected = copy.deepcopy(initial)
        expected["project"]["revision"] = initial["project"]["revision"] + 1
        expected["project"]["updated_at"] = "2026-08-10T01:01:00+08:00"
        idea = {
            "idea_id": f"idea-{suffix}",
            "parent_work_id": "work-repeat",
            "source_ref": f"opaque://input/{suffix}",
            "summary": f"Concurrent candidate {suffix}.",
            "status": "parked",
            "return_work_id": "work-repeat",
            "expiry": None,
            "attempt_budget": None,
            "promotion_target": "M3",
            "evidence_ids": [],
        }
        expected["ideas"].append(idea)
        event = build_state_event(
            event_id=event_id or f"event-{suffix}",
            event_type="state-transition",
            project_id=initial["project"]["project_id"],
            sequence_no=sequence_no,
            revision_before=initial["project"]["revision"],
            occurred_at="2026-08-10T01:01:00+08:00",
            actor_ref=f"actor-{suffix}",
            causation_ref=f"work:{suffix}",
            correlation_ref="replay:m2-03-cas",
            previous_event_sha256=previous_event_sha256,
            supersedes_event_id=None,
            changes=[{"collection": "ideas", "object_id": idea["idea_id"], "value": idea}],
            project_after=expected["project"],
        )
        return event, expected

    def test_same_expected_revision_allows_one_writer_and_conflicts_the_other(self):
        for attempt in range(8):
            with self.subTest(attempt=attempt):
                initial = self._initial_snapshot()
                self.store.create_project(initial)
                candidates = [
                    self._candidate(initial, f"writer-a-{attempt}"),
                    self._candidate(initial, f"writer-b-{attempt}"),
                ]
                barrier = threading.Barrier(2)

                def commit(candidate: tuple[dict, dict]) -> str:
                    event, expected = candidate
                    barrier.wait()
                    try:
                        self.store.commit_event(
                            project_id=initial["project"]["project_id"],
                            expected_revision=14,
                            event=event,
                            expected_snapshot=expected,
                        )
                    except PostgresStateConflict:
                        return "conflict"
                    return "committed"

                with ThreadPoolExecutor(max_workers=2) as executor:
                    outcomes = list(executor.map(commit, candidates))

                self.assertEqual(sorted(outcomes), ["committed", "conflict"])
                restored = self.store.read_project(initial["project"]["project_id"])
                events = self.store.read_events(initial["project"]["project_id"])
                self.assertEqual(restored["project"]["revision"], 15)
                self.assertEqual(len(restored["ideas"]), 1)
                self.assertEqual(len(events), 1)

    def test_second_event_advances_revision_sequence_and_hash_chain(self):
        initial = self._initial_snapshot()
        self.store.create_project(initial)
        first, after_first = self._candidate(initial, "first")
        self.store.commit_event(
            project_id=initial["project"]["project_id"],
            expected_revision=14,
            event=first,
            expected_snapshot=after_first,
        )
        second, after_second = self._candidate(
            after_first,
            "second",
            sequence_no=2,
            previous_event_sha256=first["event_sha256"],
        )

        self.store.commit_event(
            project_id=initial["project"]["project_id"],
            expected_revision=15,
            event=second,
            expected_snapshot=after_second,
        )

        restored = self.store.read_project(initial["project"]["project_id"])
        events = self.store.read_events(initial["project"]["project_id"])
        self.assertEqual(restored["project"]["revision"], 16)
        self.assertEqual([event["sequence_no"] for event in events], [1, 2])
        self.assertEqual(events[1]["previous_event_sha256"], events[0]["event_sha256"])

    def test_duplicate_event_identity_is_a_conflict_and_rolls_back(self):
        initial = self._initial_snapshot()
        self.store.create_project(initial)
        first, after_first = self._candidate(initial, "identity-first")
        self.store.commit_event(
            project_id=initial["project"]["project_id"],
            expected_revision=14,
            event=first,
            expected_snapshot=after_first,
        )
        duplicate, after_duplicate = self._candidate(
            after_first,
            "identity-duplicate",
            sequence_no=2,
            previous_event_sha256=first["event_sha256"],
            event_id=first["event_id"],
        )

        with self.assertRaisesRegex(PostgresStateConflict, "event identity"):
            self.store.commit_event(
                project_id=initial["project"]["project_id"],
                expected_revision=15,
                event=duplicate,
                expected_snapshot=after_duplicate,
            )

        restored = self.store.read_project(initial["project"]["project_id"])
        events = self.store.read_events(initial["project"]["project_id"])
        self.assertEqual(restored["project"]["revision"], 15)
        self.assertEqual(len(events), 1)

    def test_event_rows_reject_update_and_delete(self):
        initial = self._initial_snapshot()
        self.store.create_project(initial)
        event, expected = self._candidate(initial, "append-only")
        self.store.commit_event(
            project_id=initial["project"]["project_id"],
            expected_revision=14,
            event=event,
            expected_snapshot=expected,
        )

        for operation in ("UPDATE", "DELETE"):
            with self.subTest(operation=operation):
                statement = (
                    "UPDATE context_control.state_events SET envelope = '{}'::jsonb "
                    "WHERE project_id = %s"
                    if operation == "UPDATE"
                    else "DELETE FROM context_control.state_events WHERE project_id = %s"
                )
                with self.assertRaises(psycopg.Error) as failure:
                    with psycopg.connect(self.dsn) as connection:
                        connection.execute(
                            statement,
                            (initial["project"]["project_id"],),
                        )
                self.assertEqual(failure.exception.sqlstate, "55000")

        self.assertEqual(len(self.store.read_events(initial["project"]["project_id"])), 1)

    def test_row_revision_drift_is_detected_on_read(self):
        initial = self._initial_snapshot()
        self.store.create_project(initial)
        with psycopg.connect(self.dsn) as connection:
            connection.execute(
                """
                UPDATE context_control.projects
                SET revision = revision + 1
                WHERE project_id = %s
                """,
                (initial["project"]["project_id"],),
            )

        with self.assertRaisesRegex(PostgresStateIntegrityError, "row revision"):
            self.store.read_project(initial["project"]["project_id"])

    def test_replay_mismatch_rolls_back_without_an_event(self):
        initial = self._initial_snapshot()
        self.store.create_project(initial)
        event, _ = self._candidate(initial, "event-value")
        _, different_snapshot = self._candidate(initial, "different-value")

        with self.assertRaisesRegex(PostgresStateIntegrityError, "event replay"):
            self.store.commit_event(
                project_id=initial["project"]["project_id"],
                expected_revision=14,
                event=event,
                expected_snapshot=different_snapshot,
            )

        restored = self.store.read_project(initial["project"]["project_id"])
        self.assertEqual(restored, initial)
        self.assertEqual(self.store.read_events(initial["project"]["project_id"]), [])

    def test_duplicate_project_create_is_an_explicit_conflict(self):
        initial = self._initial_snapshot()
        self.store.create_project(initial)

        with self.assertRaisesRegex(PostgresStateConflict, "already exists"):
            self.store.create_project(initial)

    def test_unknown_project_is_explicitly_not_found(self):
        with self.assertRaisesRegex(PostgresStateNotFound, "does not exist"):
            self.store.read_project(f"project-missing-{uuid.uuid4().hex}")

    def test_migration_down_and_up_is_reversible(self):
        down_path = (
            Path(__file__).parents[1]
            / "database"
            / "migrations"
            / "001_m2_03_postgres_state.down.sql"
        )
        try:
            with psycopg.connect(self.dsn) as connection:
                connection.execute(
                    down_path.read_text(encoding="utf-8"),
                    prepare=False,
                )
                row = connection.execute(
                    "SELECT to_regclass('context_control.projects')"
                ).fetchone()
                self.assertIsNone(row[0])
        finally:
            self.store.initialize()

        with psycopg.connect(self.dsn) as connection:
            row = connection.execute(
                "SELECT to_regclass('context_control.projects')"
            ).fetchone()
        self.assertEqual(row[0], "context_control.projects")

    def test_acceptance_evidence_records_conflicts_latency_and_authority(self):
        evidence_path = (
            Path(__file__).parents[1]
            / "experiments"
            / "state"
            / "m2-03-postgres-cas-results.yaml"
        )
        evidence = yaml.safe_load(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(
            evidence["schema_version"],
            "context.postgres-cas-results/v1alpha1",
        )
        self.assertEqual(evidence["environment"]["postgresql_version"], "18.4")
        self.assertEqual(evidence["environment"]["psycopg_version"], "3.3.4")
        self.assertEqual(evidence["conflict_injection"]["attempts"], 8)
        self.assertEqual(evidence["conflict_injection"]["explicit_conflicts"], 8)
        self.assertEqual(evidence["conflict_injection"]["silent_overwrites"], 0)
        self.assertEqual(evidence["latency_ms"]["samples"], 40)
        self.assertGreaterEqual(
            evidence["latency_ms"]["commit_p95"],
            evidence["latency_ms"]["commit_p50"],
        )
        self.assertFalse(evidence["authority_boundary"]["vector_index_authority"])


if __name__ == "__main__":
    unittest.main()
