"""SQLite-backed append-only journal for local durable operations."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any

from .durable_operation import (
    DurableOperationError,
    validate_durable_operation,
    validate_durable_operation_chain,
    validate_durable_operation_transition,
)

STORE_SCHEMA_VERSION = 1
_APPEND_ONLY_TRIGGERS = {
    "durable_operation_heads_identity_no_update",
    "durable_operation_records_no_delete",
    "durable_operation_records_no_update",
}
_STORE_SCHEMA_SQL = """
CREATE TABLE durable_operation_metadata (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    schema_version INTEGER NOT NULL,
    schema_fingerprint TEXT NOT NULL CHECK (length(schema_fingerprint) = 64)
);
CREATE TABLE durable_operation_records (
    operation_id TEXT NOT NULL,
    record_revision INTEGER NOT NULL CHECK (record_revision >= 0),
    previous_record_sha256 TEXT,
    record_sha256 TEXT NOT NULL UNIQUE,
    record_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (operation_id, record_revision)
);
CREATE TABLE durable_operation_heads (
    operation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    effect_key TEXT NOT NULL,
    record_revision INTEGER NOT NULL,
    record_sha256 TEXT NOT NULL,
    UNIQUE (project_id, effect_key),
    FOREIGN KEY (operation_id, record_revision)
        REFERENCES durable_operation_records(operation_id, record_revision),
    FOREIGN KEY (record_sha256)
        REFERENCES durable_operation_records(record_sha256)
);
CREATE TRIGGER durable_operation_records_no_update
BEFORE UPDATE ON durable_operation_records
BEGIN
    SELECT RAISE(ABORT, 'durable operation history is append-only');
END;
CREATE TRIGGER durable_operation_records_no_delete
BEFORE DELETE ON durable_operation_records
BEGIN
    SELECT RAISE(ABORT, 'durable operation history is append-only');
END;
CREATE TRIGGER durable_operation_heads_identity_no_update
BEFORE UPDATE OF operation_id, project_id, effect_key ON durable_operation_heads
BEGIN
    SELECT RAISE(ABORT, 'durable operation head identity is immutable');
