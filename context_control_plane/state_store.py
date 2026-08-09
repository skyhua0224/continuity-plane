"""Backend-neutral state-store contracts and capability validation."""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


_CAPABILITY_SCHEMA_VERSION = "context.state-store-capabilities/v1alpha1"
_AUTHORITY_MODES = {"local", "shared", "projection"}
_OPERATIONS = {"create_project", "read_project", "read_events", "commit_event"}
_LEASE_CLOCKS = {"none", "process", "backend"}
_ARTIFACT_SCOPES = {"none", "local", "shared"}
_ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")


class StateStoreError(RuntimeError):
    """Base error for state-store operations."""


class StateStoreCapabilityError(StateStoreError):
    """Raised when an adapter does not declare a usable capability contract."""


class StateStoreConflict(StateStoreError):
    """Raised when expected state or an append position is stale."""


class StateStoreNotFound(StateStoreError):
    """Raised when a requested authority object does not exist."""


class StateStoreIntegrityError(StateStoreError):
    """Raised when persisted or proposed state violates integrity rules."""


class StateStoreBusy(StateStoreError):
    """Raised when a transient backend lock prevents an operation."""


@dataclass(frozen=True)
class StateStoreCapabilityManifest:
    """Versioned guarantees declared by one state-store adapter."""

    schema_version: str
    adapter_id: str
    adapter_version: str
    authority_mode: str
    operations: tuple[str, ...]
    shared_authority: bool
    offline_write: bool
    unique_claim: bool
    multi_writer: bool
    lease_clock: str
    artifact_scope: str
    expected_revision: bool
    migration_source: bool
    migration_target: bool


@runtime_checkable
class StateStore(Protocol):
    """Capability-gated lifecycle boundary shared by all adapters."""

    capability_manifest: StateStoreCapabilityManifest

    def initialize(self) -> None: ...


@runtime_checkable
class AuthoritativeStateStore(StateStore, Protocol):
    """Snapshot and Event persistence boundary for authority adapters."""

    def create_project(self, snapshot: dict[str, Any]) -> None: ...

    def read_project(self, project_id: str) -> dict[str, Any]: ...

    def read_events(self, project_id: str) -> list[dict[str, Any]]: ...

    def commit_event(
        self,
        *,
        project_id: str,
        expected_revision: int,
        event: dict[str, Any],
        expected_snapshot: dict[str, Any],
    ) -> None: ...


def _validate_manifest(manifest: StateStoreCapabilityManifest) -> None:
    if manifest.schema_version != _CAPABILITY_SCHEMA_VERSION:
        raise StateStoreCapabilityError("unsupported capability schema_version")
    if not isinstance(manifest.adapter_id, str) or not _ADAPTER_ID_RE.fullmatch(
        manifest.adapter_id
    ):
        raise StateStoreCapabilityError("invalid adapter_id")
    if not isinstance(manifest.adapter_version, str) or not _SEMVER_RE.fullmatch(
        manifest.adapter_version
    ):
        raise StateStoreCapabilityError("invalid adapter_version")
    if (
        not isinstance(manifest.authority_mode, str)
        or manifest.authority_mode not in _AUTHORITY_MODES
    ):
        raise StateStoreCapabilityError("unsupported authority_mode")
    if not isinstance(manifest.operations, tuple) or not manifest.operations:
        raise StateStoreCapabilityError("invalid operations")
    if any(
        not isinstance(operation, str) or operation not in _OPERATIONS
        for operation in manifest.operations
    ):
        raise StateStoreCapabilityError("invalid operations")
    if len(set(manifest.operations)) != len(manifest.operations):
        raise StateStoreCapabilityError("invalid operations")
    if (
        not isinstance(manifest.lease_clock, str)
        or manifest.lease_clock not in _LEASE_CLOCKS
    ):
        raise StateStoreCapabilityError("unsupported lease_clock")
    if (
        not isinstance(manifest.artifact_scope, str)
        or manifest.artifact_scope not in _ARTIFACT_SCOPES
    ):
        raise StateStoreCapabilityError("unsupported artifact_scope")
    for field in (
        "shared_authority",
        "offline_write",
        "unique_claim",
        "multi_writer",
        "expected_revision",
        "migration_source",
        "migration_target",
    ):
        if type(getattr(manifest, field)) is not bool:
            raise StateStoreCapabilityError(f"{field} must be boolean")
    if manifest.authority_mode == "projection" and (
        "commit_event" in manifest.operations
        or manifest.shared_authority
        or manifest.multi_writer
        or manifest.unique_claim
    ):
        raise StateStoreCapabilityError(
            "projection adapters cannot declare authoritative capabilities"
        )
    if manifest.shared_authority != (manifest.authority_mode == "shared"):
        raise StateStoreCapabilityError(
            "authority_mode and shared_authority are inconsistent"
        )
    if manifest.unique_claim and not manifest.shared_authority:
        raise StateStoreCapabilityError(
            "unique_claim requires shared_authority"
        )
    if manifest.unique_claim and manifest.lease_clock == "none":
        raise StateStoreCapabilityError(
            "unique_claim requires a non-none lease_clock"
        )
    if (
        "commit_event" in manifest.operations or manifest.multi_writer
    ) and not manifest.expected_revision:
        raise StateStoreCapabilityError(
            "commit_event and multi_writer require expected_revision"
        )


