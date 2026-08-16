"""Embedded SQLite authority adapter for local state and Event persistence."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from .durable_state_migration import (
    MIGRATION_RECEIPT_SCHEMA_VERSION as DURABLE_MIGRATION_RECEIPT_SCHEMA_VERSION,
)
from .durable_state_migration import (
    V5_SCHEMA_VERSION,
    DurableStateMigrationError,
    canonical_typed_state_migration_bytes,
    validate_durable_state_migration_receipt,
)
from .idea_review import (
    MIGRATION_RECEIPT_SCHEMA_VERSION as IDEA_REVIEW_MIGRATION_RECEIPT_SCHEMA_VERSION,
)
from .idea_review import (
    validate_typed_state_v3_to_v4_migration_receipt,
)
from .shared_state_migration import (
    MIGRATION_RECEIPT_SCHEMA_VERSION as SHARED_MIGRATION_RECEIPT_SCHEMA_VERSION,
)
from .shared_state_migration import (
    V6_SCHEMA_VERSION,
    DurableStateV6MigrationError,
    canonical_shared_state_migration_bytes,
    validate_typed_state_v5_to_v6_migration_receipt,
)
from .shared_work_ledger import ClaimLifecycleError, WorkLedger
from .state_events import (
    StateEventError,
    build_state_event,
    replay_state_events,
    validate_state_event,
)
from .state_store import (
    StateStoreBusy,
    StateStoreCapabilityManifest,
    StateStoreConflict,
    StateStoreError,
    StateStoreIntegrityError,
    StateStoreNotFound,
)
from .typed_state import TypedStateError, canonical_state_bytes, validate_typed_state

SQLITE_APPLICATION_ID = 0x43435031
SQLITE_SCHEMA_VERSION = 4


class SQLiteStateStoreError(StateStoreError):
    """Base error for the embedded SQLite adapter."""


class SQLiteStateConflict(SQLiteStateStoreError, StateStoreConflict):
    """Raised when a revision or append position is stale."""


class SQLiteStateNotFound(SQLiteStateStoreError, StateStoreNotFound):
    """Raised when a project does not exist."""


class SQLiteStateIntegrityError(SQLiteStateStoreError, StateStoreIntegrityError):
    """Raised when proposed or durable state fails validation."""


class SQLiteStateBusy(SQLiteStateStoreError, StateStoreBusy):
    """Raised when SQLite cannot acquire a lock before the configured timeout."""


def _validate_snapshot(snapshot: dict[str, Any]) -> None:
    try:
        if snapshot.get("schema_version") == V6_SCHEMA_VERSION:
            canonical_shared_state_migration_bytes(snapshot)
        elif snapshot.get("schema_version") == V5_SCHEMA_VERSION:
            canonical_typed_state_migration_bytes(snapshot)
        else:
            validate_typed_state(snapshot)
    except (
        DurableStateMigrationError,
        DurableStateV6MigrationError,
        TypedStateError,
    ) as exc:
        raise SQLiteStateIntegrityError("typed state validation failed") from exc


def _validate_event(event: dict[str, Any]) -> None:
    try:
        validate_state_event(event)
    except StateEventError as exc:
        raise SQLiteStateIntegrityError("state Event validation failed") from exc


def _snapshot_sha256(snapshot: dict[str, Any]) -> str:
    try:
        canonical = (
            canonical_shared_state_migration_bytes(snapshot)
            if snapshot.get("schema_version") == V6_SCHEMA_VERSION
            else (
                canonical_typed_state_migration_bytes(snapshot)
                if snapshot.get("schema_version") == V5_SCHEMA_VERSION
                else canonical_state_bytes(snapshot)
            )
        )
    except (
        DurableStateMigrationError,
        DurableStateV6MigrationError,
        TypedStateError,
    ) as exc:
        raise SQLiteStateIntegrityError("typed state validation failed") from exc
    return hashlib.sha256(canonical).hexdigest()


def _migration_event_head_sha256(receipt: dict[str, Any]) -> str | None:
    if receipt.get("schema_version") in {
        DURABLE_MIGRATION_RECEIPT_SCHEMA_VERSION,
        SHARED_MIGRATION_RECEIPT_SCHEMA_VERSION,
    }:
        event_head = receipt.get("source_event_head")
        if not isinstance(event_head, dict):
            raise SQLiteStateIntegrityError("migration receipt event head is invalid")
        value = event_head.get("event_sha256")
    else:
        value = receipt.get("source_event_head_sha256")
    if value is not None and (
        not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
    ):
        raise SQLiteStateIntegrityError("migration receipt event head is invalid")
    return value


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: str, *, field: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SQLiteStateIntegrityError(f"persisted {field} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise SQLiteStateIntegrityError(f"persisted {field} is not an object")
    return decoded


def _json_sha256(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SQLiteStateIntegrityError("value is not canonical JSON") from exc
    return hashlib.sha256(encoded).hexdigest()


_INITIAL_SCHEMA_V1 = """
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    last_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_sequence >= 0),
    last_event_sha256 TEXT,
    snapshot TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (last_sequence = 0 AND last_event_sha256 IS NULL)
        OR (last_sequence > 0 AND last_event_sha256 IS NOT NULL)
    )
) STRICT;

CREATE TABLE state_events (
    project_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
    event_id TEXT NOT NULL,
    revision_before INTEGER NOT NULL CHECK (revision_before >= 0),
    revision_after INTEGER NOT NULL CHECK (revision_after = revision_before + 1),
    previous_event_sha256 TEXT,
    event_sha256 TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    envelope TEXT NOT NULL,
    PRIMARY KEY (project_id, sequence_no),
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    UNIQUE (project_id, event_id),
    UNIQUE (project_id, revision_after),
    UNIQUE (project_id, event_sha256)
) STRICT;

CREATE TRIGGER state_events_no_update
BEFORE UPDATE ON state_events
BEGIN
    SELECT RAISE(ABORT, 'state_events are append-only');
END;

CREATE TRIGGER state_events_no_delete
BEFORE DELETE ON state_events
BEGIN
    SELECT RAISE(ABORT, 'state_events are append-only');
