"""Crash-safe SQLite persistence for the local Work Ledger coordinator."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .shared_work_ledger import ClaimLifecycleError, WorkLedger

SQLITE_APPLICATION_ID = 0x43435032
SQLITE_SCHEMA_VERSION = 1

_OPERATIONS = {
    "acquire_claim",
    "heartbeat_claim",
    "release_claim",
    "revoke_claim",
    "expire_claim",
    "reclaim_claim",
    "complete_work",
    "start_effect_dispatch",
}
_SNAPSHOT_FIELDS = {
    "schema_version",
    "project_id",
    "project_revision",
    "works",
    "claims",
    "effects",
    "transitions",
    "next_lease_epoch",
}
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_SCHEMA = """
CREATE TABLE work_ledgers (
    project_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    max_ttl_ms INTEGER NOT NULL CHECK (max_ttl_ms > 0),
    snapshot TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE TABLE work_ledger_requests (
    project_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    request_id TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    committed_revision INTEGER NOT NULL CHECK (committed_revision >= 0),
    committed_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, operation, request_id),
    FOREIGN KEY (project_id) REFERENCES work_ledgers(project_id)
) STRICT;

CREATE TRIGGER work_ledger_requests_no_update
BEFORE UPDATE ON work_ledger_requests
BEGIN
    SELECT RAISE(ABORT, 'work ledger requests are append-only');
END;

CREATE TRIGGER work_ledger_requests_no_delete
BEFORE DELETE ON work_ledger_requests
BEGIN
    SELECT RAISE(ABORT, 'work ledger requests are append-only');
