import json
import unittest
from pathlib import Path

from context_control_plane.archive_governance import (
    ArchiveGovernanceError,
    ArchiveStore,
)


class ArchiveGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.thread_a = "thr_abcdefghijklmnopqrstuvwxyz"
        self.thread_b = "thr_bcdefghijklmnopqrstuvwxyza"
        self.created_at = "2026-08-09T00:00:00Z"
        self.expires_at = "2026-09-08T00:00:00Z"
        self.exported_at = "2026-08-09T12:00:00Z"

    def put(self, store, thread_ref, payload=b"sealed-payload", **overrides):
        values = {
            "project_id": "project-a",
            "source_thread_ref": thread_ref,
            "retention_class": "ephemeral",
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "sealed_payload": payload,
        }
        values.update(overrides)
        return store.put(**values)

    def test_ephemeral_retention_requires_a_finite_future_expiry(self):
        store = ArchiveStore()

        with self.assertRaises(ArchiveGovernanceError):
            self.put(store, self.thread_a, expires_at=None)
        with self.assertRaises(ArchiveGovernanceError):
            self.put(store, self.thread_a, expires_at=self.created_at)

        record = self.put(store, self.thread_a)
        self.assertEqual(record["retention_class"], "ephemeral")
        self.assertEqual(
            store.expired_source_refs("2026-09-09T00:00:00Z"),
            [self.thread_a],
        )

    def test_project_and_audit_retention_can_use_policy_driven_expiry(self):
        store = ArchiveStore()
        project = self.put(
            store,
            self.thread_a,
            retention_class="project",
            expires_at=None,
        )
        audit = self.put(
            store,
            self.thread_b,
            payload=b"other-sealed-payload",
            retention_class="audit",
            expires_at=None,
        )

        self.assertIsNone(project["expires_at"])
        self.assertIsNone(audit["expires_at"])
        self.assertEqual(store.expired_source_refs("2036-08-09T00:00:00Z"), [])

    def test_export_manifest_is_deterministic_and_contains_no_private_identity(self):
        first = ArchiveStore()
        self.put(first, self.thread_b, payload=b"payload-b")
        self.put(first, self.thread_a, payload=b"payload-a")

        second = ArchiveStore()
        self.put(second, self.thread_a, payload=b"payload-a")
        self.put(second, self.thread_b, payload=b"payload-b")

        first_bundle = first.export_bundle("project-a", exported_at=self.exported_at)
        second_bundle = second.export_bundle("project-a", exported_at=self.exported_at)

        self.assertEqual(first_bundle, second_bundle)
        document = json.loads(first_bundle)
        self.assertEqual(
            [item["source_thread_ref"] for item in document["manifest"]["records"]],
            sorted([self.thread_a, self.thread_b]),
        )
        serialized = first_bundle.decode("utf-8")
        self.assertNotIn("provider_thread_id", serialized)
        self.assertNotIn("archive_path", serialized)
        self.assertEqual(
            document["manifest"]["content_protection"],
            "sealed-by-archive-adapter",
        )

    def test_export_import_round_trip_is_byte_equivalent(self):
        source = ArchiveStore()
        self.put(source, self.thread_a, payload=b"synthetic-sealed-content")
        bundle = source.export_bundle("project-a", exported_at=self.exported_at)

        restored = ArchiveStore.from_export_bundle(bundle)

        self.assertEqual(
            restored.export_bundle("project-a", exported_at=self.exported_at),
            bundle,
        )

    def test_import_rejects_payload_or_manifest_tampering(self):
        source = ArchiveStore()
        self.put(source, self.thread_a)
        document = json.loads(
            source.export_bundle("project-a", exported_at=self.exported_at)
        )
        object_ref = next(iter(document["objects"]))
        document["objects"][object_ref] = "dGFtcGVyZWQ="

        with self.assertRaises(ArchiveGovernanceError):
            ArchiveStore.from_export_bundle(
                json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
            )

    def test_delete_removes_record_and_object_and_emits_minimal_tombstone(self):
        store = ArchiveStore()
        record = self.put(store, self.thread_a, payload=b"synthetic-secret-bytes")

        tombstone = store.delete_source(
            self.thread_a,
            deleted_at="2026-08-10T00:00:00Z",
            deletion_request_ref="req_abcdefghijklmnopqrstuvwxyz",
            authorization_ref="auth_abcdefghijklmnopqrstuvwxyz",
        )

        self.assertNotIn(self.thread_a, store.records())
        self.assertNotIn(record["object_ref"], store.object_refs())
        self.assertTrue(store.verify_deletion_proof(tombstone))
        self.assertEqual(
            set(tombstone),
            {
                "schema_version",
                "tombstone_id",
                "project_id",
                "source_thread_ref",
                "object_ref",
                "retention_class",
                "deleted_content_sha256",
                "deleted_size_bytes",
                "deleted_at",
                "deletion_request_ref",
                "authorization_ref",
                "deletion_scope",
                "absence_verified",
                "proof_sha256",
            },
        )
        self.assertNotIn("synthetic-secret-bytes", json.dumps(tombstone))

    def test_delete_requires_authorization_and_respects_legal_hold(self):
        store = ArchiveStore()
        self.put(store, self.thread_a, legal_hold=True)

        with self.assertRaises(ArchiveGovernanceError):
            store.delete_source(
                self.thread_a,
                deleted_at="2026-08-10T00:00:00Z",
                deletion_request_ref="req_abcdefghijklmnopqrstuvwxyz",
                authorization_ref="auth_abcdefghijklmnopqrstuvwxyz",
            )

        other = ArchiveStore()
        self.put(other, self.thread_b)
        with self.assertRaises(ArchiveGovernanceError):
            other.delete_source(
                self.thread_b,
                deleted_at="2026-08-10T00:00:00Z",
                deletion_request_ref="req_abcdefghijklmnopqrstuvwxyz",
                authorization_ref="",
            )

    def test_deletion_proof_detects_digest_tampering(self):
        store = ArchiveStore()
        self.put(store, self.thread_a)
        tombstone = store.delete_source(
            self.thread_a,
            deleted_at="2026-08-10T00:00:00Z",
            deletion_request_ref="req_abcdefghijklmnopqrstuvwxyz",
            authorization_ref="auth_abcdefghijklmnopqrstuvwxyz",
        )
        tombstone["deleted_size_bytes"] += 1

        self.assertFalse(store.verify_deletion_proof(tombstone))

    def test_archive_governance_schema_is_versioned_and_covers_all_documents(self):
        schema_path = (
            Path(__file__).parents[1]
            / "schemas"
            / "m1-06"
            / "archive-governance.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(
            schema["$id"],
            "context-control-plane/m1-06/archive-governance.schema.json",
        )
        self.assertEqual(schema["x-schema-version"], "m1-06.v1")
        self.assertEqual(
            set(schema["$defs"]),
            {"archiveRecord", "exportBundle", "tombstone"},
        )


if __name__ == "__main__":
    unittest.main()
