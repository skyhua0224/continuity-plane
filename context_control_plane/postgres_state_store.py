"""PostgreSQL revision/CAS store for typed snapshots and state events."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .state_events import replay_state_events, validate_state_event
from .typed_state import canonical_state_bytes, validate_typed_state


_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "database"
    / "migrations"
    / "001_m2_03_postgres_state.up.sql"
)


class PostgresStateStoreError(RuntimeError):
    """Base error for the M2-03 PostgreSQL state store."""


class PostgresStateConflict(PostgresStateStoreError):
    """Raised when expected revision or append position is stale."""


class PostgresStateNotFound(PostgresStateStoreError):
    """Raised when a project does not exist."""


class PostgresStateIntegrityError(PostgresStateStoreError):
    """Raised when persisted or proposed state fails integrity checks."""


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_state_bytes(snapshot)).hexdigest()


def _json_value(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


class PostgresStateStore:
    """Store typed state with append-only events and transactional CAS."""

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("dsn must be a non-empty string")
        self._dsn = dsn

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, row_factory=dict_row)

    def initialize(self) -> None:
        migration = _MIGRATION_PATH.read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.execute(migration, prepare=False)

    def create_project(self, snapshot: dict[str, Any]) -> None:
        snapshot = copy.deepcopy(snapshot)
        validate_typed_state(snapshot)
        project = snapshot["project"]
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO context_control.projects (
                        project_id,
                        revision,
                        last_sequence,
                        last_event_sha256,
                        snapshot,
                        snapshot_sha256
                    ) VALUES (%s, %s, 0, NULL, %s, %s)
                    """,
                    (
                        project["project_id"],
                        project["revision"],
                        Jsonb(_json_value(snapshot)),
                        _snapshot_sha256(snapshot),
                    ),
                )
        except UniqueViolation as exc:
            raise PostgresStateConflict(
                f"project already exists: {project['project_id']}"
            ) from exc

    def read_project(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT revision, snapshot, snapshot_sha256
                FROM context_control.projects
                WHERE project_id = %s
                """,
                (project_id,),
            ).fetchone()
        if row is None:
            raise PostgresStateNotFound(f"project does not exist: {project_id}")
        snapshot = copy.deepcopy(row["snapshot"])
        validate_typed_state(snapshot)
        if snapshot["project"]["revision"] != row["revision"]:
            raise PostgresStateIntegrityError("persisted row revision mismatch")
        if _snapshot_sha256(snapshot) != row["snapshot_sha256"].strip():
            raise PostgresStateIntegrityError("persisted snapshot hash mismatch")
        return snapshot

    def read_events(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT envelope
                FROM context_control.state_events
                WHERE project_id = %s
                ORDER BY sequence_no
                """,
                (project_id,),
            ).fetchall()
        events = [copy.deepcopy(row["envelope"]) for row in rows]
        for event in events:
            validate_state_event(event)
        return events

    def commit_event(
        self,
        *,
        project_id: str,
        expected_revision: int,
        event: dict[str, Any],
        expected_snapshot: dict[str, Any],
    ) -> None:
        event = copy.deepcopy(event)
        expected_snapshot = copy.deepcopy(expected_snapshot)
        validate_state_event(event)
        validate_typed_state(expected_snapshot)
        if event["project_id"] != project_id:
            raise PostgresStateIntegrityError("event project_id mismatch")
        if expected_snapshot["project"]["project_id"] != project_id:
            raise PostgresStateIntegrityError("snapshot project_id mismatch")
        if event["revision_before"] != expected_revision:
            raise PostgresStateIntegrityError("event revision_before mismatch")
        if event["project_after"] != expected_snapshot["project"]:
            raise PostgresStateIntegrityError("event project_after mismatch")

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    revision,
                    last_sequence,
                    last_event_sha256,
                    snapshot,
                    snapshot_sha256
                FROM context_control.projects
                WHERE project_id = %s
                FOR UPDATE
                """,
                (project_id,),
            ).fetchone()
            if row is None:
                raise PostgresStateNotFound(f"project does not exist: {project_id}")

            actual_revision = row["revision"]
            if actual_revision != expected_revision:
                raise PostgresStateConflict(
                    f"expected revision {expected_revision}, actual revision {actual_revision}"
                )
            current_snapshot = copy.deepcopy(row["snapshot"])
            if _snapshot_sha256(current_snapshot) != row["snapshot_sha256"].strip():
                raise PostgresStateIntegrityError("persisted snapshot hash mismatch")
            expected_sequence = row["last_sequence"] + 1
            previous_event_sha256 = row["last_event_sha256"]
            if previous_event_sha256 is not None:
                previous_event_sha256 = previous_event_sha256.strip()
            if event["sequence_no"] != expected_sequence:
                raise PostgresStateConflict(
                    f"expected event sequence {expected_sequence}, got {event['sequence_no']}"
                )
            if event["previous_event_sha256"] != previous_event_sha256:
                raise PostgresStateConflict("event hash chain does not match current head")

            restored = replay_state_events(
                current_snapshot,
                [event],
                starting_sequence_no=expected_sequence,
                previous_event_sha256=previous_event_sha256,
            )
            if canonical_state_bytes(restored) != canonical_state_bytes(expected_snapshot):
                raise PostgresStateIntegrityError(
                    "event replay does not produce expected snapshot"
                )

            try:
                connection.execute(
                    """
                    INSERT INTO context_control.state_events (
                        project_id,
                        sequence_no,
                        event_id,
                        revision_before,
                        revision_after,
                        previous_event_sha256,
                        event_sha256,
                        occurred_at,
                        envelope
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        project_id,
                        event["sequence_no"],
                        event["event_id"],
                        event["revision_before"],
                        event["revision_after"],
                        event["previous_event_sha256"],
                        event["event_sha256"],
                        event["occurred_at"],
                        Jsonb(_json_value(event)),
                    ),
                )
            except UniqueViolation as exc:
                raise PostgresStateConflict(
                    "event identity or append position conflict"
                ) from exc
            updated = connection.execute(
                """
                UPDATE context_control.projects
                SET
                    revision = %s,
                    last_sequence = %s,
                    last_event_sha256 = %s,
                    snapshot = %s,
                    snapshot_sha256 = %s,
                    updated_at = statement_timestamp()
                WHERE project_id = %s AND revision = %s
                """,
                (
                    event["revision_after"],
                    event["sequence_no"],
                    event["event_sha256"],
                    Jsonb(_json_value(expected_snapshot)),
                    _snapshot_sha256(expected_snapshot),
                    project_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                raise PostgresStateConflict("project revision changed during commit")
