"""Rebuildable byte-offset index over the authoritative local change journal."""

import json
import os
import re
import sqlite3
from pathlib import Path


# Characters that make str.splitlines() cut a line that only "\n" delimits here.
_EXTRA_BREAKS = re.compile("[\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
_TAIL_BYTES = 256
_CHUNK_BYTES = 8 * 1024 * 1024


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
        columns = {row[1] for row in connection.execute("PRAGMA table_info(checkpoint)")}
        for name, kind in (("corrupt", "INTEGER"), ("irregular", "INTEGER"), ("tail", "BLOB")):
            if name not in columns:
                try:
                    connection.execute(f"ALTER TABLE checkpoint ADD COLUMN {name} {kind}")
                except sqlite3.OperationalError:
                    if name not in {
                        row[1] for row in connection.execute("PRAGMA table_info(checkpoint)")
                    }:
                        raise  # A concurrent opener may have added the column first.
    except BaseException:
        connection.close()
        raise
    return connection


def _state(connection):
    return connection.execute(
        "SELECT device, inode, offset, corrupt, irregular, tail FROM checkpoint WHERE id=1"
    ).fetchone()


def _tail_matches(journal_stream, offset, tail):
    """Cheap evidence that the indexed prefix was not rewritten in place."""
    keep = min(offset, _TAIL_BYTES)
    if tail is None or len(tail) != keep:
        return False
    journal_stream.seek(offset - keep)
    return journal_stream.read(keep) == bytes(tail)


def _classify(segment):
    """Mirror list_changes() on one newline-terminated line.

    Returns (kind, cursor, event_id) where kind is "ok", "corrupt" or "irregular";
    irregular lines need the authoritative full scan to be counted identically.
    """
    try:
        text = segment.decode("utf-8")
    except UnicodeDecodeError:
        return "irregular", None, None
    if text.startswith("\ufeff") or _EXTRA_BREAKS.search(
        text[:-1] if text.endswith("\r") else text
    ):
        return "irregular", None, None
    try:
        row = json.loads(text)
    except json.JSONDecodeError:
        return "corrupt", None, None
    except ValueError:
        return "irregular", None, None
    if not isinstance(row, dict) or type(row.get("cursor")) is not int:
        return "corrupt", None, None
    event_id = row.get("event_id")
    return "ok", row["cursor"], event_id if isinstance(event_id, str) else None


def refresh(connection, journal):
    """Index only complete appended lines; a replaced/truncated file rebuilds.

    Returns True when every byte of the journal is covered by complete lines.
    A no-op refresh never opens a write transaction.
    """
    journal = Path(journal)
    with journal.open("rb") as stream:
        info = os.fstat(stream.fileno())
        previous = _state(connection)
        if (
            previous
            and previous[3] is not None
            and previous[:2] == (info.st_dev, info.st_ino)
            and previous[2] == info.st_size
            and _tail_matches(stream, previous[2], previous[5])
        ):
            return True
    connection.execute("BEGIN IMMEDIATE")
    try:
        with journal.open("rb") as stream:
            info = os.fstat(stream.fileno())
            previous = _state(connection)
            offset, corrupt, irregular = 0, 0, 0
            rebuild = True
            if previous and previous[3] is not None:
                offset, corrupt, irregular = previous[2], previous[3], previous[4]
                rebuild = (
                    previous[:2] != (info.st_dev, info.st_ino)
                    or info.st_size < offset
                    or not _tail_matches(stream, offset, previous[5])
                )
                if rebuild:
                    offset, corrupt, irregular = 0, 0, 0
            if rebuild:
                connection.execute("DELETE FROM entries")
            start, rows, pending = offset, [], b""
            stream.seek(offset)
            while True:
                chunk = stream.read(_CHUNK_BYTES)
                if not chunk:
                    break
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()
                for line in lines:
                    kind, cursor, event_id = _classify(line)
                    if kind == "ok":
                        rows.append((start, cursor, event_id))
                    elif kind == "corrupt":
                        corrupt += 1
                    else:
                        irregular += 1
                    start += len(line) + 1
            if rows:
                connection.executemany("INSERT OR REPLACE INTO entries VALUES (?, ?, ?)", rows)
            complete = not pending
            changed = rebuild or start != offset
            if changed:
                keep = min(start, _TAIL_BYTES)
                stream.seek(start - keep)
                connection.execute(
                    "INSERT OR REPLACE INTO checkpoint (id, device, inode, offset, corrupt, "
                    "irregular, tail) VALUES (1, ?, ?, ?, ?, ?, ?)",
                    (info.st_dev, info.st_ino, start, corrupt, irregular, stream.read(keep)),
                )
        if changed:
            connection.commit()
        else:
            connection.rollback()
        return complete
    except BaseException:
        connection.rollback()
        raise


def read_page(journal, since, limit):
    """Serve one change page from the index, or None when a full scan is required.

    The result mirrors a scan of the whole journal: valid rows ordered by cursor,
    the pending total, the high-water cursor and the corrupt-line count. Any
    irregular line, unterminated tail, unreadable index or stale offset returns
    None so the authoritative journal answers instead.
    """
    journal = Path(journal)
    try:
        connection = _open(journal)
    except (sqlite3.Error, OSError):
        return None
    try:
        if not refresh(connection, journal):
            return None
        state = _state(connection)
        if state is None or state[4]:
            return None
        pending = connection.execute(
            "SELECT offset, cursor FROM entries WHERE cursor > ? ORDER BY cursor, offset LIMIT ?",
            (since, limit),
        ).fetchall()
        total = connection.execute(
            "SELECT COUNT(*) FROM entries WHERE cursor > ?", (since,)
        ).fetchone()[0]
        high = connection.execute("SELECT MAX(cursor) FROM entries").fetchone()[0]
        rows = []
        if pending:
            with journal.open("rb") as stream:
                for position, cursor in pending:
                    stream.seek(position)
                    row = json.loads(stream.readline().decode("utf-8"))
                    if row["cursor"] != cursor:
                        return None
                    rows.append(row)
        return rows, total, high or 0, state[3]
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError):
        return None
    finally:
        connection.close()


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
