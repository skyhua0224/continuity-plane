"""Generic, non-blocking project and task collaboration metadata."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import math
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from .release_version import VERSION


PROJECT_REGISTRATION_SCHEMA = "context.project-registration/v1alpha1"
DISPATCH_LEDGER_SCHEMA = "context.dispatch-ledger/v3alpha1"
GUARDED_DISPATCH_LEDGER_SCHEMA = "context.dispatch-ledger/v4alpha1"
PREVIOUS_DISPATCH_LEDGER_SCHEMA = "context.dispatch-ledger/v2alpha1"
LEGACY_DISPATCH_LEDGER_SCHEMA = "context.dispatch-ledger/v1alpha1"
TASK_CARD_SCHEMA = "context.task-card/v1alpha1"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ASSIGNEE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_RACY_NS = 2_000_000_000
# Text of ledgers already validated in this process, keyed by path and file identity.
_LEDGER_MEMO: dict[str, tuple[tuple, str]] = {}
_CONTROL_RE = re.compile("[\x00-\x1f]")
_CONTROL_MULTILINE_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
_PRIORITY = {"p0", "p1", "p2"}
_MODE = {"scout", "review", "patch", "integration", "cleanup", "infra", "decision"}
_TASK_STATUS = {
    "queued",
    "active",
    "blocked",
    "ready-for-review",
    "accepted",
    "rejected",
    "superseded",
    "dispatched",
    "completed",
    "merged",
}
_EFFECT_STATUS = {"queued", "approved", "rejected", "executed"}
_REPORT_POLICY = {
    "silent_until_stage_complete",
    "on_blocker",
    "on_decision",
    "verbose",
}


class CollaborationRegistryError(ValueError):
    """A collaboration document or operation is invalid."""


def default_data_root() -> Path:
    configured = os.environ.get("CONTINUITY_DATA_ROOT")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "continuity"
    return Path.home() / ".local" / "share" / "continuity"


def project_data_dir(data_root: Path | str, project_id: str) -> Path:
    """Return a project's local collaboration data directory."""
    root = Path(data_root) if data_root is not None else default_data_root()
    return _project_dir(root, project_id)


