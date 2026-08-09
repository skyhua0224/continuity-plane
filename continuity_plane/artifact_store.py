"""Local content-addressed artifact storage for bounded evidence access."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


ARTIFACT_REF_SCHEMA_VERSION = "context.artifact-ref/v1alpha1"
ARTIFACT_DIGEST_ALGORITHM = "sha-256"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_URI_RE = re.compile(r"^artifact://sha256/([0-9a-f]{64})$")
_REF_FIELDS = frozenset(
    {"schema_version", "digest_algorithm", "digest", "size_bytes", "artifact_uri"}
)


class ArtifactStoreError(RuntimeError):
    """Base error for local artifact operations."""


class ArtifactInputError(ArtifactStoreError):
    """Raised when an artifact source cannot be read as bytes."""


class ArtifactIntegrityError(ArtifactStoreError):
    """Raised when a durable object is missing, changed, or malformed."""


class ArtifactRangeError(ArtifactStoreError):
    """Raised when a requested byte range is outside the artifact or bound."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Stable identity and size of one raw byte artifact."""

    digest: str
    size_bytes: int
    schema_version: str = ARTIFACT_REF_SCHEMA_VERSION
    digest_algorithm: str = ARTIFACT_DIGEST_ALGORITHM

    def __post_init__(self) -> None:
        if self.schema_version != ARTIFACT_REF_SCHEMA_VERSION:
            raise ValueError("unsupported artifact ref schema_version")
        if self.digest_algorithm != ARTIFACT_DIGEST_ALGORITHM:
            raise ValueError("unsupported artifact digest_algorithm")
        if not isinstance(self.digest, str) or not _DIGEST_RE.fullmatch(self.digest):
            raise ValueError("artifact digest must be lowercase SHA-256")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("artifact size_bytes must be a non-negative integer")

    @property
    def uri(self) -> str:
        return f"artifact://sha256/{self.digest}"

    def to_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "digest_algorithm": self.digest_algorithm,
            "digest": self.digest,
            "size_bytes": self.size_bytes,
            "artifact_uri": self.uri,
        }

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "ArtifactRef":
        if not isinstance(document, dict) or frozenset(document) != _REF_FIELDS:
            raise ValueError("artifact ref document fields are invalid")
        ref = cls(
            digest=document["digest"],
            size_bytes=document["size_bytes"],
            schema_version=document["schema_version"],
            digest_algorithm=document["digest_algorithm"],
        )
        if document["artifact_uri"] != ref.uri:
            raise ValueError("artifact ref URI does not match digest")
        return ref

    @classmethod
    def from_uri(cls, uri: str, size_bytes: int) -> "ArtifactRef":
        if not isinstance(uri, str):
            raise ValueError("artifact URI must be a string")
        match = _URI_RE.fullmatch(uri)
        if match is None:
            raise ValueError("artifact URI is invalid")
        return cls(digest=match.group(1), size_bytes=size_bytes)


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.stat(follow_symlinks=False).st_mode)
    except FileNotFoundError:
        return False


