"""M8-02 scoped claim and lease capabilities for state-store adapters.

The v1 state-store manifest is intentionally kept stable.  This module adds
the stronger claim-scope and lease proof fields without changing the v1 wire.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .sqlite_state_store import SQLiteStateStore
from .state_store import (
    StateStoreCapabilityError,
    StateStoreCapabilityManifest,
    validate_state_store_adapter,
)

CAPABILITY_SCHEMA_VERSION = "context.state-store-capabilities/v2alpha1"
_AUTHORITY_MODES = {"local", "shared", "projection"}
_OPERATIONS = {
    "create_project",
    "read_project",
    "read_events",
    "commit_event",
    "initialize_work_ledger",
    "execute_work_ledger",
    "read_work_ledger",
    "read_work_ledger_receipt",
}
_CLAIM_SCOPES = {"none", "authority-instance", "shared"}
_LEASE_CLOCKS = {"none", "process", "backend"}
_ARTIFACT_SCOPES = {"none", "local", "shared"}
_RUNTIME_PROFILES = {"local-embedded", "local-coordinator", "shared-strong"}
_ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")


class StateStoreCapabilityV2Error(StateStoreCapabilityError):
    """Raised when a v2 capability declaration is inconsistent or overclaims."""


@dataclass(frozen=True)
class StateStoreCapabilityManifestV2:
    """Versioned backend guarantees, including claim and lease scope."""

    schema_version: str
    adapter_id: str
    adapter_version: str
    authority_mode: str
    operations: tuple[str, ...]
    shared_authority: bool
    offline_write: bool
    unique_claim_scope: str
    multi_writer: bool
    lease_clock: str
    atomic_lease_transition: bool
    fencing: bool
    durable_request_receipts: bool
    artifact_scope: str
    expected_revision: bool
    migration_source: bool
    migration_target: bool


def _error(message: str) -> StateStoreCapabilityV2Error:
    return StateStoreCapabilityV2Error(message)


def validate_state_store_capability_manifest_v2(
    manifest: StateStoreCapabilityManifestV2,
) -> None:
    """Validate a v2 manifest without consulting mutable adapter state."""
    if not isinstance(manifest, StateStoreCapabilityManifestV2):
        raise _error("manifest must be StateStoreCapabilityManifestV2")
    if manifest.schema_version != CAPABILITY_SCHEMA_VERSION:
        raise _error("unsupported capability schema_version")
    if not isinstance(manifest.adapter_id, str) or not _ADAPTER_ID_RE.fullmatch(
        manifest.adapter_id
    ):
        raise _error("invalid adapter_id")
    if not isinstance(manifest.adapter_version, str) or not _SEMVER_RE.fullmatch(
        manifest.adapter_version
    ):
        raise _error("invalid adapter_version")
    if (
        not isinstance(manifest.authority_mode, str)
        or manifest.authority_mode not in _AUTHORITY_MODES
    ):
        raise _error("unsupported authority_mode")
    if not isinstance(manifest.operations, tuple) or not manifest.operations:
        raise _error("invalid operations")
    if any(
        not isinstance(operation, str) or operation not in _OPERATIONS
        for operation in manifest.operations
    ):
        raise _error("invalid operations")
    if len(set(manifest.operations)) != len(manifest.operations):
        raise _error("invalid operations")
    if (
        not isinstance(manifest.unique_claim_scope, str)
        or manifest.unique_claim_scope not in _CLAIM_SCOPES
    ):
        raise _error("unsupported unique_claim_scope")
    if (
        not isinstance(manifest.lease_clock, str)
        or manifest.lease_clock not in _LEASE_CLOCKS
    ):
        raise _error("unsupported lease_clock")
    if (
        not isinstance(manifest.artifact_scope, str)
        or manifest.artifact_scope not in _ARTIFACT_SCOPES
    ):
        raise _error("unsupported artifact_scope")
    for field in (
        "shared_authority",
        "offline_write",
        "multi_writer",
        "atomic_lease_transition",
        "fencing",
        "durable_request_receipts",
        "expected_revision",
        "migration_source",
        "migration_target",
    ):
        if type(getattr(manifest, field)) is not bool:
            raise _error(f"{field} must be boolean")

    if manifest.shared_authority != (manifest.authority_mode == "shared"):
        raise _error("authority_mode and shared_authority are inconsistent")
    if manifest.authority_mode == "projection" and (
        "commit_event" in manifest.operations
        or manifest.shared_authority
        or manifest.multi_writer
        or manifest.unique_claim_scope != "none"
        or manifest.atomic_lease_transition
        or manifest.fencing
    ):
        raise _error("projection adapters cannot declare authoritative capabilities")
    if (
        "commit_event" in manifest.operations or manifest.multi_writer
    ) and not manifest.expected_revision:
        raise _error("commit_event and multi_writer require expected_revision")
    if manifest.atomic_lease_transition and not manifest.expected_revision:
        raise _error("atomic_lease_transition requires expected_revision")
    if manifest.fencing and (
        not manifest.atomic_lease_transition or not manifest.expected_revision
    ):
        raise _error("fencing requires atomic_lease_transition and expected_revision")

    if manifest.unique_claim_scope == "none":
        if manifest.lease_clock != "none":
            raise _error("unique_claim_scope none requires lease_clock none")
        if manifest.atomic_lease_transition:
            raise _error("unique_claim_scope none cannot claim atomic_lease_transition")
        if manifest.fencing:
            raise _error("unique_claim_scope none cannot claim fencing")
    elif manifest.unique_claim_scope == "authority-instance":
        if manifest.authority_mode != "local":
            raise _error(
                "authority-instance unique claims require local authority_mode"
            )
        if manifest.lease_clock != "process":
            raise _error("authority-instance unique claims require process lease_clock")
        if not manifest.atomic_lease_transition:
            raise _error("authority-instance claims require atomic_lease_transition")
        if not manifest.fencing:
            raise _error("authority-instance claims require fencing")
    else:
        if manifest.authority_mode != "shared":
            raise _error("shared unique claims require shared authority_mode")
        if manifest.lease_clock != "backend":
            raise _error("shared unique claims require backend lease_clock")
        if not manifest.atomic_lease_transition:
            raise _error("shared claims require atomic_lease_transition")
        if not manifest.fencing:
            raise _error("shared claims require fencing")
        if not manifest.durable_request_receipts:
            raise _error("shared claims require durable_request_receipts")


def capability_manifest_v2_to_document(
    manifest: StateStoreCapabilityManifestV2,
) -> dict[str, Any]:
    """Return a strict JSON-compatible v2 manifest document."""
    validate_state_store_capability_manifest_v2(manifest)
    return {
        "schema_version": manifest.schema_version,
        "adapter_id": manifest.adapter_id,
        "adapter_version": manifest.adapter_version,
        "authority_mode": manifest.authority_mode,
        "operations": list(manifest.operations),
        "shared_authority": manifest.shared_authority,
        "offline_write": manifest.offline_write,
        "unique_claim_scope": manifest.unique_claim_scope,
        "multi_writer": manifest.multi_writer,
        "lease_clock": manifest.lease_clock,
        "atomic_lease_transition": manifest.atomic_lease_transition,
        "fencing": manifest.fencing,
        "durable_request_receipts": manifest.durable_request_receipts,
        "artifact_scope": manifest.artifact_scope,
        "expected_revision": manifest.expected_revision,
        "migration_source": manifest.migration_source,
        "migration_target": manifest.migration_target,
    }


def capability_manifest_v2_from_document(
    document: dict[str, Any],
) -> StateStoreCapabilityManifestV2:
    """Parse and validate a strict v2 manifest document."""
    if not isinstance(document, dict):
        raise _error("capability document must be an object")
    expected_fields = set(StateStoreCapabilityManifestV2.__dataclass_fields__)
    if set(document) != expected_fields:
        raise _error("capability document fields are invalid")
    operations = document.get("operations")
    if not isinstance(operations, list):
        raise _error("capability document operations must be an array")
    try:
        manifest = StateStoreCapabilityManifestV2(
            **{**document, "operations": tuple(operations)}
        )
    except (TypeError, ValueError) as exc:
        raise _error("capability document is invalid") from exc
    validate_state_store_capability_manifest_v2(manifest)
    return manifest


def capability_manifest_v2_from_v1(
    manifest: StateStoreCapabilityManifest,
) -> StateStoreCapabilityManifestV2:
    """Project a v1 adapter into the conservative v2 capability surface."""
    if not isinstance(manifest, StateStoreCapabilityManifest):
        raise _error("v1 manifest is invalid")
    if manifest.unique_claim:
        raise _error(
            "v1 unique_claim cannot be scoped safely; provide an explicit v2 manifest"
        )
    projected = StateStoreCapabilityManifestV2(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        adapter_id=manifest.adapter_id,
        adapter_version=manifest.adapter_version,
        authority_mode=manifest.authority_mode,
        operations=manifest.operations,
        shared_authority=manifest.shared_authority,
        offline_write=manifest.offline_write,
        unique_claim_scope="none",
        multi_writer=manifest.multi_writer,
        lease_clock="none",
        atomic_lease_transition=False,
        fencing=False,
        durable_request_receipts=False,
        artifact_scope=manifest.artifact_scope,
        expected_revision=manifest.expected_revision,
        migration_source=manifest.migration_source,
        migration_target=manifest.migration_target,
    )
    validate_state_store_capability_manifest_v2(projected)
    return projected


_MISSING = object()


def _static_attribute(adapter: Any, name: str) -> Any:
    return inspect.getattr_static(adapter, name, _MISSING)


def validate_state_store_adapter_v2(
    adapter: Any,
) -> StateStoreCapabilityManifestV2:
    """Validate both the stable v1 surface and the explicit v2 projection."""
    try:
        validate_state_store_adapter(adapter)
    except StateStoreCapabilityError as exc:
        raise StateStoreCapabilityV2Error(str(exc)) from exc
    manifest = _static_attribute(adapter, "capability_manifest_v2")
    if manifest is _MISSING:
        raise _error("adapter requires capability_manifest_v2")
    if not isinstance(manifest, StateStoreCapabilityManifestV2):
        raise _error("capability_manifest_v2 must be StateStoreCapabilityManifestV2")
    validate_state_store_capability_manifest_v2(manifest)
    if manifest.adapter_id != adapter.capability_manifest.adapter_id:
        raise _error("v1 and v2 adapter_id are inconsistent")
    for operation in manifest.operations:
        if not callable(getattr(adapter, operation, None)):
            raise _error(f"adapter does not implement v2 operation: {operation}")
    return manifest


def project_state_store_capabilities_v2(
    adapter: Any,
    *,
    requested_runtime_profile: str | None = None,
) -> StateStoreCapabilityManifestV2:
    """Project adapter evidence into v2, refusing unsupported profiles."""
    try:
        v1 = validate_state_store_adapter(adapter)
    except StateStoreCapabilityError as exc:
        raise StateStoreCapabilityV2Error(str(exc)) from exc
    explicit = _static_attribute(adapter, "capability_manifest_v2")
    if explicit is not _MISSING:
        if not isinstance(explicit, StateStoreCapabilityManifestV2):
            raise _error(
                "capability_manifest_v2 must be StateStoreCapabilityManifestV2"
            )
        manifest = explicit
        validate_state_store_capability_manifest_v2(manifest)
        if manifest.adapter_id != v1.adapter_id:
            raise _error("v1 and v2 adapter_id are inconsistent")
    else:
        manifest = capability_manifest_v2_from_v1(v1)

    if requested_runtime_profile is None:
        return manifest
    if requested_runtime_profile not in _RUNTIME_PROFILES:
        raise _error(f"unsupported runtime profile: {requested_runtime_profile}")
    if requested_runtime_profile == "local-embedded":
        if manifest.authority_mode != "local" or manifest.unique_claim_scope != "none":
            raise _error("adapter does not prove local-embedded runtime profile")
    elif requested_runtime_profile == "local-coordinator":
        if not (
            manifest.authority_mode == "local"
            and manifest.unique_claim_scope == "authority-instance"
            and manifest.lease_clock == "process"
        ):
            raise _error("adapter does not prove local-coordinator runtime profile")
    elif not (
        manifest.authority_mode == "shared"
        and manifest.unique_claim_scope == "shared"
        and manifest.lease_clock == "backend"
    ):
        raise _error("adapter does not prove shared-strong runtime profile")
    return manifest


class SQLiteLocalCoordinatorStateStore:
    """Opt-in process-scoped coordinator layered over one SQLite authority."""

    def __init__(
        self,
        store_or_path: Any,
        *,
        busy_timeout_ms: int = 5_000,
        fault_hook: Any = None,
    ) -> None:
        if isinstance(store_or_path, SQLiteStateStore):
            self._store = store_or_path
        else:
            self._store = SQLiteStateStore(
                Path(store_or_path),
                busy_timeout_ms=busy_timeout_ms,
                fault_hook=fault_hook,
            )

    capability_manifest = replace(
        # The wrapper has a distinct adapter identity but the same stable v1 API.
        SQLiteStateStore.capability_manifest,
        adapter_id="context.sqlite-local-coordinator",
    )
    capability_manifest_v2 = StateStoreCapabilityManifestV2(
        schema_version=CAPABILITY_SCHEMA_VERSION,
        adapter_id="context.sqlite-local-coordinator",
        adapter_version="0.1.0-alpha.1",
        authority_mode="local",
        operations=(
            "create_project",
            "read_project",
            "read_events",
            "commit_event",
            "initialize_work_ledger",
            "execute_work_ledger",
            "read_work_ledger",
            "read_work_ledger_receipt",
        ),
        shared_authority=False,
        offline_write=True,
        unique_claim_scope="authority-instance",
        multi_writer=True,
        lease_clock="process",
        atomic_lease_transition=True,
        fencing=True,
        durable_request_receipts=True,
        artifact_scope="none",
        expected_revision=True,
        migration_source=False,
        migration_target=False,
    )

    def initialize(self) -> None:
        self._store.initialize()

    def create_project(self, snapshot: dict[str, Any]) -> None:
        self._store.create_project(snapshot)

    def read_project(self, project_id: str) -> dict[str, Any]:
        return self._store.read_project(project_id)

    def read_events(self, project_id: str) -> list[dict[str, Any]]:
        return self._store.read_events(project_id)

    def commit_event(
        self,
        *,
        project_id: str,
        expected_revision: int,
        event: dict[str, Any],
        expected_snapshot: dict[str, Any],
    ) -> None:
        self._store.commit_event(
            project_id=project_id,
            expected_revision=expected_revision,
            event=event,
            expected_snapshot=expected_snapshot,
        )

    def initialize_work_ledger(
        self,
        *,
        project_id: str,
        project_revision: int,
        works: list[dict[str, Any]],
        max_ttl_ms: int,
    ) -> None:
        self._store.initialize_work_ledger(
            project_id=project_id,
            project_revision=project_revision,
            works=works,
            max_ttl_ms=max_ttl_ms,
        )

    def execute_work_ledger(
        self,
        *,
        project_id: str,
        operation: str,
        request_id: str,
        arguments: dict[str, Any],
        request_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._store.execute_work_ledger(
            project_id=project_id,
            operation=operation,
            request_id=request_id,
            arguments=arguments,
            request_payload=request_payload,
            request_namespace="context.state-mcp/v3alpha1",
        )

    def read_work_ledger(self, project_id: str) -> dict[str, Any]:
        return self._store.read_work_ledger(project_id)

    def read_work_ledger_receipt(
        self,
        project_id: str,
        operation: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        del operation
        return self._store.read_work_ledger_receipt(
            project_id,
            "context.state-mcp/v3alpha1",
            request_id,
        )


validate_state_store_capability_manifest_v2(
    SQLiteLocalCoordinatorStateStore.capability_manifest_v2
)
