"""Durable request receipt storage for unattended runtime ports."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class UnattendedReceiptStoreError(ValueError):
    """A receipt is invalid or conflicts with an existing request."""


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
        raise UnattendedReceiptStoreError("receipt is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


class SQLiteUnattendedPortReceiptStore:
    """Small local durable store with exact request replay semantics."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS unattended_port_receipts (
                    action TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    receipt_sha256 TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    PRIMARY KEY (action, request_id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _validate_key(action: str, request_id: str) -> None:
        if not isinstance(action, str) or not action:
            raise UnattendedReceiptStoreError("receipt action is invalid")
        if not isinstance(request_id, str) or not request_id:
            raise UnattendedReceiptStoreError("receipt request_id is invalid")

    def read(self, action: str, request_id: str) -> dict[str, Any] | None:
        self._validate_key(action, request_id)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT receipt_sha256, receipt_json "
                "FROM unattended_port_receipts WHERE action = ? AND request_id = ?",
                (action, request_id),
            ).fetchone()
        if row is None:
            return None
        try:
            receipt = json.loads(row["receipt_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise UnattendedReceiptStoreError("stored receipt JSON is invalid") from exc
        if not isinstance(receipt, dict) or _digest(receipt) != row["receipt_sha256"]:
            raise UnattendedReceiptStoreError("stored receipt digest mismatch")
        return copy.deepcopy(receipt)

    def write(self, action: str, request_id: str, receipt: dict[str, Any]) -> dict[str, Any]:
        self._validate_key(action, request_id)
        if not isinstance(receipt, dict):
            raise UnattendedReceiptStoreError("receipt must be an object")
        payload = _canonical(receipt).decode("utf-8")
        receipt_sha256 = _digest(receipt)
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT receipt_sha256, receipt_json "
                    "FROM unattended_port_receipts "
                    "WHERE action = ? AND request_id = ?",
                    (action, request_id),
                ).fetchone()
                if row is not None:
                    if row["receipt_sha256"] != receipt_sha256:
                        raise UnattendedReceiptStoreError("request receipt conflict")
                    connection.execute("COMMIT")
                    return copy.deepcopy(receipt)
                connection.execute(
                    "INSERT INTO unattended_port_receipts "
                    "(action, request_id, receipt_sha256, receipt_json) "
                    "VALUES (?, ?, ?, ?)",
                    (action, request_id, receipt_sha256, payload),
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return copy.deepcopy(receipt)

    def close(self) -> None:
        """Closeable for interface symmetry; each operation owns its connection."""


__all__ = [
    "SQLiteUnattendedPortReceiptStore",
    "UnattendedReceiptStoreError",
]
