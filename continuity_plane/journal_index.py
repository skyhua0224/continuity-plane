"""Rebuildable byte-offset index over the authoritative local change journal."""

import json
import os
import sqlite3
from pathlib import Path


def _open(journal):
    journal = Path(journal)
    journal.parent.mkdir(parents=True, exist_ok=True)
    path = journal.with_name("changes-index.sqlite3")
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY, "
            "device INTEGER, inode INTEGER, offset INTEGER)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS entries (offset INTEGER PRIMARY KEY, "
            "cursor INTEGER NOT NULL, event_id TEXT)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS entries_cursor ON entries(cursor)")
        connection.execute("CREATE INDEX IF NOT EXISTS entries_event_id ON entries(event_id)")
    except BaseException:
        connection.close()
        raise
    return connection


def refresh(connection, journal):
    """Index only complete appended lines; a replaced/truncated file rebuilds."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        with Path(journal).open("rb") as stream:
            info = os.fstat(stream.fileno())
            previous = connection.execute(
                "SELECT device, inode, offset FROM checkpoint WHERE id=1"
            ).fetchone()
            offset = previous[2] if previous else 0
            if previous and (
                previous[:2] != (info.st_dev, info.st_ino) or info.st_size < offset
            ):
                connection.execute("DELETE FROM entries")
                offset = 0
            stream.seek(offset)
            while stream.tell() < info.st_size:
                start = stream.tell()
                line = stream.readline()
                if not line.endswith(b"\n"):
                    break
                offset = stream.tell()
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(row, dict) and type(row.get("cursor")) is int:
                    event_id = row.get("event_id")
                    connection.execute(
                        "INSERT OR REPLACE INTO entries VALUES (?, ?, ?)",
                        (start, row["cursor"], event_id if isinstance(event_id, str) else None),
                    )
            connection.execute(
                "INSERT OR REPLACE INTO checkpoint VALUES (1, ?, ?, ?)",
                (info.st_dev, info.st_ino, offset),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def offset_after(journal, since):
    journal = Path(journal)
    if not journal.exists():
        return 0
    try:
        connection = _open(journal)
    except sqlite3.Error:
        return 0  # Derived-cache failure falls back to the authoritative journal.
    try:
        refresh(connection, journal)
        row = connection.execute(
            "SELECT MIN(offset) FROM entries WHERE cursor > ?", (since,)
        ).fetchone()
        if row and row[0] is not None:
            return row[0]
        return connection.execute("SELECT offset FROM checkpoint WHERE id=1").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        connection.close()


def find_event(journal, event_id):
    journal = Path(journal)
    if not journal.exists():
        return None
    try:
        return _find_indexed_event(journal, event_id)
    except sqlite3.Error:
        with journal.open("rb") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict) and value.get("event_id") == event_id:
                    return value
        return None


def _find_indexed_event(journal, event_id):
    connection = _open(journal)
    try:
        refresh(connection, journal)
        row = connection.execute(
            "SELECT offset FROM entries WHERE event_id=? ORDER BY offset LIMIT 1", (event_id,)
        ).fetchone()
        if row is None:
            return None
        with journal.open("rb") as stream:
            stream.seek(row[0])
            value = json.loads(stream.readline())
        return value if value.get("event_id") == event_id else None
    finally:
        connection.close()
