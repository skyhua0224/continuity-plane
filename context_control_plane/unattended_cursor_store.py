"""Durable CAS storage for authority-free unattended campaign cursors."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .unattended_receipts import (
    UnattendedReceiptError,
    validate_condition_decision,
    validate_unattended_campaign_receipt,
    validate_unattended_dispatch_step,
)

CURSOR_SCHEMA_VERSION = "context.unattended-campaign-cursor/v1alpha1"

_FIELDS = {
    "schema_version",
    "campaign_run_id",
    "project_id",
    "profile_id",
    "governance_revision",
    "start_project_revision",
    "current_project_revision",
    "cursor_revision",
    "phase",
    "next_action",
    "selection",
    "completed_steps",
    "terminal_receipt",
    "state_write_authority",
    "completion_authority",
    "provider_authority",
    "external_effect_authority",
    "cursor_sha256",
}
_PHASES = {
    "selecting",
    "prepared",
    "claimed",
    "composed",
    "executed",
    "verified",
    "verification_failed",
    "completed",
    "blocked",
    "closed",
}
_NEXT_ACTIONS = {
    "select",
    "claim",
    "compose",
    "execute",
    "verify",
    "complete",
    "block_and_release",
    "record_step",
    "terminal",
}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SELECTION_FIELDS = {
    "selection_id",
    "attempt_id",
    "attempt_no",
    "attempt_budget",
    "project_revision_before",
    "work_id",
    "work_revision",
    "scope_owners",
    "obligation_id",
    "obligation_revision",
    "verification_profile_ref",
    "condition_decision",
    "claim_id",
    "request_ids",
    "claim",
    "packet",
    "execution",
    "verification",
    "completion",
    "block",
}
_REQUEST_ID_FIELDS = {"claim", "packet", "execute", "verify", "complete", "block"}
_PHASE_NEXT_ACTION = {
    "selecting": "select",
    "prepared": "claim",
    "claimed": "compose",
    "composed": "execute",
    "executed": "verify",
    "verified": "complete",
    "verification_failed": "block_and_release",
    "completed": "record_step",
    "blocked": "terminal",
    "closed": "terminal",
}


class UnattendedCursorError(ValueError):
    """Base error for unattended cursor validation or persistence."""


class UnattendedCursorConflict(UnattendedCursorError):
    """The expected cursor revision does not match durable state."""


class UnattendedCursorIntegrityError(UnattendedCursorError):
    """Persisted cursor bytes do not match their integrity digest."""


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
        raise UnattendedCursorError("cursor is not canonical JSON") from exc


def _digest(cursor: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in cursor.items() if key != "cursor_sha256"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise UnattendedCursorError(f"{field} is invalid")
    return value


def _validate_selection(selection: Any, phase: str) -> dict[str, Any]:
    if not isinstance(selection, dict) or set(selection) != _SELECTION_FIELDS:
        raise UnattendedCursorError("campaign cursor selection fields are invalid")
    normalized = copy.deepcopy(selection)
    for field in ("selection_id", "attempt_id", "work_id", "obligation_id", "claim_id"):
        _identifier(normalized[field], f"selection.{field}")
    for field in (
        "attempt_no",
        "attempt_budget",
        "work_revision",
        "obligation_revision",
    ):
        if type(normalized[field]) is not int or normalized[field] < 1:
            raise UnattendedCursorError(f"selection.{field} is invalid")
    if (
        type(normalized["project_revision_before"]) is not int
        or normalized["project_revision_before"] < 0
        or normalized["attempt_no"] > normalized["attempt_budget"]
    ):
        raise UnattendedCursorError("campaign cursor selection revisions are invalid")
    scopes = normalized["scope_owners"]
    if not isinstance(scopes, list) or not scopes:
        raise UnattendedCursorError("campaign cursor selection scopes are invalid")
    for scope in scopes:
        if not isinstance(scope, dict) or set(scope) != {"scope_kind", "scope_ref"}:
            raise UnattendedCursorError("campaign cursor selection scope is invalid")
        _identifier(scope["scope_kind"], "selection.scope_kind")
        if not isinstance(scope["scope_ref"], str) or not scope["scope_ref"]:
            raise UnattendedCursorError("campaign cursor selection scope_ref is invalid")
    if not isinstance(normalized["verification_profile_ref"], str) or not normalized[
        "verification_profile_ref"
    ]:
        raise UnattendedCursorError("selection.verification_profile_ref is invalid")
    if normalized["condition_decision"] is not None:
        try:
            validate_condition_decision(normalized["condition_decision"])
        except UnattendedReceiptError as exc:
            raise UnattendedCursorError("selection condition decision is invalid") from exc
    request_ids = normalized["request_ids"]
    if not isinstance(request_ids, dict) or set(request_ids) != _REQUEST_ID_FIELDS:
        raise UnattendedCursorError("campaign cursor request IDs are invalid")
    for field, request_id in request_ids.items():
        _identifier(request_id, f"selection.request_ids.{field}")
    receipts = ("claim", "packet", "execution", "verification", "completion", "block")
    for field in receipts:
        if normalized[field] is not None and (
            not isinstance(normalized[field], dict) or not normalized[field]
        ):
            raise UnattendedCursorError(f"selection.{field} is invalid")
    required_by_phase = {
        "prepared": (),
        "claimed": ("claim",),
        "composed": ("claim", "packet"),
        "executed": ("claim", "packet", "execution"),
        "verified": ("claim", "packet", "execution", "verification"),
        "verification_failed": ("claim", "packet", "execution", "verification"),
        "completed": ("claim", "packet", "execution", "verification", "completion"),
        "blocked": ("claim", "packet", "execution", "verification", "block"),
    }
    required = set(required_by_phase.get(phase, ()))
    if any(normalized[field] is None for field in required):
        raise UnattendedCursorError("campaign cursor selection phase is incomplete")
    if any(normalized[field] is not None for field in set(receipts) - required):
        raise UnattendedCursorError("campaign cursor selection phase is inconsistent")
    return normalized


def validate_campaign_cursor(value: Any) -> dict[str, Any]:
    """Validate and return an isolated campaign cursor."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise UnattendedCursorError("campaign cursor fields are invalid")
    cursor = copy.deepcopy(value)
    if cursor["schema_version"] != CURSOR_SCHEMA_VERSION:
        raise UnattendedCursorError("campaign cursor schema_version is unsupported")
    for field in ("campaign_run_id", "project_id", "profile_id"):
        _identifier(cursor[field], field)
    for field in (
        "governance_revision",
        "start_project_revision",
        "current_project_revision",
        "cursor_revision",
    ):
        if type(cursor[field]) is not int or cursor[field] < 0:
            raise UnattendedCursorError(f"{field} is invalid")
    if cursor["governance_revision"] < 1:
        raise UnattendedCursorError("governance_revision is invalid")
    if cursor["phase"] not in _PHASES or cursor["next_action"] not in _NEXT_ACTIONS:
        raise UnattendedCursorError("campaign cursor phase is invalid")
    if cursor["next_action"] != _PHASE_NEXT_ACTION[cursor["phase"]]:
        raise UnattendedCursorError("campaign cursor phase and next_action are inconsistent")
    if cursor["selection"] is not None:
        _validate_selection(cursor["selection"], cursor["phase"])
    if not isinstance(cursor["completed_steps"], list):
        raise UnattendedCursorError("campaign cursor completed_steps are invalid")
    try:
        for step in cursor["completed_steps"]:
            validate_unattended_dispatch_step(step)
        if cursor["terminal_receipt"] is not None:
            terminal = validate_unattended_campaign_receipt(cursor["terminal_receipt"])
            if (
                terminal["campaign_run_id"] != cursor["campaign_run_id"]
                or terminal["project_id"] != cursor["project_id"]
                or terminal["profile_id"] != cursor["profile_id"]
                or terminal["governance_revision"] != cursor["governance_revision"]
                or terminal["start_project_revision"] != cursor["start_project_revision"]
                or terminal["end_project_revision"] != cursor["current_project_revision"]
                or terminal["steps"] != cursor["completed_steps"]
            ):
                raise UnattendedCursorError("terminal receipt binding is invalid")
    except UnattendedReceiptError as exc:
        raise UnattendedCursorError("campaign cursor terminal receipt is invalid") from exc
    if (
        cursor["state_write_authority"] is not False
        or cursor["completion_authority"] is not False
        or cursor["provider_authority"] != 0
        or cursor["external_effect_authority"] != 0
    ):
        raise UnattendedCursorError("campaign cursor cannot claim authority")
    if (
        not isinstance(cursor["cursor_sha256"], str)
        or _SHA256_RE.fullmatch(cursor["cursor_sha256"]) is None
        or cursor["cursor_sha256"] != _digest(cursor)
    ):
        raise UnattendedCursorIntegrityError("campaign cursor digest mismatch")
    if cursor["phase"] in {"selecting", "closed"} and cursor["selection"] is not None:
        raise UnattendedCursorError(f"{cursor['phase']} cursor cannot retain a selection")
    if cursor["phase"] not in {"selecting", "closed"} and cursor["selection"] is None:
        raise UnattendedCursorError(f"{cursor['phase']} cursor requires a selection")
    if cursor["phase"] == "closed":
        if cursor["next_action"] != "terminal" or cursor["terminal_receipt"] is None:
            raise UnattendedCursorError("closed cursor requires a terminal receipt")
    elif cursor["terminal_receipt"] is not None:
        raise UnattendedCursorError("open cursor cannot contain a terminal receipt")
    return cursor