END;
"""

_MIGRATION_SCHEMA_V2 = """
CREATE TABLE typed_state_migrations (
    project_id TEXT NOT NULL,
    migration_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    source_event_head_sha256 TEXT,
    source_snapshot_sha256 TEXT NOT NULL,
    target_snapshot_sha256 TEXT NOT NULL,
    from_schema_version TEXT NOT NULL,
    to_schema_version TEXT NOT NULL,
    receipt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, migration_id),
    UNIQUE (project_id, from_schema_version, to_schema_version),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
) STRICT;

CREATE TRIGGER typed_state_migrations_no_update
BEFORE UPDATE ON typed_state_migrations
BEGIN
    SELECT RAISE(ABORT, 'typed_state_migrations are append-only');
END;

CREATE TRIGGER typed_state_migrations_no_delete
BEFORE DELETE ON typed_state_migrations
BEGIN
    SELECT RAISE(ABORT, 'typed_state_migrations are append-only');
END;
"""

_MIGRATION_SCHEMA_V3 = """
CREATE TABLE work_ledger_configs (
    project_id TEXT PRIMARY KEY,
    max_ttl_ms INTEGER NOT NULL CHECK (max_ttl_ms > 0),
    initialized_snapshot_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
) STRICT;

CREATE TABLE work_ledger_requests (
    project_id TEXT NOT NULL,
    request_namespace TEXT NOT NULL,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    committed_revision INTEGER NOT NULL CHECK (committed_revision >= 0),
    committed_at TEXT NOT NULL,
    PRIMARY KEY (project_id, request_namespace, request_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
) STRICT;

CREATE TRIGGER work_ledger_configs_no_update
BEFORE UPDATE ON work_ledger_configs
BEGIN
    SELECT RAISE(ABORT, 'work_ledger_configs are immutable');
END;

CREATE TRIGGER work_ledger_configs_no_delete
BEFORE DELETE ON work_ledger_configs
BEGIN
    SELECT RAISE(ABORT, 'work_ledger_configs are immutable');
END;

CREATE TRIGGER work_ledger_requests_no_update
BEFORE UPDATE ON work_ledger_requests
BEGIN
    SELECT RAISE(ABORT, 'work_ledger_requests are append-only');
END;

CREATE TRIGGER work_ledger_requests_no_delete
BEFORE DELETE ON work_ledger_requests
BEGIN
    SELECT RAISE(ABORT, 'work_ledger_requests are append-only');
END;
"""

_MIGRATION_TABLE_V4 = """
CREATE TABLE typed_state_migrations (
    project_id TEXT NOT NULL,
    migration_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 0),
    source_event_head_sha256 TEXT,
    source_snapshot_sha256 TEXT NOT NULL,
    target_snapshot_sha256 TEXT NOT NULL,
    from_schema_version TEXT NOT NULL,
    to_schema_version TEXT NOT NULL,
    receipt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, migration_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
) STRICT;
"""

_MIGRATION_TRIGGERS_V4 = """
CREATE TRIGGER typed_state_migrations_no_update
BEFORE UPDATE ON typed_state_migrations
BEGIN
    SELECT RAISE(ABORT, 'typed_state_migrations are append-only');
END;

CREATE TRIGGER typed_state_migrations_no_delete
BEFORE DELETE ON typed_state_migrations
BEGIN
    SELECT RAISE(ABORT, 'typed_state_migrations are append-only');
END;
"""

_MIGRATION_SCHEMA_CURRENT = _MIGRATION_TABLE_V4 + "\n" + _MIGRATION_TRIGGERS_V4

_MIGRATION_SCHEMA_V4 = (
    """
DROP TRIGGER typed_state_migrations_no_update;
DROP TRIGGER typed_state_migrations_no_delete;

ALTER TABLE typed_state_migrations RENAME TO typed_state_migrations_v3;
"""
    + _MIGRATION_TABLE_V4
    + """

INSERT INTO typed_state_migrations (
    project_id, migration_id, source_revision, source_event_head_sha256,
    source_snapshot_sha256, target_snapshot_sha256, from_schema_version,
    to_schema_version, receipt, created_at
)
SELECT project_id, migration_id, source_revision, source_event_head_sha256,
       source_snapshot_sha256, target_snapshot_sha256, from_schema_version,
       to_schema_version, receipt, created_at
FROM typed_state_migrations_v3;

DROP TABLE typed_state_migrations_v3;
"""
    + _MIGRATION_TRIGGERS_V4
)

_INITIAL_SCHEMA_V2 = _INITIAL_SCHEMA_V1 + "\n" + _MIGRATION_SCHEMA_V2
_INITIAL_SCHEMA_V3 = _INITIAL_SCHEMA_V2 + "\n" + _MIGRATION_SCHEMA_V3
_INITIAL_SCHEMA = (
    _INITIAL_SCHEMA_V1 + "\n" + _MIGRATION_SCHEMA_CURRENT + "\n" + _MIGRATION_SCHEMA_V3
)


def _schema_signature(
    connection: sqlite3.Connection,
) -> frozenset[tuple[str, str, str]]:
    return frozenset(
        (row[0], row[1], row[2])
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
        )
    )


@lru_cache(maxsize=1)
def _expected_schema_signature() -> frozenset[tuple[str, str, str]]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.executescript(_INITIAL_SCHEMA)
        return _schema_signature(connection)
    finally:
        connection.close()


@lru_cache(maxsize=1)
def _expected_schema_signature_v1() -> frozenset[tuple[str, str, str]]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.executescript(_INITIAL_SCHEMA_V1)
        return _schema_signature(connection)
    finally:
        connection.close()


@lru_cache(maxsize=1)
def _expected_schema_signature_v2() -> frozenset[tuple[str, str, str]]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.executescript(_INITIAL_SCHEMA_V2)
        return _schema_signature(connection)
    finally:
        connection.close()


@lru_cache(maxsize=1)
def _expected_schema_signature_v3() -> frozenset[tuple[str, str, str]]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        connection.executescript(_INITIAL_SCHEMA_V3)
        return _schema_signature(connection)
    finally:
        connection.close()


def _validate_schema_objects(connection: sqlite3.Connection) -> None:
    if _schema_signature(connection) != _expected_schema_signature():
        raise SQLiteStateIntegrityError("SQLite schema objects are missing or invalid")


def _validate_database_integrity(connection: sqlite3.Connection) -> None:
    results = [row[0] for row in connection.execute("PRAGMA quick_check")]
    if results != ["ok"]:
        raise SQLiteStateIntegrityError(
            "SQLite quick_check detected database corruption"
        )


def _read_validated_events(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    project_revision: int,
    last_sequence: int,
    last_event_sha256: str | None,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT project_id, sequence_no, event_id, revision_before, revision_after,
               previous_event_sha256, event_sha256, occurred_at, envelope
        FROM state_events
        WHERE project_id = ?
        ORDER BY sequence_no
        """,
        (project_id,),
    ).fetchall()
    events: list[dict[str, Any]] = []
    expected_sequence = 1
    previous_hash: str | None = None
    previous_revision_after: int | None = None
    known_event_ids: set[str] = set()
    for row in rows:
        event = _json_object(row[8], field="Event envelope")
        _validate_event(event)
        durable_fields = (
            event["project_id"],
            event["sequence_no"],
            event["event_id"],
            event["revision_before"],
            event["revision_after"],
            event["previous_event_sha256"],
            event["event_sha256"],
            event["occurred_at"],
        )
        if row[:8] != durable_fields:
            raise SQLiteStateIntegrityError("persisted Event column/envelope mismatch")
        if event["sequence_no"] != expected_sequence:
            raise SQLiteStateIntegrityError("persisted Event sequence has a gap")
        if event["previous_event_sha256"] != previous_hash:
            raise SQLiteStateIntegrityError("persisted Event hash chain is broken")
        if (
            previous_revision_after is not None
            and event["revision_before"] != previous_revision_after
        ):
            raise SQLiteStateIntegrityError("persisted Event revision chain is broken")
        supersedes_event_id = event["supersedes_event_id"]
        if (
            supersedes_event_id is not None
            and supersedes_event_id not in known_event_ids
        ):
            raise SQLiteStateIntegrityError(
                "persisted Event supersedes an unknown event"
            )
        known_event_ids.add(event["event_id"])
        expected_sequence += 1
        previous_hash = event["event_sha256"]
        previous_revision_after = event["revision_after"]
        events.append(event)

    if len(events) != last_sequence or previous_hash != last_event_sha256:
        raise SQLiteStateIntegrityError("persisted project/Event head mismatch")
    if events and events[-1]["revision_after"] != project_revision:
        raise SQLiteStateIntegrityError("persisted project/Event revision mismatch")
    return events