def write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    """Atomically write one collaboration JSON document."""
    _atomic_write(path, document)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    if (
        isinstance(document, dict)
        and document.get("schema_version") == GUARDED_DISPATCH_LEDGER_SCHEMA
    ):
        document["writer_version"] = VERSION
        document["minimum_reader_schema"] = 4
        document["controller_fencing_required"] = True
    if (
        isinstance(document, dict)
        and document.get("schema_version") == DISPATCH_LEDGER_SCHEMA
    ):
        document["writer_version"] = VERSION
        active = [
            (r.get("assignee"), r.get("lane_id"))
            for r in document.get("tasks", [])
            if r.get("status") == "active"
        ]
        if (
            len(active) != len(set(active))
            or document.get("minimum_reader_schema") == 3
        ):
            document["minimum_reader_schema"] = 3
        else:
            document["schema_version"] = PREVIOUS_DISPATCH_LEDGER_SCHEMA
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        from .persistent_storage import sync_directory

        sync_directory(path.parent)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock a stable inode: the document itself is replaced by atomic writes.
    with path.with_name(path.name + ".lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_text(path: Path) -> tuple[str | None, tuple | None]:
    """Read one document; the identity is None while metadata could still be racy."""
    try:
        with path.open("rb") as stream:
            info = os.fstat(stream.fileno())
            # Decoding bytes avoids the text layer's newline translation pass.
            content = stream.read().decode("utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError) as exc:
        raise CollaborationRegistryError(
            f"collaboration document is unreadable: {exc}"
        ) from exc
    return content, _stable_identity(info)


def _stable_identity(info: os.stat_result) -> tuple | None:
    # A file rewritten within the clock granularity could keep its metadata.
    if time.time_ns() - info.st_mtime_ns < _RACY_NS:
        return None
    return (info.st_ino, info.st_size, info.st_mtime_ns)


def _parse(content: str | None) -> dict[str, Any] | None:
    if content is None or not content.strip():
        return None
    try:
        document = json.loads(content)
    except json.JSONDecodeError as exc:
        raise CollaborationRegistryError(
            f"collaboration document is unreadable: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise CollaborationRegistryError("collaboration document is invalid")
    return document


def _load(path: Path) -> dict[str, Any] | None:
    return _parse(_read_text(path)[0])


def _text(value: Any, field: str, limit: int = 4096, *, multiline=False) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or (len(value) if value.isascii() else len(value.encode("utf-8"))) > limit
        or (_CONTROL_MULTILINE_RE if multiline else _CONTROL_RE).search(value)
    ):
        raise CollaborationRegistryError(f"{field} is invalid")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise CollaborationRegistryError(f"{field} must be a list")
    result = [_text(item, field, 2048) for item in value]
    if len(result) != len(set(result)):
        raise CollaborationRegistryError(f"{field} must contain unique values")
    return result


def _absolute_path(value: Any, field: str) -> str:
    path = Path(_text(value, field, 4096))
    if not path.is_absolute():
        raise CollaborationRegistryError(f"{field} must be absolute")
    return str(path)


def _project_dir(data_root: Path, project_id: str) -> Path:
    if _ID_RE.fullmatch(project_id) is None:
        raise CollaborationRegistryError("project_id is invalid")
    return data_root / "projects" / project_id


def register_project(
    data_root: Path | str | None = None,
    *,
    project_id: str,
    control_root: Path | str,
    repository_roots: list[Path | str] | None = None,
) -> dict[str, Any]:
    """Register one project and its participating repository roots."""
    root = Path(data_root) if data_root is not None else default_data_root()
    if _ID_RE.fullmatch(project_id) is None:
        raise CollaborationRegistryError("project_id is invalid")
    control = str(Path(control_root).resolve())
    roots = [control]
    for item in repository_roots or []:
        path = str(Path(item).resolve())
        if path not in roots:
            roots.append(path)
    document = {
        "schema_version": PROJECT_REGISTRATION_SCHEMA,
        "project_id": project_id,
        "control_root": control,
        "repository_roots": roots,
        "updated_at": _now(),
    }
    target = _project_dir(root, project_id) / "project.json"
    with _locked(target):
        _atomic_write(target, document)
    return document


def list_projects(data_root: Path | str | None = None) -> list[dict[str, Any]]:
    root = Path(data_root) if data_root is not None else default_data_root()
    directory = root / "projects"
    if not directory.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*/project.json")):
        document = _load(path)
        if document is None:
            continue
        if document.get(
            "schema_version"
        ) != PROJECT_REGISTRATION_SCHEMA or not isinstance(
            document.get("project_id"), str
        ):
            raise CollaborationRegistryError(
                f"unsupported project registration: {path}"
            )
        result.append(document)
    return result


def resolve_project(
    root: Path | str,
    data_root: Path | str | None = None,
) -> dict[str, Any] | None:
    """Resolve an exact registered control or repository root."""
    requested = str(Path(root).resolve())
    for project in list_projects(data_root):
        roots = project.get("repository_roots", [])
        if requested in roots or requested == project.get("control_root"):
            return project
    return None


def _validate_task(task: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "schema_version",
        "task_id",
        "lane_id",
        "mode",
        "priority",
        "status",
        "title",
        "objective",
        "next_action",
        "assignee",
        "worktree",
        "report_policy",
        "allowed_files",
        "preauthorized_effects",
        "queued_effects",
        "dependencies",
        "exit_criteria",
        "blocked_reason",
        "result",
        "branch",
        "commit",
        "verification",
        "landed_commit",
        "released_in",
        "created_at",
        "updated_at",
    }
    if not isinstance(task, dict) or set(task) != fields:
        raise CollaborationRegistryError("task fields are invalid")
    if task["schema_version"] != TASK_CARD_SCHEMA:
        raise CollaborationRegistryError("task schema is unsupported")
    for field in ("task_id", "lane_id"):
        if _ID_RE.fullmatch(str(task[field])) is None:
            raise CollaborationRegistryError(f"{field} is invalid")
    if task["mode"] not in _MODE:
        raise CollaborationRegistryError(
            f"task mode {task['mode']!r} is invalid; valid modes: {', '.join(sorted(_MODE))}"
        )
    if task["priority"] not in _PRIORITY:
        raise CollaborationRegistryError(
            f"task priority {task['priority']!r} is invalid; valid priorities: {', '.join(sorted(_PRIORITY))}"
        )
    if task["status"] not in _TASK_STATUS:
        raise CollaborationRegistryError(
            f"task status {task['status']!r} is invalid; valid statuses: {', '.join(sorted(_TASK_STATUS))}"
        )
    _text(task["title"], "title", 512)
    _text(task["objective"], "objective")
    _text(task["next_action"], "next_action", 1024)
    if (
        task["assignee"] is not None
        and _ASSIGNEE_RE.fullmatch(task["assignee"]) is None
    ):
        raise CollaborationRegistryError("assignee is invalid")
    if task["status"] == "active" and task["assignee"] is None:
        raise CollaborationRegistryError(
            "active task requires assignee; use task update --assignee ACTOR or task claim --assignee ACTOR"
        )
    if task["worktree"] is not None:
        _absolute_path(task["worktree"], "worktree")
    if task["report_policy"] not in _REPORT_POLICY:
        raise CollaborationRegistryError("report_policy is invalid")
    for field in (
        "allowed_files",
        "preauthorized_effects",
        "queued_effects",
        "dependencies",
        "exit_criteria",
    ):
        _string_list(task[field], field)
    if len(task["dependencies"]) > 64 or any(
        _ID_RE.fullmatch(value) is None for value in task["dependencies"]
    ):
        raise CollaborationRegistryError(
            "dependencies must contain at most 64 stable task IDs"
        )
    if task["task_id"] in task["dependencies"]:
        raise CollaborationRegistryError("task cannot depend on itself")
    if not task["exit_criteria"]:
        raise CollaborationRegistryError("exit_criteria must not be empty")
    overlap = set(task["preauthorized_effects"]) & set(task["queued_effects"])
    if overlap:
        raise CollaborationRegistryError(
            "effect cannot be both preauthorized and queued"
        )
    if task["blocked_reason"] is not None:
        _text(task["blocked_reason"], "blocked_reason")
    if task["result"] is not None:
        _text(task["result"], "result")
    if task["branch"] is not None:
        _text(task["branch"], "branch", 512)
    if task["commit"] is not None:
        if not isinstance(task["commit"], str) or not re.fullmatch(
            r"[0-9a-f]{7,64}", task["commit"]
        ):
            raise CollaborationRegistryError("commit is invalid")
    if task["verification"] is not None:
        _text(task["verification"], "verification", 4096)
    for field in ("landed_commit",):
        if task[field] is not None and (
            not isinstance(task[field], str)
            or not re.fullmatch(r"[0-9a-f]{7,64}", task[field])
        ):
            raise CollaborationRegistryError(f"{field} is invalid")
    if task["released_in"] is not None:
        _text(task["released_in"], "released_in", 256)
    _text(task["created_at"], "created_at", 64)
    _text(task["updated_at"], "updated_at", 64)
    return task


def _ledger_path(data_root: Path, project_id: str) -> Path:
    return _project_dir(data_root, project_id) / "dispatch.json"


def _changes_path(data_root: Path, project_id: str) -> Path:
    return _project_dir(data_root, project_id) / "changes.jsonl"


def _record_change(
    data_root: Path,
    project_id: str,
    event: str,
    task_id: str | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    path = _changes_path(data_root, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        prefix = ""
        if fields.get("event_id"):
            from .journal_index import find_event

            existing = find_event(path, fields["event_id"])
            if existing is not None:
                return existing
        cursor = latest_change_cursor(data_root, project_id=project_id)
        try:
            with path.open("rb") as stream:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b"\n":
                    prefix = "\n"
        except (FileNotFoundError, OSError):
            pass
        payload = {
            **fields,
            "cursor": cursor + 1,
            "observed_at": _now(),
            "event": event,
            "task_id": task_id,
        }
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                prefix + json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())
        return payload


def list_changes(
    data_root: Path | str | None,
    *,
    project_id: str,
    since: int = 0,
    max_results: int = 50,
) -> dict[str, Any]:
    root = Path(data_root) if data_root is not None else default_data_root()
    if (
        type(since) is not int
        or type(max_results) is not int
        or since < 0
        or max_results < 1
        or max_results > 500
    ):
        raise CollaborationRegistryError("since or max_results is invalid")
    path = _changes_path(root, project_id)
    if path.exists():
        from .journal_index import read_page

        # The index answers without parsing history; None means scan the journal.
        indexed = read_page(path, since, max_results)
        if indexed is not None:
            selected, total, high_water, corrupt = indexed
            return {
                "schema_version": "context.collaboration-changes/v1alpha1",
                "since": since,
                "next_cursor": selected[-1]["cursor"] if selected else since,
                "high_water_cursor": high_water,
                "changes": selected,
                "total_changes": total,
                "truncated": len(selected) < total,
                "corrupt_lines": corrupt,
            }
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    rows = []
    corrupt = 0
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            corrupt += 1
            continue
        if not isinstance(row, dict) or type(row.get("cursor")) is not int:
            corrupt += 1
            continue
        rows.append(row)
    rows.sort(key=lambda row: row["cursor"])
    pending = [row for row in rows if row["cursor"] > since]
    selected = pending[:max_results]
    return {
        "schema_version": "context.collaboration-changes/v1alpha1",
        "since": since,
        "next_cursor": selected[-1]["cursor"] if selected else since,
        "high_water_cursor": max([row["cursor"] for row in rows] or [0]),
        "changes": selected,
        "total_changes": len(pending),
        "truncated": len(selected) < len(pending),
        "corrupt_lines": corrupt,
    }


def iter_changes(
    data_root: Path | str | None,
    *,
    project_id: str,
    since: int = 0,
):
    """Stream valid appended events without repeatedly reparsing pages."""

    if type(since) is not int or since < 0:
        raise CollaborationRegistryError("since is invalid")
    root = Path(data_root) if data_root is not None else default_data_root()
    path = _changes_path(root, project_id)
    try:
        from .journal_index import offset_after

        offset = offset_after(path, since) if since else 0
        with path.open("rb") as stream:
            stream.seek(offset)
            for line in stream:
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if (
                    isinstance(row, dict)
                    and type(row.get("cursor")) is int
                    and row["cursor"] > since
                ):
                    yield row
    except FileNotFoundError:
        return


def latest_change_cursor(data_root: Path | str | None, *, project_id: str) -> int:
    """Read the last valid cursor without parsing the full append-only history."""
    root = Path(data_root) if data_root is not None else default_data_root()
    path = _changes_path(root, project_id)
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 1024 * 1024))
            lines = stream.read().splitlines()
    except FileNotFoundError:
        return 0
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if isinstance(row, dict) and type(row.get("cursor")) is int:
            return row["cursor"]
    return 0


def change_cursor_at_or_before(
    data_root: Path | str | None,
    *,
    project_id: str,
    timestamp: str,
) -> int:
    """Find the newest event cursor at or before an ISO time by scanning backward."""
    cutoff = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise CollaborationRegistryError("change cursor timestamp requires a timezone")
    root = Path(data_root) if data_root is not None else default_data_root()
    path = _changes_path(root, project_id)
    try:
        stream = path.open("rb")
    except FileNotFoundError:
        return 0
    with stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        carry = b""
        while position > 0:
            size = min(1024 * 1024, position)
            position -= size
            stream.seek(position)
            block = stream.read(size) + carry
            lines = block.split(b"\n")
            carry = lines[0] if position > 0 else b""
            if position > 0:
                lines = lines[1:]
            for line in reversed(lines):
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(row, dict) or type(row.get("cursor")) is not int:
                    continue
                try:
                    observed = datetime.fromisoformat(
                        str(row["observed_at"]).replace("Z", "+00:00")
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if observed.tzinfo is not None and observed <= cutoff:
                    return row["cursor"]
    return 0


def wait_for_changes(
    data_root,
    *,
    project_id: str,
    since: int = 0,
    event: str | list[str] | None = None,
    task_id: str | None = None,
    assignee: str | None = None,
    timeout_seconds: float = 300,
    poll_seconds: float = 0.1,
) -> dict[str, Any]:
    """Wait quietly for matching facts; never launch or wake another provider."""
    names = [event] if isinstance(event, str) else event
    if names is not None and (
        not isinstance(names, (list, tuple))
        or not names
        or any(
            not isinstance(name, str) or not name.strip() or len(name) > 128
            for name in names
        )
    ):
        raise CollaborationRegistryError(
            "event must be a nonempty event name or list of names"
        )
    events = set(names) if names is not None else None
    if (
        not math.isfinite(timeout_seconds)
        or timeout_seconds < 0
        or not math.isfinite(poll_seconds)
        or poll_seconds <= 0
    ):
        raise CollaborationRegistryError("wait timeout/poll interval is invalid")
    deadline = time.monotonic() + timeout_seconds
    cursor = since
    while True:
        page = list_changes(
            data_root, project_id=project_id, since=cursor, max_results=500
        )
        selected = [
            row
            for row in page["changes"]
            if (events is None or row.get("event") in events)
            and (task_id is None or row.get("task_id") == task_id)
            and (assignee is None or row.get("assignee") == assignee)
        ]
        cursor = page["next_cursor"]
        page.update(since=since, changes=selected, timed_out=False)
        if selected:
            return page
        if time.monotonic() >= deadline:
            page["timed_out"] = True
            return page
        if not page["truncated"]:
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": DISPATCH_LEDGER_SCHEMA,
        "writer_version": VERSION,
        "tasks": [],
        "effect_requests": [],
        "updated_at": _now(),
    }


def _load_ledger(data_root: Path, project_id: str) -> dict[str, Any]:
    path = _ledger_path(data_root, project_id)
    memo = _LEDGER_MEMO.get(str(path))
    try:
        trusted = memo is not None and memo[0] == _stable_identity(path.stat())
    except OSError:
        trusted = False
    if trusted:
        # Same file as one already validated: parse a private copy, skip re-validation.
        content, identity = memo[1], memo[0]
    else:
        content, identity = _read_text(path)
    document = _parse(content) or _empty_ledger()
    if not isinstance(document, dict):
        raise CollaborationRegistryError("dispatch ledger is invalid")
    if document.get("schema_version") == GUARDED_DISPATCH_LEDGER_SCHEMA:
        document["controller_fencing_required"] = True
        document.setdefault("minimum_reader_schema", 4)
    if document.get("schema_version") == DISPATCH_LEDGER_SCHEMA and document.get(
        "tasks"
    ):
        document.setdefault("minimum_reader_schema", 3)
    if document.get("schema_version") in {
        LEGACY_DISPATCH_LEDGER_SCHEMA,
        PREVIOUS_DISPATCH_LEDGER_SCHEMA,
    }:
        document["schema_version"] = DISPATCH_LEDGER_SCHEMA
        document.setdefault("writer_version", "legacy")
    elif document.get("schema_version") not in {
        DISPATCH_LEDGER_SCHEMA,
        GUARDED_DISPATCH_LEDGER_SCHEMA,
    }:
        raise CollaborationRegistryError(
            "ledger was written by a newer Continuity build; upgrade this CLI/MCP before reading it"
        )
    # Sidecar metadata is forward-compatible: older alpha14 ledgers did not
    # contain report_policy or effect_requests.
    document.setdefault("effect_requests", [])
    for item in document.get("tasks", []):
        if isinstance(item, dict):
            item.setdefault("report_policy", "silent_until_stage_complete")
            item.setdefault("dependencies", [])
            item.setdefault("branch", None)
            item.setdefault("commit", None)
            item.setdefault("verification", None)
            item.setdefault("landed_commit", None)
            item.setdefault("released_in", None)
    if trusted:
        return document
    tasks = [_validate_task(item) for item in document.get("tasks", [])]
    ids = [item["task_id"] for item in tasks]
    if len(ids) != len(set(ids)):
        raise CollaborationRegistryError("task_id must be unique")
    effects = document.get("effect_requests", [])
    if not isinstance(effects, list):
        raise CollaborationRegistryError("effect_requests must be a list")
    effect_ids = []
    for effect in effects:
        if not isinstance(effect, dict) or set(effect) != {
            "schema_version",
            "effect_id",
            "task_id",
            "requested_by",
            "effect",
            "target",
            "reason",
            "evidence_refs",
            "status",
            "created_at",
            "updated_at",
        }:
            raise CollaborationRegistryError("effect request fields are invalid")
        if effect["schema_version"] != "context.effect-request/v1alpha1":
            raise CollaborationRegistryError("effect request schema is unsupported")
        if _ID_RE.fullmatch(effect["effect_id"]) is None:
            raise CollaborationRegistryError("effect_id is invalid")
        _text(effect["task_id"], "effect.task_id")
        if _ASSIGNEE_RE.fullmatch(effect["requested_by"]) is None:
            raise CollaborationRegistryError("effect.requested_by is invalid")
        _text(effect["effect"], "effect.effect", 256)
        _text(effect["target"], "effect.target", 2048)
        _text(effect["reason"], "effect.reason")
        _string_list(effect["evidence_refs"], "effect.evidence_refs")
        if effect["status"] not in _EFFECT_STATUS:
            raise CollaborationRegistryError("effect.status is invalid")
        _text(effect["created_at"], "effect.created_at", 64)
        _text(effect["updated_at"], "effect.updated_at", 64)
        effect_ids.append(effect["effect_id"])
    if len(effect_ids) != len(set(effect_ids)):
        raise CollaborationRegistryError("effect_id must be unique")
    if identity is not None and content is not None:
        if len(_LEDGER_MEMO) >= 2:
            _LEDGER_MEMO.clear()
        _LEDGER_MEMO[str(path)] = (identity, content)
    return document


def _note_lane(ledger, task):
    peers = [
        r["task_id"]
        for r in ledger["tasks"]
        if r["task_id"] != task["task_id"]
        and r["status"] == "active"
        and r["assignee"] == task["assignee"]
        and r["lane_id"] == task["lane_id"]
    ]
    existing = ledger.get("task_details", {}).get(task["task_id"], {})
    if task["status"] == "active" and peers:
        ledger.setdefault("task_details", {}).setdefault(task["task_id"], {})[
            "planning_warnings"
        ] = [
            f"{len(peers)} other active cards share this assignee/lane; coordinate checkout and build scope before execution"
        ]
    elif existing.get("planning_warnings"):
        existing["planning_warnings"] = []


def _set_body(details, body, actor=None):
    """Record a card body; every changed text is a new revision with its sha256."""
    text = _text(body, "task body", 32768, multiline=True)
    digest = hashlib.sha256(text.encode()).hexdigest()
    previous = details.get("body")
    if previous == text and details.get("body_sha256") == digest:
        return False
    revision = 1 if previous is None else int(details.get("body_revision") or 1) + 1
    details.update(
        body=text,
        body_sha256=digest,
        body_revision=revision,
        body_updated_at=_now(),
    )
    if actor is not None:
        details["body_updated_by"] = actor
    return True


def dependencies_ready(task, tasks):
    by_id = {item["task_id"]: item for item in tasks}
    return all(
        by_id.get(dependency_id, {}).get("status")
        in {"completed", "merged", "superseded"}
        for dependency_id in task.get("dependencies", [])
    )


def add_task(
    data_root: Path | str | None,
    *,
    project_id: str,
    task_id: str,
    lane_id: str,
    mode: str,
    priority: str,
    title: str,
    objective: str,
    next_action: str,
    exit_criteria: list[str],
    worktree: Path | str | None = None,
    allowed_files: list[str] | None = None,
    preauthorized_effects: list[str] | None = None,
    queued_effects: list[str] | None = None,
    dependencies: list[str] | None = None,
    assignee: str | None = None,
    report_policy: str = "silent_until_stage_complete",
    branch: str | None = None,
    commit: str | None = None,
    verification: str | None = None,
    landed_commit: str | None = None,
    released_in: str | None = None,
    body: str | None = None,
    actor: str | None = None,
    min_tier: str | None = None,
    deny_models: list[str] | None = None,
) -> dict[str, Any]:
    """Add a bounded task card without granting execution authority."""
    if actor is not None and (
        not isinstance(actor, str) or _ASSIGNEE_RE.fullmatch(actor) is None
    ):
        raise CollaborationRegistryError("actor must be a valid stable assignee")
    root = Path(data_root) if data_root is not None else default_data_root()
    task = {
        "schema_version": TASK_CARD_SCHEMA,
        "task_id": task_id,
        "lane_id": lane_id,
        "mode": mode,
        "priority": priority,
        "status": "active" if assignee is not None else "queued",
        "title": title,
        "objective": objective,
        "next_action": next_action,
        "assignee": assignee,
        "worktree": str(Path(worktree).resolve()) if worktree is not None else None,
        "report_policy": report_policy,
        "allowed_files": list(allowed_files or []),
        "preauthorized_effects": list(preauthorized_effects or []),
        "queued_effects": list(queued_effects or []),
        "dependencies": list(dependencies or []),
        "exit_criteria": list(exit_criteria),
        "blocked_reason": None,
        "result": None,
        "branch": branch,
        "commit": commit,
        "verification": verification,
        "landed_commit": landed_commit,
        "released_in": released_in,
        "created_at": _now(),
        "updated_at": _now(),
    }
    task = _validate_task(task)
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        if any(item["task_id"] == task_id for item in ledger["tasks"]):
            raise CollaborationRegistryError("task_id already exists")
        if task["status"] == "active" and not dependencies_ready(task, ledger["tasks"]):
            # Declare the dependency graph up front: the card waits, queued,
            # and the claim/activate/dispatch gates keep it from starting.
            task["status"] = "queued"
        _note_lane(ledger, task)
        ledger["tasks"].append(task)
        if actor is not None:
            ledger.setdefault("task_details", {}).setdefault(task_id, {})[
                "created_by"
            ] = actor
        if body is not None:
            _set_body(
                ledger.setdefault("task_details", {}).setdefault(task_id, {}),
                body,
                actor,
            )
        from .model_constraints import validate as validate_constraints

        constraints = validate_constraints(min_tier, deny_models)
        if constraints:
            ledger.setdefault("task_details", {}).setdefault(task_id, {})[
                "model_constraints"
            ] = constraints
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)
        _record_change(
            root,
            project_id,
            "task.added",
            task_id,
            status=task["status"],
            assignee=task["assignee"],
            actor=actor,
        )
    return task


