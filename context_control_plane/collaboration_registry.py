"""Generic, non-blocking project and task collaboration metadata."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


PROJECT_REGISTRATION_SCHEMA = "context.project-registration/v1alpha1"
DISPATCH_LEDGER_SCHEMA = "context.dispatch-ledger/v1alpha1"
TASK_CARD_SCHEMA = "context.task-card/v1alpha1"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_ASSIGNEE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,255}$")
_PRIORITY = {"p0", "p1", "p2"}
_MODE = {"scout", "patch", "integration", "cleanup", "infra", "decision"}
_TASK_STATUS = {
    "queued",
    "active",
    "blocked",
    "ready-for-review",
    "accepted",
    "rejected",
    "superseded",
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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise CollaborationRegistryError(f"collaboration document is unreadable: {exc}") from exc
    if not content.strip():
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


def _text(value: Any, field: str, limit: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > limit
        or "\n" in value
        or "\r" in value
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
        if (
            document.get("schema_version") != PROJECT_REGISTRATION_SCHEMA
            or not isinstance(document.get("project_id"), str)
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
        "exit_criteria",
        "blocked_reason",
        "result",
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
    if task["mode"] not in _MODE or task["priority"] not in _PRIORITY:
        raise CollaborationRegistryError("task mode or priority is invalid")
    if task["status"] not in _TASK_STATUS:
        raise CollaborationRegistryError("task status is invalid")
    _text(task["title"], "title", 512)
    _text(task["objective"], "objective")
    _text(task["next_action"], "next_action", 1024)
    if task["assignee"] is not None and _ASSIGNEE_RE.fullmatch(task["assignee"]) is None:
        raise CollaborationRegistryError("assignee is invalid")
    if task["status"] == "active" and task["assignee"] is None:
        raise CollaborationRegistryError("active task requires assignee")
    if task["worktree"] is not None:
        _absolute_path(task["worktree"], "worktree")
    if task["report_policy"] not in _REPORT_POLICY:
        raise CollaborationRegistryError("report_policy is invalid")
    for field in (
        "allowed_files",
        "preauthorized_effects",
        "queued_effects",
        "exit_criteria",
    ):
        _string_list(task[field], field)
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
    _text(task["created_at"], "created_at", 64)
    _text(task["updated_at"], "updated_at", 64)
    return task


def _ledger_path(data_root: Path, project_id: str) -> Path:
    return _project_dir(data_root, project_id) / "dispatch.json"


def _empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": DISPATCH_LEDGER_SCHEMA,
        "tasks": [],
        "effect_requests": [],
        "updated_at": _now(),
    }


def _load_ledger(data_root: Path, project_id: str) -> dict[str, Any]:
    document = _load(_ledger_path(data_root, project_id)) or _empty_ledger()
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != DISPATCH_LEDGER_SCHEMA
    ):
        raise CollaborationRegistryError("dispatch ledger schema is unsupported")
    # Sidecar metadata is forward-compatible: older alpha14 ledgers did not
    # contain report_policy or effect_requests.
    document.setdefault("effect_requests", [])
    for item in document.get("tasks", []):
        if isinstance(item, dict):
            item.setdefault("report_policy", "silent_until_stage_complete")
    tasks = [_validate_task(item) for item in document.get("tasks", [])]
    ids = [item["task_id"] for item in tasks]
    if len(ids) != len(set(ids)):
        raise CollaborationRegistryError("task_id must be unique")
    active_assignees = [
        item["assignee"]
        for item in tasks
        if item["status"] == "active" and item["assignee"] is not None
    ]
    if len(active_assignees) != len(set(active_assignees)):
        raise CollaborationRegistryError("an assignee may hold only one active task")
    effects = document.get("effect_requests", [])
    if not isinstance(effects, list):
        raise CollaborationRegistryError("effect_requests must be a list")
    effect_ids = []
    for effect in effects:
        if (
            not isinstance(effect, dict)
            or set(effect)
            != {
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
            }
        ):
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
    return document


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
    assignee: str | None = None,
    report_policy: str = "silent_until_stage_complete",
) -> dict[str, Any]:
    """Add a bounded task card without granting execution authority."""
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
        "exit_criteria": list(exit_criteria),
        "blocked_reason": None,
        "result": None,
        "created_at": _now(),
        "updated_at": _now(),
    }
    task = _validate_task(task)
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        if any(item["task_id"] == task_id for item in ledger["tasks"]):
            raise CollaborationRegistryError("task_id already exists")
        if task["status"] == "active" and any(
            item["status"] == "active" and item["assignee"] == task["assignee"]
            for item in ledger["tasks"]
        ):
            raise CollaborationRegistryError("an assignee may hold only one active task")
        ledger["tasks"].append(task)
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)
    return task


def update_task(
    data_root: Path | str | None,
    *,
    project_id: str,
    task_id: str,
    status: str,
    next_action: str | None = None,
    blocked_reason: str | None = None,
    result: str | None = None,
) -> dict[str, Any]:
    """Update a card; local completion is ready-for-review, not acceptance."""
    root = Path(data_root) if data_root is not None else default_data_root()
    path = _ledger_path(root, project_id)
    with _locked(path):
        ledger = _load_ledger(root, project_id)
        task = next(
            (item for item in ledger["tasks"] if item["task_id"] == task_id), None
        )
        if task is None:
            raise CollaborationRegistryError("task_id does not exist")
        if status == "accepted":
            raise CollaborationRegistryError(
                "acceptance requires project review; submit ready-for-review instead"
            )
        if status not in _TASK_STATUS:
            raise CollaborationRegistryError("task status is invalid")
        task["status"] = status
        if next_action is not None:
            task["next_action"] = _text(next_action, "next_action", 1024)
        task["blocked_reason"] = blocked_reason
        task["result"] = result
        if status != "blocked":
            task["blocked_reason"] = None
        if status not in {"ready-for-review", "rejected", "superseded"}:
            task["result"] = None
        if status == "queued":
            task["assignee"] = None
        task["updated_at"] = _now()
        _validate_task(task)
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)
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
            raise CollaborationRegistryError("only queued or active tasks can be claimed")
        if task["status"] == "active" and task["assignee"] != assignee:
            raise CollaborationRegistryError("task is already assigned to another session")
        if any(
            item["status"] == "active"
            and item["assignee"] == assignee
            and item["task_id"] != task_id
            for item in ledger["tasks"]
        ):
            raise CollaborationRegistryError("an assignee may hold only one active task")
        task["status"] = "active"
        task["assignee"] = assignee
        if resolved_worktree is not None:
            task["worktree"] = resolved_worktree
        task["updated_at"] = _now()
        _validate_task(task)
        ledger["updated_at"] = _now()
        _atomic_write(path, ledger)
        return task


def list_tasks(
    data_root: Path | str | None,
    *,
    project_id: str,
) -> list[dict[str, Any]]:
    root = Path(data_root) if data_root is not None else default_data_root()
    return _load_ledger(root, project_id)["tasks"]


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
            (item for item in ledger["effect_requests"] if item["effect_id"] == effect_id),
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
            if item["status"] == "active" and item["assignee"] == assignee
        ]
        if active:
            return min(
                active, key=lambda item: (order[item["priority"]], item["task_id"])
            )
    queued = [item for item in ledger["tasks"] if item["status"] == "queued"]
    if not queued:
        return None
    return min(queued, key=lambda item: (order[item["priority"]], item["task_id"]))
