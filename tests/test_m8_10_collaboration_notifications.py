"""M8-10 signed collaboration notification and Agent inbox contract."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from context_control_plane.collaboration_notifications import (
    CollaborationNotificationConflict,
    CollaborationNotificationError,
    CollaborationNotificationService,
    HMACNotificationSigner,
    SQLiteCollaborationNotificationStore,
    build_agent_inbox_items,
    render_sse_batch,
    validate_collaboration_delivery_batch,
    validate_collaboration_notification_event,
)
from context_control_plane.state_mcp import RequestContext

NOW = "2026-08-17T13:00:00+08:00"


def _digest(value: dict) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _Authorizer:
    def __init__(self, denied_subjects: set[str] | None = None) -> None:
        self.denied_subjects = denied_subjects or set()
        self.calls: list[tuple[str, str, str, str]] = []
        self._tenant_by_project = {
            "project-m8-10": "tenant-m8-10",
            "project-m8-10-secondary": "tenant-m8-10",
        }

    def authorize(self, context, action: str, project_id: str) -> bool:
        return context.subject_ref not in self.denied_subjects

    def authorize_scope(
        self,
        context,
        action: str,
        tenant_id: str,
        project_id: str,
    ) -> bool:
        self.calls.append((context.subject_ref, action, tenant_id, project_id))
        return (
            context.subject_ref not in self.denied_subjects
            and self._tenant_by_project.get(project_id) == tenant_id
        )

    def verify_notification_source(self, context, request: dict) -> bool:
        return (
            request["tenant_id"] == self._tenant_by_project.get(request["project_id"])
            and request["state_revision"] == 67
            and request["work_id"] == "M8-10"
            and request["evidence_refs"] == ["artifact://m8-10/evidence"]
        )


def _publish_request(
    request_id: str,
    *,
    event_kind: str = "work_claimed",
    summary: str = "Work claimed by the active executor.",
    requires_approval: bool = False,
) -> dict:
    return {
        "schema_version": "context.collaboration-notification-publish/v1alpha1",
        "request_id": request_id,
        "tenant_id": "tenant-m8-10",
        "project_id": "project-m8-10",
        "state_revision": 67,
        "event_kind": event_kind,
        "work_id": "M8-10",
        "summary": summary,
        "target_refs": ["actor://reviewer"],
        "evidence_refs": ["artifact://m8-10/evidence"],
        "requires_approval": requires_approval,
        "causation_ref": "work:M8-10",
        "correlation_ref": "campaign:M8",
    }


def _subscription_request(
    subscription_id: str,
    *,
    provider: str,
    transport: str = "sse",
    request_id: str | None = None,
    project_id: str = "project-m8-10",
) -> dict:
    return {
        "schema_version": "context.collaboration-subscription-request/v1alpha1",
        "request_id": request_id or f"request-{subscription_id}",
        "subscription_id": subscription_id,
        "tenant_id": "tenant-m8-10",
        "project_id": project_id,
        "provider": provider,
        "transport": transport,
        "event_kinds": [
            "approval_requested",
            "conflict_detected",
            "deploy_intent",
            "review_requested",
            "work_claimed",
        ],
    }


class M810CollaborationNotificationTests(unittest.TestCase):
    def _service(self, path: Path, *, websocket: bool = False):
        signer = HMACNotificationSigner(
            key_id="local-m8-10",
            secret=b"m8-10-test-signing-key-material",
        )
        authorizer = _Authorizer(denied_subjects={"actor-denied"})
        store = SQLiteCollaborationNotificationStore(path, signer=signer)
        service = CollaborationNotificationService(
            store,
            authorizer=authorizer,
            clock=lambda: NOW,
            websocket_available=websocket,
        )
        return service, store, signer, authorizer

    def test_publish_is_append_only_signed_exactly_replayable_and_conflict_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notifications.sqlite3"
            service, store, signer, _authorizer = self._service(path)
            context = RequestContext("actor-executor", "authorization-executor")
            request = _publish_request("publish-1")

            first = service.publish(request, context=context)
            replay = service.publish(copy.deepcopy(request), context=context)

            self.assertEqual(first, replay)
            self.assertEqual(first["sequence_no"], 1)
            self.assertEqual(first["previous_event_sha256"], "0" * 64)
            self.assertTrue(signer.verify(first["event_sha256"], first["signature"]))
            self.assertEqual(
                validate_collaboration_notification_event(first, signer=signer),
                first,
            )

            changed = copy.deepcopy(request)
            changed["summary"] = "Conflicting replay payload."
            with self.assertRaises(CollaborationNotificationConflict):
                service.publish(changed, context=context)

            second = service.publish(
                _publish_request(
                    "publish-2",
                    event_kind="review_requested",
                    summary="Independent review requested.",
                ),
                context=context,
            )
            self.assertEqual(second["sequence_no"], 2)
            self.assertEqual(
                second["previous_event_sha256"], first["event_sha256"]
            )

            with closing(sqlite3.connect(path)) as connection:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE collaboration_notification_events "
                        "SET event_json = '{}' WHERE sequence_no = 1"
                    )
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "DELETE FROM collaboration_notification_events "
                        "WHERE sequence_no = 1"
                    )
            store.close()

    def test_two_provider_sessions_receive_identical_offline_catch_up_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notifications.sqlite3"
            service, store, _signer, _authorizer = self._service(path)
            publisher = RequestContext("actor-executor", "authorization-executor")
            codex = RequestContext("actor-codex-session", "authorization-codex")
            claude = RequestContext("actor-claude-session", "authorization-claude")
            for index, kind in enumerate(
                ("work_claimed", "review_requested", "deploy_intent"), start=1
            ):
                service.publish(
                    _publish_request(
                        f"publish-{index}",
                        event_kind=kind,
                        summary=f"Notification {index}.",
                        requires_approval=kind == "deploy_intent",
                    ),
                    context=publisher,
                )
            codex_subscription = service.subscribe(
                _subscription_request("subscription-codex", provider="codex"),
                context=codex,
            )
            claude_subscription = service.subscribe(
                _subscription_request("subscription-claude", provider="claude"),
                context=claude,
            )

            codex_first = service.pull(
                codex_subscription["subscription_id"],
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                cursor=None,
                limit=2,
                context=codex,
            )
            claude_first = service.pull(
                claude_subscription["subscription_id"],
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                cursor=None,
                limit=2,
                context=claude,
            )
            self.assertEqual(codex_first["events"], claude_first["events"])
            self.assertTrue(codex_first["has_more"])
            store.close()

            restarted, restarted_store, _signer, _authorizer = self._service(path)
            codex_second = restarted.pull(
                "subscription-codex",
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                cursor=codex_first["next_cursor"],
                limit=10,
                context=codex,
            )
            claude_second = restarted.pull(
                "subscription-claude",
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                cursor=claude_first["next_cursor"],
                limit=10,
                context=claude,
            )
            self.assertEqual(codex_second["events"], claude_second["events"])
            self.assertEqual(
                [item["sequence_no"] for item in codex_second["events"]], [3]
            )
            self.assertFalse(codex_second["has_more"])

            forged_cursor = copy.deepcopy(codex_first["next_cursor"])
            forged_cursor["last_sequence_no"] = 1
            with self.assertRaisesRegex(CollaborationNotificationError, "cursor"):
                restarted.pull(
                    "subscription-codex",
                    tenant_id="tenant-m8-10",
                    project_id="project-m8-10",
                    cursor=forged_cursor,
                    limit=10,
                    context=codex,
                )
            restarted_store.close()

    def test_authorization_precedes_lookup_and_cross_subscription_reads_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _signer, authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            allowed = RequestContext("actor-codex", "authorization-codex")
            denied = RequestContext("actor-denied", "authorization-denied")
            service.subscribe(
                _subscription_request("subscription-private", provider="codex"),
                context=allowed,
            )

            for subscription_id in ("subscription-private", "subscription-missing"):
                with self.assertRaisesRegex(
                    CollaborationNotificationError, "not authorized"
                ):
                    service.pull(
                        subscription_id,
                        tenant_id="tenant-m8-10",
                        project_id="project-m8-10",
                        cursor=None,
                        limit=10,
                        context=denied,
                    )
            self.assertEqual(
                authorizer.calls[-2:],
                [
                    (
                        "actor-denied",
                        "notification.read",
                        "tenant-m8-10",
                        "project-m8-10",
                    ),
                    (
                        "actor-denied",
                        "notification.read",
                        "tenant-m8-10",
                        "project-m8-10",
                    ),
                ],
            )
            with self.assertRaisesRegex(
                CollaborationNotificationError, "not authorized"
            ):
                service.pull(
                    "subscription-private",
                    tenant_id="tenant-m8-10",
                    project_id="project-m8-10",
                    cursor=None,
                    limit=10,
                    context=RequestContext(
                        "actor-other", "authorization-other"
                    ),
                )
            store.close()

    def test_sse_is_deterministic_websocket_is_capability_gated_and_inbox_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _signer, _authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            publisher = RequestContext("actor-executor", "authorization-executor")
            subscriber = RequestContext("actor-codex", "authorization-codex")
            service.publish(
                _publish_request(
                    "publish-deploy",
                    event_kind="deploy_intent",
                    summary="Deployment approval requested.",
                    requires_approval=True,
                ),
                context=publisher,
            )
            subscription = service.subscribe(
                _subscription_request("subscription-sse", provider="codex"),
                context=subscriber,
            )
            batch = service.pull(
                subscription["subscription_id"],
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                cursor=None,
                limit=10,
                context=subscriber,
            )
            self.assertEqual(
                render_sse_batch(batch, signer=_signer),
                render_sse_batch(batch, signer=_signer),
            )
            self.assertIn(
                b"event: deploy_intent\n",
                render_sse_batch(batch, signer=_signer),
            )
            inbox = build_agent_inbox_items(batch, signer=_signer)
            self.assertEqual(len(inbox), 1)
            self.assertTrue(inbox[0]["display_allowed"])
            self.assertFalse(inbox[0]["approval_submission_allowed"])
            self.assertFalse(inbox[0]["context_injection_allowed"])
            self.assertFalse(inbox[0]["operation_allowed"])
            self.assertFalse(inbox[0]["state_write_authority"])

            with self.assertRaisesRegex(
                CollaborationNotificationError, "websocket"
            ):
                service.subscribe(
                    _subscription_request(
                        "subscription-ws", provider="codex", transport="websocket"
                    ),
                    context=subscriber,
                )
            store.close()

            websocket_service, websocket_store, _signer, _authorizer = self._service(
                Path(directory) / "notifications-ws.sqlite3", websocket=True
            )
            websocket_subscription = websocket_service.subscribe(
                _subscription_request(
                    "subscription-ws", provider="codex", transport="websocket"
                ),
                context=subscriber,
            )
            self.assertEqual(websocket_subscription["transport"], "websocket")
            self.assertTrue(websocket_subscription["websocket_available"])
            websocket_store.close()

    def test_batch_rejects_nested_event_tampering_even_with_recomputed_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, signer, _authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            publisher = RequestContext("actor-executor", "authorization-executor")
            subscriber = RequestContext("actor-codex", "authorization-codex")
            service.publish(_publish_request("publish-signed"), context=publisher)
            subscription = service.subscribe(
                _subscription_request("subscription-signed", provider="codex"),
                context=subscriber,
            )
            batch = service.pull(
                subscription["subscription_id"],
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                cursor=None,
                limit=10,
                context=subscriber,
            )

            forged = copy.deepcopy(batch)
            forged["events"][0]["summary"] = "Forged inbox content."
            forged["batch_sha256"] = _digest(
                {key: value for key, value in forged.items() if key != "batch_sha256"}
            )
            with self.assertRaisesRegex(CollaborationNotificationError, "digest"):
                validate_collaboration_delivery_batch(forged, signer=signer)
            store.close()

    def test_batch_cursor_is_bound_to_subscription_and_last_delivered_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, signer, _authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            publisher = RequestContext("actor-executor", "authorization-executor")
            subscriber = RequestContext("actor-codex", "authorization-codex")
            service.publish(_publish_request("publish-cursor"), context=publisher)
            subscription = service.subscribe(
                _subscription_request("subscription-cursor", provider="codex"),
                context=subscriber,
            )
            batch = service.pull(
                subscription["subscription_id"],
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                cursor=None,
                limit=10,
                context=subscriber,
            )

            forged = copy.deepcopy(batch)
            forged["next_cursor"]["last_sequence_no"] = 0
            cursor_body = {
                key: value
                for key, value in forged["next_cursor"].items()
                if key not in {"cursor_sha256", "signature"}
            }
            forged["next_cursor"]["cursor_sha256"] = _digest(cursor_body)
            forged["next_cursor"]["signature"] = signer.sign(
                forged["next_cursor"]["cursor_sha256"]
            )
            forged["batch_sha256"] = _digest(
                {key: value for key, value in forged.items() if key != "batch_sha256"}
            )
            with self.assertRaisesRegex(CollaborationNotificationError, "cursor"):
                validate_collaboration_delivery_batch(forged, signer=signer)
            store.close()

    def test_signed_batch_rejects_event_omission_with_a_valid_future_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, signer, _authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            publisher = RequestContext("actor-executor", "authorization-executor")
            subscriber = RequestContext("actor-codex", "authorization-codex")
            service.publish(_publish_request("publish-omission"), context=publisher)
            subscription = service.subscribe(
                _subscription_request("subscription-omission", provider="codex"),
                context=subscriber,
            )
            batch = service.pull(
                subscription["subscription_id"],
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                cursor=None,
                limit=10,
                context=subscriber,
            )

            forged = copy.deepcopy(batch)
            forged["events"] = []
            forged["batch_sha256"] = _digest(
                {key: value for key, value in forged.items() if key != "batch_sha256"}
            )
            with self.assertRaisesRegex(CollaborationNotificationError, "signature"):
                validate_collaboration_delivery_batch(forged, signer=signer)
            store.close()

    def test_tenant_scope_and_current_state_source_are_required_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _signer, _authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            context = RequestContext("actor-executor", "authorization-executor")
            wrong_tenant = _publish_request("publish-wrong-tenant")
            wrong_tenant["tenant_id"] = "tenant-other"
            with self.assertRaisesRegex(CollaborationNotificationError, "authorized"):
                service.publish(wrong_tenant, context=context)

            stale_state = _publish_request("publish-stale-state")
            stale_state["state_revision"] = 66
            with self.assertRaisesRegex(CollaborationNotificationError, "state source"):
                service.publish(stale_state, context=context)
            store.close()

    def test_subscription_request_identity_is_project_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _signer, _authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            context = RequestContext("actor-codex", "authorization-codex")
            first = service.subscribe(
                _subscription_request(
                    "subscription-shared",
                    provider="codex",
                    request_id="subscription-request-shared",
                ),
                context=context,
            )
            second = service.subscribe(
                _subscription_request(
                    "subscription-shared",
                    provider="codex",
                    request_id="subscription-request-shared",
                    project_id="project-m8-10-secondary",
                ),
                context=context,
            )

            self.assertEqual(first["project_id"], "project-m8-10")
            self.assertEqual(second["project_id"], "project-m8-10-secondary")
            store.close()

    def test_non_approval_events_cannot_open_an_approval_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, _signer, _authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            request = _publish_request("publish-false-approval")
            request["requires_approval"] = True
            with self.assertRaisesRegex(CollaborationNotificationError, "approval"):
                service.publish(
                    request,
                    context=RequestContext(
                        "actor-executor", "authorization-executor"
                    ),
                )
            store.close()

    def test_sse_last_event_id_resumes_after_restart_without_replaying_old_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notifications.sqlite3"
            service, store, _signer, _authorizer = self._service(path)
            publisher = RequestContext("actor-executor", "authorization-executor")
            subscriber = RequestContext("actor-codex", "authorization-codex")
            for index in range(1, 3):
                service.publish(
                    _publish_request(f"publish-sse-{index}"),
                    context=publisher,
                )
            subscription = service.subscribe(
                _subscription_request("subscription-reconnect", provider="codex"),
                context=subscriber,
            )
            first = service.pull_sse(
                subscription["subscription_id"],
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                last_event_id=None,
                limit=2,
                context=subscriber,
            )
            self.assertEqual(first["delivered_events"], 2)
            self.assertEqual(first["last_event_id"].split(":", 1)[0], "2")
            store.close()

            restarted, restarted_store, _signer, authorizer = self._service(path)
            restarted.publish(_publish_request("publish-sse-3"), context=publisher)
            second = restarted.pull_sse(
                "subscription-reconnect",
                tenant_id="tenant-m8-10",
                project_id="project-m8-10",
                last_event_id=first["last_event_id"],
                limit=1,
                context=subscriber,
            )
            self.assertEqual(second["delivered_events"], 1)
            self.assertIn(b"publish-sse-3", second["payload"])
            self.assertEqual(second["last_event_id"].split(":", 1)[0], "3")
            self.assertEqual(
                authorizer.calls[-1],
                (
                    "actor-codex",
                    "notification.read",
                    "tenant-m8-10",
                    "project-m8-10",
                ),
            )
            restarted_store.close()

    def test_concurrent_publishers_commit_one_gapless_signed_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, store, signer, _authorizer = self._service(
                Path(directory) / "notifications.sqlite3"
            )
            context = RequestContext("actor-executor", "authorization-executor")
            with ThreadPoolExecutor(max_workers=8) as executor:
                events = list(
                    executor.map(
                        lambda index: service.publish(
                            _publish_request(f"publish-concurrent-{index}"),
                            context=context,
                        ),
                        range(20),
                    )
                )

            ordered = sorted(events, key=lambda event: event["sequence_no"])
            self.assertEqual(
                [event["sequence_no"] for event in ordered],
                list(range(1, 21)),
            )
            self.assertEqual(
                [event["previous_event_sha256"] for event in ordered[1:]],
                [event["event_sha256"] for event in ordered[:-1]],
            )
            self.assertTrue(
                all(
                    signer.verify(event["event_sha256"], event["signature"])
                    for event in ordered
                )
            )
            store.close()

    def test_restart_with_the_wrong_hmac_key_rejects_existing_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notifications.sqlite3"
            service, store, _signer, _authorizer = self._service(path)
            service.publish(
                _publish_request("publish-key-mismatch"),
                context=RequestContext(
                    "actor-executor", "authorization-executor"
                ),
            )
            store.close()

            wrong_signer = HMACNotificationSigner(
                key_id="wrong-m8-10",
                secret=b"different-m8-10-signing-material",
            )
            wrong_store = SQLiteCollaborationNotificationStore(
                path,
                signer=wrong_signer,
            )
            with self.assertRaisesRegex(
                CollaborationNotificationError,
                "signature",
            ):
                wrong_store.read_project_events(
                    tenant_id="tenant-m8-10",
                    project_id="project-m8-10",
                )
            wrong_store.close()


if __name__ == "__main__":
    unittest.main()