def update_task(
    data_root: Path | str | None,
    *,
    project_id: str,
    task_id: str,
    status: str | None = None,
    next_action: str | None = None,
    blocked_reason: str | None = None,
    result: str | None = None,
    assignee: str | None = None,
    branch: str | None = None,
    commit: str | None = None,
    verification: str | None = None,
    landed_commit: str | None = None,
    released_in: str | None = None,
    worktree: Path | str | None = None,
    allowed_files: list[str] | None = None,
    dependencies: list[str] | None = None,
    report: dict | None = None,
    verification_data: dict | None = None,
    body: str | None = None,
    title: str | None = None,
    objective: str | None = None,
    priority: str | None = None,
    exit_criteria: list[str] | None = None,
    min_tier: str | None = None,
    deny_models: list[str] | None = None,
    clear_model_constraints: bool = False,
    unassign: bool = False,
    force: bool = False,
    force_reason: str | None = None,
    actor: str | None = None,
    controller_token: str | None = None,
) -> dict[str, Any]:
    """Update a card; local completion is ready-for-review, not acceptance."""
    if actor is not None and (
        not isinstance(actor, str) or _ASSIGNEE_RE.fullmatch(actor) is None
    ):
        raise CollaborationRegistryError("actor must be a valid stable assignee")
    if force and (not isinstance(force_reason, str) or not force_reason.strip()):
        raise CollaborationRegistryError("--force requires a recorded --force-reason")
    if unassign and assignee is not None:
        raise CollaborationRegistryError("choose --assignee or --unassign, not both")
    root = Path(data_root) if data_root is not None else default_data_root()
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        task = next(
            (item for item in ledger["tasks"] if item["task_id"] == task_id), None
        )
        if task is None:
            raise CollaborationRegistryError("task_id does not exist")
        details = ledger.setdefault("task_details", {}).setdefault(task_id, {})
        previous_assignee = task["assignee"]
        if unassign:
            assignee = None
        reassigning = unassign or (
            assignee is not None and assignee != previous_assignee
        )
        controller_can_reassign = False
        if reassigning and previous_assignee is not None and not force:
            binding = ledger.get("execution_bindings", {}).get(task_id, {})
            from .controller_lease import active_owner

            controller_can_reassign = (
                actor is not None
                and active_owner(data_root, project_id) == actor
            )
            if controller_can_reassign:
                from .controller_lease import _require
                from .project_context import _load as load_context, _path as context_path

                _require(load_context(context_path(root, project_id)), actor, controller_token)
            if controller_can_reassign and (
                not isinstance(force_reason, str) or not force_reason.strip()
            ):
                raise CollaborationRegistryError(
                    "active controller reassignment requires --reason so the ownership transfer is auditable"
                )
            creator_can_reassign = (
                actor is not None
                and details.get("created_by") == actor
                and details.get("claimed_by") != previous_assignee
                and task["status"] != "dispatched"
            )
            if binding.get("status") == "running" or not (
                actor == previous_assignee
                or creator_can_reassign
                or controller_can_reassign
            ):
                raise CollaborationRegistryError(
                    "task is already assigned to another session; use the current "
                    "owner, recorded creator, or active controller-lease holder to "
                    "reassign an unclaimed card, "
                    "declaring the caller with --actor; "
                    "a running attempt requires explicit --force --force-reason. "
                    f"If you are the recorded owner, use --actor {previous_assignee} --assignee {assignee}."
                )
        status_supplied = status is not None
        if not status_supplied:
            status = task["status"]
        if status == "accepted":
            raise CollaborationRegistryError(
                "acceptance requires project review; submit ready-for-review instead"
            )
        if status not in _TASK_STATUS:
            raise CollaborationRegistryError(
                f"task status {status!r} is invalid; valid statuses: {', '.join(sorted(_TASK_STATUS))}"
            )
        candidate_dependencies = (
            task["dependencies"] if dependencies is None else dependencies
        )
        if (
            status == "active"
            and not dependencies_ready(
                {**task, "dependencies": candidate_dependencies},
                ledger["tasks"],
            )
            and not force
        ):
            raise CollaborationRegistryError(
                "task dependencies are not complete; set --status queued to wait for them, resolve them, or use an audited force override"
            )
        for field, value in (
            ("branch", branch),
            ("commit", commit),
            ("verification", verification),
        ):
            if value is not None:
                task[field] = value
        for field, value in (
            ("landed_commit", landed_commit),
            ("released_in", released_in),
        ):
            if value is not None:
                task[field] = value
        from .delivery_records import verification_record, merge_gate

        if verification_data is not None:
            verified = verification_record(verification_data, task.get("commit"))
            details["verification_records"] = (
                details.get("verification_records", []) + [verified]
            )[-32:]
        if report is not None:
            import hashlib

            content = report.get("text", "")
            if not isinstance(content, str) or not 1 <= len(content.encode()) <= 65536:
                raise CollaborationRegistryError("report must contain 1–65536 bytes")
            details["report"] = {
                "text": content,
                "bytes": len(content.encode()),
                "sha256": hashlib.sha256(content.encode()).hexdigest(),
                "recorded_at": _now(),
                "commit": task.get("commit"),
            }
        body_changed = body is not None and _set_body(details, body, actor)
        for field, value in (
            ("title", title),
            ("objective", objective),
            ("priority", priority),
            ("exit_criteria", exit_criteria),
        ):
            if value is not None:
                task[field] = value
        if clear_model_constraints:
            details.pop("model_constraints", None)
        if min_tier is not None or deny_models:
            from .model_constraints import validate as validate_constraints

            current = details.get("model_constraints") or {}
            merged = validate_constraints(
                min_tier or current.get("min_tier"),
                deny_models if deny_models else current.get("deny_models"),
            )
            if merged:
                details["model_constraints"] = merged
        if status == "merged":
            details["evidence_gaps"] = merge_gate(task, details)
        if force:
            details.setdefault("overrides", []).append(
                {"reason": _text(force_reason, "force_reason", 2048), "at": _now()}
            )
        if assignee is not None:
            task["assignee"] = assignee
        if reassigning:
            details.pop("claimed_by", None)
            details["reassignments"] = (
                details.get("reassignments", [])
                + [
                    {
                        "from": previous_assignee,
                        "to": None if unassign else assignee,
                        "actor": actor,
                        "at": _now(),
                        "forced": force,
                        "reason": force_reason,
                        "authority": (
                            "controller-lease"
                            if controller_can_reassign
                            else "recorded-owner-or-creator"
                        ),
                    }
                ]
            )[-32:]
        if (
            status_supplied
            and status == "active"
            and actor == task["assignee"]
            and actor is not None
        ):
            details["claimed_by"] = actor
        task["status"] = status
        if next_action is not None:
            task["next_action"] = _text(next_action, "next_action", 1024)
        if worktree is not None:
            task["worktree"] = str(Path(worktree).resolve())
        if allowed_files is not None:
            task["allowed_files"] = allowed_files
        if dependencies is not None:
            task["dependencies"] = dependencies
        if blocked_reason is not None:
            task["blocked_reason"] = blocked_reason
        if result is not None:
            task["result"] = result
        if status_supplied and status != "blocked":
            task["blocked_reason"] = None
        if unassign:
            task["assignee"] = None
            details.pop("claimed_by", None)
        elif status_supplied and status == "queued":
            details.pop("claimed_by", None)
        task["updated_at"] = _now()
        _validate_task(task)
        _note_lane(ledger, task)
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)
        _record_change(
            root,
            project_id,
            "task.updated",
            task_id,
            status=status,
            assignee=task["assignee"],
            actor=actor,
            previous_assignee=previous_assignee,
            **(
                {
                    "body_revision": details["body_revision"],
                    "body_sha256": details["body_sha256"],
                }
                if body_changed
                else {}
            ),
        )
        return task


