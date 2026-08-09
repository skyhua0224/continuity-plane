import hashlib
import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import json
import yaml

from context_control_plane.artifact_store import (
    ARTIFACT_REF_SCHEMA_VERSION,
    ArtifactInputError,
    ArtifactIntegrityError,
    ArtifactRangeError,
    ArtifactRef,
    LocalArtifactStore,
)


class _ShortReader:
    def __init__(self, payload: bytes, chunk_size: int = 3):
        self._stream = io.BytesIO(payload)
        self._chunk_size = chunk_size

    def read(self, _size: int = -1) -> bytes:
        return self._stream.read(self._chunk_size)


class _FailingReader:
    def __init__(self, payload: bytes):
        self._payload = io.BytesIO(payload)
        self._failed = False

    def read(self, _size: int = -1) -> bytes:
        if self._failed:
            raise OSError("injected source failure")
        self._failed = True
        return self._payload.read()


class M204ArtifactRefTests(unittest.TestCase):
    def test_artifact_ref_schema_is_registered_and_hashed(self):
        root = Path(__file__).parents[1]
        schema_path = root / "schemas/m2-04/artifact-ref.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        registry = yaml.safe_load(
            (root / "schemas/registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item for item in registry["schemas"]
            if item["schema_id"] == "context.artifact-ref"
        )
        self.assertEqual(entry["artifact_path"], "schemas/m2-04/artifact-ref.schema.json")
        self.assertEqual(
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
            entry["content_sha256"],
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))

    def test_ref_is_content_addressed_and_round_trips_strictly(self):
        payload = b"artifact-content\n"
        digest = hashlib.sha256(payload).hexdigest()
        ref = ArtifactRef(digest=digest, size_bytes=len(payload))

        self.assertEqual(ref.schema_version, ARTIFACT_REF_SCHEMA_VERSION)
        self.assertEqual(ref.digest_algorithm, "sha-256")
        self.assertEqual(ref.uri, f"artifact://sha256/{digest}")
        self.assertEqual(
            ArtifactRef.from_document(ref.to_document()),
            ref,
        )
        self.assertEqual(ArtifactRef.from_uri(ref.uri, len(payload)), ref)

    def test_ref_rejects_unknown_algorithm_digest_and_size(self):
        digest = "a" * 64
        with self.assertRaisesRegex(ValueError, "digest_algorithm"):
            ArtifactRef(digest=digest, size_bytes=1, digest_algorithm="sha1")
        with self.assertRaisesRegex(ValueError, "digest"):
            ArtifactRef(digest="not-a-digest", size_bytes=1)
        with self.assertRaisesRegex(ValueError, "size_bytes"):
            ArtifactRef(digest=digest, size_bytes=-1)
        with self.assertRaisesRegex(ValueError, "URI"):
            ArtifactRef.from_uri("artifact://sha256/../secret", 1)

    def test_ref_document_requires_exact_versioned_fields(self):
        ref = ArtifactRef(digest="b" * 64, size_bytes=0)
        broken = ref.to_document()
        broken["extra"] = True
        with self.assertRaisesRegex(ValueError, "fields"):
            ArtifactRef.from_document(broken)