def build_campaign_cursor(
    *,
    campaign_run_id: str,
    project_id: str,
    profile_id: str,
    governance_revision: int,
    start_project_revision: int,
) -> dict[str, Any]:
    """Build the initial authority-free campaign cursor."""
    cursor = {
        "schema_version": CURSOR_SCHEMA_VERSION,
        "campaign_run_id": _identifier(campaign_run_id, "campaign_run_id"),
        "project_id": _identifier(project_id, "project_id"),
        "profile_id": _identifier(profile_id, "profile_id"),
        "governance_revision": governance_revision,
        "start_project_revision": start_project_revision,
        "current_project_revision": start_project_revision,
        "cursor_revision": 0,
        "phase": "selecting",
        "next_action": "select",
        "selection": None,
        "completed_steps": [],
        "terminal_receipt": None,
        "state_write_authority": False,
        "completion_authority": False,
        "provider_authority": 0,
        "external_effect_authority": 0,
        "cursor_sha256": "",
    }
    cursor["cursor_sha256"] = _digest(cursor)
    return validate_campaign_cursor(cursor)


def evolve_campaign_cursor(
    cursor: dict[str, Any], **changes: Any
) -> dict[str, Any]:
    """Return a digest-bound cursor candidate for the next CAS revision."""
    current = validate_campaign_cursor(cursor)
    immutable = {
        "schema_version",
        "campaign_run_id",
        "project_id",
        "profile_id",
        "governance_revision",
        "start_project_revision",
        "cursor_revision",
        "cursor_sha256",
        "state_write_authority",
        "completion_authority",
        "provider_authority",
        "external_effect_authority",
    }
    if not set(changes).issubset(_FIELDS - immutable):
        raise UnattendedCursorError("campaign cursor change fields are invalid")
    candidate = copy.deepcopy(current)
    candidate.update(copy.deepcopy(changes))
    candidate["cursor_sha256"] = ""
    candidate["cursor_sha256"] = _digest(candidate)
    return validate_campaign_cursor(candidate)