def claim_task(
    data_root: Path | str | None,
    *,
    project_id: str,
    task_id: str,
    assignee: str,
    worktree: Path | str | None = None,
) -> dict[str, Any]:
    """Assign one queued card to a session without granting review authority."""
    root = Path(data_root) if data_root is not None else default_data_root()
    if _ASSIGNEE_RE.fullmatch(assignee) is None:
        raise CollaborationRegistryError("assignee is invalid")
    resolved_worktree = str(Path(worktree).resolve()) if worktree is not None else None
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        task = next(
            (item for item in ledger["tasks"] if item["task_id"] == task_id), None
        )
        if task is None:
            raise CollaborationRegistryError("task_id does not exist")
        if task["status"] not in {"queued", "active"}:
            raise CollaborationRegistryError(
                "only queued or active tasks can be claimed"
            )
        if task["assignee"] not in (None, assignee):
            raise CollaborationRegistryError(
                "task is already assigned to another session"
            )
        if task["status"] == "queued" and not dependencies_ready(task, ledger["tasks"]):
            raise CollaborationRegistryError(
                "task dependencies are not complete; claim after dependency cards are completed or merged"
            )
        task["status"] = "active"
        task["assignee"] = assignee
        ledger.setdefault("task_details", {}).setdefault(task_id, {})["claimed_by"] = (
            assignee
        )
        if resolved_worktree is not None:
            task["worktree"] = resolved_worktree
        task["updated_at"] = _now()
        _validate_task(task)
        _note_lane(ledger, task)
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)
        _record_change(
            root,
            project_id,
            "task.claimed",
            task_id,
            status="active",
            assignee=assignee,
        )
        return task


