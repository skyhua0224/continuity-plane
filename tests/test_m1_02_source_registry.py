import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

from context_control_plane.source_secret import provision_namespace_secret
from context_control_plane.source_registry import (
    ProvenanceError,
    SourceRegistry,
    build_provenance,
    validate_provenance,
)


class SourceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.key = b"m1-02-test-key"

    def test_same_source_maps_to_the_same_opaque_thread_ref(self):
        first = SourceRegistry(self.key).register(
            "context-control-plane", "codex", "019fe216-111c-71e3-a1af-497306ba2391"
        )
        second = SourceRegistry(self.key).register(
            "context-control-plane", "codex", "019fe216-111c-71e3-a1af-497306ba2391"
        )

        self.assertEqual(first["source_thread_ref"], second["source_thread_ref"])
        self.assertNotIn("019fe216", json.dumps(first))
        self.assertRegex(first["source_thread_ref"], r"^thr_[a-z2-7]{26}$")

    def test_different_source_identity_gets_a_different_ref(self):
        registry = SourceRegistry(self.key)
        first = registry.register("project-a", "codex", "thread-a")
        second = registry.register("project-a", "codex", "thread-b")

        self.assertNotEqual(first["source_thread_ref"], second["source_thread_ref"])

    def test_namespace_key_is_part_of_the_opaque_reference_boundary(self):
        first = SourceRegistry(b"namespace-a").register("project-a", "codex", "thread-a")
        second = SourceRegistry(b"namespace-b").register("project-a", "codex", "thread-a")

        self.assertNotEqual(first["source_thread_ref"], second["source_thread_ref"])

    def test_encoded_namespace_secret_is_strong_and_never_serialized(self):
        encoded_secret = base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")

        registry = SourceRegistry.from_base64_secret(
            encoded_secret,
            opaque_key_id="source-key-2026-08",
        )
        registry.register("project-a", "codex", "thread-a")
        public_document = json.dumps(registry.to_document())

        self.assertNotIn(encoded_secret, public_document)
        self.assertNotIn("kkkkkkkk", public_document)
        self.assertIn("source-key-2026-08", public_document)

    def test_encoded_namespace_secret_rejects_weak_or_malformed_input(self):
        weak_secret = base64.urlsafe_b64encode(b"short").decode().rstrip("=")

        for encoded_secret in (weak_secret, "not+base64url", ""):
            with self.subTest(encoded_secret=encoded_secret):
                with self.assertRaises(ValueError):
                    SourceRegistry.from_base64_secret(
                        encoded_secret,
                        opaque_key_id="source-key-2026-08",
                    )

    def test_public_key_identifier_rejects_paths_and_secret_like_values(self):
        for key_id in ("/home/user/key", "token=private", "contains spaces"):
            with self.subTest(key_id=key_id):
                with self.assertRaises(ValueError):
                    SourceRegistry(self.key, opaque_key_id=key_id)

    def test_private_secret_file_provisions_once_and_loads_without_public_leakage(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "source-namespace.json"

            key_id = provision_namespace_secret(
                secret_path,
                opaque_key_id="source-key-2026-08",
            )
            registry = SourceRegistry.from_secret_file(secret_path)
            registry.register("project-a", "codex", "thread-a")

            self.assertEqual(key_id, "source-key-2026-08")
            self.assertEqual(secret_path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn(
                json.loads(secret_path.read_text())["encoded_namespace_key"],
                json.dumps(registry.to_document()),
            )
            with self.assertRaises(FileExistsError):
                provision_namespace_secret(
                    secret_path,
                    opaque_key_id="source-key-2026-08",
                )

    def test_secret_file_loader_rejects_wide_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            secret_path = Path(directory) / "source-namespace.json"
            provision_namespace_secret(
                secret_path,
                opaque_key_id="source-key-2026-08",
            )
            os.chmod(secret_path, 0o644)

            with self.assertRaises(ValueError):
                SourceRegistry.from_secret_file(secret_path)

            os.chmod(secret_path, 0o600)
            link_path = Path(directory) / "source-namespace-link.json"
            link_path.symlink_to(secret_path)
            with self.assertRaises(ValueError):
                SourceRegistry.from_secret_file(link_path)

    def test_range_ref_is_stable_and_distinct_for_a_byte_range(self):
        registry = SourceRegistry(self.key)
        thread_ref = registry.register("project-a", "claude", "thread-a")[
            "source_thread_ref"
        ]

        first = registry.range_ref(thread_ref, 10, 20)
        second = registry.range_ref(thread_ref, 10, 20)
        other = registry.range_ref(thread_ref, 10, 21)

        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertRegex(first, r"^rng_[a-z2-7]{26}$")

    def test_range_ref_rejects_invalid_thread_ref_and_ranges(self):
        registry = SourceRegistry(self.key)
        with self.assertRaises(ValueError):
            registry.range_ref("provider-thread-id", 0, 1)

        thread_ref = registry.register("project-a", "codex", "thread-a")[
            "source_thread_ref"
        ]
        with self.assertRaises(ValueError):
            registry.range_ref(thread_ref, -1, 1)
        with self.assertRaises(ValueError):
            registry.range_ref(thread_ref, 2, 1)

    def test_public_registry_record_contains_only_non_sensitive_metadata(self):
        record = SourceRegistry(self.key).register(
            "project-a", "claude", "/private/archive/thread-7"
        )

        self.assertEqual(
            set(record),
            {
                "schema_version",
                "project_id",
                "source_provider",
                "source_thread_ref",
                "source_kind",
                "retention_class",
                "opaque_key_id",
            },
        )
        self.assertNotIn("archive", json.dumps(record))
        self.assertNotIn("thread-7", json.dumps(record))


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        registry = SourceRegistry(b"m1-02-test-key")
        self.thread_ref = registry.register("project-a", "codex", "thread-a")[
            "source_thread_ref"
        ]
        self.range_ref = registry.range_ref(self.thread_ref, 0, 128)

    def make_payload(self, **overrides):
        payload = build_provenance(
            source_provider="codex",
            source_thread_ref=self.thread_ref,
            source_range_ref=self.range_ref,
            project_id="project-a",
            extracted_at="2026-08-09T12:00:00Z",
            extractor_version="1.0.0",
            content_sha256="a" * 64,
            classification="decision",
            validity="candidate",
            verified_against=[],
            contains_sensitive_data=False,
            retention_class="project",
        )
        payload.update(overrides)
        return payload

    def test_candidate_provenance_can_have_no_current_evidence(self):
        validate_provenance(self.make_payload())

    def test_verified_provenance_requires_current_evidence(self):
        with self.assertRaises(ProvenanceError):
            validate_provenance(self.make_payload(validity="verified"))

        validate_provenance(
            self.make_payload(
                validity="verified", verified_against=["assertion:RFC9002-5.3"]
            )
        )

    def test_provenance_rejects_original_provider_thread_id(self):
        payload = self.make_payload(provider_thread_id="raw-provider-id")

        with self.assertRaises(ProvenanceError):
            validate_provenance(payload)

    def test_provenance_rejects_unknown_fields_and_wrong_schema_version(self):
        with self.assertRaises(ProvenanceError):
            validate_provenance(self.make_payload(unexpected="value"))
        with self.assertRaises(ProvenanceError):
            validate_provenance(self.make_payload(schema_version="m1-02.v0"))

    def test_sensitive_provenance_cannot_pass_admission(self):
        payload = self.make_payload(contains_sensitive_data=True)
        validate_provenance(payload)

        with self.assertRaises(ProvenanceError):
            validate_provenance(payload, for_admission=True)

    def test_schema_files_are_versioned(self):
        schema_dir = Path(__file__).parents[1] / "schemas" / "m1-02"
        for name in ("source-registry.schema.json", "provenance.schema.json"):
            with self.subTest(name=name):
                schema = json.loads((schema_dir / name).read_text())
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
                self.assertEqual(schema["$id"], f"context-control-plane/m1-02/{name}")
                self.assertEqual(schema["x-schema-version"], "m1-02.v1")


if __name__ == "__main__":
    unittest.main()