def _semantic_bytes(cursor: dict[str, Any]) -> bytes:
    semantic = {
        key: value
        for key, value in cursor.items()
        if key not in {"cursor_revision", "cursor_sha256"}
    }
    return _canonical(semantic)


def _advance(cursor: dict[str, Any], revision: int) -> dict[str, Any]:
    advanced = copy.deepcopy(cursor)
    advanced["cursor_revision"] = revision
    advanced["cursor_sha256"] = ""
    advanced["cursor_sha256"] = _digest(advanced)
    return validate_campaign_cursor(advanced)


class InMemoryUnattendedCursorStore:
    """Reference CAS store used by local tests and embedded callers."""

    def __init__(self) -> None:
        self._cursors: dict[str, dict[str, Any]] = {}

    def read(self, campaign_run_id: str) -> dict[str, Any] | None:
        campaign_run_id = _identifier(campaign_run_id, "campaign_run_id")
        cursor = self._cursors.get(campaign_run_id)
        return copy.deepcopy(cursor) if cursor is not None else None

    def compare_and_set(
        self,
        campaign_run_id: str,
        *,
        expected_cursor_revision: int | None,
        cursor: dict[str, Any],
    ) -> dict[str, Any]:
        campaign_run_id = _identifier(campaign_run_id, "campaign_run_id")
        proposed = validate_campaign_cursor(cursor)
        if proposed["campaign_run_id"] != campaign_run_id:
            raise UnattendedCursorConflict("campaign cursor identity mismatch")
        current = self._cursors.get(campaign_run_id)
        if current is None:
            if expected_cursor_revision is not None:
                raise UnattendedCursorConflict("campaign cursor does not exist")
            committed = _advance(proposed, 1)
            self._cursors[campaign_run_id] = committed
            return copy.deepcopy(committed)
        if expected_cursor_revision is None:
            if _semantic_bytes(current) == _semantic_bytes(proposed):
                return copy.deepcopy(current)
            raise UnattendedCursorConflict("campaign cursor already exists")
        if current["cursor_revision"] != expected_cursor_revision:
            raise UnattendedCursorConflict("stale campaign cursor revision")
        committed = _advance(proposed, expected_cursor_revision + 1)
        self._cursors[campaign_run_id] = committed
        return copy.deepcopy(committed)