def list_tasks(
    data_root: Path | str | None,
    *,
    project_id: str,
    status: str | None = None,
    assignee: str | None = None,
    lane_id: str | None = None,
    updated_after: str | None = None,
    query: str | None = None,
    dependencies_ready: bool = False,
    max_results: int | None = None,
) -> list[dict[str, Any]]:
    root = Path(data_root) if data_root is not None else default_data_root()
    tasks = _load_ledger(root, project_id)["tasks"]
    if status:
        tasks = [item for item in tasks if item["status"] == status]
    if assignee:
        tasks = [item for item in tasks if item["assignee"] == assignee]
    if lane_id:
        tasks = [item for item in tasks if item["lane_id"] == lane_id]
    if updated_after:
        tasks = [item for item in tasks if item["updated_at"] >= updated_after]
    if query:
        needle = query.casefold()
        tasks = [
            item
            for item in tasks
            if needle in json.dumps(item, ensure_ascii=False).casefold()
        ]
    if dependencies_ready:
        all_tasks = _load_ledger(root, project_id)["tasks"]
        by_id = {item["task_id"]: item for item in all_tasks}
        tasks = [
            item
            for item in tasks
            if all(
                by_id.get(dependency_id, {}).get("status") in {"completed", "merged"}
                for dependency_id in item["dependencies"]
            )
        ]
    if dependencies_ready or any(
        value is not None
        for value in (
            status,
            assignee,
            lane_id,
            updated_after,
            query,
            max_results,
        )
    ):
        tasks.sort(key=lambda item: (item["updated_at"], item["task_id"]), reverse=True)
    return tasks[:max_results] if max_results is not None else tasks