END;
"""


class DurableOperationStoreError(RuntimeError):
    """Base error for the local durable operation journal."""


class DurableOperationStoreConflict(DurableOperationStoreError):
    """Raised when an operation identity or expected head is stale."""


class DurableOperationStoreIntegrityError(DurableOperationStoreError):
    """Raised when persisted operation history is malformed or changed."""


class DurableOperationStoreNotFound(DurableOperationStoreError):
    """Raised when a requested operation is absent."""


def _json_text(document: dict[str, Any]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _strict_json(payload: str) -> dict[str, Any]:
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    try:
        document = json.loads(payload, object_pairs_hook=reject_duplicates)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DurableOperationStoreIntegrityError(
            "durable operation record is not strict JSON"
        ) from exc
    if not isinstance(document, dict):
        raise DurableOperationStoreIntegrityError(
            "durable operation record must be an object"
        )
    return document


def _normalized_schema_sql(value: str) -> str:
    return " ".join(value.split())


def _schema_contract(connection: sqlite3.Connection) -> tuple[str, dict[str, str]]:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE sql IS NOT NULL
          AND type IN ('table', 'index', 'trigger')
          AND (
              name GLOB 'durable_operation_*'
              OR tbl_name GLOB 'durable_operation_*'
          )
        ORDER BY type, name
        """
    ).fetchall()
    objects = [
        {
            "type": row["type"],
            "name": row["name"],
            "table": row["tbl_name"],
            "sql": _normalized_schema_sql(row["sql"]),
        }
        for row in rows
    ]
    payload = json.dumps(
        objects,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    triggers = {
        item["name"]: item["sql"]
        for item in objects
        if item["type"] == "trigger"
    }
    return hashlib.sha256(payload).hexdigest(), triggers


@lru_cache(maxsize=1)
def _expected_schema_contract() -> tuple[str, dict[str, str]]:
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(_STORE_SCHEMA_SQL)
        fingerprint, triggers = _schema_contract(connection)
    return fingerprint, triggers


class SQLiteDurableOperationStore:
    """Zero-service operation journal with SQLite CAS and append-only history."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 5000,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        if str(database_path) == ":memory:":
            raise ValueError("durable operation store requires a filesystem path")
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be a positive integer")
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable")
        self.busy_timeout_ms = busy_timeout_ms
        self.fault_hook = fault_hook
        self._closed = False

    def _fault(self, point: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point)

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise DurableOperationStoreError("durable operation store is closed")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _verify_quick_check(connection: sqlite3.Connection) -> None:
        try:
            rows = connection.execute("PRAGMA quick_check").fetchall()
        except sqlite3.DatabaseError as exc:
            raise DurableOperationStoreIntegrityError(
                "SQLite quick check failed"
            ) from exc
        if len(rows) != 1 or rows[0][0] != "ok":
            raise DurableOperationStoreIntegrityError("SQLite quick check failed")

    @staticmethod
    def _verify_existing_schema(connection: sqlite3.Connection) -> None:
        SQLiteDurableOperationStore._verify_quick_check(connection)
        expected_fingerprint, expected_triggers = _expected_schema_contract()
        try:
            actual_fingerprint, actual_triggers = _schema_contract(connection)
        except sqlite3.DatabaseError as exc:
            raise DurableOperationStoreIntegrityError(
                "durable operation schema fingerprint could not be read"
            ) from exc
        if (
            set(actual_triggers) != _APPEND_ONLY_TRIGGERS
            or actual_triggers != expected_triggers
        ):
            raise DurableOperationStoreIntegrityError(
                "durable operation append-only trigger is missing or changed"
            )
        if actual_fingerprint != expected_fingerprint:
            raise DurableOperationStoreIntegrityError(
                "durable operation schema fingerprint mismatch"
            )
        try:
            row = connection.execute(
                """
                SELECT schema_version, schema_fingerprint
                FROM durable_operation_metadata WHERE singleton = 1
                """
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise DurableOperationStoreIntegrityError(
                "durable operation schema fingerprint metadata is invalid"
            ) from exc
        if row is None:
            raise DurableOperationStoreIntegrityError(
                "durable operation store metadata is missing"
            )
        if row["schema_version"] != STORE_SCHEMA_VERSION:
            raise DurableOperationStoreIntegrityError(
                "unsupported durable operation store schema"
            )
        if row["schema_fingerprint"] != expected_fingerprint:
            raise DurableOperationStoreIntegrityError(
                "durable operation schema fingerprint metadata mismatch"
            )

    def initialize(self) -> None:
        """Create or verify the durable journal schema."""
        with closing(self._connect()) as connection:
            existing = connection.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE name GLOB 'durable_operation_*'
                LIMIT 1
                """
            ).fetchone()
            if existing is not None:
                self._verify_existing_schema(connection)
                return
            connection.executescript("BEGIN IMMEDIATE;\n" + _STORE_SCHEMA_SQL)
            self._fault("after-schema-ddl")
            self._verify_quick_check(connection)
            expected_fingerprint, expected_triggers = _expected_schema_contract()
            actual_fingerprint, actual_triggers = _schema_contract(connection)
            if actual_triggers != expected_triggers:
                raise DurableOperationStoreIntegrityError(
                    "durable operation append-only trigger is missing or changed"
                )
            if actual_fingerprint != expected_fingerprint:
                raise DurableOperationStoreIntegrityError(
                    "durable operation schema fingerprint mismatch"
                )
            connection.execute(
                """
                INSERT INTO durable_operation_metadata(
                    singleton, schema_version, schema_fingerprint
                ) VALUES (1, ?, ?)
                """,
                (STORE_SCHEMA_VERSION, expected_fingerprint),
            )
            connection.commit()

    @staticmethod
    def _document_from_row(row: sqlite3.Row) -> dict[str, Any]:
        document = _strict_json(row["record_json"])
        try:
            normalized = validate_durable_operation(document)
        except DurableOperationError as exc:
            raise DurableOperationStoreIntegrityError(
                "persisted durable operation is invalid"
            ) from exc
        if (
            normalized["operation_id"] != row["operation_id"]
            or normalized["record_revision"] != row["record_revision"]
            or normalized["previous_record_sha256"]
            != row["previous_record_sha256"]
            or normalized["record_sha256"] != row["record_sha256"]
            or normalized["updated_at"] != row["observed_at"]
            or _json_text(normalized) != row["record_json"]
        ):
            raise DurableOperationStoreIntegrityError(
                "persisted durable operation columns do not match its record"
            )
        return normalized

    @classmethod
    def _verify_head_identities(
        cls,
        connection: sqlite3.Connection,
        *,
        operation_id: str | None = None,
    ) -> None:
        where = "" if operation_id is None else "WHERE heads.operation_id = ?"
        parameters = () if operation_id is None else (operation_id,)
        rows = connection.execute(
            f"""
            SELECT records.operation_id, records.record_revision,
                   records.previous_record_sha256, records.record_sha256,
                   records.record_json, records.observed_at,
                   heads.project_id AS head_project_id,
                   heads.effect_key AS head_effect_key
            FROM durable_operation_heads AS heads
            JOIN durable_operation_records AS records
              ON records.operation_id = heads.operation_id
             AND records.record_revision = 0
            {where}
            ORDER BY heads.operation_id
            """,
            parameters,
        ).fetchall()
        expected_count = connection.execute(
            "SELECT COUNT(*) FROM durable_operation_heads"
            + ("" if operation_id is None else " WHERE operation_id = ?"),
            parameters,
        ).fetchone()[0]
        if len(rows) != expected_count:
            raise DurableOperationStoreIntegrityError(
                "durable operation head identity has no genesis record"
            )
        for row in rows:
            genesis = cls._document_from_row(row)
            if (
                genesis["project_id"] != row["head_project_id"]
                or genesis["effect"]["effect_key"] != row["head_effect_key"]
            ):
                raise DurableOperationStoreIntegrityError(
                    "durable operation head identity does not match genesis"
                )

    @classmethod
    def _history_with_connection(
        cls, connection: sqlite3.Connection, operation_id: str
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT operation_id, record_revision, previous_record_sha256,
                   record_sha256, record_json, observed_at
            FROM durable_operation_records
            WHERE operation_id = ?
            ORDER BY record_revision
            """,
            (operation_id,),
        ).fetchall()
        if not rows:
            raise DurableOperationStoreNotFound(
                f"durable operation does not exist: {operation_id}"
            )
        history = [cls._document_from_row(row) for row in rows]
        try:
            return validate_durable_operation_chain(history)
        except DurableOperationError as exc:
            raise DurableOperationStoreIntegrityError(
                "persisted durable operation chain is invalid"
            ) from exc

    def create_operation(self, operation: dict[str, Any]) -> dict[str, Any]:
        """Insert one prepared operation, idempotently for identical bytes."""
        try:
            operation = validate_durable_operation(copy.deepcopy(operation))
        except DurableOperationError as exc:
            raise DurableOperationStoreIntegrityError("operation is invalid") from exc
        if operation["phase"] != "prepared" or operation["record_revision"] != 0:
            raise DurableOperationStoreIntegrityError(
                "new durable operation must start in prepared phase"
            )
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._verify_head_identities(connection)
                row = connection.execute(
                    "SELECT record_sha256 FROM durable_operation_heads WHERE operation_id = ?",
                    (operation["operation_id"],),
                ).fetchone()
                if row is not None:
                    existing = self._history_with_connection(
                        connection, operation["operation_id"]
                    )[-1]
                    if existing == operation:
                        connection.rollback()
                        return copy.deepcopy(existing)
                    raise DurableOperationStoreConflict(
                        "durable operation identity already exists"
                    )
                connection.execute(
                    """
                    INSERT INTO durable_operation_records(
                        operation_id, record_revision, previous_record_sha256,
                        record_sha256, record_json, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation["operation_id"],
                        operation["record_revision"],
                        operation["previous_record_sha256"],
                        operation["record_sha256"],
                        _json_text(operation),
                        operation["updated_at"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO durable_operation_heads(
                        operation_id, project_id, effect_key,
                        record_revision, record_sha256
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        operation["operation_id"],
                        operation["project_id"],
                        operation["effect"]["effect_key"],
                        operation["record_revision"],
                        operation["record_sha256"],
                    ),
                )
                self._fault("before-create-commit")
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if "durable_operation_heads.project_id" in str(exc):
                    raise DurableOperationStoreConflict(
                        "durable operation effect key is already reserved"
                    ) from exc
                raise DurableOperationStoreError(
                    "durable operation create transaction failed"
                ) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise DurableOperationStoreError(
                    "durable operation create transaction failed"
                ) from exc
        return copy.deepcopy(operation)

    def append_transition(
        self,
        operation: dict[str, Any],
        *,
        expected_record_sha256: str,
    ) -> dict[str, Any]:
        """Append one transition, accepting an identical committed replay."""
        return self._append_transition(
            operation,
            expected_record_sha256=expected_record_sha256,
            accept_committed_replay=True,
        )

    def append_transition_owned(
        self,
        operation: dict[str, Any],
        *,
        expected_record_sha256: str,
    ) -> dict[str, Any]:
        """Append one transition only for the unique CAS winner."""
        return self._append_transition(
            operation,
            expected_record_sha256=expected_record_sha256,
            accept_committed_replay=False,
        )

    def _append_transition(
        self,
        operation: dict[str, Any],
        *,
        expected_record_sha256: str,
        accept_committed_replay: bool,
    ) -> dict[str, Any]:
        try:
            operation = validate_durable_operation(copy.deepcopy(operation))
        except DurableOperationError as exc:
            raise DurableOperationStoreIntegrityError("operation is invalid") from exc
        if not isinstance(expected_record_sha256, str):
            raise DurableOperationStoreConflict("expected record hash is invalid")
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._verify_head_identities(
                    connection, operation_id=operation["operation_id"]
                )
                head_row = connection.execute(
                    """
                    SELECT record_revision, record_sha256
                    FROM durable_operation_heads WHERE operation_id = ?
                    """,
                    (operation["operation_id"],),
                ).fetchone()
                if head_row is None:
                    raise DurableOperationStoreNotFound(
                        f"durable operation does not exist: {operation['operation_id']}"
                    )
                history = self._history_with_connection(
                    connection, operation["operation_id"]
                )
                current = history[-1]
                if current["record_sha256"] != expected_record_sha256:
                    if current == operation and accept_committed_replay:
                        connection.rollback()
                        return copy.deepcopy(current)
                    raise DurableOperationStoreConflict(
                        "expected durable operation head is stale"
                    )
                try:
                    validate_durable_operation_transition(current, operation)
                except DurableOperationError as exc:
                    raise DurableOperationStoreIntegrityError(
                        "operation transition is invalid"
                    ) from exc
                connection.execute(
                    """
                    INSERT INTO durable_operation_records(
                        operation_id, record_revision, previous_record_sha256,
                        record_sha256, record_json, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        operation["operation_id"],
                        operation["record_revision"],
                        operation["previous_record_sha256"],
                        operation["record_sha256"],
                        _json_text(operation),
                        operation["updated_at"],
                    ),
                )
                updated = connection.execute(
                    """
                    UPDATE durable_operation_heads
                    SET record_revision = ?, record_sha256 = ?
                    WHERE operation_id = ? AND record_sha256 = ?
                    """,
                    (
                        operation["record_revision"],
                        operation["record_sha256"],
                        operation["operation_id"],
                        expected_record_sha256,
                    ),
                )
                if updated.rowcount != 1:
                    raise DurableOperationStoreConflict(
                        "durable operation head changed during append"
                    )
                self._fault("before-transition-commit")
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DurableOperationStoreConflict(
                    "durable operation transition conflicts with persisted history"
                ) from exc
            except sqlite3.Error as exc:
                connection.rollback()
                raise DurableOperationStoreError(
                    "durable operation append transaction failed"
                ) from exc
        return copy.deepcopy(operation)

    def read_history(self, operation_id: str) -> list[dict[str, Any]]:
        """Read and validate the complete operation history."""
        with closing(self._connect()) as connection:
            self._verify_head_identities(connection, operation_id=operation_id)
            history = self._history_with_connection(connection, operation_id)
            head = connection.execute(
                """
                SELECT record_revision, record_sha256
                FROM durable_operation_heads WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if head is None or (
                head["record_revision"] != history[-1]["record_revision"]
                or head["record_sha256"] != history[-1]["record_sha256"]
            ):
                raise DurableOperationStoreIntegrityError(
                    "durable operation head does not match its history"
                )
            return copy.deepcopy(history)

    def read_operation(self, operation_id: str) -> dict[str, Any]:
        """Read the current validated operation head."""
        return self.read_history(operation_id)[-1]

    def close(self) -> None:
        """Prevent new connections from this store instance."""
        self._closed = True
