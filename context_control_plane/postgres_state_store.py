"""PostgreSQL revision/CAS store for typed snapshots and state events."""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .state_store import (
    StateStoreCapabilityManifest,
    StateStoreBusy,
    StateStoreConflict,
    StateStoreError,
    StateStoreIntegrityError,
    StateStoreNotFound,
)
from .state_events import StateEventError, replay_state_events, validate_state_event
from .typed_state import TypedStateError, canonical_state_bytes, validate_typed_state


_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "database"
    / "migrations"
    / "001_m2_03_postgres_state.up.sql"
)


class PostgresStateStoreError(StateStoreError):
    """Base error for the M2-03 PostgreSQL state store."""


class PostgresStateConflict(PostgresStateStoreError, StateStoreConflict):
    """Raised when expected revision or append position is stale."""


class PostgresStateNotFound(PostgresStateStoreError, StateStoreNotFound):
    """Raised when a project does not exist."""


class PostgresStateBusy(PostgresStateStoreError, StateStoreBusy):
    """Raised when PostgreSQL is temporarily unreachable or unavailable."""


class PostgresStateIntegrityError(PostgresStateStoreError, StateStoreIntegrityError):
    """Raised when persisted or proposed state fails integrity checks."""


def _validate_snapshot(snapshot: dict[str, Any]) -> None:
    try:
        validate_typed_state(snapshot)
    except TypedStateError as exc:
        raise PostgresStateIntegrityError("typed state validation failed") from exc


def _validate_event(event: dict[str, Any]) -> None:
    try:
        validate_state_event(event)
    except StateEventError as exc:
        raise PostgresStateIntegrityError("state Event validation failed") from exc


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    try:
        canonical = canonical_state_bytes(snapshot)
    except TypedStateError as exc:
        raise PostgresStateIntegrityError("typed state validation failed") from exc
    return hashlib.sha256(canonical).hexdigest()


def _json_value(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


class PostgresStateStore:
    """Store typed state with append-only events and transactional CAS."""

    capability_manifest = StateStoreCapabilityManifest(
        schema_version="context.state-store-capabilities/v1alpha1",
        adapter_id="context.postgresql",
        adapter_version="1.0.0-alpha.1",
        authority_mode="shared",
        operations=("create_project", "read_project", "read_events", "commit_event"),
        shared_authority=True,
        offline_write=False,
        unique_claim=False,
        multi_writer=True,
        lease_clock="none",
        artifact_scope="none",
        expected_revision=True,
        migration_source=False,
        migration_target=False,
    )

    def __init__(self, dsn: str):
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("dsn must be a non-empty string")
        self._dsn = dsn

    @contextmanager
    def _connect(self):
        try:
            with psycopg.connect(self._dsn, row_factory=dict_row) as connection:
                yield connection
        except psycopg.OperationalError as exc:
            raise PostgresStateBusy("PostgreSQL state store is unavailable") from exc

    def initialize(self) -> None:
        migration = _MIGRATION_PATH.read_text(encoding="utf-8")
        with self._connect() as connection:
            connection.execute(migration, prepare=False)

    def create_project(self, snapshot: dict[str, Any]) -> None:
        snapshot = copy.deepcopy(snapshot)
        _validate_snapshot(snapshot)
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
        _validate_snapshot(snapshot)
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
            _validate_event(event)
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
        _validate_event(event)
        _validate_snapshot(expected_snapshot)
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

            duplicate = connection.execute(
                """
                SELECT 1
                FROM context_control.state_events
                WHERE project_id = %s AND event_id = %s
                """,
                (project_id, event["event_id"]),
            ).fetchone()
            if duplicate is not None:
                raise PostgresStateConflict("event identity already exists")

            prior_events = None
            if event["supersedes_event_id"] is not None:
                prior_rows = connection.execute(
                    """
                    SELECT envelope
                    FROM context_control.state_events
                    WHERE project_id = %s
                    ORDER BY sequence_no
                    """,
                    (project_id,),
                ).fetchall()
                prior_events = [copy.deepcopy(item["envelope"]) for item in prior_rows]
            try:
                restored = replay_state_events(
                    current_snapshot,
                    [event],
                    starting_sequence_no=expected_sequence,
                    previous_event_sha256=previous_event_sha256,
                    prior_events=prior_events,
                )
            except (StateEventError, TypedStateError) as exc:
                raise PostgresStateIntegrityError("state Event replay failed") from exc
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