def request_effect(
    data_root: Path | str | None,
    *,
    project_id: str,
    effect_id: str,
    task_id: str,
    requested_by: str,
    effect: str,
    target: str,
    reason: str,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    """Queue an external effect for review without executing it."""
    root = Path(data_root) if data_root is not None else default_data_root()
    if _ID_RE.fullmatch(effect_id) is None:
        raise CollaborationRegistryError("effect_id is invalid")
    if _ASSIGNEE_RE.fullmatch(requested_by) is None:
        raise CollaborationRegistryError("requested_by is invalid")
    document = {
        "schema_version": "context.effect-request/v1alpha1",
        "effect_id": effect_id,
        "task_id": _text(task_id, "task_id"),
        "requested_by": requested_by,
        "effect": _text(effect, "effect", 256),
        "target": _text(target, "target", 2048),
        "reason": _text(reason, "reason"),
        "evidence_refs": _string_list(evidence_refs or [], "evidence_refs"),
        "status": "queued",
        "created_at": _now(),
        "updated_at": _now(),
    }
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        if any(item["effect_id"] == effect_id for item in ledger["effect_requests"]):
            raise CollaborationRegistryError("effect_id already exists")
        ledger["effect_requests"].append(document)
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)
    return document


def list_effects(
    data_root: Path | str | None,
    *,
    project_id: str,
) -> list[dict[str, Any]]:
    root = Path(data_root) if data_root is not None else default_data_root()
    return _load_ledger(root, project_id)["effect_requests"]