class SQLiteStateStore:
    """Zero-service local authority backed by one SQLite database file."""

    capability_manifest = StateStoreCapabilityManifest(
        schema_version="context.state-store-capabilities/v1alpha1",
        adapter_id="context.sqlite",
        adapter_version="0.1.0-alpha.1",
        authority_mode="local",
        operations=("create_project", "read_project", "read_events", "commit_event"),
        shared_authority=False,
        offline_write=True,
        unique_claim=False,
        multi_writer=True,
        lease_clock="none",
        artifact_scope="none",
        expected_revision=True,
        migration_source=False,
        migration_target=False,
    )

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        fault_hook: Callable[[str], None] | None = None,
    ):
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be a positive integer")
        if fault_hook is not None and not callable(fault_hook):
            raise ValueError("fault_hook must be callable")
        self.database_path = Path(database_path)
        self.busy_timeout_ms = busy_timeout_ms
        self._fault_hook = fault_hook

    @contextmanager
    def _connect(
        self,
        *,
        allow_pristine_unowned: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.database_path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            if application_id != SQLITE_APPLICATION_ID:
                pristine = (
                    application_id == 0
                    and connection.execute("PRAGMA user_version").fetchone()[0] == 0
                    and connection.execute("PRAGMA page_count").fetchone()[0] == 0
                    and not _schema_signature(connection)
                )
                if not (allow_pristine_unowned and pristine):
                    raise SQLiteStateStoreError("unclaimed SQLite database")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            connection.execute("PRAGMA synchronous = FULL")
            if str(journal_mode).lower() != "wal":
                raise SQLiteStateIntegrityError("SQLite WAL mode is unavailable")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise SQLiteStateIntegrityError(
                    "SQLite foreign key enforcement is unavailable"
                )
            if (
                connection.execute("PRAGMA busy_timeout").fetchone()[0]
                != self.busy_timeout_ms
            ):
                raise SQLiteStateIntegrityError("SQLite busy timeout was not applied")
            if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                raise SQLiteStateIntegrityError(
                    "SQLite FULL synchronous mode is unavailable"
                )
            yield connection
        except sqlite3.OperationalError as exc:
            primary_code = getattr(exc, "sqlite_errorcode", -1) & 0xFF
            if primary_code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                raise SQLiteStateBusy("SQLite lock timeout expired") from exc
            raise SQLiteStateIntegrityError("SQLite operation failed") from exc
        except sqlite3.DatabaseError as exc:
            raise SQLiteStateIntegrityError("SQLite database operation failed") from exc
        finally:
            if connection is not None:
                connection.close()

    def initialize(self) -> None:
        try:
            with self._connect(allow_pristine_unowned=True) as connection:
                application_id = connection.execute("PRAGMA application_id").fetchone()[
                    0
                ]
                schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
                if schema_version > SQLITE_SCHEMA_VERSION:
                    raise SQLiteStateStoreError(
                        f"database uses a newer schema version: {schema_version}"
                    )
                if application_id not in (0, SQLITE_APPLICATION_ID):
                    raise SQLiteStateStoreError(
                        "database application_id is not continuity"
                    )
                if application_id == SQLITE_APPLICATION_ID and schema_version == 0:
                    raise SQLiteStateStoreError(
                        "database has an incomplete continuity ownership marker"
                    )
                if schema_version == SQLITE_SCHEMA_VERSION:
                    if application_id != SQLITE_APPLICATION_ID:
                        raise SQLiteStateStoreError(
                            "database application_id is missing"
                        )
                    _validate_schema_objects(connection)
                    _validate_database_integrity(connection)
                    return

                if schema_version == 1:
                    if application_id != SQLITE_APPLICATION_ID:
                        raise SQLiteStateStoreError(
                            "database application_id is missing"
                        )
                    if _schema_signature(connection) != _expected_schema_signature_v1():
                        raise SQLiteStateIntegrityError(
                            "SQLite v1 schema objects are missing or invalid"
                        )
                    try:
                        connection.executescript(
                            "BEGIN IMMEDIATE;\n"
                            + _MIGRATION_SCHEMA_V2
                            + "\n"
                            + _MIGRATION_SCHEMA_V3
                            + "\n"
                            + _MIGRATION_SCHEMA_V4
                            + f"\nPRAGMA user_version = {SQLITE_SCHEMA_VERSION};"
                            + "\nCOMMIT;"
                        )
                        _validate_schema_objects(connection)
                        _validate_database_integrity(connection)
                        return
                    except BaseException:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK")
                        raise

                if schema_version == 2:
                    if application_id != SQLITE_APPLICATION_ID:
                        raise SQLiteStateStoreError(
                            "database application_id is missing"
                        )
                    if _schema_signature(connection) != _expected_schema_signature_v2():
                        raise SQLiteStateIntegrityError(
                            "SQLite v2 schema objects are missing or invalid"
                        )
                    try:
                        connection.executescript(
                            "BEGIN IMMEDIATE;\n"
                            + _MIGRATION_SCHEMA_V3
                            + "\n"
                            + _MIGRATION_SCHEMA_V4
                            + f"\nPRAGMA user_version = {SQLITE_SCHEMA_VERSION};"
                            + "\nCOMMIT;"
                        )
                        _validate_schema_objects(connection)
                        _validate_database_integrity(connection)
                        return
                    except BaseException:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK")
                        raise

                if schema_version == 3:
                    if application_id != SQLITE_APPLICATION_ID:
                        raise SQLiteStateStoreError(
                            "database application_id is missing"
                        )
                    if _schema_signature(connection) != _expected_schema_signature_v3():
                        raise SQLiteStateIntegrityError(
                            "SQLite v3 schema objects are missing or invalid"
                        )
                    try:
                        connection.executescript(
                            "BEGIN IMMEDIATE;\n"
                            + _MIGRATION_SCHEMA_V4
                            + f"\nPRAGMA user_version = {SQLITE_SCHEMA_VERSION};"
                            + "\nCOMMIT;"
                        )
                        _validate_schema_objects(connection)
                        _validate_database_integrity(connection)
                        return
                    except BaseException:
                        if connection.in_transaction:
                            connection.execute("ROLLBACK")
                        raise

                if _schema_signature(connection):
                    raise SQLiteStateStoreError(
                        "unclaimed SQLite database contains user schema objects"
                    )

                try:
                    connection.executescript(
                        "BEGIN IMMEDIATE;\n"
                        + _INITIAL_SCHEMA
                        + f"\nPRAGMA application_id = {SQLITE_APPLICATION_ID};"
                        + f"\nPRAGMA user_version = {SQLITE_SCHEMA_VERSION};"
                        + "\nCOMMIT;"
                    )
                    _validate_schema_objects(connection)
                    _validate_database_integrity(connection)
                except BaseException:
                    if connection.in_transaction:
                        connection.execute("ROLLBACK")
                    raise
        except SQLiteStateStoreError:
            raise
        except sqlite3.DatabaseError as exc:
            raise SQLiteStateStoreError("SQLite schema initialization failed") from exc

    def create_project(self, snapshot: dict[str, Any]) -> None:
        snapshot = copy.deepcopy(snapshot)
        _validate_snapshot(snapshot)
        project = snapshot["project"]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO projects (
                        project_id,
                        revision,
                        last_sequence,
                        last_event_sha256,
                        snapshot,
                        snapshot_sha256,
                        created_at,
                        updated_at
                    ) VALUES (?, ?, 0, NULL, ?, ?, datetime('now'), datetime('now'))
                    """,
                    (
                        project["project_id"],
                        project["revision"],
                        _json_text(snapshot),
                        _snapshot_sha256(snapshot),
                    ),
                )
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise SQLiteStateConflict(
                    f"project already exists: {project['project_id']}"
                ) from exc
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def read_project(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                """
                SELECT revision, last_sequence, last_event_sha256,
                       snapshot, snapshot_sha256
                FROM projects
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if row is None:
                connection.execute("COMMIT")
                raise SQLiteStateNotFound(f"project does not exist: {project_id}")
            snapshot = _json_object(row[3], field="snapshot")
            _validate_snapshot(snapshot)
            if snapshot["project"]["revision"] != row[0]:
                raise SQLiteStateIntegrityError("persisted row revision mismatch")
            if _snapshot_sha256(snapshot) != row[4]:
                raise SQLiteStateIntegrityError("persisted snapshot hash mismatch")
            _read_validated_events(
                connection,
                project_id=project_id,
                project_revision=row[0],
                last_sequence=row[1],
                last_event_sha256=row[2],
            )
            connection.execute("COMMIT")
            return snapshot

    def read_events(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            head = connection.execute(
                """
                SELECT revision, last_sequence, last_event_sha256
                FROM projects
                WHERE project_id = ?
                """,
                (project_id,),
            ).fetchone()
            if head is None:
                connection.execute("COMMIT")
                return []
            events = _read_validated_events(
                connection,
                project_id=project_id,
                project_revision=head[0],
                last_sequence=head[1],
                last_event_sha256=head[2],
            )
            connection.execute("COMMIT")
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
            raise SQLiteStateIntegrityError("event project_id mismatch")
        if expected_snapshot["project"]["project_id"] != project_id:
            raise SQLiteStateIntegrityError("snapshot project_id mismatch")
        if event["revision_before"] != expected_revision:
            raise SQLiteStateIntegrityError("event revision_before mismatch")
        if event["project_after"] != expected_snapshot["project"]:
            raise SQLiteStateIntegrityError("event project_after mismatch")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT revision, last_sequence, last_event_sha256,
                           snapshot, snapshot_sha256
                    FROM projects
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()
                if row is None:
                    raise SQLiteStateNotFound(f"project does not exist: {project_id}")
                (
                    actual_revision,
                    last_sequence,
                    previous_hash,
                    snapshot_text,
                    stored_hash,
                ) = row
                if actual_revision != expected_revision:
                    raise SQLiteStateConflict(
                        f"expected revision {expected_revision}, actual revision {actual_revision}"
                    )
                current_snapshot = _json_object(snapshot_text, field="snapshot")
                _validate_snapshot(current_snapshot)
                if current_snapshot["project"]["revision"] != actual_revision:
                    raise SQLiteStateIntegrityError("persisted row revision mismatch")
                if _snapshot_sha256(current_snapshot) != stored_hash:
                    raise SQLiteStateIntegrityError("persisted snapshot hash mismatch")

                existing_events = _read_validated_events(
                    connection,
                    project_id=project_id,
                    project_revision=actual_revision,
                    last_sequence=last_sequence,
                    last_event_sha256=previous_hash,
                )

                expected_sequence = last_sequence + 1
                if event["sequence_no"] != expected_sequence:
                    raise SQLiteStateConflict(
                        f"expected event sequence {expected_sequence}, got {event['sequence_no']}"
                    )
                if event["previous_event_sha256"] != previous_hash:
                    raise SQLiteStateConflict(
                        "event hash chain does not match current head"
                    )
                duplicate = connection.execute(
                    "SELECT 1 FROM state_events WHERE project_id = ? AND event_id = ?",
                    (project_id, event["event_id"]),
                ).fetchone()
                if duplicate is not None:
                    raise SQLiteStateConflict("event identity already exists")

                try:
                    restored = replay_state_events(
                        current_snapshot,
                        [event],
                        starting_sequence_no=expected_sequence,
                        previous_event_sha256=previous_hash,
                        prior_events=(
                            existing_events
                            if event["supersedes_event_id"] is not None
                            else None
                        ),
                    )
                except (StateEventError, TypedStateError) as exc:
                    raise SQLiteStateIntegrityError(
                        "state Event replay failed"
                    ) from exc
                if canonical_state_bytes(restored) != canonical_state_bytes(
                    expected_snapshot
                ):
                    raise SQLiteStateIntegrityError(
                        "event replay does not produce expected snapshot"
                    )

                try:
                    connection.execute(
                        """
                        INSERT INTO state_events (
                            project_id,
                            sequence_no,
                            event_id,
                            revision_before,
                            revision_after,
                            previous_event_sha256,
                            event_sha256,
                            occurred_at,
                            envelope
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                            _json_text(event),
                        ),
                    )
                    if self._fault_hook is not None:
                        self._fault_hook("after_event_insert")
                except sqlite3.IntegrityError as exc:
                    raise SQLiteStateConflict(
                        "event identity or append position conflict"
                    ) from exc
                updated = connection.execute(
                    """
                    UPDATE projects
                    SET revision = ?, last_sequence = ?, last_event_sha256 = ?,
                        snapshot = ?, snapshot_sha256 = ?, updated_at = datetime('now')
                    WHERE project_id = ? AND revision = ?
                    """,
                    (
                        event["revision_after"],
                        event["sequence_no"],
                        event["event_sha256"],
                        _json_text(expected_snapshot),
                        _snapshot_sha256(expected_snapshot),
                        project_id,
                        expected_revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise SQLiteStateConflict("project revision changed during commit")
                connection.execute("COMMIT")
                if self._fault_hook is not None:
                    self._fault_hook("after_commit")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _work_ledger_from_rows(
        snapshot: dict[str, Any],
        *,
        max_ttl_ms: int,
        response_rows: list[tuple[str, str]],
    ) -> WorkLedger:
        ledger = WorkLedger(
            project_id=snapshot["project"]["project_id"],
            project_revision=snapshot["project"]["revision"],
            works=snapshot["works"],
            max_ttl_ms=max_ttl_ms,
        )
        ledger._claims = {
            claim["claim_id"]: copy.deepcopy(claim) for claim in snapshot["claims"]
        }
        ledger._effects = {
            effect["effect_id"]: copy.deepcopy(effect) for effect in snapshot["effects"]
        }
        transitions: list[dict[str, Any]] = []
        for response_text, response_sha256 in response_rows:
            response = _json_object(response_text, field="Work Ledger response")
            if _json_sha256(response) != response_sha256:
                raise SQLiteStateIntegrityError("Work Ledger response digest mismatch")
            transition = response.get("transition")
            if not isinstance(transition, dict):
                raise SQLiteStateIntegrityError("Work Ledger transition is missing")
            transitions.append(copy.deepcopy(transition))
        ledger._transitions = transitions
        ledger._next_lease_epoch = max(
            (claim["lease_epoch"] for claim in snapshot["claims"]),
            default=0,
        )
        return ledger

    def initialize_work_ledger(
        self,
        *,
        project_id: str,
        project_revision: int,
        works: list[dict[str, Any]],
        max_ttl_ms: int,
    ) -> None:
        """Bind local claim coordination to an existing canonical v6 project."""
        if type(max_ttl_ms) is not int or max_ttl_ms <= 0:
            raise SQLiteStateIntegrityError("max_ttl_ms is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT revision, snapshot, snapshot_sha256 FROM projects "
                    "WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if row is None:
                    raise SQLiteStateNotFound(f"project does not exist: {project_id}")
                snapshot = _json_object(row[1], field="snapshot")
                _validate_snapshot(snapshot)
                if snapshot.get("schema_version") != V6_SCHEMA_VERSION:
                    raise SQLiteStateIntegrityError(
                        "Work Ledger requires a canonical typed-state v6 project"
                    )
                if _snapshot_sha256(snapshot) != row[2]:
                    raise SQLiteStateIntegrityError("persisted snapshot hash mismatch")
                if (
                    row[0] != project_revision
                    or snapshot["project"]["revision"] != row[0]
                ):
                    raise SQLiteStateConflict("Work Ledger project revision mismatch")
                if works != snapshot["works"]:
                    raise SQLiteStateIntegrityError(
                        "Work Ledger Work projection mismatch"
                    )
                existing = connection.execute(
                    "SELECT max_ttl_ms, initialized_snapshot_sha256 "
                    "FROM work_ledger_configs WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                expected = (max_ttl_ms, row[2])
                if existing is None:
                    connection.execute(
                        "INSERT INTO work_ledger_configs "
                        "(project_id, max_ttl_ms, initialized_snapshot_sha256, created_at) "
                        "VALUES (?, ?, ?, datetime('now'))",
                        (project_id, max_ttl_ms, row[2]),
                    )
                elif existing != expected:
                    raise SQLiteStateIntegrityError(
                        "Work Ledger configuration is inconsistent"
                    )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def _read_work_ledger_on_connection(
        self,
        connection: sqlite3.Connection,
        project_id: str,
    ) -> tuple[dict[str, Any], WorkLedger]:
        row = connection.execute(
            "SELECT p.revision, p.snapshot, p.snapshot_sha256, c.max_ttl_ms "
            "FROM projects AS p JOIN work_ledger_configs AS c "
            "ON c.project_id = p.project_id WHERE p.project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise SQLiteStateNotFound(
                f"Work Ledger project does not exist: {project_id}"
            )
        snapshot = _json_object(row[1], field="snapshot")
        _validate_snapshot(snapshot)
        if snapshot.get("schema_version") != V6_SCHEMA_VERSION:
            raise SQLiteStateIntegrityError("Work Ledger canonical snapshot is not v6")
        if (
            snapshot["project"]["revision"] != row[0]
            or _snapshot_sha256(snapshot) != row[2]
        ):
            raise SQLiteStateIntegrityError(
                "Work Ledger canonical snapshot is inconsistent"
            )
        responses = connection.execute(
            "SELECT response, response_sha256 FROM work_ledger_requests "
            "WHERE project_id = ? ORDER BY committed_revision",
            (project_id,),
        ).fetchall()
        return snapshot, self._work_ledger_from_rows(
            snapshot,
            max_ttl_ms=row[3],
            response_rows=responses,
        )

    def read_work_ledger(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            _, ledger = self._read_work_ledger_on_connection(connection, project_id)
            result = ledger.snapshot()
            connection.execute("COMMIT")
            return result

    def read_work_ledger_receipt(
        self,
        project_id: str,
        request_namespace: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT response, response_sha256 FROM work_ledger_requests "
                "WHERE project_id = ? AND request_namespace = ? AND request_id = ?",
                (project_id, request_namespace, request_id),
            ).fetchone()
            if row is None:
                return None
            response = _json_object(row[0], field="Work Ledger response")
            if _json_sha256(response) != row[1]:
                raise SQLiteStateIntegrityError("Work Ledger response digest mismatch")
            return response

    def execute_work_ledger(
        self,
        *,
        project_id: str,
        operation: str,
        request_id: str,
        arguments: dict[str, Any],
        request_payload: dict[str, Any] | None = None,
        request_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Commit one Work transition, Event, snapshot, and receipt atomically."""
        namespace = request_namespace or operation
        if not all(
            isinstance(value, str) and value
            for value in (project_id, operation, request_id, namespace)
        ):
            raise SQLiteStateIntegrityError("Work Ledger request identity is invalid")
        if not isinstance(arguments, dict):
            raise SQLiteStateIntegrityError("Work Ledger arguments are invalid")
        payload = arguments if request_payload is None else request_payload
        if not isinstance(payload, dict):
            raise SQLiteStateIntegrityError("Work Ledger request payload is invalid")
        request_sha256 = _json_sha256(
            {
                "project_id": project_id,
                "request_namespace": namespace,
                "request_id": request_id,
                "payload": payload,
            }
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT request_sha256, response, response_sha256 "
                    "FROM work_ledger_requests WHERE project_id = ? "
                    "AND request_namespace = ? AND request_id = ?",
                    (project_id, namespace, request_id),
                ).fetchone()
                if existing is not None:
                    if existing[0] != request_sha256:
                        raise SQLiteStateConflict(
                            "request_id was reused for a different payload"
                        )
                    response = _json_object(existing[1], field="Work Ledger response")
                    if _json_sha256(response) != existing[2]:
                        raise SQLiteStateIntegrityError(
                            "Work Ledger response digest mismatch"
                        )
                    connection.execute("COMMIT")
                    return response

                current, ledger = self._read_work_ledger_on_connection(
                    connection, project_id
                )
                try:
                    response = getattr(ledger, operation)(**copy.deepcopy(arguments))
                except (AttributeError, ClaimLifecycleError) as exc:
                    reason = (
                        exc.code
                        if isinstance(exc, ClaimLifecycleError)
                        else "unsupported_operation"
                    )
                    raise SQLiteStateConflict(reason) from exc
                ledger_snapshot = ledger.snapshot()
                candidate = copy.deepcopy(current)
                candidate["works"] = [
                    {key: value for key, value in work.items() if key != "identity_key"}
                    for work in ledger_snapshot["works"]
                ]
                candidate["claims"] = ledger_snapshot["claims"]
                candidate["effects"] = ledger_snapshot["effects"]
                project = candidate["project"]
                project["revision"] = ledger_snapshot["project_revision"]
                project["updated_at"] = response["transition"]["observed_at"]
                active_ids = [
                    work["work_id"]
                    for work in candidate["works"]
                    if work["status"] == "active"
                ]
                project["active_work_ids"] = active_ids
                if project["primary_work_id"] not in active_ids:
                    project["primary_work_id"] = active_ids[0] if active_ids else None
                project["effect_high_watermark"] = max(
                    (
                        effect["sequence_no"]
                        for effect in candidate["effects"]
                        if effect["status"] in {"succeeded", "failed", "compensated"}
                    ),
                    default=0,
                )
                _validate_snapshot(candidate)
                changes: list[dict[str, Any]] = []
                for collection, id_field in (
                    ("works", "work_id"),
                    ("claims", "claim_id"),
                    ("effects", "effect_id"),
                ):
                    before = {item[id_field]: item for item in current[collection]}
                    for item in candidate[collection]:
                        if before.get(item[id_field]) != item:
                            changes.append(
                                {
                                    "collection": collection,
                                    "object_id": item[id_field],
                                    "value": item,
                                }
                            )
                transition = response["transition"]
                head = connection.execute(
                    "SELECT last_sequence, last_event_sha256 FROM projects "
                    "WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                event = build_state_event(
                    event_id=f"event-{transition['transition_sha256'][:32]}",
                    event_type="state-transition",
                    project_id=project_id,
                    sequence_no=head[0] + 1,
                    revision_before=current["project"]["revision"],
                    occurred_at=transition["observed_at"],
                    actor_ref=transition["actor_ref"],
                    causation_ref=f"{namespace}:{request_id}",
                    correlation_ref="work-ledger",
                    previous_event_sha256=head[1],
                    supersedes_event_id=None,
                    changes=changes,
                    project_after=project,
                    schema_version="context.state-event/v4alpha1",
                )
                restored = replay_state_events(
                    current,
                    [event],
                    starting_sequence_no=head[0] + 1,
                    previous_event_sha256=head[1],
                )
                if canonical_state_bytes(restored) != canonical_state_bytes(candidate):
                    raise SQLiteStateIntegrityError(
                        "Work Ledger Event does not produce the candidate snapshot"
                    )
                connection.execute(
                    "INSERT INTO state_events "
                    "(project_id, sequence_no, event_id, revision_before, revision_after, "
                    "previous_event_sha256, event_sha256, occurred_at, envelope) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        event["sequence_no"],
                        event["event_id"],
                        event["revision_before"],
                        event["revision_after"],
                        event["previous_event_sha256"],
                        event["event_sha256"],
                        event["occurred_at"],
                        _json_text(event),
                    ),
                )
                updated = connection.execute(
                    "UPDATE projects SET revision = ?, last_sequence = ?, "
                    "last_event_sha256 = ?, snapshot = ?, snapshot_sha256 = ?, "
                    "updated_at = datetime('now') WHERE project_id = ? AND revision = ?",
                    (
                        project["revision"],
                        event["sequence_no"],
                        event["event_sha256"],
                        _json_text(candidate),
                        _snapshot_sha256(candidate),
                        project_id,
                        current["project"]["revision"],
                    ),
                )
                if updated.rowcount != 1:
                    raise SQLiteStateConflict("project revision changed")
                connection.execute(
                    "INSERT INTO work_ledger_requests "
                    "(project_id, request_namespace, request_id, operation, "
                    "request_sha256, response, response_sha256, committed_revision, "
                    "committed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
                    (
                        project_id,
                        namespace,
                        request_id,
                        operation,
                        request_sha256,
                        _json_text(response),
                        _json_sha256(response),
                        project["revision"],
                    ),
                )
                if self._fault_hook is not None:
                    self._fault_hook("before_work_ledger_commit")
                connection.execute("COMMIT")
                return response
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    def read_migration_receipt(
        self, project_id: str, migration_id: str
    ) -> dict[str, Any] | None:
        """Return one immutable typed-state migration receipt when present."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT source_revision, source_event_head_sha256,
                       source_snapshot_sha256, target_snapshot_sha256,
                       from_schema_version, to_schema_version, receipt
                FROM typed_state_migrations
                WHERE project_id = ? AND migration_id = ?
                """,
                (project_id, migration_id),
            ).fetchone()
            if row is None:
                return None
            receipt = _json_object(row[6], field="migration receipt")
            durable = (
                receipt.get("source_revision"),
                _migration_event_head_sha256(receipt),
                receipt.get("source_snapshot_sha256"),
                receipt.get("target_snapshot_sha256"),
                receipt.get("from_schema_version"),
                receipt.get("to_schema_version"),
            )
            if durable != row[:6]:
                raise SQLiteStateIntegrityError(
                    "persisted migration receipt column/envelope mismatch"
                )
            return receipt

    def migrate_project(
        self,
        *,
        project_id: str,
        expected_revision: int,
        expected_event_head_sha256: str | None,
        target_snapshot: dict[str, Any],
        migration_receipt: dict[str, Any],
        expected_registry_digest: str | None = None,
        expected_authorization_ref: str | None = None,
    ) -> dict[str, Any]:
        """Atomically advance a stored snapshot across a versioned schema boundary."""
        target_snapshot = copy.deepcopy(target_snapshot)
        migration_receipt = copy.deepcopy(migration_receipt)
        _validate_snapshot(target_snapshot)
        if target_snapshot["project"]["project_id"] != project_id:
            raise SQLiteStateIntegrityError("migration target project_id mismatch")
        if not isinstance(expected_event_head_sha256, (str, type(None))):
            raise SQLiteStateIntegrityError("migration event head is invalid")
        if isinstance(expected_event_head_sha256, str) and not re.fullmatch(
            r"[0-9a-f]{64}", expected_event_head_sha256
        ):
            raise SQLiteStateIntegrityError("migration event head is invalid")

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT receipt FROM typed_state_migrations
                    WHERE project_id = ? AND migration_id = ?
                    """,
                    (project_id, migration_receipt.get("migration_id")),
                ).fetchone()
                if existing is not None:
                    persisted = _json_object(existing[0], field="migration receipt")
                    if persisted != migration_receipt:
                        raise SQLiteStateConflict(
                            "migration identity conflicts with durable receipt"
                        )
                    row = connection.execute(
                        """
                        SELECT revision, last_sequence, last_event_sha256,
                               snapshot, snapshot_sha256
                        FROM projects WHERE project_id = ?
                        """,
                        (project_id,),
                    ).fetchone()
                    if row is None:
                        raise SQLiteStateNotFound(
                            f"project does not exist: {project_id}"
                        )
                    if (
                        _snapshot_sha256(target_snapshot) != row[4]
                        or _json_object(row[3], field="snapshot") != target_snapshot
                    ):
                        raise SQLiteStateConflict(
                            "migration receipt target does not match durable snapshot"
                        )
                    if persisted.get("schema_version") in {
                        DURABLE_MIGRATION_RECEIPT_SCHEMA_VERSION,
                        SHARED_MIGRATION_RECEIPT_SCHEMA_VERSION,
                    }:
                        event_head = persisted.get("source_event_head")
                        if (
                            row[0] != expected_revision
                            or row[2] != expected_event_head_sha256
                            or persisted.get("source_revision") != expected_revision
                            or event_head
                            != {"sequence_no": row[1], "event_sha256": row[2]}
                        ):
                            raise SQLiteStateConflict(
                                "migration replay authority does not match durable state"
                            )
                        if (
                            persisted.get("registry_digest") != expected_registry_digest
                            or persisted.get("authorization_ref")
                            != expected_authorization_ref
                        ):
                            raise SQLiteStateIntegrityError(
                                "migration replay authorization does not match durable receipt"
                            )
                    connection.execute("COMMIT")
                    return persisted

                row = connection.execute(
                    """
                    SELECT revision, last_sequence, last_event_sha256, snapshot, snapshot_sha256
                    FROM projects WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()
                if row is None:
                    raise SQLiteStateNotFound(f"project does not exist: {project_id}")
                (
                    actual_revision,
                    last_sequence,
                    actual_head,
                    source_text,
                    source_hash,
                ) = row
                if actual_revision != expected_revision:
                    raise SQLiteStateConflict(
                        f"expected revision {expected_revision}, actual revision {actual_revision}"
                    )
                if target_snapshot["project"]["revision"] != actual_revision:
                    raise SQLiteStateIntegrityError(
                        "migration target revision mismatch"
                    )
                if actual_head != expected_event_head_sha256:
                    raise SQLiteStateConflict(
                        "migration event head does not match current head"
                    )
                source_snapshot = _json_object(source_text, field="snapshot")
                _validate_snapshot(source_snapshot)
                if _snapshot_sha256(source_snapshot) != source_hash:
                    raise SQLiteStateIntegrityError(
                        "persisted source snapshot hash mismatch"
                    )
                receipt_schema = migration_receipt.get("schema_version")
                migration_event_head_sha256: str | None
                try:
                    if receipt_schema == DURABLE_MIGRATION_RECEIPT_SCHEMA_VERSION:
                        if (
                            migration_receipt.get("from_schema_version")
                            == V5_SCHEMA_VERSION
                            and migration_receipt.get("to_schema_version")
                            != V5_SCHEMA_VERSION
                        ):
                            upgrade_row = connection.execute(
                                """
                                SELECT receipt
                                FROM typed_state_migrations
                                WHERE project_id = ?
                                  AND from_schema_version = ?
                                  AND to_schema_version = ?
                                """,
                                (
                                    project_id,
                                    "context.typed-state/v4alpha1",
                                    V5_SCHEMA_VERSION,
                                ),
                            ).fetchone()
                            if upgrade_row is not None:
                                upgrade_receipt = _json_object(
                                    upgrade_row[0], field="migration receipt"
                                )
                                if source_hash != upgrade_receipt.get(
                                    "target_snapshot_sha256"
                                ) or upgrade_receipt.get("source_event_head") != {
                                    "sequence_no": last_sequence,
                                    "event_sha256": actual_head,
                                }:
                                    raise SQLiteStateIntegrityError(
                                        "rollback source advanced beyond the upgrade boundary"
                                    )
                        validate_durable_state_migration_receipt(
                            migration_receipt,
                            source=source_snapshot,
                            target=target_snapshot,
                            expected_source_event_head={
                                "sequence_no": last_sequence,
                                "event_sha256": actual_head,
                            },
                            expected_registry_digest=expected_registry_digest,
                            expected_authorization_ref=expected_authorization_ref,
                        )
                        migration_event_head_sha256 = _migration_event_head_sha256(
                            migration_receipt
                        )
                    elif receipt_schema == SHARED_MIGRATION_RECEIPT_SCHEMA_VERSION:
                        if (
                            migration_receipt.get("from_schema_version")
                            == V6_SCHEMA_VERSION
                            and migration_receipt.get("to_schema_version")
                            != V6_SCHEMA_VERSION
                        ):
                            upgrade_row = connection.execute(
                                """
                                SELECT receipt
                                FROM typed_state_migrations
                                WHERE project_id = ?
                                  AND from_schema_version = ?
                                  AND to_schema_version = ?
                                """,
                                (project_id, V5_SCHEMA_VERSION, V6_SCHEMA_VERSION),
                            ).fetchone()
                            if upgrade_row is not None:
                                upgrade_receipt = _json_object(
                                    upgrade_row[0], field="migration receipt"
                                )
                                if source_hash != upgrade_receipt.get(
                                    "target_snapshot_sha256"
                                ) or upgrade_receipt.get("source_event_head") != {
                                    "sequence_no": last_sequence,
                                    "event_sha256": actual_head,
                                }:
                                    raise SQLiteStateIntegrityError(
                                        "rollback source advanced beyond the upgrade boundary"
                                    )
                        validate_typed_state_v5_to_v6_migration_receipt(
                            migration_receipt,
                            source=source_snapshot,
                            target=target_snapshot,
                            expected_source_event_head={
                                "sequence_no": last_sequence,
                                "event_sha256": actual_head,
                            },
                            expected_registry_digest=expected_registry_digest,
                            expected_authorization_ref=expected_authorization_ref,
                        )
                        migration_event_head_sha256 = _migration_event_head_sha256(
                            migration_receipt
                        )
                    elif receipt_schema == IDEA_REVIEW_MIGRATION_RECEIPT_SCHEMA_VERSION:
                        validate_typed_state_v3_to_v4_migration_receipt(
                            migration_receipt,
                            source=source_snapshot,
                            target=target_snapshot,
                        )
                        migration_event_head_sha256 = _migration_event_head_sha256(
                            migration_receipt
                        )
                    else:
                        raise ValueError(
                            "typed-state migration receipt version is unsupported"
                        )
                except ValueError as exc:
                    raise SQLiteStateIntegrityError(
                        "typed-state migration receipt is invalid"
                    ) from exc
                if migration_receipt["project_id"] != project_id:
                    raise SQLiteStateIntegrityError(
                        "migration receipt project_id mismatch"
                    )
                if migration_event_head_sha256 != actual_head:
                    raise SQLiteStateIntegrityError(
                        "migration receipt event head mismatch"
                    )

                try:
                    connection.execute(
                        """
                        INSERT INTO typed_state_migrations (
                            project_id, migration_id, source_revision,
                            source_event_head_sha256, source_snapshot_sha256,
                            target_snapshot_sha256, from_schema_version,
                            to_schema_version, receipt, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        """,
                        (
                            project_id,
                            migration_receipt["migration_id"],
                            migration_receipt["source_revision"],
                            migration_event_head_sha256,
                            migration_receipt["source_snapshot_sha256"],
                            migration_receipt["target_snapshot_sha256"],
                            migration_receipt["from_schema_version"],
                            migration_receipt["to_schema_version"],
                            _json_text(migration_receipt),
                        ),
                    )
                    if self._fault_hook is not None:
                        self._fault_hook("after_migration_receipt_insert")
                except sqlite3.IntegrityError as exc:
                    raise SQLiteStateConflict(
                        "migration receipt identity conflict"
                    ) from exc
                updated = connection.execute(
                    """
                    UPDATE projects
                    SET snapshot = ?, snapshot_sha256 = ?, updated_at = datetime('now')
                    WHERE project_id = ? AND revision = ? AND last_event_sha256 IS ?
                    """,
                    (
                        _json_text(target_snapshot),
                        _snapshot_sha256(target_snapshot),
                        project_id,
                        expected_revision,
                        expected_event_head_sha256,
                    ),
                )
                if updated.rowcount != 1:
                    raise SQLiteStateConflict("project changed during migration")
                if self._fault_hook is not None:
                    self._fault_hook("after_migration_snapshot_update")
                connection.execute("COMMIT")
                return migration_receipt
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
