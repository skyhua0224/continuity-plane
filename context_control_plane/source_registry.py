"""Stable source references and M1-02 provenance validation.

The provider's original thread identifier is accepted only at the private
derivation boundary. It is never retained in public registry records.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "m1-02.v1"
_PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_SOURCE_KINDS = {"raw_transcript", "handoff", "memory", "other"}
_RETENTION_CLASSES = {"ephemeral", "project", "audit"}
_CLASSIFICATIONS = {
    "decision",
    "evidence",
    "constraint",
    "work",
    "preference",
    "replay",
}
_VALIDITIES = {"candidate", "verified", "stale", "rejected"}
_REF_RE = re.compile(r"^(?:thr|rng)_[a-z2-7]{26}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
_OPAQUE_KEY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_FORBIDDEN_PUBLIC_KEYS = {
    "provider_thread_id",
    "raw_thread_id",
    "provider_id",
    "archive_path",
    "source_path",
    "local_path",
}


class ProvenanceError(ValueError):
    """Raised when a provenance object violates its versioned contract."""


def _require_non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProvenanceError(f"{field} must be a non-empty string")
    return value


def _opaque_ref(prefix: str, key: bytes, material: str) -> str:
    digest = hmac.new(key, material.encode("utf-8"), hashlib.sha256).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"{prefix}_{encoded[:26]}"


class SourceRegistry:
    """Derive stable public source records without retaining raw identities."""

    def __init__(self, namespace_key: bytes, *, opaque_key_id: str = SCHEMA_VERSION):
        if not isinstance(namespace_key, bytes) or not namespace_key:
            raise ValueError("namespace_key must be non-empty bytes")
        self._namespace_key = namespace_key
        self._opaque_key_id = _require_non_empty_string(opaque_key_id, "opaque_key_id")
        if not _OPAQUE_KEY_ID_RE.fullmatch(self._opaque_key_id):
            raise ValueError("opaque_key_id must be a public lowercase identifier")
        self._records: dict[str, dict[str, str]] = {}
        self._identity_digests: dict[str, str] = {}

    @classmethod
    def from_base64_secret(
        cls,
        encoded_namespace_key: str,
        *,
        opaque_key_id: str,
    ) -> "SourceRegistry":
        """Build a registry from an injected base64url secret without retaining its encoding."""
        if not isinstance(encoded_namespace_key, str) or not encoded_namespace_key:
            raise ValueError("encoded_namespace_key must be non-empty base64url")
        try:
            encoded = encoded_namespace_key.encode("ascii")
            padding = b"=" * (-len(encoded) % 4)
            namespace_key = base64.b64decode(
                encoded + padding,
                altchars=b"-_",
                validate=True,
            )
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ValueError("encoded_namespace_key must be valid base64url") from exc
        if len(namespace_key) < 32:
            raise ValueError("namespace_key must contain at least 256 bits")
        return cls(namespace_key, opaque_key_id=opaque_key_id)

    @classmethod
    def from_secret_file(cls, path: Path | str) -> "SourceRegistry":
        """Load a private provisioned secret file without retaining its path."""
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("namespace secret path must be a regular file")
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("namespace secret file must use mode 0600")
        if path.stat().st_size > 4096:
            raise ValueError("namespace secret file exceeds the bounded size")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("namespace secret file is invalid") from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "opaque_key_id",
            "encoded_namespace_key",
        }:
            raise ValueError("namespace secret fields do not match the contract")
        if payload["schema_version"] != "context.source-namespace-secret/v1alpha1":
            raise ValueError("unsupported namespace secret schema_version")
        return cls.from_base64_secret(
            payload["encoded_namespace_key"],
            opaque_key_id=payload["opaque_key_id"],
        )

    @staticmethod
    def _canonical_identity(
        project_id: str, source_provider: str, provider_thread_id: str
    ) -> str:
        project_id = _require_non_empty_string(project_id, "project_id")
        source_provider = _require_non_empty_string(
            source_provider, "source_provider"
        ).lower()
        provider_thread_id = _require_non_empty_string(
            provider_thread_id, "provider_thread_id"
        )
        if _PROVIDER_RE.fullmatch(source_provider) is None:
            raise ValueError(f"unsupported source_provider: {source_provider}")
        return json.dumps(
            {
                "project_id": project_id,
                "source_provider": source_provider,
                "provider_thread_id": provider_thread_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def thread_ref(
        self, project_id: str, source_provider: str, provider_thread_id: str
    ) -> str:
        """Return a deterministic opaque reference for one provider thread."""
        identity = self._canonical_identity(
            project_id, source_provider, provider_thread_id
        )
        return _opaque_ref("thr", self._namespace_key, identity)

    def range_ref(self, source_thread_ref: str, start: int, end: int) -> str:
        """Return a deterministic opaque reference for an inclusive-exclusive range."""
        if not _REF_RE.fullmatch(source_thread_ref) or not source_thread_ref.startswith(
            "thr_"
        ):
            raise ValueError("source_thread_ref must be a valid thr_ reference")
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or start < 0
            or end < start
        ):
            raise ValueError("range must use non-negative start and end >= start")
        return _opaque_ref(
            "rng",
            self._namespace_key,
            f"{source_thread_ref}\0{start}:{end}",
        )

    def register(
        self,
        project_id: str,
        source_provider: str,
        provider_thread_id: str,
        *,
        source_kind: str = "raw_transcript",
        retention_class: str = "project",
    ) -> dict[str, str]:
        """Register a source and return its public, non-sensitive record."""
        identity = self._canonical_identity(
            project_id, source_provider, provider_thread_id
        )
        source_provider = source_provider.lower()
        if source_kind not in _SOURCE_KINDS:
            raise ValueError(f"unsupported source_kind: {source_kind}")
        if retention_class not in _RETENTION_CLASSES:
            raise ValueError(f"unsupported retention_class: {retention_class}")

        source_thread_ref = _opaque_ref("thr", self._namespace_key, identity)
        identity_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        previous_digest = self._identity_digests.get(source_thread_ref)
        if previous_digest is not None and previous_digest != identity_digest:
            raise RuntimeError("opaque reference collision detected")
        self._identity_digests[source_thread_ref] = identity_digest

        record = {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "source_provider": source_provider,
            "source_thread_ref": source_thread_ref,
            "source_kind": source_kind,
            "retention_class": retention_class,
            "opaque_key_id": self._opaque_key_id,
        }
        self._records[source_thread_ref] = record
        return dict(record)

    def records(self) -> list[dict[str, str]]:
        """Return public records in stable order."""
        return [dict(self._records[key]) for key in sorted(self._records)]

    def to_document(self) -> dict[str, Any]:
        """Return a versioned registry document suitable for persistence."""
        return {
            "schema_version": SCHEMA_VERSION,
            "opaque_key_id": self._opaque_key_id,
            "records": [
                {
                    key: value
                    for key, value in record.items()
                    if key not in {"schema_version", "opaque_key_id"}
                }
                for record in self.records()
            ],
        }


def build_provenance(
    *,
    source_provider: str,
    source_thread_ref: str,
    source_range_ref: str,
    project_id: str,
    extracted_at: str,
    extractor_version: str,
    content_sha256: str,
    classification: str,
    validity: str,
    verified_against: Iterable[str],
    contains_sensitive_data: bool,
    retention_class: str,
) -> dict[str, Any]:
    """Build a provenance object; validation is explicit via validate_provenance."""
    return {
        "schema_version": SCHEMA_VERSION,
        "source_provider": source_provider,
        "source_thread_ref": source_thread_ref,
        "source_range_ref": source_range_ref,
        "project_id": project_id,
        "extracted_at": extracted_at,
        "extractor_version": extractor_version,
        "content_sha256": content_sha256,
        "classification": classification,
        "validity": validity,
        "verified_against": list(verified_against),
        "contains_sensitive_data": contains_sensitive_data,
        "retention_class": retention_class,
    }


def validate_provenance(
    payload: dict[str, Any], *, for_admission: bool = False
) -> None:
    """Validate the M1-02 provenance contract and optional admission gate."""
    if not isinstance(payload, dict):
        raise ProvenanceError("provenance must be an object")

    required = {
        "schema_version",
        "source_provider",
        "source_thread_ref",
        "source_range_ref",
        "project_id",
        "extracted_at",
        "extractor_version",
        "content_sha256",
        "classification",
        "validity",
        "verified_against",
        "contains_sensitive_data",
        "retention_class",
    }
    missing = required - payload.keys()
    if missing:
        raise ProvenanceError(f"missing fields: {', '.join(sorted(missing))}")
    forbidden = _FORBIDDEN_PUBLIC_KEYS.intersection(payload.keys())
    if forbidden:
        raise ProvenanceError(
            f"forbidden public fields: {', '.join(sorted(forbidden))}"
        )
    if payload.keys() - required:
        extra = payload.keys() - required
        raise ProvenanceError(f"unknown fields: {', '.join(sorted(extra))}")

    if payload["schema_version"] != SCHEMA_VERSION:
        raise ProvenanceError("unsupported schema_version")
    if (
        not isinstance(payload["source_provider"], str)
        or _PROVIDER_RE.fullmatch(payload["source_provider"]) is None
    ):
        raise ProvenanceError("unsupported source_provider")
    if (
        not isinstance(payload["source_thread_ref"], str)
        or not _REF_RE.fullmatch(payload["source_thread_ref"])
        or not payload["source_thread_ref"].startswith("thr_")
    ):
        raise ProvenanceError("invalid source_thread_ref")
    if (
        not isinstance(payload["source_range_ref"], str)
        or not _REF_RE.fullmatch(payload["source_range_ref"])
        or not payload["source_range_ref"].startswith("rng_")
    ):
        raise ProvenanceError("invalid source_range_ref")
    _require_non_empty_string(payload["project_id"], "project_id")
    _require_non_empty_string(payload["extracted_at"], "extracted_at")
    try:
        extracted_at = payload["extracted_at"].replace("Z", "+00:00")
        if datetime.fromisoformat(extracted_at).tzinfo is None:
            raise ValueError
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProvenanceError("extracted_at must be RFC3339 with timezone") from exc
    if not isinstance(payload["extractor_version"], str) or not _SEMVER_RE.fullmatch(
        payload["extractor_version"]
    ):
        raise ProvenanceError("extractor_version must be semver")
    if not isinstance(payload["content_sha256"], str) or not _SHA256_RE.fullmatch(
        payload["content_sha256"]
    ):
        raise ProvenanceError("content_sha256 must be lowercase SHA-256")
    if payload["classification"] not in _CLASSIFICATIONS:
        raise ProvenanceError("unsupported classification")
    if payload["validity"] not in _VALIDITIES:
        raise ProvenanceError("unsupported validity")
    if not isinstance(payload["verified_against"], list) or not all(
        isinstance(ref, str)
        and (ref.startswith("artifact://") or ref.startswith("assertion:"))
        for ref in payload["verified_against"]
    ):
        raise ProvenanceError(
            "verified_against must contain artifact:// or assertion: refs"
        )
    if payload["validity"] == "verified" and not payload["verified_against"]:
        raise ProvenanceError("verified provenance requires current evidence")
    if not isinstance(payload["contains_sensitive_data"], bool):
        raise ProvenanceError("contains_sensitive_data must be boolean")
    if payload["retention_class"] not in _RETENTION_CLASSES:
        raise ProvenanceError("unsupported retention_class")
    if for_admission and payload["contains_sensitive_data"]:
        raise ProvenanceError("sensitive provenance cannot pass admission")