def update_effect(
    data_root: Path | str | None,
    *,
    project_id: str,
    effect_id: str,
    status: str,
) -> dict[str, Any]:
    """Update a request receipt; execution remains outside this metadata ledger."""
    if status not in _EFFECT_STATUS:
        raise CollaborationRegistryError("effect status is invalid")
    root = Path(data_root) if data_root is not None else default_data_root()
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        effect = next(
            (
                item
                for item in ledger["effect_requests"]
                if item["effect_id"] == effect_id
            ),
            None,
        )
        if effect is None:
            raise CollaborationRegistryError("effect_id does not exist")
        effect["status"] = status
        effect["updated_at"] = _now()
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)
        return effect


def next_task(
    data_root: Path | str | None,
    *,
    project_id: str,
    assignee: str | None = None,
) -> dict[str, Any] | None:
    """Return an assignee's active task or the highest-priority queued card."""
    root = Path(data_root) if data_root is not None else default_data_root()
    ledger = _load_ledger(root, project_id)
    order = {"p0": 0, "p1": 1, "p2": 2}
    if assignee is not None:
        active = [
            item
            for item in ledger["tasks"]
            if item["status"] in {"active", "dispatched", "blocked"}
            and item["assignee"] == assignee
        ]
        if active:
            return min(
                active, key=lambda item: (order[item["priority"]], item["task_id"])
            )
    queued = [
        item
        for item in ledger["tasks"]
        if item["status"] == "queued" and dependencies_ready(item, ledger["tasks"])
    ]
    if not queued:
        return None
    return min(queued, key=lambda item: (order[item["priority"]], item["task_id"]))