class SQLiteUnattendedCursorStore:
    """SQLite-backed local durable cursor store with revision CAS."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS unattended_campaign_cursors (
                    campaign_run_id TEXT PRIMARY KEY,
                    cursor_revision INTEGER NOT NULL,
                    cursor_sha256 TEXT NOT NULL,
                    cursor_json TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row) -> dict[str, Any]:
        try:
            cursor = json.loads(row["cursor_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise UnattendedCursorIntegrityError("stored cursor JSON is invalid") from exc
        try:
            validated = validate_campaign_cursor(cursor)
        except UnattendedCursorError as exc:
            raise UnattendedCursorIntegrityError(
                "stored cursor semantic validation failed"
            ) from exc
        if (
            validated["cursor_revision"] != row["cursor_revision"]
            or validated["cursor_sha256"] != row["cursor_sha256"]
        ):
            raise UnattendedCursorIntegrityError("stored cursor metadata mismatch")
        return validated

    def read(self, campaign_run_id: str) -> dict[str, Any] | None:
        campaign_run_id = _identifier(campaign_run_id, "campaign_run_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT cursor_revision, cursor_sha256, cursor_json "
                "FROM unattended_campaign_cursors WHERE campaign_run_id = ?",
                (campaign_run_id,),
            ).fetchone()
        return None if row is None else self._decode(row)

    def compare_and_set(
        self,
        campaign_run_id: str,
        *,
        expected_cursor_revision: int | None,
        cursor: dict[str, Any],
    ) -> dict[str, Any]:
        campaign_run_id = _identifier(campaign_run_id, "campaign_run_id")
        proposed = validate_campaign_cursor(cursor)
        if proposed["campaign_run_id"] != campaign_run_id:
            raise UnattendedCursorConflict("campaign cursor identity mismatch")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT cursor_revision, cursor_sha256, cursor_json "
                    "FROM unattended_campaign_cursors WHERE campaign_run_id = ?",
                    (campaign_run_id,),
                ).fetchone()
                if row is None:
                    if expected_cursor_revision is not None:
                        raise UnattendedCursorConflict("campaign cursor does not exist")
                    committed = _advance(proposed, 1)
                    connection.execute(
                        "INSERT INTO unattended_campaign_cursors "
                        "(campaign_run_id, cursor_revision, cursor_sha256, cursor_json) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            campaign_run_id,
                            committed["cursor_revision"],
                            committed["cursor_sha256"],
                            _canonical(committed).decode("utf-8"),
                        ),
                    )
                else:
                    current = self._decode(row)
                    if expected_cursor_revision is None:
                        if _semantic_bytes(current) == _semantic_bytes(proposed):
                            connection.execute("COMMIT")
                            return current
                        raise UnattendedCursorConflict("campaign cursor already exists")
                    if current["cursor_revision"] != expected_cursor_revision:
                        raise UnattendedCursorConflict("stale campaign cursor revision")
                    committed = _advance(proposed, expected_cursor_revision + 1)
                    updated = connection.execute(
                        "UPDATE unattended_campaign_cursors SET "
                        "cursor_revision = ?, cursor_sha256 = ?, cursor_json = ? "
                        "WHERE campaign_run_id = ? AND cursor_revision = ?",
                        (
                            committed["cursor_revision"],
                            committed["cursor_sha256"],
                            _canonical(committed).decode("utf-8"),
                            campaign_run_id,
                            expected_cursor_revision,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise UnattendedCursorConflict("stale campaign cursor revision")
                connection.execute("COMMIT")
                return committed
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def close(self) -> None:
        """Connections are operation-scoped; close is an API symmetry no-op."""


__all__ = [
    "CURSOR_SCHEMA_VERSION",
    "InMemoryUnattendedCursorStore",
    "SQLiteUnattendedCursorStore",
    "UnattendedCursorConflict",
    "UnattendedCursorError",
    "UnattendedCursorIntegrityError",
    "build_campaign_cursor",
    "evolve_campaign_cursor",
    "validate_campaign_cursor",
]