END;
"""


class SQLiteWorkLedgerError(RuntimeError):
    """Base error for the embedded Work Ledger authority."""


class SQLiteWorkLedgerConflict(SQLiteWorkLedgerError):
    """Raised when a request loses CAS or reuses an identity."""


class SQLiteWorkLedgerIntegrityError(SQLiteWorkLedgerError):
    """Raised when durable Work Ledger bytes fail validation."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SQLiteWorkLedgerIntegrityError("value is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _json_object(value: str, *, field: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise SQLiteWorkLedgerIntegrityError(f"{field} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise SQLiteWorkLedgerIntegrityError(f"{field} is not an object")
    return decoded


def _validate_transition_chain(snapshot: dict[str, Any]) -> None:
    transitions = snapshot["transitions"]
    if not isinstance(transitions, list):
        raise SQLiteWorkLedgerIntegrityError("transition log is invalid")
    previous_sha256: str | None = None
    previous_revision: int | None = None
    for transition in transitions:
        if not isinstance(transition, dict):
            raise SQLiteWorkLedgerIntegrityError("transition is invalid")
        transition_id = transition.get("transition_id")
        transition_sha256 = transition.get("transition_sha256")
        if (
            not isinstance(transition_sha256, str)
            or _SHA256_RE.fullmatch(transition_sha256) is None
            or transition_id != f"transition-{transition_sha256[:32]}"
        ):
            raise SQLiteWorkLedgerIntegrityError("transition digest is invalid")
        body = copy.deepcopy(transition)
        body.pop("transition_id", None)
        body.pop("transition_sha256", None)
        if _digest(body) != transition_sha256:
            raise SQLiteWorkLedgerIntegrityError("transition digest mismatch")
        if transition.get("previous_transition_sha256") != previous_sha256:
            raise SQLiteWorkLedgerIntegrityError("transition hash chain is invalid")
        before = transition.get("project_revision_before")
        after = transition.get("project_revision_after")
        if type(before) is not int or type(after) is not int or after != before + 1:
            raise SQLiteWorkLedgerIntegrityError("transition revision is invalid")
        if previous_revision is not None and before != previous_revision:
            raise SQLiteWorkLedgerIntegrityError("transition revision chain is invalid")
        previous_sha256 = transition_sha256
        previous_revision = after
    if transitions and previous_revision != snapshot["project_revision"]:
        raise SQLiteWorkLedgerIntegrityError(
            "snapshot revision does not match transition head"
        )


def _validate_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise SQLiteWorkLedgerIntegrityError("snapshot fields are invalid")
    if snapshot["schema_version"] != "context.work-ledger/v1alpha1":
        raise SQLiteWorkLedgerIntegrityError("snapshot version is invalid")
    project_id = snapshot["project_id"]
    revision = snapshot["project_revision"]
    next_epoch = snapshot["next_lease_epoch"]
    if not isinstance(project_id, str) or not project_id:
        raise SQLiteWorkLedgerIntegrityError("snapshot project identity is invalid")
    if type(revision) is not int or revision < 0:
        raise SQLiteWorkLedgerIntegrityError("snapshot revision is invalid")
    if type(next_epoch) is not int or next_epoch < 0:
        raise SQLiteWorkLedgerIntegrityError("snapshot lease epoch is invalid")
    for field in ("works", "claims", "effects"):
        if not isinstance(snapshot[field], list) or any(
            not isinstance(item, dict) for item in snapshot[field]
        ):
            raise SQLiteWorkLedgerIntegrityError(f"snapshot {field} are invalid")
    claim_epochs = [claim.get("lease_epoch") for claim in snapshot["claims"]]
    if any(type(epoch) is not int or epoch < 0 for epoch in claim_epochs):
        raise SQLiteWorkLedgerIntegrityError("snapshot claim lease epoch is invalid")
    if claim_epochs and next_epoch < max(claim_epochs):
        raise SQLiteWorkLedgerIntegrityError("snapshot lease epoch regressed")
    _validate_transition_chain(snapshot)
    return copy.deepcopy(snapshot)


def _restore_ledger(snapshot: dict[str, Any], *, max_ttl_ms: int) -> WorkLedger:
    current = _validate_snapshot(snapshot)
    ledger = WorkLedger(
        project_id=current["project_id"],
        project_revision=current["project_revision"],
        works=current["works"],
        max_ttl_ms=max_ttl_ms,
    )
    ledger._claims = {
        claim["claim_id"]: copy.deepcopy(claim) for claim in current["claims"]
    }
    ledger._effects = {
        effect["effect_id"]: copy.deepcopy(effect) for effect in current["effects"]
    }
    ledger._transitions = copy.deepcopy(current["transitions"])
    ledger._next_lease_epoch = current["next_lease_epoch"]
    if ledger.snapshot() != current:
        raise SQLiteWorkLedgerIntegrityError("snapshot cannot be restored losslessly")
    return ledger


class SQLiteWorkLedgerStore:
    """One-file authority for a local multi-session coordinator."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        busy_timeout_ms: int = 5_000,
        fault_hook: Callable[[str], None] | None = None,
    ) -> None:
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        if fault_hook is not None and not callable(fault_hook):
            raise TypeError("fault_hook must be callable")
        self.database_path = Path(database_path)
        self.busy_timeout_ms = busy_timeout_ms
        self.fault_hook = fault_hook

    @contextmanager
    def _connect(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            application_id = connection.execute("PRAGMA application_id").fetchone()[0]
            if initialize and application_id == 0:
                connection.execute(f"PRAGMA application_id = {SQLITE_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
                connection.executescript(_SCHEMA)
            elif (
                application_id != SQLITE_APPLICATION_ID
                or connection.execute("PRAGMA user_version").fetchone()[0]
                != SQLITE_SCHEMA_VERSION
            ):
                raise SQLiteWorkLedgerIntegrityError(
                    "SQLite Work Ledger identity is invalid"
                )
            yield connection
        except sqlite3.OperationalError as exc:
            raise SQLiteWorkLedgerConflict("SQLite Work Ledger is busy") from exc
        finally:
            connection.close()

    def initialize(
        self,
        *,
        project_id: str,
        project_revision: int,
        works: list[dict[str, Any]],
        max_ttl_ms: int,
    ) -> None:
        ledger = WorkLedger(
            project_id=project_id,
            project_revision=project_revision,
            works=works,
            max_ttl_ms=max_ttl_ms,
        )
        snapshot = ledger.snapshot()
        with self._connect(initialize=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT snapshot, snapshot_sha256, max_ttl_ms FROM work_ledgers "
                    "WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO work_ledgers "
                        "(project_id, revision, max_ttl_ms, snapshot, snapshot_sha256) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            project_id,
                            project_revision,
                            max_ttl_ms,
                            _canonical(snapshot).decode("utf-8"),
                            _digest(snapshot),
                        ),
                    )
                else:
                    stored = _json_object(row[0], field="snapshot")
                    if row[1] != _digest(stored) or row[2] != max_ttl_ms:
                        raise SQLiteWorkLedgerIntegrityError(
                            "existing Work Ledger configuration is inconsistent"
                        )
                    _validate_snapshot(stored)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise

    @staticmethod
    def _read_row(
        connection: sqlite3.Connection,
        project_id: str,
    ) -> tuple[int, int, dict[str, Any]]:
        row = connection.execute(
            "SELECT revision, max_ttl_ms, snapshot, snapshot_sha256 "
            "FROM work_ledgers WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            raise SQLiteWorkLedgerConflict("project does not exist")
        snapshot = _json_object(row[2], field="snapshot")
        _validate_snapshot(snapshot)
        if row[3] != _digest(snapshot):
            raise SQLiteWorkLedgerIntegrityError("snapshot digest mismatch")
        if snapshot["project_revision"] != row[0]:
            raise SQLiteWorkLedgerIntegrityError("snapshot revision mismatch")
        return row[0], row[1], snapshot

    def read_snapshot(self, project_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN")
            _, _, snapshot = self._read_row(connection, project_id)
            connection.execute("COMMIT")
            return snapshot

    def read_request_receipt(
        self,
        project_id: str,
        operation: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT response, response_sha256 FROM work_ledger_requests "
                "WHERE project_id = ? AND operation = ? AND request_id = ?",
                (project_id, operation, request_id),
            ).fetchone()
            if row is None:
                return None
            response = _json_object(row[0], field="request response")
            if row[1] != _digest(response):
                raise SQLiteWorkLedgerIntegrityError("request response digest mismatch")
            return response

    def execute(
        self,
        *,
        project_id: str,
        operation: str,
        request_id: str,
        arguments: dict[str, Any],
        request_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if operation not in _OPERATIONS:
            raise SQLiteWorkLedgerIntegrityError("operation is unsupported")
        if not isinstance(request_id, str) or not request_id:
            raise SQLiteWorkLedgerIntegrityError("request_id is invalid")
        if not isinstance(arguments, dict):
            raise SQLiteWorkLedgerIntegrityError("arguments are invalid")
        if request_payload is not None and not isinstance(request_payload, dict):
            raise SQLiteWorkLedgerIntegrityError("request_payload is invalid")
        request = {
            "project_id": project_id,
            "operation": operation,
            "request_id": request_id,
            "payload": copy.deepcopy(
                arguments if request_payload is None else request_payload
            ),
        }
        request_sha256 = _digest(request)
        response: dict[str, Any]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT request_sha256, response, response_sha256 "
                    "FROM work_ledger_requests "
                    "WHERE project_id = ? AND operation = ? AND request_id = ?",
                    (project_id, operation, request_id),
                ).fetchone()
                if existing is not None:
                    if existing[0] != request_sha256:
                        raise SQLiteWorkLedgerConflict(
                            "request_id was reused for a different payload"
                        )
                    response = _json_object(existing[1], field="request response")
                    if existing[2] != _digest(response):
                        raise SQLiteWorkLedgerIntegrityError(
                            "request response digest mismatch"
                        )
                    connection.execute("COMMIT")
                    return response

                revision, max_ttl_ms, snapshot = self._read_row(connection, project_id)
                ledger = _restore_ledger(snapshot, max_ttl_ms=max_ttl_ms)
                try:
                    response = getattr(ledger, operation)(**copy.deepcopy(arguments))
                except ClaimLifecycleError as exc:
                    raise SQLiteWorkLedgerConflict(exc.code) from exc
                candidate = ledger.snapshot()
                _validate_snapshot(candidate)
                if candidate["project_revision"] != revision + 1:
                    raise SQLiteWorkLedgerIntegrityError(
                        "mutation did not advance exactly one revision"
                    )
                updated = connection.execute(
                    "UPDATE work_ledgers SET revision = ?, snapshot = ?, "
                    "snapshot_sha256 = ?, updated_at = datetime('now') "
                    "WHERE project_id = ? AND revision = ?",
                    (
                        candidate["project_revision"],
                        _canonical(candidate).decode("utf-8"),
                        _digest(candidate),
                        project_id,
                        revision,
                    ),
                )
                if updated.rowcount != 1:
                    raise SQLiteWorkLedgerConflict("project revision changed")
                connection.execute(
                    "INSERT INTO work_ledger_requests "
                    "(project_id, operation, request_id, request_sha256, response, "
                    "response_sha256, committed_revision) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        operation,
                        request_id,
                        request_sha256,
                        _canonical(response).decode("utf-8"),
                        _digest(response),
                        candidate["project_revision"],
                    ),
                )
                if self.fault_hook is not None:
                    self.fault_hook("before_commit")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        if self.fault_hook is not None:
            self.fault_hook("after_commit")
        return copy.deepcopy(response)


__all__ = [
    "SQLiteWorkLedgerConflict",
    "SQLiteWorkLedgerError",
    "SQLiteWorkLedgerIntegrityError",
    "SQLiteWorkLedgerStore",
]
