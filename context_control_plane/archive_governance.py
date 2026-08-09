"""M1-06 archive retention, export, import, and deletion-proof contracts.

The in-memory store is an offline reference implementation. Payload bytes are
required to be sealed by the archive adapter before they enter this boundary.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from datetime import datetime
from typing import Any


ARCHIVE_RECORD_VERSION = "context.archive-record/v1alpha1"
ARCHIVE_EXPORT_VERSION = "context.archive-export/v1alpha1"
ARCHIVE_TOMBSTONE_VERSION = "context.archive-tombstone/v1alpha1"
RETENTION_CLASSES = {"ephemeral", "project", "audit"}

_SOURCE_REF_RE = re.compile(r"^thr_[a-z2-7]{26}$")
_OBJECT_REF_RE = re.compile(r"^obj_[a-z2-7]{26}$")
_REQUEST_REF_RE = re.compile(r"^req_[a-z2-7]{26}$")
_AUTHORIZATION_REF_RE = re.compile(r"^auth_[a-z2-7]{26}$")
_TOMBSTONE_REF_RE = re.compile(r"^tmb_[a-z2-7]{26}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

_RECORD_FIELDS = {
    "schema_version",
    "project_id",
    "source_thread_ref",
    "object_ref",
    "content_sha256",
    "size_bytes",
    "retention_class",
    "created_at",
    "expires_at",
    "legal_hold",
}
_TOMBSTONE_FIELDS = {
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
}
_MANIFEST_FIELDS = {
    "schema_version",
    "project_id",
    "exported_at",
    "content_protection",
    "record_count",
    "tombstone_count",
    "records",
    "tombstones",
    "manifest_sha256",
}


class ArchiveGovernanceError(ValueError):
    """Raised when an archive lifecycle operation violates its contract."""


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _opaque_ref(prefix: str, material: bytes) -> str:
    encoded = base64.b32encode(hashlib.sha256(material).digest()).decode("ascii")
    return f"{prefix}_{encoded.rstrip('=').lower()[:26]}"


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ArchiveGovernanceError(f"{field} must be a non-empty string")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    value = _require_string(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArchiveGovernanceError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ArchiveGovernanceError(f"{field} must include a timezone")
    return parsed


def _validate_record(record: dict[str, Any], payload: bytes) -> None:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise ArchiveGovernanceError("archive record fields do not match the contract")
    if record["schema_version"] != ARCHIVE_RECORD_VERSION:
        raise ArchiveGovernanceError("unsupported archive record version")
    _require_string(record["project_id"], "project_id")
    if not isinstance(record["source_thread_ref"], str) or not _SOURCE_REF_RE.fullmatch(
        record["source_thread_ref"]
    ):
        raise ArchiveGovernanceError("source_thread_ref must be opaque")
    if not isinstance(record["object_ref"], str) or not _OBJECT_REF_RE.fullmatch(
        record["object_ref"]
    ):
        raise ArchiveGovernanceError("object_ref must be opaque")
    if record["retention_class"] not in RETENTION_CLASSES:
        raise ArchiveGovernanceError("unsupported retention_class")
    if not isinstance(record["legal_hold"], bool):
        raise ArchiveGovernanceError("legal_hold must be boolean")

    created_at = _timestamp(record["created_at"], "created_at")
    expires_at = record["expires_at"]
    if record["retention_class"] == "ephemeral" and expires_at is None:
        raise ArchiveGovernanceError("ephemeral retention requires expires_at")
    if expires_at is not None and _timestamp(expires_at, "expires_at") <= created_at:
        raise ArchiveGovernanceError("expires_at must be later than created_at")

    if not isinstance(payload, bytes) or not payload:
        raise ArchiveGovernanceError("sealed payload must be non-empty bytes")
    actual_hash = hashlib.sha256(payload).hexdigest()
    if record["content_sha256"] != actual_hash:
        raise ArchiveGovernanceError("sealed payload hash mismatch")
    if record["size_bytes"] != len(payload):
        raise ArchiveGovernanceError("sealed payload size mismatch")

    expected_ref = _opaque_ref(
        "obj",
        f"{record['source_thread_ref']}\0{actual_hash}".encode("ascii"),
    )
    if record["object_ref"] != expected_ref:
        raise ArchiveGovernanceError("object_ref does not match the source and payload")


def _proof_core(tombstone: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in tombstone.items()
        if key not in {"tombstone_id", "proof_sha256"}
    }


def _validate_tombstone_shape(tombstone: dict[str, Any]) -> None:
    if not isinstance(tombstone, dict) or set(tombstone) != _TOMBSTONE_FIELDS:
        raise ArchiveGovernanceError("tombstone fields do not match the contract")
    if tombstone["schema_version"] != ARCHIVE_TOMBSTONE_VERSION:
        raise ArchiveGovernanceError("unsupported tombstone version")
    _require_string(tombstone["project_id"], "project_id")
    if not _SOURCE_REF_RE.fullmatch(str(tombstone["source_thread_ref"])):
        raise ArchiveGovernanceError("invalid tombstone source_thread_ref")
    if not _OBJECT_REF_RE.fullmatch(str(tombstone["object_ref"])):
        raise ArchiveGovernanceError("invalid tombstone object_ref")
    if tombstone["retention_class"] not in RETENTION_CLASSES:
        raise ArchiveGovernanceError("invalid tombstone retention_class")
    if not _SHA256_RE.fullmatch(str(tombstone["deleted_content_sha256"])):
        raise ArchiveGovernanceError("invalid tombstone content hash")
    if not isinstance(tombstone["deleted_size_bytes"], int) or tombstone["deleted_size_bytes"] < 0:
        raise ArchiveGovernanceError("invalid tombstone content size")
    _timestamp(tombstone["deleted_at"], "deleted_at")
    if not _REQUEST_REF_RE.fullmatch(str(tombstone["deletion_request_ref"])):
        raise ArchiveGovernanceError("invalid deletion_request_ref")
    if not _AUTHORIZATION_REF_RE.fullmatch(str(tombstone["authorization_ref"])):
        raise ArchiveGovernanceError("invalid authorization_ref")
    if tombstone["deletion_scope"] != ["archive-record", "sealed-payload"]:
        raise ArchiveGovernanceError("invalid deletion_scope")
    if tombstone["absence_verified"] is not True:
        raise ArchiveGovernanceError("deletion proof requires absence verification")

    proof_sha256 = _digest(_proof_core(tombstone))
    if tombstone["proof_sha256"] != proof_sha256:
        raise ArchiveGovernanceError("deletion proof digest mismatch")
    expected_id = _opaque_ref("tmb", bytes.fromhex(proof_sha256))
    if tombstone["tombstone_id"] != expected_id or not _TOMBSTONE_REF_RE.fullmatch(
        tombstone["tombstone_id"]
    ):
        raise ArchiveGovernanceError("tombstone_id does not match its proof")


class ArchiveStore:
    """Deterministic offline archive adapter used by M1-06 contract tests."""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        self._objects: dict[str, bytes] = {}
        self._tombstones: dict[str, dict[str, Any]] = {}

    def put(
        self,
        *,
        project_id: str,
        source_thread_ref: str,
        retention_class: str,
        created_at: str,
        expires_at: str | None,
        sealed_payload: bytes,
        legal_hold: bool = False,
    ) -> dict[str, Any]:
        """Admit one adapter-sealed payload and return public archive metadata."""
        if source_thread_ref in self._records:
            raise ArchiveGovernanceError("source_thread_ref is already active")
        if not isinstance(sealed_payload, bytes):
            raise ArchiveGovernanceError("sealed payload must be bytes")
        content_sha256 = hashlib.sha256(sealed_payload).hexdigest()
        object_ref = _opaque_ref(
            "obj", f"{source_thread_ref}\0{content_sha256}".encode("ascii")
        )
        record = {
            "schema_version": ARCHIVE_RECORD_VERSION,
            "project_id": project_id,
            "source_thread_ref": source_thread_ref,
            "object_ref": object_ref,
            "content_sha256": content_sha256,
            "size_bytes": len(sealed_payload),
            "retention_class": retention_class,
            "created_at": created_at,
            "expires_at": expires_at,
            "legal_hold": legal_hold,
        }
        _validate_record(record, sealed_payload)
        if object_ref in self._objects:
            raise ArchiveGovernanceError("object_ref collision detected")
        self._records[source_thread_ref] = record
        self._objects[object_ref] = sealed_payload
        return dict(record)

    def records(self) -> dict[str, dict[str, Any]]:
        return {key: dict(value) for key, value in self._records.items()}

    def object_refs(self) -> set[str]:
        return set(self._objects)

    def expired_source_refs(self, observed_at: str) -> list[str]:
        cutoff = _timestamp(observed_at, "observed_at")
        expired = []
        for source_ref, record in self._records.items():
            expires_at = record["expires_at"]
            if expires_at is not None and _timestamp(expires_at, "expires_at") <= cutoff:
                expired.append(source_ref)
        return sorted(expired)

    def export_bundle(self, project_id: str, *, exported_at: str) -> bytes:
        """Return a canonical bundle containing metadata and sealed payloads."""
        _require_string(project_id, "project_id")
        _timestamp(exported_at, "exported_at")
        records = sorted(
            (
                dict(record)
                for record in self._records.values()
                if record["project_id"] == project_id
            ),
            key=lambda item: item["source_thread_ref"],
        )
        tombstones = sorted(
            (
                dict(item)
                for item in self._tombstones.values()
                if item["project_id"] == project_id
            ),
            key=lambda item: item["tombstone_id"],
        )
        object_refs = [record["object_ref"] for record in records]
        objects = {
            object_ref: base64.b64encode(self._objects[object_ref]).decode("ascii")
            for object_ref in sorted(object_refs)
        }
        manifest_core = {
            "schema_version": ARCHIVE_EXPORT_VERSION,
            "project_id": project_id,
            "exported_at": exported_at,
            "content_protection": "sealed-by-archive-adapter",
            "record_count": len(records),
            "tombstone_count": len(tombstones),
            "records": records,
            "tombstones": tombstones,
        }
        manifest = {**manifest_core, "manifest_sha256": _digest(manifest_core)}
        bundle_core = {"manifest": manifest, "objects": objects}
        return _canonical_bytes({**bundle_core, "bundle_sha256": _digest(bundle_core)})

    @classmethod
    def from_export_bundle(cls, bundle: bytes) -> "ArchiveStore":
        """Validate and import one canonical archive bundle."""
        if not isinstance(bundle, bytes):
            raise ArchiveGovernanceError("archive bundle must be bytes")
        try:
            document = json.loads(bundle.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveGovernanceError("archive bundle must be UTF-8 JSON") from exc
        if not isinstance(document, dict) or set(document) != {
            "manifest",
            "objects",
            "bundle_sha256",
        }:
            raise ArchiveGovernanceError("archive bundle fields do not match the contract")
        bundle_core = {"manifest": document["manifest"], "objects": document["objects"]}
        if document["bundle_sha256"] != _digest(bundle_core):
            raise ArchiveGovernanceError("archive bundle digest mismatch")

        manifest = document["manifest"]
        if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
            raise ArchiveGovernanceError("export manifest fields do not match the contract")
        if manifest["schema_version"] != ARCHIVE_EXPORT_VERSION:
            raise ArchiveGovernanceError("unsupported archive export version")
        _require_string(manifest["project_id"], "project_id")
        _timestamp(manifest["exported_at"], "exported_at")
        if manifest["content_protection"] != "sealed-by-archive-adapter":
            raise ArchiveGovernanceError("archive payloads must be adapter-sealed")
        manifest_core = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        if manifest["manifest_sha256"] != _digest(manifest_core):
            raise ArchiveGovernanceError("export manifest digest mismatch")
        records = manifest["records"]
        tombstones = manifest["tombstones"]
        if not isinstance(records, list) or manifest["record_count"] != len(records):
            raise ArchiveGovernanceError("record_count does not match export records")
        if not isinstance(tombstones, list) or manifest["tombstone_count"] != len(tombstones):
            raise ArchiveGovernanceError("tombstone_count does not match export tombstones")
        if records != sorted(records, key=lambda item: item.get("source_thread_ref", "")):
            raise ArchiveGovernanceError("export records must use stable order")
        if tombstones != sorted(tombstones, key=lambda item: item.get("tombstone_id", "")):
            raise ArchiveGovernanceError("export tombstones must use stable order")
        objects = document["objects"]
        if not isinstance(objects, dict):
            raise ArchiveGovernanceError("export objects must be a mapping")

        store = cls()
        expected_object_refs: set[str] = set()
        for record in records:
            object_ref = record.get("object_ref") if isinstance(record, dict) else None
            if object_ref not in objects:
                raise ArchiveGovernanceError("export record payload is missing")
            try:
                payload = base64.b64decode(objects[object_ref], validate=True)
            except (binascii.Error, TypeError, ValueError) as exc:
                raise ArchiveGovernanceError("sealed payload encoding is invalid") from exc
            _validate_record(record, payload)
            if record["project_id"] != manifest["project_id"]:
                raise ArchiveGovernanceError("export record project mismatch")
            if record["source_thread_ref"] in store._records or object_ref in expected_object_refs:
                raise ArchiveGovernanceError("duplicate export record")
            store._records[record["source_thread_ref"]] = dict(record)
            store._objects[object_ref] = payload
            expected_object_refs.add(object_ref)
        if set(objects) != expected_object_refs:
            raise ArchiveGovernanceError("export contains unreferenced objects")

        for tombstone in tombstones:
            _validate_tombstone_shape(tombstone)
            if tombstone["project_id"] != manifest["project_id"]:
                raise ArchiveGovernanceError("export tombstone project mismatch")
            if tombstone["source_thread_ref"] in store._records:
                raise ArchiveGovernanceError("deleted source is active in the same export")
            if tombstone["object_ref"] in store._objects:
                raise ArchiveGovernanceError("deleted object is present in the same export")
            if tombstone["tombstone_id"] in store._tombstones:
                raise ArchiveGovernanceError("duplicate export tombstone")
            store._tombstones[tombstone["tombstone_id"]] = dict(tombstone)
        return store

    def delete_source(
        self,
        source_thread_ref: str,
        *,
        deleted_at: str,
        deletion_request_ref: str,
        authorization_ref: str,
    ) -> dict[str, Any]:
        """Delete one source and return a digest-bound absence proof."""
        record = self._records.get(source_thread_ref)
        if record is None:
            raise ArchiveGovernanceError("source_thread_ref is not active")
        if not _REQUEST_REF_RE.fullmatch(str(deletion_request_ref)):
            raise ArchiveGovernanceError("deletion_request_ref must be opaque")
        if not _AUTHORIZATION_REF_RE.fullmatch(str(authorization_ref)):
            raise ArchiveGovernanceError("authorization_ref must be opaque")
        deletion_time = _timestamp(deleted_at, "deleted_at")
        if deletion_time < _timestamp(record["created_at"], "created_at"):
            raise ArchiveGovernanceError("deleted_at cannot precede created_at")
        if record["legal_hold"]:
            raise ArchiveGovernanceError("legal hold blocks deletion")

        object_ref = record["object_ref"]
        del self._records[source_thread_ref]
        del self._objects[object_ref]
        absence_verified = source_thread_ref not in self._records and object_ref not in self._objects
        proof_core = {
            "schema_version": ARCHIVE_TOMBSTONE_VERSION,
            "project_id": record["project_id"],
            "source_thread_ref": source_thread_ref,
            "object_ref": object_ref,
            "retention_class": record["retention_class"],
            "deleted_content_sha256": record["content_sha256"],
            "deleted_size_bytes": record["size_bytes"],
            "deleted_at": deleted_at,
            "deletion_request_ref": deletion_request_ref,
            "authorization_ref": authorization_ref,
            "deletion_scope": ["archive-record", "sealed-payload"],
            "absence_verified": absence_verified,
        }
        proof_sha256 = _digest(proof_core)
        tombstone = {
            **proof_core,
            "tombstone_id": _opaque_ref("tmb", bytes.fromhex(proof_sha256)),
            "proof_sha256": proof_sha256,
        }
        _validate_tombstone_shape(tombstone)
        self._tombstones[tombstone["tombstone_id"]] = tombstone
        return dict(tombstone)

    def verify_deletion_proof(self, tombstone: dict[str, Any]) -> bool:
        """Verify proof integrity and current absence from this adapter inventory."""
        try:
            _validate_tombstone_shape(tombstone)
        except ArchiveGovernanceError:
            return False
        return (
            tombstone["source_thread_ref"] not in self._records
            and tombstone["object_ref"] not in self._objects
        )
