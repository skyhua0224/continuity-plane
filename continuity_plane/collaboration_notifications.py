"""Signed local-first collaboration notifications and provider inbox delivery."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from .state_mcp import RequestContext

PUBLISH_REQUEST_SCHEMA_VERSION = (
    "context.collaboration-notification-publish/v1alpha1"
)
NOTIFICATION_EVENT_SCHEMA_VERSION = "context.collaboration-notification/v1alpha1"
SUBSCRIPTION_REQUEST_SCHEMA_VERSION = (
    "context.collaboration-subscription-request/v1alpha1"
)
SUBSCRIPTION_SCHEMA_VERSION = "context.collaboration-subscription/v1alpha1"
CURSOR_SCHEMA_VERSION = "context.collaboration-subscription-cursor/v1alpha1"
BATCH_SCHEMA_VERSION = "context.collaboration-delivery-batch/v1alpha1"

GENESIS_EVENT_SHA256 = "0" * 64
EVENT_KINDS = frozenset(
    {
        "approval_requested",
        "conflict_detected",
        "deploy_intent",
        "review_requested",
        "work_claimed",
    }
)
TRANSPORTS = frozenset({"sse", "websocket"})

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SSE_EVENT_ID_RE = re.compile(r"^([1-9][0-9]*):([0-9a-f]{64})$")
_PUBLISH_FIELDS = {
    "schema_version",
    "request_id",
    "tenant_id",
    "project_id",
    "state_revision",
    "event_kind",
    "work_id",
    "summary",
    "target_refs",
    "evidence_refs",
    "requires_approval",
    "causation_ref",
    "correlation_ref",
}
_EVENT_FIELDS = {
    "schema_version",
    "event_id",
    "request_id",
    "tenant_id",
    "project_id",
    "state_revision",
    "sequence_no",
    "previous_event_sha256",
    "event_kind",
    "actor_ref",
    "work_id",
    "summary",
    "target_refs",
    "evidence_refs",
    "requires_approval",
    "causation_ref",
    "correlation_ref",
    "observed_at",
    "context_injection_authority",
    "operation_authority",
    "state_write_authority",
    "provider_authority",
    "external_effect_authority",
    "event_sha256",
    "signature",
}
_SUBSCRIPTION_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "subscription_id",
    "tenant_id",
    "project_id",
    "provider",
    "transport",
    "event_kinds",
}
_SUBSCRIPTION_FIELDS = {
    "schema_version",
    "request_id",
    "subscription_id",
    "tenant_id",
    "project_id",
    "subscriber_ref",
    "provider",
    "transport",
    "event_kinds",
    "created_at",
    "websocket_available",
    "state_write_authority",
    "subscription_sha256",
    "signature",
}
_CURSOR_FIELDS = {
    "schema_version",
    "subscription_id",
    "tenant_id",
    "project_id",
    "subscriber_ref",
    "subscription_sha256",
    "last_sequence_no",
    "last_event_sha256",
    "issued_at",
    "cursor_sha256",
    "signature",
}
_BATCH_FIELDS = {
    "schema_version",
    "subscription_id",
    "tenant_id",
    "project_id",
    "provider",
    "events",
    "next_cursor",
    "has_more",
    "delivery_authority",
    "state_write_authority",
    "batch_sha256",
    "signature",
}
_SIGNATURE_FIELDS = {"algorithm", "key_id", "value"}


class CollaborationNotificationError(ValueError):
    """A collaboration notification violates its integrity or authority contract."""


class CollaborationNotificationConflict(CollaborationNotificationError):
    """A stable request or subscription identity was reused with changed input."""


class NotificationAuthorizer(Protocol):
    """Authorize tenant/project scope and verify current State provenance."""

    def authorize_scope(
        self,
        context: RequestContext,
        action: str,
        tenant_id: str,
        project_id: str,
    ) -> bool: ...

    def verify_notification_source(
        self,
        context: RequestContext,
        request: dict[str, Any],
    ) -> bool: ...


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
        raise CollaborationNotificationError("notification data is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _strict_object(value: Any, fields: set[str], field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CollaborationNotificationError(f"{field} fields are invalid")
    return copy.deepcopy(value)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise CollaborationNotificationError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise CollaborationNotificationError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise CollaborationNotificationError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollaborationNotificationError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CollaborationNotificationError(f"{field} must include an offset")
    return value


def _text(value: Any, field: str, *, maximum: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(character in value for character in "\r\n\x00")
    ):
        raise CollaborationNotificationError(f"{field} is invalid")
    return value


def _identifier_list(
    value: Any,
    field: str,
    *,
    required: bool = True,
    maximum: int = 256,
) -> list[str]:
    if (
        not isinstance(value, list)
        or (required and not value)
        or len(value) > maximum
    ):
        raise CollaborationNotificationError(f"{field} is invalid")
    normalized = sorted(_identifier(item, field) for item in value)
    if len(set(normalized)) != len(normalized):
        raise CollaborationNotificationError(f"{field} contains duplicates")
    return normalized


def _signature(value: Any) -> dict[str, str]:
    signature = _strict_object(value, _SIGNATURE_FIELDS, "signature")
    if signature["algorithm"] != "hmac-sha256":
        raise CollaborationNotificationError("signature algorithm is invalid")
    _identifier(signature["key_id"], "signature.key_id")
    _sha256(signature["value"], "signature.value")
    return signature


class HMACNotificationSigner:
    """Small standard-library signer for embedded notification deployments."""

    def __init__(self, *, key_id: str, secret: bytes) -> None:
        self.key_id = _identifier(key_id, "key_id")
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise CollaborationNotificationError("signing secret is too short")
        self._secret = bytes(secret)

    def sign(self, digest: str) -> dict[str, str]:
        digest = _sha256(digest, "signed digest")
        value = hmac.new(self._secret, digest.encode("ascii"), hashlib.sha256).hexdigest()
        return {
            "algorithm": "hmac-sha256",
            "key_id": self.key_id,
            "value": value,
        }

    def verify(self, digest: str, signature: Any) -> bool:
        try:
            normalized = _signature(signature)
            expected = self.sign(digest)
        except CollaborationNotificationError:
            return False
        return normalized["key_id"] == self.key_id and hmac.compare_digest(
            normalized["value"], expected["value"]
        )


def _validate_publish_request(value: Any) -> dict[str, Any]:
    request = _strict_object(value, _PUBLISH_FIELDS, "publish request")
    if request["schema_version"] != PUBLISH_REQUEST_SCHEMA_VERSION:
        raise CollaborationNotificationError("publish request version is invalid")
    for field in (
        "request_id",
        "tenant_id",
        "project_id",
        "work_id",
        "causation_ref",
        "correlation_ref",
    ):
        _identifier(request[field], field)
    if type(request["state_revision"]) is not int or request["state_revision"] < 0:
        raise CollaborationNotificationError("state_revision is invalid")
    if request["event_kind"] not in EVENT_KINDS:
        raise CollaborationNotificationError("event_kind is invalid")
    request["summary"] = _text(request["summary"], "summary")
    request["target_refs"] = _identifier_list(request["target_refs"], "target_refs")
    request["evidence_refs"] = _identifier_list(
        request["evidence_refs"], "evidence_refs"
    )
    if type(request["requires_approval"]) is not bool:
        raise CollaborationNotificationError("requires_approval is invalid")
    approval_event = request["event_kind"] in {
        "approval_requested",
        "deploy_intent",
    }
    if request["requires_approval"] is not approval_event:
        raise CollaborationNotificationError("approval requirement is invalid")
    return request


def _event_body(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in event.items()
        if key not in {"event_sha256", "signature"}
    }


def validate_collaboration_notification_event(
    value: Any,
    *,
    signer: HMACNotificationSigner,
) -> dict[str, Any]:
    """Validate one signature- and digest-bound authority-free notification."""
    event = _strict_object(value, _EVENT_FIELDS, "notification event")
    if event["schema_version"] != NOTIFICATION_EVENT_SCHEMA_VERSION:
        raise CollaborationNotificationError("notification event version is invalid")
    for field in (
        "event_id",
        "request_id",
        "tenant_id",
        "project_id",
        "actor_ref",
        "work_id",
        "causation_ref",
        "correlation_ref",
    ):
        _identifier(event[field], field)
    if type(event["state_revision"]) is not int or event["state_revision"] < 0:
        raise CollaborationNotificationError("event state_revision is invalid")
    if type(event["sequence_no"]) is not int or event["sequence_no"] < 1:
        raise CollaborationNotificationError("event sequence_no is invalid")
    _sha256(event["previous_event_sha256"], "previous_event_sha256")
    if event["event_kind"] not in EVENT_KINDS:
        raise CollaborationNotificationError("event_kind is invalid")
    _text(event["summary"], "summary")
    target_refs = _identifier_list(event["target_refs"], "target_refs")
    evidence_refs = _identifier_list(event["evidence_refs"], "evidence_refs")
    if target_refs != event["target_refs"] or evidence_refs != event["evidence_refs"]:
        raise CollaborationNotificationError("event refs are not canonical")
    if type(event["requires_approval"]) is not bool:
        raise CollaborationNotificationError("requires_approval is invalid")
    _timestamp(event["observed_at"], "observed_at")
    if (
        event["context_injection_authority"] is not False
        or event["operation_authority"] is not False
        or event["state_write_authority"] is not False
        or event["provider_authority"] != 0
        or event["external_effect_authority"] != 0
    ):
        raise CollaborationNotificationError("notification event cannot claim authority")
    event_sha256 = _sha256(event["event_sha256"], "event_sha256")
    if event_sha256 != _digest(_event_body(event)):
        raise CollaborationNotificationError("notification event digest mismatch")
    if not signer.verify(event_sha256, event["signature"]):
        raise CollaborationNotificationError("notification event signature mismatch")
    return event


def validate_collaboration_notification_chain(
    events: Any,
    *,
    signer: HMACNotificationSigner,
) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        raise CollaborationNotificationError("notification chain is invalid")
    previous = GENESIS_EVENT_SHA256
    project_binding: tuple[str, str] | None = None
    observed_at: datetime | None = None
    event_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for sequence_no, value in enumerate(events, start=1):
        event = validate_collaboration_notification_event(value, signer=signer)
        binding = (event["tenant_id"], event["project_id"])
        current_observed_at = datetime.fromisoformat(
            event["observed_at"].replace("Z", "+00:00")
        )
        if (
            event["sequence_no"] != sequence_no
            or event["previous_event_sha256"] != previous
            or (project_binding is not None and binding != project_binding)
            or (observed_at is not None and current_observed_at < observed_at)
            or event["event_id"] in event_ids
        ):
            raise CollaborationNotificationError("notification event chain is invalid")
        project_binding = binding
        observed_at = current_observed_at
        event_ids.add(event["event_id"])
        previous = event["event_sha256"]
        normalized.append(event)
    return normalized


def _validate_subscription_request(value: Any) -> dict[str, Any]:
    request = _strict_object(value, _SUBSCRIPTION_REQUEST_FIELDS, "subscription request")
    if request["schema_version"] != SUBSCRIPTION_REQUEST_SCHEMA_VERSION:
        raise CollaborationNotificationError("subscription request version is invalid")
    for field in (
        "request_id",
        "subscription_id",
        "tenant_id",
        "project_id",
        "provider",
    ):
        _identifier(request[field], field)
    if request["transport"] not in TRANSPORTS:
        raise CollaborationNotificationError("subscription transport is invalid")
    event_kinds = _identifier_list(request["event_kinds"], "event_kinds")
    if not set(event_kinds).issubset(EVENT_KINDS):
        raise CollaborationNotificationError("subscription event_kinds are invalid")
    request["event_kinds"] = event_kinds
    return request


def _signed_document(
    body: dict[str, Any],
    *,
    digest_field: str,
    signer: HMACNotificationSigner,
) -> dict[str, Any]:
    document = copy.deepcopy(body)
    document[digest_field] = _digest(document)
    document["signature"] = signer.sign(document[digest_field])
    return document


def _validate_subscription(
    value: Any, *, signer: HMACNotificationSigner
) -> dict[str, Any]:
    subscription = _strict_object(value, _SUBSCRIPTION_FIELDS, "subscription")
    if subscription["schema_version"] != SUBSCRIPTION_SCHEMA_VERSION:
        raise CollaborationNotificationError("subscription version is invalid")
    for field in (
        "request_id",
        "subscription_id",
        "tenant_id",
        "project_id",
        "subscriber_ref",
        "provider",
    ):
        _identifier(subscription[field], field)
    if subscription["transport"] not in TRANSPORTS:
        raise CollaborationNotificationError("subscription transport is invalid")
    event_kinds = _identifier_list(subscription["event_kinds"], "event_kinds")
    if event_kinds != subscription["event_kinds"] or not set(event_kinds).issubset(
        EVENT_KINDS
    ):
        raise CollaborationNotificationError("subscription event_kinds are invalid")
    _timestamp(subscription["created_at"], "created_at")
    if type(subscription["websocket_available"]) is not bool:
        raise CollaborationNotificationError("websocket_available is invalid")
    if subscription["state_write_authority"] is not False:
        raise CollaborationNotificationError("subscription cannot claim authority")
    digest = _sha256(subscription["subscription_sha256"], "subscription_sha256")
    body = {
        key: item
        for key, item in subscription.items()
        if key not in {"subscription_sha256", "signature"}
    }
    if digest != _digest(body) or not signer.verify(digest, subscription["signature"]):
        raise CollaborationNotificationError("subscription digest or signature mismatch")
    return subscription


def _validate_cursor(
    value: Any,
    *,
    signer: HMACNotificationSigner,
) -> dict[str, Any]:
    cursor = _strict_object(value, _CURSOR_FIELDS, "subscription cursor")
    if cursor["schema_version"] != CURSOR_SCHEMA_VERSION:
        raise CollaborationNotificationError("subscription cursor version is invalid")
    for field in (
        "subscription_id",
        "tenant_id",
        "project_id",
        "subscriber_ref",
    ):
        _identifier(cursor[field], field)
    _sha256(cursor["subscription_sha256"], "subscription_sha256")
    if type(cursor["last_sequence_no"]) is not int or cursor["last_sequence_no"] < 0:
        raise CollaborationNotificationError("subscription cursor sequence is invalid")
    _sha256(cursor["last_event_sha256"], "last_event_sha256")
    _timestamp(cursor["issued_at"], "issued_at")
    digest = _sha256(cursor["cursor_sha256"], "cursor_sha256")
    body = {
        key: item
        for key, item in cursor.items()
        if key not in {"cursor_sha256", "signature"}
    }
    if digest != _digest(body) or not signer.verify(digest, cursor["signature"]):
        raise CollaborationNotificationError("subscription cursor digest or signature mismatch")
    return cursor


class SQLiteCollaborationNotificationStore:
    """Embedded append-only event and immutable subscription store."""

    def __init__(
        self,
        path: str | Path,
        *,
        signer: HMACNotificationSigner,
    ) -> None:
        self.path = Path(path)
        self.signer = signer
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS collaboration_notification_events (
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    sequence_no INTEGER NOT NULL,
                    request_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    event_id TEXT NOT NULL UNIQUE,
                    event_sha256 TEXT NOT NULL UNIQUE,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, project_id, sequence_no),
                    UNIQUE (tenant_id, project_id, request_id)
                );
                CREATE TRIGGER IF NOT EXISTS collaboration_notification_events_no_update
                BEFORE UPDATE ON collaboration_notification_events
                BEGIN
                    SELECT RAISE(ABORT, 'collaboration notification events are append-only');
                END;
                CREATE TRIGGER IF NOT EXISTS collaboration_notification_events_no_delete
                BEFORE DELETE ON collaboration_notification_events
                BEGIN
                    SELECT RAISE(ABORT, 'collaboration notification events are append-only');
                END;
                CREATE TABLE IF NOT EXISTS collaboration_notification_subscriptions (
                    subscription_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    subscriber_ref TEXT NOT NULL,
                    subscription_sha256 TEXT NOT NULL UNIQUE,
                    subscription_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, project_id, subscription_id),
                    UNIQUE (tenant_id, project_id, request_id)
                );
                CREATE TRIGGER IF NOT EXISTS collaboration_notification_subscriptions_no_update
                BEFORE UPDATE ON collaboration_notification_subscriptions
                BEGIN
                    SELECT RAISE(ABORT, 'collaboration subscriptions are immutable');
                END;
                CREATE TRIGGER IF NOT EXISTS collaboration_notification_subscriptions_no_delete
                BEFORE DELETE ON collaboration_notification_subscriptions
                BEGIN
                    SELECT RAISE(ABORT, 'collaboration subscriptions are immutable');
                END;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _decode_event(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            event = json.loads(row["event_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise CollaborationNotificationError("stored notification event is invalid") from exc
        validated = validate_collaboration_notification_event(
            event, signer=self.signer
        )
        if any(
            validated[field] != row[field]
            for field in (
                "tenant_id",
                "project_id",
                "sequence_no",
                "request_id",
                "event_id",
                "event_sha256",
            )
        ):
            raise CollaborationNotificationError("stored notification row binding mismatch")
        return validated

    def append_event(
        self,
        request: dict[str, Any],
        *,
        actor_ref: str,
        observed_at: str,
    ) -> dict[str, Any]:
        request = _validate_publish_request(request)
        actor_ref = _identifier(actor_ref, "actor_ref")
        observed_at = _timestamp(observed_at, "observed_at")
        request_sha256 = _digest({"request": request, "actor_ref": actor_ref})
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT tenant_id, project_id, sequence_no, request_id, "
                    "event_id, event_sha256, event_json, request_sha256 "
                    "FROM collaboration_notification_events "
                    "WHERE tenant_id = ? AND project_id = ? AND request_id = ?",
                    (
                        request["tenant_id"],
                        request["project_id"],
                        request["request_id"],
                    ),
                ).fetchone()
                if existing is not None:
                    if existing["request_sha256"] != request_sha256:
                        raise CollaborationNotificationConflict(
                            "notification request replay conflicts"
                        )
                    event = self._decode_event(existing)
                    connection.execute("COMMIT")
                    return event
                previous_row = connection.execute(
                    "SELECT tenant_id, project_id, sequence_no, request_id, "
                    "event_id, event_sha256, event_json "
                    "FROM collaboration_notification_events "
                    "WHERE tenant_id = ? AND project_id = ? "
                    "ORDER BY sequence_no DESC LIMIT 1",
                    (request["tenant_id"], request["project_id"]),
                ).fetchone()
                if previous_row is None:
                    sequence_no = 1
                    previous_event_sha256 = GENESIS_EVENT_SHA256
                else:
                    previous_event = self._decode_event(previous_row)
                    previous_time = datetime.fromisoformat(
                        previous_event["observed_at"].replace("Z", "+00:00")
                    )
                    if datetime.fromisoformat(
                        observed_at.replace("Z", "+00:00")
                    ) < previous_time:
                        raise CollaborationNotificationError(
                            "notification observed_at regressed"
                        )
                    sequence_no = previous_event["sequence_no"] + 1
                    previous_event_sha256 = previous_event["event_sha256"]
                event_body = {
                    "schema_version": NOTIFICATION_EVENT_SCHEMA_VERSION,
                    "event_id": "notification-" + _digest(
                        {
                            "tenant_id": request["tenant_id"],
                            "project_id": request["project_id"],
                            "request_id": request["request_id"],
                        }
                    ),
                    "request_id": request["request_id"],
                    "tenant_id": request["tenant_id"],
                    "project_id": request["project_id"],
                    "state_revision": request["state_revision"],
                    "sequence_no": sequence_no,
                    "previous_event_sha256": previous_event_sha256,
                    "event_kind": request["event_kind"],
                    "actor_ref": actor_ref,
                    "work_id": request["work_id"],
                    "summary": request["summary"],
                    "target_refs": request["target_refs"],
                    "evidence_refs": request["evidence_refs"],
                    "requires_approval": request["requires_approval"],
                    "causation_ref": request["causation_ref"],
                    "correlation_ref": request["correlation_ref"],
                    "observed_at": observed_at,
                    "context_injection_authority": False,
                    "operation_authority": False,
                    "state_write_authority": False,
                    "provider_authority": 0,
                    "external_effect_authority": 0,
                }
                event = copy.deepcopy(event_body)
                event["event_sha256"] = _digest(event_body)
                event["signature"] = self.signer.sign(event["event_sha256"])
                event = validate_collaboration_notification_event(
                    event, signer=self.signer
                )
                connection.execute(
                    "INSERT INTO collaboration_notification_events "
                    "(tenant_id, project_id, sequence_no, request_id, "
                    "request_sha256, event_id, event_sha256, event_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event["tenant_id"],
                        event["project_id"],
                        event["sequence_no"],
                        event["request_id"],
                        request_sha256,
                        event["event_id"],
                        event["event_sha256"],
                        _canonical(event).decode("utf-8"),
                    ),
                )
                connection.execute("COMMIT")
                return event
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def create_subscription(
        self,
        request: dict[str, Any],
        *,
        subscriber_ref: str,
        created_at: str,
        websocket_available: bool,
    ) -> dict[str, Any]:
        request = _validate_subscription_request(request)
        subscriber_ref = _identifier(subscriber_ref, "subscriber_ref")
        created_at = _timestamp(created_at, "created_at")
        if request["transport"] == "websocket" and not websocket_available:
            raise CollaborationNotificationError("websocket transport is unavailable")
        request_sha256 = _digest(
            {"request": request, "subscriber_ref": subscriber_ref}
        )
        body = {
            "schema_version": SUBSCRIPTION_SCHEMA_VERSION,
            "request_id": request["request_id"],
            "subscription_id": request["subscription_id"],
            "tenant_id": request["tenant_id"],
            "project_id": request["project_id"],
            "subscriber_ref": subscriber_ref,
            "provider": request["provider"],
            "transport": request["transport"],
            "event_kinds": request["event_kinds"],
            "created_at": created_at,
            "websocket_available": websocket_available,
            "state_write_authority": False,
        }
        subscription = _validate_subscription(
            _signed_document(
                body,
                digest_field="subscription_sha256",
                signer=self.signer,
            ),
            signer=self.signer,
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT request_sha256, tenant_id, project_id, subscriber_ref, "
                    "subscription_sha256, subscription_json "
                    "FROM collaboration_notification_subscriptions "
                    "WHERE tenant_id = ? AND project_id = ? "
                    "AND (subscription_id = ? OR request_id = ?)",
                    (
                        request["tenant_id"],
                        request["project_id"],
                        request["subscription_id"],
                        request["request_id"],
                    ),
                ).fetchone()
                if existing is not None:
                    if existing["request_sha256"] != request_sha256:
                        raise CollaborationNotificationConflict(
                            "subscription request replay conflicts"
                        )
                    stored = self._decode_subscription(existing)
                    connection.execute("COMMIT")
                    return stored
                connection.execute(
                    "INSERT INTO collaboration_notification_subscriptions "
                    "(subscription_id, request_id, request_sha256, tenant_id, "
                    "project_id, subscriber_ref, subscription_sha256, "
                    "subscription_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        subscription["subscription_id"],
                        subscription["request_id"],
                        request_sha256,
                        subscription["tenant_id"],
                        subscription["project_id"],
                        subscription["subscriber_ref"],
                        subscription["subscription_sha256"],
                        _canonical(subscription).decode("utf-8"),
                    ),
                )
                connection.execute("COMMIT")
                return subscription
            except Exception:
                connection.execute("ROLLBACK")
                raise

    def _decode_subscription(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            subscription = json.loads(row["subscription_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise CollaborationNotificationError("stored subscription is invalid") from exc
        validated = _validate_subscription(subscription, signer=self.signer)
        if any(
            validated[field] != row[field]
            for field in (
                "tenant_id",
                "project_id",
                "subscriber_ref",
                "subscription_sha256",
            )
        ):
            raise CollaborationNotificationError("stored subscription binding mismatch")
        return validated

    def read_subscription(
        self,
        subscription_id: str,
        *,
        tenant_id: str,
        project_id: str,
    ) -> dict[str, Any] | None:
        subscription_id = _identifier(subscription_id, "subscription_id")
        tenant_id = _identifier(tenant_id, "tenant_id")
        project_id = _identifier(project_id, "project_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT request_sha256, tenant_id, project_id, subscriber_ref, "
                "subscription_sha256, subscription_json "
                "FROM collaboration_notification_subscriptions "
                "WHERE tenant_id = ? AND project_id = ? AND subscription_id = ?",
                (tenant_id, project_id, subscription_id),
            ).fetchone()
        return None if row is None else self._decode_subscription(row)

    def read_project_events(
        self, *, tenant_id: str, project_id: str
    ) -> list[dict[str, Any]]:
        tenant_id = _identifier(tenant_id, "tenant_id")
        project_id = _identifier(project_id, "project_id")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT tenant_id, project_id, sequence_no, request_id, event_id, "
                "event_sha256, event_json FROM collaboration_notification_events "
                "WHERE tenant_id = ? AND project_id = ? ORDER BY sequence_no",
                (tenant_id, project_id),
            ).fetchall()
        events = [self._decode_event(row) for row in rows]
        return validate_collaboration_notification_chain(events, signer=self.signer)

    def close(self) -> None:
        """Connections are operation-scoped; close is an API symmetry no-op."""


class CollaborationNotificationService:
    """Authorization boundary for publish, subscribe and cursor-based delivery."""

    def __init__(
        self,
        store: SQLiteCollaborationNotificationStore,
        *,
        authorizer: NotificationAuthorizer,
        clock,
        websocket_available: bool = False,
    ) -> None:
        self.store = store
        self.authorizer = authorizer
        self.clock = clock
        if type(websocket_available) is not bool:
            raise CollaborationNotificationError("websocket capability is invalid")
        self.websocket_available = websocket_available

    def _authorize(
        self,
        context: RequestContext,
        *,
        action: str,
        tenant_id: str,
        project_id: str,
    ) -> None:
        authorize_scope = getattr(self.authorizer, "authorize_scope", None)
        if not callable(authorize_scope) or authorize_scope(
            context,
            action,
            tenant_id,
            project_id,
        ) is not True:
            raise CollaborationNotificationError("notification operation is not authorized")

    def publish(
        self, request: dict[str, Any], *, context: RequestContext
    ) -> dict[str, Any]:
        request = _validate_publish_request(request)
        self._authorize(
            context,
            action="notification.publish",
            tenant_id=request["tenant_id"],
            project_id=request["project_id"],
        )
        verify_source = getattr(self.authorizer, "verify_notification_source", None)
        if not callable(verify_source) or verify_source(context, request) is not True:
            raise CollaborationNotificationError(
                "notification state source is not verified"
            )
        return self.store.append_event(
            request,
            actor_ref=context.subject_ref,
            observed_at=self.clock(),
        )

    def subscribe(
        self, request: dict[str, Any], *, context: RequestContext
    ) -> dict[str, Any]:
        request = _validate_subscription_request(request)
        self._authorize(
            context,
            action="notification.subscribe",
            tenant_id=request["tenant_id"],
            project_id=request["project_id"],
        )
        return self.store.create_subscription(
            request,
            subscriber_ref=context.subject_ref,
            created_at=self.clock(),
            websocket_available=self.websocket_available,
        )

    def _build_cursor(
        self,
        subscription: dict[str, Any],
        *,
        last_sequence_no: int,
        last_event_sha256: str,
    ) -> dict[str, Any]:
        body = {
            "schema_version": CURSOR_SCHEMA_VERSION,
            "subscription_id": subscription["subscription_id"],
            "tenant_id": subscription["tenant_id"],
            "project_id": subscription["project_id"],
            "subscriber_ref": subscription["subscriber_ref"],
            "subscription_sha256": subscription["subscription_sha256"],
            "last_sequence_no": last_sequence_no,
            "last_event_sha256": last_event_sha256,
            "issued_at": _timestamp(self.clock(), "cursor issued_at"),
        }
        return _validate_cursor(
            _signed_document(
                body,
                digest_field="cursor_sha256",
                signer=self.store.signer,
            ),
            signer=self.store.signer,
        )

    def pull(
        self,
        subscription_id: str,
        *,
        tenant_id: str,
        project_id: str,
        cursor: dict[str, Any] | None,
        limit: int,
        context: RequestContext,
    ) -> dict[str, Any]:
        subscription_id = _identifier(subscription_id, "subscription_id")
        tenant_id = _identifier(tenant_id, "tenant_id")
        project_id = _identifier(project_id, "project_id")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise CollaborationNotificationError("notification pull limit is invalid")
        self._authorize(
            context,
            action="notification.read",
            tenant_id=tenant_id,
            project_id=project_id,
        )
        subscription = self.store.read_subscription(
            subscription_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if (
            subscription is None
            or subscription["tenant_id"] != tenant_id
            or subscription["project_id"] != project_id
            or subscription["subscriber_ref"] != context.subject_ref
        ):
            raise CollaborationNotificationError("notification operation is not authorized")
        events = self.store.read_project_events(
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if cursor is None:
            last_sequence_no = 0
            last_event_sha256 = GENESIS_EVENT_SHA256
        else:
            normalized_cursor = _validate_cursor(cursor, signer=self.store.signer)
            if any(
                normalized_cursor[field] != subscription[field]
                for field in (
                    "subscription_id",
                    "tenant_id",
                    "project_id",
                    "subscriber_ref",
                    "subscription_sha256",
                )
            ):
                raise CollaborationNotificationError("subscription cursor binding is invalid")
            last_sequence_no = normalized_cursor["last_sequence_no"]
            last_event_sha256 = normalized_cursor["last_event_sha256"]
            if last_sequence_no > len(events) or (
                last_sequence_no == 0
                and last_event_sha256 != GENESIS_EVENT_SHA256
            ) or (
                last_sequence_no > 0
                and events[last_sequence_no - 1]["event_sha256"]
                != last_event_sha256
            ):
                raise CollaborationNotificationError("subscription cursor is stale")
        matching = [
            event
            for event in events[last_sequence_no:]
            if event["event_kind"] in subscription["event_kinds"]
        ]
        delivered = matching[:limit]
        if delivered:
            next_sequence_no = delivered[-1]["sequence_no"]
            next_event_sha256 = delivered[-1]["event_sha256"]
        elif events:
            next_sequence_no = events[-1]["sequence_no"]
            next_event_sha256 = events[-1]["event_sha256"]
        else:
            next_sequence_no = last_sequence_no
            next_event_sha256 = last_event_sha256
        next_cursor = self._build_cursor(
            subscription,
            last_sequence_no=next_sequence_no,
            last_event_sha256=next_event_sha256,
        )
        body = {
            "schema_version": BATCH_SCHEMA_VERSION,
            "subscription_id": subscription_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "provider": subscription["provider"],
            "events": copy.deepcopy(delivered),
            "next_cursor": next_cursor,
            "has_more": len(matching) > len(delivered),
            "delivery_authority": False,
            "state_write_authority": False,
        }
        batch = _signed_document(
            body,
            digest_field="batch_sha256",
            signer=self.store.signer,
        )
        return validate_collaboration_delivery_batch(
            batch,
            signer=self.store.signer,
        )

    def pull_sse(
        self,
        subscription_id: str,
        *,
        tenant_id: str,
        project_id: str,
        last_event_id: str | None,
        limit: int,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Build one authorized SSE reconnect response with bounded delivery."""
        subscription_id = _identifier(subscription_id, "subscription_id")
        tenant_id = _identifier(tenant_id, "tenant_id")
        project_id = _identifier(project_id, "project_id")
        self._authorize(
            context,
            action="notification.read",
            tenant_id=tenant_id,
            project_id=project_id,
        )
        subscription = self.store.read_subscription(
            subscription_id,
            tenant_id=tenant_id,
            project_id=project_id,
        )
        if subscription is None or subscription["subscriber_ref"] != context.subject_ref:
            raise CollaborationNotificationError("notification operation is not authorized")
        cursor = None
        if last_event_id is not None:
            if not isinstance(last_event_id, str):
                raise CollaborationNotificationError("SSE Last-Event-ID is invalid")
            match = _SSE_EVENT_ID_RE.fullmatch(last_event_id)
            events = self.store.read_project_events(
                tenant_id=tenant_id,
                project_id=project_id,
            )
            if match is None:
                raise CollaborationNotificationError("SSE Last-Event-ID is invalid")
            sequence_no = int(match.group(1))
            event_sha256 = match.group(2)
            if (
                sequence_no > len(events)
                or events[sequence_no - 1]["event_sha256"] != event_sha256
            ):
                raise CollaborationNotificationError("SSE Last-Event-ID is stale")
            cursor = self._build_cursor(
                subscription,
                last_sequence_no=sequence_no,
                last_event_sha256=event_sha256,
            )
        batch = self.pull(
            subscription_id,
            tenant_id=tenant_id,
            project_id=project_id,
            cursor=cursor,
            limit=limit,
            context=context,
        )
        delivered = batch["events"]
        return {
            "content_type": "text/event-stream",
            "payload": render_sse_batch(batch, signer=self.store.signer),
            "delivered_events": len(delivered),
            "last_event_id": (
                _sse_event_id(delivered[-1]) if delivered else last_event_id
            ),
            "has_more": batch["has_more"],
            "next_cursor": copy.deepcopy(batch["next_cursor"]),
            "retry_ms": 1000,
            "context_injection_authority": False,
            "operation_authority": False,
            "state_write_authority": False,
        }