def _fsync_directory(path: Path) -> None:
    """Persist directory entry updates where the host supports directory fsync."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class LocalArtifactStore:
    """Zero-service filesystem CAS with full-object integrity verification."""

    def __init__(
        self,
        root: str | Path,
        *,
        chunk_size: int = 1024 * 1024,
        max_range_bytes: int = 1024 * 1024,
    ):
        if type(chunk_size) is not int or chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")
        if type(max_range_bytes) is not int or max_range_bytes <= 0:
            raise ValueError("max_range_bytes must be a positive integer")
        self.root = Path(root)
        self.chunk_size = chunk_size
        self.max_range_bytes = max_range_bytes

    def initialize(self) -> None:
        if self.root.is_symlink():
            raise ArtifactIntegrityError("artifact root must not be a symlink")
        if self.root.exists() and not self.root.is_dir():
            raise ArtifactIntegrityError("artifact root is not a directory")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        objects = self.root / "objects"
        algorithm = objects / "sha256"
        temporary = self.root / "tmp"
        for directory in (self.root, objects, algorithm, temporary):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if directory.is_symlink() or not directory.is_dir():
                raise ArtifactIntegrityError("artifact store directory is invalid")
            try:
                directory.chmod(0o700)
            except OSError:
                pass

    def _ensure_initialized(self) -> None:
        expected = (self.root, self.root / "objects", self.root / "objects" / "sha256", self.root / "tmp")
        if any(not path.is_dir() or path.is_symlink() for path in expected):
            raise ArtifactStoreError("artifact store is not initialized")

    def object_path(self, ref: ArtifactRef) -> Path:
        return self._object_path(ref, create_shard=True)

    def _object_path(self, ref: ArtifactRef, *, create_shard: bool) -> Path:
        if not isinstance(ref, ArtifactRef):
            raise TypeError("object_path requires ArtifactRef")
        self._ensure_initialized()
        shard = self.root / "objects" / "sha256" / ref.digest[:2]
        if shard.is_symlink() or (shard.exists() and not shard.is_dir()):
            raise ArtifactIntegrityError("artifact shard directory is invalid")
        if create_shard:
            shard.mkdir(mode=0o700, exist_ok=True)
        return shard / ref.digest[2:]

    def put_bytes(self, payload: bytes | bytearray | memoryview) -> ArtifactRef:
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ArtifactInputError("artifact payload must be bytes")
        return self.put_stream(BinaryIOBytes(bytes(payload)))

    def put_stream(self, source: BinaryIO) -> ArtifactRef:
        self._ensure_initialized()
        if not hasattr(source, "read"):
            raise ArtifactInputError("artifact source must provide read()")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".artifact-", suffix=".tmp", dir=self.root / "tmp"
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as destination:
                while True:
                    try:
                        chunk = source.read(self.chunk_size)
                    except Exception as exc:
                        raise ArtifactInputError("artifact source read failed") from exc
                    if not isinstance(chunk, (bytes, bytearray, memoryview)):
                        raise ArtifactInputError("artifact source must return bytes")
                    chunk = bytes(chunk)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())

            ref = ArtifactRef(digest=digest.hexdigest(), size_bytes=size)
            target = self.object_path(ref)
            if target.exists() or target.is_symlink():
                self._verify_path(target, ref)
                return ref
            try:
                os.link(temporary, target)
                temporary.unlink(missing_ok=True)
            except FileExistsError:
                self._verify_path(target, ref)
                temporary.unlink(missing_ok=True)
            except (NotImplementedError, OSError):
                if target.exists() or target.is_symlink():
                    self._verify_path(target, ref)
                    temporary.unlink(missing_ok=True)
                else:
                    os.replace(temporary, target)
            try:
                target.chmod(0o600)
            except OSError:
                pass
            _fsync_directory(target.parent)
            return ref
        except ArtifactStoreError:
            temporary.unlink(missing_ok=True)
            raise
        except (OSError, ValueError) as exc:
            temporary.unlink(missing_ok=True)
            raise ArtifactStoreError("artifact publication failed") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def _verify_path(self, path: Path, ref: ArtifactRef) -> None:
        if not path.exists() and not path.is_symlink():
            raise ArtifactIntegrityError("artifact object is missing")
        if path.is_symlink() or not _is_regular(path):
            raise ArtifactIntegrityError("artifact object is not a regular file")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as source:
                while chunk := source.read(self.chunk_size):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError as exc:
            raise ArtifactIntegrityError("artifact object cannot be read") from exc
        if size != ref.size_bytes or digest.hexdigest() != ref.digest:
            raise ArtifactIntegrityError("artifact object checksum or size mismatch")

    def verify(self, ref: ArtifactRef) -> ArtifactRef:
        self._verify_path(self._object_path(ref, create_shard=False), ref)
        return ref

    def read(self, ref: ArtifactRef) -> bytes:
        return self._read_verified(ref, offset=0, length=ref.size_bytes)

    def read_range(self, ref: ArtifactRef, *, offset: int, length: int) -> bytes:
        if type(offset) is not int or offset < 0:
            raise ArtifactRangeError("artifact range offset must be non-negative")
        if type(length) is not int or length < 0:
            raise ArtifactRangeError("artifact range length must be non-negative")
        if length > self.max_range_bytes:
            raise ArtifactRangeError("artifact range exceeds configured bound")
        if offset > ref.size_bytes or offset + length > ref.size_bytes:
            raise ArtifactRangeError("artifact range exceeds artifact bounds")
        return self._read_verified(ref, offset=offset, length=length)

    def _read_verified(self, ref: ArtifactRef, *, offset: int, length: int) -> bytes:
        path = self._object_path(ref, create_shard=False)
        if path.is_symlink() or not path.exists():
            raise ArtifactIntegrityError("artifact object is missing or is a symlink")
        if not _is_regular(path):
            raise ArtifactIntegrityError("artifact object is not a regular file")
        digest = hashlib.sha256()
        size = 0
        output = bytearray()
        end = offset + length
        try:
            with path.open("rb") as source:
                while chunk := source.read(self.chunk_size):
                    chunk_start = size
                    size += len(chunk)
                    digest.update(chunk)
                    chunk_end = size
                    overlap_start = max(offset, chunk_start)
                    overlap_end = min(end, chunk_end)
                    if overlap_start < overlap_end:
                        output.extend(chunk[overlap_start - chunk_start : overlap_end - chunk_start])
        except OSError as exc:
            raise ArtifactIntegrityError("artifact object cannot be read") from exc
        if size != ref.size_bytes or digest.hexdigest() != ref.digest:
            raise ArtifactIntegrityError("artifact object checksum or size mismatch")
        return bytes(output)


class BinaryIOBytes:
    """Small file-like adapter that keeps put_bytes on the streaming path."""

    def __init__(self, payload: bytes):
        self._payload = payload
        self._offset = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self._payload) - self._offset
        start = self._offset
        self._offset = min(len(self._payload), self._offset + size)
        return self._payload[start : self._offset]
