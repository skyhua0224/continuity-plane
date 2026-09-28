"""Local-first authorization policy and append-only audit reference core."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .state_mcp import RequestContext

AUTHORIZATION_POLICY_SCHEMA_VERSION = "context.authorization-policy/v1alpha1"
AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION = (
    "context.authorization-audit-event/v1alpha1"
)
GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION = (
    "context.authorization-audit-event/v2alpha1"
)

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_POLICY_FIELDS = {
    "schema_version",
    "policy_id",
    "policy_revision",
    "issued_at",
    "expires_at",
    "projects",
    "grants",
}
_PROJECT_FIELDS = {"tenant_id", "project_id", "status"}
_GRANT_FIELDS = {
    "grant_id",
    "authorization_ref",
    "subject_ref",
    "tenant_id",
    "project_ids",
    "actions",
    "not_before",
    "expires_at",
    "status",
}
_AUDIT_DRAFT_FIELDS = {
    "schema_version",
    "event_type",
    "policy_id",
    "policy_revision",
    "policy_sha256",
    "authorization_ref",
    "subject_ref",
    "subject_tenant_id",
    "tenant_id",
    "project_id",
    "action",
    "decision",
    "reason_code",
    "observed_at",
}
_GOVERNANCE_AUDIT_DRAFT_FIELDS = _AUDIT_DRAFT_FIELDS | {
    "request_id",
    "request_sha256",
}
_AUDIT_EVENT_FIELDS = _AUDIT_DRAFT_FIELDS | {
    "event_id",
    "sequence_no",
    "previous_event_sha256",
    "event_sha256",
}
_GOVERNANCE_AUDIT_EVENT_FIELDS = _GOVERNANCE_AUDIT_DRAFT_FIELDS | {
    "event_id",
    "sequence_no",
    "previous_event_sha256",
    "event_sha256",
}
_CURRENT_AUTHORIZATION_BINDING_FIELDS = _GOVERNANCE_AUDIT_DRAFT_FIELDS - {
    "observed_at"
}


class AuthorizationAuditError(ValueError):
    """Raised when authorization or audit evidence violates its contract."""


class AuthorizationAuditStore(Protocol):
    """Append and read immutable authorization decision events."""

    def append(self, event: dict[str, Any]) -> dict[str, Any]: ...

    def read_events(
        self,
        *,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]: ...


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _strict_object(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AuthorizationAuditError(f"{name} must be an object")
    if set(value) != fields:
        raise AuthorizationAuditError(f"{name} fields do not match the contract")
    return value


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise AuthorizationAuditError(f"{name} must be a bounded identifier")
    return value


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise AuthorizationAuditError(f"{name} must be an offset timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AuthorizationAuditError(f"{name} must be an offset timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AuthorizationAuditError(f"{name} must include an offset")
    return parsed


def _string_set(
    value: Any,
    name: str,
    *,
    maximum: int,
) -> list[str]:
    if not isinstance(value, list) or not value or len(value) > maximum:
        raise AuthorizationAuditError(f"{name} must be a bounded non-empty list")
    normalized = [_identifier(item, name) for item in value]
    if len(set(normalized)) != len(normalized):
        raise AuthorizationAuditError(f"{name} must not contain duplicates")
    return sorted(normalized)


def validate_authorization_policy(policy: Any) -> dict[str, Any]:
    """Validate and canonicalize one immutable authorization policy snapshot."""
    source = _strict_object(policy, _POLICY_FIELDS, "authorization policy")
    if source["schema_version"] != AUTHORIZATION_POLICY_SCHEMA_VERSION:
        raise AuthorizationAuditError("unsupported authorization policy schema")
    _identifier(source["policy_id"], "policy_id")
    if type(source["policy_revision"]) is not int or source["policy_revision"] < 1:
        raise AuthorizationAuditError("policy_revision must be a positive integer")
    issued_at = _timestamp(source["issued_at"], "issued_at")
    policy_expiry = _timestamp(source["expires_at"], "expires_at", nullable=True)
    if policy_expiry is not None and policy_expiry <= issued_at:
        raise AuthorizationAuditError("policy expires_at must follow issued_at")

    projects = source["projects"]
    if not isinstance(projects, list) or not projects or len(projects) > 4_096:
        raise AuthorizationAuditError("projects must be a bounded non-empty list")
    project_tenants: dict[str, str] = {}
    normalized_projects: list[dict[str, Any]] = []
    for index, item in enumerate(projects):
        project = _strict_object(item, _PROJECT_FIELDS, f"projects[{index}]")
        tenant_id = _identifier(project["tenant_id"], "tenant_id")
        project_id = _identifier(project["project_id"], "project_id")
        if project["status"] not in {"active", "disabled"}:
            raise AuthorizationAuditError("project status is invalid")
        if project_id in project_tenants:
            raise AuthorizationAuditError("project_id must be unique")
        project_tenants[project_id] = tenant_id
        normalized_projects.append(copy.deepcopy(project))

    grants = source["grants"]
    if not isinstance(grants, list) or len(grants) > 8_192:
        raise AuthorizationAuditError("grants must be a bounded list")
    grant_ids: set[str] = set()
    authorization_refs: set[str] = set()
    normalized_grants: list[dict[str, Any]] = []
    for index, item in enumerate(grants):
        grant = _strict_object(item, _GRANT_FIELDS, f"grants[{index}]")
        grant_id = _identifier(grant["grant_id"], "grant_id")
        authorization_ref = _identifier(
            grant["authorization_ref"], "authorization_ref"
        )
        subject_ref = _identifier(grant["subject_ref"], "subject_ref")
        tenant_id = _identifier(grant["tenant_id"], "tenant_id")
        if grant_id in grant_ids:
            raise AuthorizationAuditError("grant_id must be unique")
        if authorization_ref in authorization_refs:
            raise AuthorizationAuditError("authorization_ref must be unique")
        grant_ids.add(grant_id)
        authorization_refs.add(authorization_ref)
        project_ids = _string_set(
            grant["project_ids"], "project_ids", maximum=1_024
        )
        actions = _string_set(grant["actions"], "actions", maximum=256)
        for project_id in project_ids:
            if project_id not in project_tenants:
                raise AuthorizationAuditError("grant references an unknown project")
            if project_tenants[project_id] != tenant_id:
                raise AuthorizationAuditError("grant crosses a tenant boundary")
        not_before = _timestamp(grant["not_before"], "not_before")
        expires_at = _timestamp(grant["expires_at"], "expires_at", nullable=True)
        if expires_at is not None and expires_at <= not_before:
            raise AuthorizationAuditError("grant expires_at must follow not_before")
        if policy_expiry is not None and (
            expires_at is None or expires_at > policy_expiry
        ):
            raise AuthorizationAuditError("grant expiry exceeds policy expiry")
        if grant["status"] not in {"active", "revoked"}:
            raise AuthorizationAuditError("grant status is invalid")
        normalized_grants.append(
            {
                **copy.deepcopy(grant),
                "subject_ref": subject_ref,
                "project_ids": project_ids,
                "actions": actions,
            }
        )

    return {
        **copy.deepcopy(source),
        "projects": sorted(normalized_projects, key=lambda item: item["project_id"]),
        "grants": sorted(normalized_grants, key=lambda item: item["grant_id"]),
    }


def authorization_policy_sha256(policy: Any) -> str:
    """Return the digest of a validated, canonical policy snapshot."""
    return _sha256(validate_authorization_policy(policy))


def _audit_event_sha256(event: dict[str, Any]) -> str:
    return _sha256({key: value for key, value in event.items() if key != "event_sha256"})


def validate_authorization_audit_event(event: Any) -> dict[str, Any]:
    """Validate one complete authorization decision audit event."""
    if not isinstance(event, dict):
        raise AuthorizationAuditError("authorization audit event must be an object")
    schema_version = event.get("schema_version")
    if schema_version == AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION:
        fields = _AUDIT_EVENT_FIELDS
    elif schema_version == GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION:
        fields = _GOVERNANCE_AUDIT_EVENT_FIELDS
    else:
        raise AuthorizationAuditError("unsupported authorization audit event schema")
    source = _strict_object(event, fields, "authorization audit event")
    if source["event_type"] != "authorization_decision":
        raise AuthorizationAuditError("authorization audit event type is invalid")
    for field in (
        "event_id",
        "policy_id",
        "authorization_ref",
        "subject_ref",
        "tenant_id",
        "project_id",
        "action",
        "reason_code",
    ):
        _identifier(source[field], field)
    if source["subject_tenant_id"] is not None:
        _identifier(source["subject_tenant_id"], "subject_tenant_id")
    if type(source["sequence_no"]) is not int or source["sequence_no"] < 1:
        raise AuthorizationAuditError("sequence_no must be a positive integer")
    if type(source["policy_revision"]) is not int or source["policy_revision"] < 1:
        raise AuthorizationAuditError("policy_revision must be a positive integer")
    if not isinstance(source["policy_sha256"], str) or _SHA256_RE.fullmatch(
        source["policy_sha256"]
    ) is None:
        raise AuthorizationAuditError("policy_sha256 must be lowercase SHA-256")
    if source["decision"] not in {"allow", "deny"}:
        raise AuthorizationAuditError("authorization decision is invalid")
    _timestamp(source["observed_at"], "observed_at")
    previous = source["previous_event_sha256"]
    if previous is not None and (
        not isinstance(previous, str) or _SHA256_RE.fullmatch(previous) is None
    ):
        raise AuthorizationAuditError("previous_event_sha256 is invalid")
    if not isinstance(source["event_sha256"], str) or _SHA256_RE.fullmatch(
        source["event_sha256"]
    ) is None:
        raise AuthorizationAuditError("event_sha256 must be lowercase SHA-256")
    if _audit_event_sha256(source) != source["event_sha256"]:
        raise AuthorizationAuditError("authorization audit event hash mismatch")
    if schema_version == GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION:
        _identifier(source["request_id"], "request_id")
        if (
            not isinstance(source["request_sha256"], str)
            or _SHA256_RE.fullmatch(source["request_sha256"]) is None
        ):
            raise AuthorizationAuditError("request_sha256 must be lowercase SHA-256")
    return copy.deepcopy(source)


def replay_authorization_audit_events(events: Any) -> dict[str, Any]:
    """Replay one complete audit chain and return its trusted head."""
    if not isinstance(events, list):
        raise AuthorizationAuditError("authorization audit events must be a list")
    previous: str | None = None
    event_ids: set[str] = set()
    tenant_id: str | None = None
    previous_event: dict[str, Any] | None = None
    for expected_sequence, raw_event in enumerate(events, start=1):
        event = validate_authorization_audit_event(raw_event)
        if tenant_id is None:
            tenant_id = event["tenant_id"]
        elif event["tenant_id"] != tenant_id:
            raise AuthorizationAuditError("authorization audit chain crosses tenants")
        if event["sequence_no"] != expected_sequence:
            raise AuthorizationAuditError("authorization audit sequence is not contiguous")
        if event["previous_event_sha256"] != previous:
            raise AuthorizationAuditError("authorization audit hash chain is broken")
        if event["event_id"] in event_ids:
            raise AuthorizationAuditError("authorization audit event_id is duplicated")
        _reject_observed_at_regression(event, previous_event)
        event_ids.add(event["event_id"])
        previous = event["event_sha256"]
        previous_event = event
    return {
        "tenant_id": tenant_id,
        "event_count": len(events),
        "event_head_sha256": previous,
    }


def _validate_event_streams(events: list[dict[str, Any]]) -> None:
    streams: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("tenant_id"), str):
            raise AuthorizationAuditError("authorization audit tenant binding is invalid")
        streams.setdefault(event["tenant_id"], []).append(event)
    for stream in streams.values():
        replay_authorization_audit_events(
            sorted(stream, key=lambda item: item.get("sequence_no", -1))
        )


def _build_event(
    draft: Any,
    *,
    sequence_no: int,
    previous_event_sha256: str | None,
) -> dict[str, Any]:
    if not isinstance(draft, dict):
        raise AuthorizationAuditError("authorization audit draft must be an object")
    schema_version = draft.get("schema_version")
    if schema_version == AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION:
        fields = _AUDIT_DRAFT_FIELDS
    elif schema_version == GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION:
        fields = _GOVERNANCE_AUDIT_DRAFT_FIELDS
    else:
        raise AuthorizationAuditError("unsupported authorization audit event schema")
    source = _strict_object(draft, fields, "authorization audit draft")
    event = {
        **copy.deepcopy(source),
        "event_id": (
            "authorization-event-"
            + hashlib.sha256(source["tenant_id"].encode("utf-8")).hexdigest()[:12]
            + f"-{sequence_no:020d}"
        ),
        "sequence_no": sequence_no,
        "previous_event_sha256": previous_event_sha256,
    }
    event["event_sha256"] = _audit_event_sha256(event)
    return validate_authorization_audit_event(event)


def _reject_observed_at_regression(
    event: dict[str, Any],
    previous_event: dict[str, Any] | None,
) -> None:
    if previous_event is None:
        return
    observed_at = _timestamp(event["observed_at"], "observed_at")
    previous_observed_at = _timestamp(
        previous_event["observed_at"], "previous observed_at"
    )
    if observed_at < previous_observed_at:
        raise AuthorizationAuditError("authorization audit observed_at regressed")


def _same_governance_request(
    existing: dict[str, Any],
    draft: dict[str, Any],
) -> bool:
    return (
        existing.get("schema_version")
        == GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
        and draft.get("schema_version")
        == GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
        and existing["tenant_id"] == draft["tenant_id"]
        and existing["project_id"] == draft["project_id"]
        and existing["request_id"] == draft["request_id"]
    )


def _existing_governance_request(
    events: list[dict[str, Any]], draft: dict[str, Any]
) -> dict[str, Any] | None:
    matches = [event for event in events if _same_governance_request(event, draft)]
    if len(matches) > 1:
        raise AuthorizationAuditError("governance audit request identity is ambiguous")
    if not matches:
        return None
    existing = matches[0]
    if existing["request_sha256"] != draft["request_sha256"]:
        raise AuthorizationAuditError("governance audit request identity conflicts")
    return copy.deepcopy(existing)


class InMemoryAuthorizationAuditStore:
    """Process-local append-only audit store used by portable fixtures."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            tenant_events = [
                item for item in self._events if item["tenant_id"] == event["tenant_id"]
            ]
            if (
                event.get("schema_version")
                == GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
            ):
                existing = _existing_governance_request(tenant_events, event)
                if existing is not None:
                    return existing
            previous = tenant_events[-1]["event_sha256"] if tenant_events else None
            committed = _build_event(
                event,
                sequence_no=len(tenant_events) + 1,
                previous_event_sha256=previous,
            )
            _reject_observed_at_regression(
                committed,
                tenant_events[-1] if tenant_events else None,
            )
            self._events.append(committed)
            return copy.deepcopy(committed)

    def read_events(
        self,
        *,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if project_id is not None and tenant_id is None:
            raise AuthorizationAuditError("project audit reads require tenant_id")
        with self._lock:
            _validate_event_streams(self._events)
            return [
                copy.deepcopy(event)
                for event in self._events
                if (tenant_id is None or event["tenant_id"] == tenant_id)
                and (project_id is None or event["project_id"] == project_id)
            ]


class SQLiteAuthorizationAuditStore:
    """Embedded durable audit ledger with database-enforced append-only rows."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS authorization_audit_events (
                    sequence_no INTEGER NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, sequence_no)
                );
                CREATE TRIGGER IF NOT EXISTS authorization_audit_events_no_update
                BEFORE UPDATE ON authorization_audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'authorization audit events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS authorization_audit_events_no_delete
                BEFORE DELETE ON authorization_audit_events
                BEGIN
                    SELECT RAISE(ABORT, 'authorization audit events are append-only');
                END;
                """
            )

    @staticmethod
    def _rows_to_events(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in rows:
            try:
                event = json.loads(row["event_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise AuthorizationAuditError("authorization audit row is invalid") from exc
            if (
                event.get("sequence_no") != row["sequence_no"]
                or event.get("event_id") != row["event_id"]
                or event.get("tenant_id") != row["tenant_id"]
                or event.get("project_id") != row["project_id"]
                or event.get("event_sha256") != row["event_sha256"]
            ):
                raise AuthorizationAuditError("authorization audit row binding mismatch")
            events.append(event)
        _validate_event_streams(events)
        return events

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT sequence_no, event_id, tenant_id, project_id, "
                "event_sha256, event_json FROM authorization_audit_events "
                "WHERE tenant_id = ? ORDER BY sequence_no",
                (event["tenant_id"],),
            ).fetchall()
            events = self._rows_to_events(rows)
            if (
                event.get("schema_version")
                == GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
            ):
                existing = _existing_governance_request(events, event)
                if existing is not None:
                    connection.commit()
                    return existing
            previous = events[-1]["event_sha256"] if events else None
            committed = _build_event(
                event,
                sequence_no=len(events) + 1,
                previous_event_sha256=previous,
            )
            _reject_observed_at_regression(
                committed,
                events[-1] if events else None,
            )
            connection.execute(
                "INSERT INTO authorization_audit_events "
                "(sequence_no, event_id, tenant_id, project_id, event_sha256, event_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    committed["sequence_no"],
                    committed["event_id"],
                    committed["tenant_id"],
                    committed["project_id"],
                    committed["event_sha256"],
                    _canonical_bytes(committed).decode("utf-8"),
                ),
            )
            connection.commit()
            return committed
        except sqlite3.Error as exc:
            connection.rollback()
            raise AuthorizationAuditError("authorization audit append failed") from exc
        finally:
            connection.close()

    def read_events(
        self,
        *,
        tenant_id: str | None = None,
        project_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if project_id is not None and tenant_id is None:
            raise AuthorizationAuditError("project audit reads require tenant_id")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT sequence_no, event_id, tenant_id, project_id, "
                "event_sha256, event_json FROM authorization_audit_events "
                "ORDER BY tenant_id, sequence_no"
            ).fetchall()
        events = self._rows_to_events(rows)
        return [
            copy.deepcopy(event)
            for event in events
            if (tenant_id is None or event["tenant_id"] == tenant_id)
            and (project_id is None or event["project_id"] == project_id)
        ]


class TenantProjectAuthorizer:
    """Resolve exact grants and persist every decision before returning it."""

    def __init__(
        self,
        policy: Any,
        *,
        expected_policy_sha256: str,
        audit_store: AuthorizationAuditStore,
        clock: Any,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(getattr(audit_store, "append", None)):
            raise TypeError("audit_store must provide append")
        if not isinstance(expected_policy_sha256, str) or _SHA256_RE.fullmatch(
            expected_policy_sha256
        ) is None:
            raise AuthorizationAuditError(
                "expected policy digest must be lowercase SHA-256"
            )
        self._policy = validate_authorization_policy(policy)
        self._policy_sha256 = _sha256(self._policy)
        if self._policy_sha256 != expected_policy_sha256:
            raise AuthorizationAuditError("authorization policy digest mismatch")
        self._audit_store = audit_store
        self._clock = clock
        self._projects = {
            item["project_id"]: item for item in self._policy["projects"]
        }
        self._grants = {
            item["authorization_ref"]: item for item in self._policy["grants"]
        }
        self.audit_write_failures = 0

    @property
    def policy_sha256(self) -> str:
        return self._policy_sha256

    def _decision(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
        observed_at: datetime,
    ) -> tuple[bool, str, dict[str, Any] | None, dict[str, Any] | None]:
        grant = self._grants.get(context.authorization_ref)
        project = self._projects.get(project_id)
        policy_issued_at = _timestamp(self._policy["issued_at"], "issued_at")
        policy_expiry = _timestamp(
            self._policy["expires_at"], "expires_at", nullable=True
        )
        if observed_at < policy_issued_at:
            return False, "policy_not_active", grant, project
        if policy_expiry is not None and observed_at >= policy_expiry:
            return False, "policy_expired", grant, project
        if grant is None:
            return False, "grant_not_found", None, project
        if grant["subject_ref"] != context.subject_ref:
            return False, "subject_mismatch", grant, project
        if grant["status"] != "active":
            return False, "grant_revoked", grant, project
        not_before = _timestamp(grant["not_before"], "not_before")
        expires_at = _timestamp(grant["expires_at"], "expires_at", nullable=True)
        if observed_at < not_before:
            return False, "grant_not_active", grant, project
        if expires_at is not None and observed_at >= expires_at:
            return False, "grant_expired", grant, project
        if project is None:
            return False, "project_not_registered", grant, None
        if project["status"] != "active":
            return False, "project_disabled", grant, project
        if project["tenant_id"] != grant["tenant_id"]:
            return False, "tenant_mismatch", grant, project
        if project_id not in grant["project_ids"]:
            return False, "project_not_granted", grant, project
        if action not in grant["actions"]:
            return False, "action_not_granted", grant, project
        return True, "grant_matched", grant, project

    def _authorize_attempt(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
        *,
        request_id: str | None = None,
        request_sha256: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if not isinstance(context, RequestContext):
            return None
        try:
            action = _identifier(action, "action")
            project_id = _identifier(project_id, "project_id")
            observed_at_text = self._clock()
            observed_at = _timestamp(observed_at_text, "observed_at")
            if request_id is not None:
                _identifier(request_id, "request_id")
                if (
                    not isinstance(request_sha256, str)
                    or _SHA256_RE.fullmatch(request_sha256) is None
                ):
                    raise AuthorizationAuditError(
                        "request_sha256 must be lowercase SHA-256"
                    )
            elif request_sha256 is not None:
                raise AuthorizationAuditError(
                    "request_sha256 requires a request_id"
                )
        except (AuthorizationAuditError, Exception):  # noqa: BLE001
            return None
        allowed, reason, grant, project = self._decision(
            context, action, project_id, observed_at
        )
        tenant_id = (
            project["tenant_id"]
            if project is not None
            else grant["tenant_id"]
            if grant is not None
            else "tenant-unresolved"
        )
        draft = {
            "schema_version": (
                GOVERNANCE_AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
                if request_id is not None
                else AUTHORIZATION_AUDIT_EVENT_SCHEMA_VERSION
            ),
            "event_type": "authorization_decision",
            "policy_id": self._policy["policy_id"],
            "policy_revision": self._policy["policy_revision"],
            "policy_sha256": self._policy_sha256,
            "authorization_ref": context.authorization_ref,
            "subject_ref": context.subject_ref,
            "subject_tenant_id": grant["tenant_id"] if grant is not None else None,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "action": action,
            "decision": "allow" if allowed else "deny",
            "reason_code": reason,
            "observed_at": observed_at_text,
        }
        if request_id is not None:
            draft["request_id"] = request_id
            draft["request_sha256"] = request_sha256
        try:
            return self._audit_store.append(draft), draft
        except Exception:  # noqa: BLE001
            self.audit_write_failures += 1
            return None

    def authorize(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
    ) -> bool:
        attempt = self._authorize_attempt(context, action, project_id)
        return (
            attempt is not None
            and attempt[0]["decision"] == "allow"
            and attempt[1]["decision"] == "allow"
        )

    def authorize_with_receipt(
        self,
        context: RequestContext,
        action: str,
        project_id: str,
        *,
        request_id: str,
        request_sha256: str,
    ) -> dict[str, Any] | None:
        """Return current authorization and any exact historical audit evidence."""
        attempt = self._authorize_attempt(
            context,
            action,
            project_id,
            request_id=request_id,
            request_sha256=request_sha256,
        )
        if attempt is None:
            return None
        event, current_draft = attempt
        current_binding_matches = all(
            event[field] == current_draft[field]
            for field in _CURRENT_AUTHORIZATION_BINDING_FIELDS
        )
        currently_authorized = (
            current_draft["decision"] == "allow"
            and event["decision"] == "allow"
            and current_binding_matches
        )
        return {
            "audit_event": event,
            "currently_authorized": currently_authorized,
            "historical_replay_only": (
                event["decision"] == "allow" and not currently_authorized
            ),
        }