def validate_collaboration_delivery_batch(
    value: Any,
    *,
    signer: HMACNotificationSigner,
) -> dict[str, Any]:
    """Validate a batch and every signed document it carries."""
    batch = _strict_object(value, _BATCH_FIELDS, "delivery batch")
    if batch["schema_version"] != BATCH_SCHEMA_VERSION:
        raise CollaborationNotificationError("delivery batch version is invalid")
    for field in ("subscription_id", "tenant_id", "project_id", "provider"):
        _identifier(batch[field], field)
    if not isinstance(batch["events"], list) or len(batch["events"]) > 1000:
        raise CollaborationNotificationError("delivery batch events are invalid")
    events = [
        validate_collaboration_notification_event(event, signer=signer)
        for event in batch["events"]
    ]
    for index, event in enumerate(events):
        if (
            event["tenant_id"] != batch["tenant_id"]
            or event["project_id"] != batch["project_id"]
            or (index > 0 and event["sequence_no"] <= events[index - 1]["sequence_no"])
        ):
            raise CollaborationNotificationError("delivery batch event binding is invalid")
    cursor = _validate_cursor(batch["next_cursor"], signer=signer)
    if (
        cursor["subscription_id"] != batch["subscription_id"]
        or cursor["tenant_id"] != batch["tenant_id"]
        or cursor["project_id"] != batch["project_id"]
    ):
        raise CollaborationNotificationError("delivery batch cursor binding is invalid")
    if events and (
        cursor["last_sequence_no"] != events[-1]["sequence_no"]
        or cursor["last_event_sha256"] != events[-1]["event_sha256"]
    ):
        raise CollaborationNotificationError("delivery batch cursor position is invalid")
    if type(batch["has_more"]) is not bool:
        raise CollaborationNotificationError("delivery batch has_more is invalid")
    if (
        batch["delivery_authority"] is not False
        or batch["state_write_authority"] is not False
    ):
        raise CollaborationNotificationError("delivery batch cannot claim authority")
    digest = _sha256(batch["batch_sha256"], "batch_sha256")
    body = {
        key: item
        for key, item in batch.items()
        if key not in {"batch_sha256", "signature"}
    }
    if digest != _digest(body) or not signer.verify(digest, batch["signature"]):
        raise CollaborationNotificationError("delivery batch digest or signature mismatch")
    return batch


