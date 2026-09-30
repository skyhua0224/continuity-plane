"""M8-10 collaboration notification strict schema and registry tests."""

from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from context_control_plane.collaboration_notification_benchmark import (
    benchmark_collaboration_notifications,
)
from context_control_plane.collaboration_notifications import (
    CollaborationNotificationService,
    HMACNotificationSigner,
    SQLiteCollaborationNotificationStore,
    build_agent_inbox_items,
)
from context_control_plane.state_mcp import RequestContext
from tests.test_m8_10_collaboration_notifications import (
    NOW,
    _Authorizer,
    _publish_request,
    _subscription_request,
)

SCHEMAS = {
    "context.collaboration-notification-publish": "collaboration-notification-publish.schema.json",
    "context.collaboration-notification": "collaboration-notification.schema.json",
    "context.collaboration-subscription-request": "collaboration-subscription-request.schema.json",
    "context.collaboration-subscription": "collaboration-subscription.schema.json",
    "context.collaboration-subscription-cursor": "collaboration-subscription-cursor.schema.json",
    "context.collaboration-delivery-batch": "collaboration-delivery-batch.schema.json",
    "context.agent-inbox-item": "agent-inbox-item.schema.json",
    "context.collaboration-notification-benchmark": "collaboration-notification-benchmark.schema.json",
}


class M810ContractSchemaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]
        cls.schema_dir = cls.root / "schemas" / "m8-10"
        cls.schemas = {
            schema_id: json.loads((cls.schema_dir / filename).read_text(encoding="utf-8"))
            for schema_id, filename in SCHEMAS.items()
        }
        for schema in cls.schemas.values():
            Draft202012Validator.check_schema(schema)
        cls.registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema))
            for schema in cls.schemas.values()
        )

    @classmethod
    def samples(cls) -> dict[str, dict]:
        with tempfile.TemporaryDirectory() as directory:
            signer = HMACNotificationSigner(
                key_id="local-m8-10",
                secret=b"m8-10-test-signing-key-material",
            )
            store = SQLiteCollaborationNotificationStore(
                Path(directory) / "notifications.sqlite3",
                signer=signer,
            )
            service = CollaborationNotificationService(
                store,
                authorizer=_Authorizer(denied_subjects={"actor-denied"}),
                clock=lambda: NOW,
            )
            publisher = RequestContext("actor-executor", "authorization-executor")
            subscriber = RequestContext("actor-codex", "authorization-codex")
            publish_request = _publish_request("publish-schema")
            event = service.publish(publish_request, context=publisher)
            subscription_request = _subscription_request(
                "subscription-schema", provider="codex"
            )
            subscription = service.subscribe(
                subscription_request,
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
            inbox = build_agent_inbox_items(batch, signer=signer)[0]
            store.close()
        return {
            "context.collaboration-notification-publish": publish_request,
            "context.collaboration-notification": event,
            "context.collaboration-subscription-request": subscription_request,
            "context.collaboration-subscription": subscription,
            "context.collaboration-subscription-cursor": batch["next_cursor"],
            "context.collaboration-delivery-batch": batch,
            "context.agent-inbox-item": inbox,
            "context.collaboration-notification-benchmark": benchmark_collaboration_notifications(
                root=cls.root,
                event_count=2,
                campaign_steps=2,
                generated_at="2026-08-17T13:30:00+08:00",
            ),
        }

    def test_runtime_documents_match_strict_schemas(self) -> None:
        for schema_id, sample in self.samples().items():
            with self.subTest(schema_id=schema_id):
                Draft202012Validator(
                    self.schemas[schema_id],
                    format_checker=FormatChecker(),
                    registry=self.registry,
                ).validate(sample)

    def test_all_top_level_fields_are_required_and_unknown_fields_rejected(self) -> None:
        for schema_id, sample in self.samples().items():
            schema = self.schemas[schema_id]
            validator = Draft202012Validator(
                schema,
                format_checker=FormatChecker(),
                registry=self.registry,
            )
            self.assertFalse(schema["additionalProperties"])
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            with self.subTest(schema_id=schema_id, mutation="extra"), self.assertRaises(
                ValidationError
            ):
                validator.validate({**sample, "unexpected": True})
            for field in sample:
                missing = copy.deepcopy(sample)
                del missing[field]
                with self.subTest(
                    schema_id=schema_id,
                    missing=field,
                ), self.assertRaises(ValidationError):
                    validator.validate(missing)

    def test_registry_entries_match_schema_hashes(self) -> None:
        registry = yaml.safe_load(
            (self.root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entries = {
            entry["schema_id"]: entry
            for entry in registry["schemas"]
            if entry["schema_id"] in SCHEMAS
        }
        self.assertEqual(set(entries), set(SCHEMAS))
        for schema_id, filename in SCHEMAS.items():
            path = self.schema_dir / filename
            with self.subTest(schema_id=schema_id):
                self.assertEqual(
                    entries[schema_id]["artifact_path"],
                    path.relative_to(self.root).as_posix(),
                )
                self.assertEqual(
                    entries[schema_id]["content_sha256"],
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )


if __name__ == "__main__":
    unittest.main()