def capability_manifest_to_document(
    manifest: StateStoreCapabilityManifest,
) -> dict[str, Any]:
    """Return the canonical JSON-compatible representation of a manifest."""
    _validate_manifest(manifest)
    return {
        "schema_version": manifest.schema_version,
        "adapter_id": manifest.adapter_id,
        "adapter_version": manifest.adapter_version,
        "authority_mode": manifest.authority_mode,
        "operations": list(manifest.operations),
        "shared_authority": manifest.shared_authority,
        "offline_write": manifest.offline_write,
        "unique_claim": manifest.unique_claim,
        "multi_writer": manifest.multi_writer,
        "lease_clock": manifest.lease_clock,
        "artifact_scope": manifest.artifact_scope,
        "expected_revision": manifest.expected_revision,
        "migration_source": manifest.migration_source,
        "migration_target": manifest.migration_target,
    }


def capability_manifest_from_document(
    document: dict[str, Any],
) -> StateStoreCapabilityManifest:
    """Parse a strict JSON-compatible manifest document."""
    if not isinstance(document, dict):
        raise StateStoreCapabilityError("capability document must be an object")
    expected_fields = set(StateStoreCapabilityManifest.__dataclass_fields__)
    if set(document) != expected_fields:
        raise StateStoreCapabilityError("capability document fields are invalid")
    operations = document.get("operations")
    if not isinstance(operations, list):
        raise StateStoreCapabilityError("capability document operations must be an array")
    try:
        manifest = StateStoreCapabilityManifest(
            **{
                **document,
                "operations": tuple(operations),
            }
        )
    except TypeError as exc:
        raise StateStoreCapabilityError("capability document is invalid") from exc
    _validate_manifest(manifest)
    return manifest


_MISSING = object()


def _static_attribute(adapter: Any, name: str) -> Any:
    return inspect.getattr_static(adapter, name, _MISSING)


def _has_static_callable(adapter: Any, name: str) -> bool:
    attribute = _static_attribute(adapter, name)
    if isinstance(attribute, (staticmethod, classmethod)):
        attribute = attribute.__func__
    return callable(attribute)


def validate_state_store_adapter(adapter: Any) -> StateStoreCapabilityManifest:
    """Reject adapters that do not expose a capability manifest."""
    manifest = _static_attribute(adapter, "capability_manifest")
    if manifest is _MISSING:
        raise StateStoreCapabilityError("adapter requires capability_manifest")
    if not isinstance(manifest, StateStoreCapabilityManifest):
        raise StateStoreCapabilityError(
            "capability_manifest must be StateStoreCapabilityManifest"
        )
    _validate_manifest(manifest)
    for operation in ("initialize", *manifest.operations):
        if not _has_static_callable(adapter, operation):
            raise StateStoreCapabilityError(
                f"adapter operation is not callable: {operation}"
            )
    return manifest


def initialize_state_store(adapter: Any) -> None:
    """Initialize an adapter after validating its static capability contract."""
    validate_state_store_adapter(adapter)
    adapter.initialize()


def invoke_state_store(
    adapter: Any,
    operation: str,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Invoke one operation only after its capability has been validated."""
    manifest = validate_state_store_adapter(adapter)
    if operation not in manifest.operations:
        raise StateStoreCapabilityError(
            f"state-store operation is not declared: {operation}"
        )
    return getattr(adapter, operation)(*args, **kwargs)