def _sse_event_id(event: dict[str, Any]) -> str:
    return f"{event['sequence_no']}:{event['event_sha256']}"


def render_sse_batch(
    value: Any,
    *,
    signer: HMACNotificationSigner,
) -> bytes:
    """Render one deterministic SSE batch without granting delivery authority."""
    batch = validate_collaboration_delivery_batch(value, signer=signer)
    frames = []
    for event in batch["events"]:
        frames.append(
            b"id: "
            + _sse_event_id(event).encode("ascii")
            + b"\nevent: "
            + event["event_kind"].encode("ascii")
            + b"\ndata: "
            + _canonical(event)
            + b"\n\n"
        )
    return b"".join(frames)


def build_agent_inbox_items(
    value: Any,
    *,
    signer: HMACNotificationSigner,
) -> list[dict[str, Any]]:
    """Project signed events into display-only Agent inbox records."""
    batch = validate_collaboration_delivery_batch(value, signer=signer)
    return [
        {
            "event_id": event["event_id"],
            "event_kind": event["event_kind"],
            "summary": event["summary"],
            "work_id": event["work_id"],
            "evidence_refs": copy.deepcopy(event["evidence_refs"]),
            "requires_approval": event["requires_approval"],
            "display_allowed": True,
            "approval_submission_allowed": False,
            "context_injection_allowed": False,
            "operation_allowed": False,
            "state_write_authority": False,
        }
        for event in batch["events"]
    ]


__all__ = [
    "BATCH_SCHEMA_VERSION",
    "CURSOR_SCHEMA_VERSION",
    "EVENT_KINDS",
    "NOTIFICATION_EVENT_SCHEMA_VERSION",
    "PUBLISH_REQUEST_SCHEMA_VERSION",
    "SUBSCRIPTION_REQUEST_SCHEMA_VERSION",
    "SUBSCRIPTION_SCHEMA_VERSION",
    "CollaborationNotificationConflict",
    "CollaborationNotificationError",
    "CollaborationNotificationService",
    "HMACNotificationSigner",
    "SQLiteCollaborationNotificationStore",
    "build_agent_inbox_items",
    "render_sse_batch",
    "validate_collaboration_delivery_batch",
    "validate_collaboration_notification_chain",
    "validate_collaboration_notification_event",
]