class _ArtifactStoreFixture(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "artifacts"
        self.store = LocalArtifactStore(self.root, max_range_bytes=16)
        self.store.initialize()


class M204ArtifactStoreTests(_ArtifactStoreFixture):
    def test_streamed_put_deduplicates_and_reads_full_content(self):
        payload = b"hello content-addressed store"
        first = self.store.put_stream(_ShortReader(payload))
        second = self.store.put_bytes(payload)

        self.assertEqual(first, second)
        self.assertEqual(self.store.read(first), payload)
        self.assertEqual(self.store.verify(first), first)
        self.assertEqual(
            [
                path
                for path in (self.root / "objects" / "sha256").rglob("*")
                if path.is_file()
            ],
            [self.store.object_path(first)],
        )

    def test_empty_artifact_has_a_stable_reference(self):
        ref = self.store.put_bytes(b"")
        self.assertEqual(ref.size_bytes, 0)
        self.assertEqual(ref.digest, hashlib.sha256(b"").hexdigest())
        self.assertEqual(self.store.read(ref), b"")

    def test_range_read_is_bounded_and_checks_edges(self):
        payload = b"0123456789abcdef"
        ref = self.store.put_bytes(payload)

        self.assertEqual(self.store.read_range(ref, offset=3, length=5), b"34567")
        self.assertEqual(self.store.read_range(ref, offset=len(payload), length=0), b"")
        with self.assertRaisesRegex(ArtifactRangeError, "range"):
            self.store.read_range(ref, offset=0, length=17)
        with self.assertRaisesRegex(ArtifactRangeError, "offset"):
            self.store.read_range(ref, offset=-1, length=1)
        with self.assertRaisesRegex(ArtifactRangeError, "bounds"):
            self.store.read_range(ref, offset=15, length=2)

    def test_missing_artifact_is_typed(self):
        ref = ArtifactRef(digest="c" * 64, size_bytes=4)
        with self.assertRaisesRegex(ArtifactIntegrityError, "missing"):
            self.store.verify(ref)

    def test_bit_flip_and_truncation_are_detected_by_read_and_verify(self):
        payload = b"integrity matters"
        ref = self.store.put_bytes(payload)
        path = self.store.object_path(ref)
        path.write_bytes(b"integrity maters")
        with self.assertRaises(ArtifactIntegrityError):
            self.store.verify(ref)
        with self.assertRaises(ArtifactIntegrityError):
            self.store.read_range(ref, offset=0, length=1)

        path.write_bytes(payload[:-1])
        with self.assertRaises(ArtifactIntegrityError):
            self.store.verify(ref)

    def test_source_failure_does_not_publish_partial_artifact(self):
        with self.assertRaises(ArtifactInputError):
            self.store.put_stream(_FailingReader(b"partial"))
        self.assertEqual(
            [
                path
                for path in (self.root / "objects").rglob("*")
                if path.is_file()
            ],
            [],
        )
        self.assertEqual(list((self.root / "tmp").iterdir()), [])

    def test_concurrent_writers_publish_one_verified_object(self):
        payload = b"same bytes from many local workers" * 128

        with ThreadPoolExecutor(max_workers=8) as workers:
            refs = list(workers.map(lambda _: self.store.put_bytes(payload), range(8)))

        self.assertEqual(set(refs), {refs[0]})
        self.assertEqual(self.store.read(refs[0]), payload)

    def test_existing_symlink_is_rejected(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is unavailable")
        payload = b"symlink target"
        ref = self.store.put_bytes(payload)
        path = self.store.object_path(ref)
        target = path.with_name("outside-target")
        target.write_bytes(payload)
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        with self.assertRaises(ArtifactIntegrityError):
            self.store.read(ref)

    def test_broken_root_symlink_is_typed_integrity_error(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is unavailable")
        broken_root = self.root.with_name("broken-root")
        broken_root.symlink_to(self.root.with_name("missing-root"))
        with self.assertRaises(ArtifactIntegrityError):
            LocalArtifactStore(broken_root).initialize()

    def test_broken_shard_symlink_is_typed_integrity_error(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is unavailable")
        payload = b"shard safety"
        digest = hashlib.sha256(payload).hexdigest()
        shard = self.root / "objects" / "sha256" / digest[:2]
        shard.symlink_to(self.root.with_name("missing-shard"))
        ref = ArtifactRef(digest=digest, size_bytes=len(payload))
        with self.assertRaises(ArtifactIntegrityError):
            self.store.object_path(ref)


class M204ArtifactBenchmarkTests(unittest.TestCase):
    def test_benchmark_quantifies_bounded_range_bytes_without_a_service(self):
        from context_control_plane.artifact_benchmark import run_artifact_benchmark

        with tempfile.TemporaryDirectory() as temporary_directory:
            result = run_artifact_benchmark(
                Path(temporary_directory) / "artifact-store",
                payload_bytes=65_536,
                range_bytes=4_096,
            )

        self.assertEqual(
            result["schema_version"],
            "context.artifact-store-results/v1alpha1",
        )
        self.assertEqual(result["payload_bytes"], 65_536)
        self.assertEqual(result["range_bytes"], 4_096)
        self.assertEqual(result["external_services"], 0)
        self.assertEqual(result["integrity"], "passed")
        self.assertEqual(result["context_bytes_reduction_percent"], 93.75)
        self.assertEqual(result["range_output_sha256"], result["range_sha256"])

    def test_benchmark_rejects_invalid_sizes(self):
        from context_control_plane.artifact_benchmark import run_artifact_benchmark

        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "payload_bytes"):
                run_artifact_benchmark(
                    Path(temporary_directory) / "artifact-store",
                    payload_bytes=0,
                    range_bytes=1,
                )
            with self.assertRaisesRegex(ValueError, "range_bytes"):
                run_artifact_benchmark(
                    Path(temporary_directory) / "artifact-store-2",
                    payload_bytes=1,
                    range_bytes=2,
                )


if __name__ == "__main__":
    unittest.main()
