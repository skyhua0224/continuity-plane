"""Checkpoint restore receipts that gate durable operation execution."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, ClassVar

from .artifact_store import ArtifactRef, LocalArtifactStore
from .checkpoint import CheckpointError, restore_checkpoint
from .durable_operation import validate_durable_operation
from .postcompact_canary import (
    PostCompactCanaryError,
    validate_postcompact_canary_receipt,
)

RECEIPT_SCHEMA_VERSION = "context.durable-checkpoint-gate/v1alpha1"
_RECEIPT_FIELDS = {
    "schema_version",
    "operation_id",
    "project_id",
    "work_id",
    "claim_id",
    "project_revision",
    "event_head",
    "checkpoint_ref",
    "critical_projection_sha256",
    "recovery_kind",
    "postcompact_canary_sha256",
    "execution_gate",
    "state_write_authority",
    "provider_native_authority",
    "receipt_sha256",
}
_MANIFEST_FIELDS = {
    "schema_version",
    "adapter_id",
    "adapter_version",
    "source_ref",
    "receipt_schema_version",
    "state_write_authority",
    "provider_native_authority",
}
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")


class DurableCheckpointGateError(RuntimeError):
    """Raised when a checkpoint cannot reopen durable execution."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise DurableCheckpointGateError("checkpoint gate data is not canonical") from exc


def _receipt_digest(receipt: dict[str, Any]) -> str:
    body = copy.deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return hashlib.sha256(_canonical(body)).hexdigest()


def validate_checkpoint_gate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise DurableCheckpointGateError("checkpoint gate manifest is invalid")
    normalized = copy.deepcopy(manifest)
    if normalized["schema_version"] != "context.durable-checkpoint-adapter/v1alpha1":
        raise DurableCheckpointGateError("checkpoint gate manifest version is invalid")
    for field in ("adapter_id", "adapter_version"):
        value = normalized[field]
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            raise DurableCheckpointGateError(f"checkpoint gate {field} is invalid")
    source_ref = normalized["source_ref"]
    if not isinstance(source_ref, str) or not source_ref or any(
        character in source_ref for character in "\r\n\x00"
    ):
        raise DurableCheckpointGateError("checkpoint gate source_ref is invalid")
    if normalized["receipt_schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise DurableCheckpointGateError("checkpoint gate receipt version is invalid")
    if normalized["state_write_authority"] is not False:
        raise DurableCheckpointGateError("checkpoint gate cannot claim State authority")
    if normalized["provider_native_authority"] is not False:
        raise DurableCheckpointGateError("checkpoint gate cannot claim provider authority")
    return normalized


def validate_durable_checkpoint_receipt(
    receipt: Any, *, operation: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise DurableCheckpointGateError("checkpoint gate receipt fields are invalid")
    normalized = copy.deepcopy(receipt)
    if normalized["schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise DurableCheckpointGateError("checkpoint gate receipt version is invalid")
    for field in ("operation_id", "project_id", "work_id", "claim_id"):
        value = normalized[field]
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            raise DurableCheckpointGateError(f"checkpoint receipt {field} is invalid")
    if type(normalized["project_revision"]) is not int or normalized["project_revision"] < 0:
        raise DurableCheckpointGateError("checkpoint receipt revision is invalid")
    head = normalized["event_head"]
    if (
        not isinstance(head, dict)
        or set(head) != {"sequence_no", "event_sha256"}
        or type(head["sequence_no"]) is not int
        or head["sequence_no"] <= 0
        or not isinstance(head["event_sha256"], str)
        or _SHA_RE.fullmatch(head["event_sha256"]) is None
    ):
        raise DurableCheckpointGateError("checkpoint receipt event head is invalid")
    try:
        ArtifactRef.from_document(normalized["checkpoint_ref"])
    except (TypeError, ValueError) as exc:
        raise DurableCheckpointGateError("checkpoint receipt artifact ref is invalid") from exc
    for field in ("critical_projection_sha256", "receipt_sha256"):
        value = normalized[field]
        if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
            raise DurableCheckpointGateError(f"checkpoint receipt {field} is invalid")
    if normalized["recovery_kind"] not in {"process-restart", "postcompact"}:
        raise DurableCheckpointGateError("checkpoint receipt recovery kind is invalid")
    canary_sha256 = normalized["postcompact_canary_sha256"]
    if normalized["recovery_kind"] == "process-restart":
        if canary_sha256 is not None:
            raise DurableCheckpointGateError(
                "process restart receipt cannot claim a PostCompact canary"
            )
    elif not isinstance(canary_sha256, str) or _SHA_RE.fullmatch(canary_sha256) is None:
        raise DurableCheckpointGateError(
            "postcompact receipt requires a valid canary digest"
        )
    if normalized["execution_gate"] != "allow":
        raise DurableCheckpointGateError("checkpoint execution gate is closed")
    if normalized["state_write_authority"] is not False:
        raise DurableCheckpointGateError("checkpoint receipt cannot grant State authority")
    if normalized["provider_native_authority"] is not False:
        raise DurableCheckpointGateError("checkpoint receipt cannot grant provider authority")
    if normalized["receipt_sha256"] != _receipt_digest(normalized):
        raise DurableCheckpointGateError("checkpoint receipt digest mismatch")
    if operation is not None:
        current = validate_durable_operation(copy.deepcopy(operation))
        expected = {
            "operation_id": current["operation_id"],
            "project_id": current["project_id"],
            "work_id": current["work_id"],
            "claim_id": current["claim_id"],
            "project_revision": current["authority"]["project_revision"],
            "event_head": current["authority"]["event_head"],
            "checkpoint_ref": current["checkpoint_ref"],
        }
        for field, value in expected.items():
            if normalized[field] != value:
                raise DurableCheckpointGateError(
                    f"checkpoint receipt {field} does not match operation"
                )
    return normalized


def compose_durable_checkpoint_receipt(
    operation: dict[str, Any],
    *,
    critical_projection_sha256: str,
    recovery_kind: str = "process-restart",
    postcompact_canary_sha256: str | None = None,
) -> dict[str, Any]:
    current = validate_durable_operation(copy.deepcopy(operation))
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "operation_id": current["operation_id"],
        "project_id": current["project_id"],
        "work_id": current["work_id"],
        "claim_id": current["claim_id"],
        "project_revision": current["authority"]["project_revision"],
        "event_head": copy.deepcopy(current["authority"]["event_head"]),
        "checkpoint_ref": copy.deepcopy(current["checkpoint_ref"]),
        "critical_projection_sha256": critical_projection_sha256,
        "recovery_kind": recovery_kind,
        "postcompact_canary_sha256": postcompact_canary_sha256,
        "execution_gate": "allow",
        "state_write_authority": False,
        "provider_native_authority": False,
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    return validate_durable_checkpoint_receipt(receipt, operation=current)


class LocalDurableCheckpointGate:
    """Restore an M2 checkpoint before opening a local operation gate."""

    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-checkpoint-adapter/v1alpha1",
        "adapter_id": "context.local-checkpoint/v1",
        "adapter_version": "1.0.0-alpha.1",
        "source_ref": "component://context.checkpoint",
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        governance_ref: str,
        canonical_plan_sha256: str,
        registry_digest: str,
        recovery_kind: str = "process-restart",
        postcompact_canary_receipt: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(artifact_store, LocalArtifactStore):
            raise TypeError("artifact_store must be a LocalArtifactStore")
        if not isinstance(governance_ref, str) or not governance_ref:
            raise ValueError("governance_ref is invalid")
        for field, value in (
            ("canonical_plan_sha256", canonical_plan_sha256),
            ("registry_digest", registry_digest),
        ):
            if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
                raise ValueError(f"{field} is invalid")
        if recovery_kind not in {"process-restart", "postcompact"}:
            raise DurableCheckpointGateError("checkpoint recovery kind is invalid")
        if recovery_kind == "process-restart":
            if postcompact_canary_receipt is not None:
                raise DurableCheckpointGateError(
                    "process restart does not accept a PostCompact canary"
                )
            normalized_canary = None
        else:
            if not isinstance(postcompact_canary_receipt, dict):
                raise DurableCheckpointGateError(
                    "postcompact recovery requires a canary"
                )
            try:
                validate_postcompact_canary_receipt(postcompact_canary_receipt)
            except (PostCompactCanaryError, TypeError, ValueError) as exc:
                raise DurableCheckpointGateError(
                    "postcompact recovery canary is invalid"
                ) from exc
            normalized_canary = copy.deepcopy(postcompact_canary_receipt)
        self.artifact_store = artifact_store
        self.governance_ref = governance_ref
        self.canonical_plan_sha256 = canonical_plan_sha256
        self.registry_digest = registry_digest
        self.recovery_kind = recovery_kind
        self.postcompact_canary_receipt = normalized_canary

    def verify(self, operation: dict[str, Any]) -> dict[str, Any]:
        current = validate_durable_operation(copy.deepcopy(operation))
        try:
            restored = restore_checkpoint(
                ArtifactRef.from_document(current["checkpoint_ref"]),
                self.artifact_store,
                expected_project_id=current["project_id"],
                expected_revision=current["authority"]["project_revision"],
                expected_event_head=current["authority"]["event_head"],
                expected_governance_ref=self.governance_ref,
                expected_plan_sha256=self.canonical_plan_sha256,
                expected_registry_digest=self.registry_digest,
            )
        except (CheckpointError, TypeError, ValueError) as exc:
            raise DurableCheckpointGateError("checkpoint restore failed") from exc
        work = next(
            (
                item
                for item in restored.snapshot["works"]
                if item["work_id"] == current["work_id"]
            ),
            None,
        )
        claim = next(
            (
                item
                for item in restored.snapshot["claims"]
                if item["claim_id"] == current["claim_id"]
            ),
            None,
        )
        if (
            work is None
            or work["status"] != "active"
            or claim is None
            or claim["work_id"] != current["work_id"]
            or claim["status"] != "active"
        ):
            raise DurableCheckpointGateError(
                "checkpoint does not contain the active Work and Claim"
            )
        canary_sha256 = None
        if self.recovery_kind == "postcompact":
            canary = self.postcompact_canary_receipt
            expected_canary = {
                "project_id": current["project_id"],
                "project_revision": current["authority"]["project_revision"],
                "event_head": current["authority"]["event_head"],
                "active_work_id": current["work_id"],
                "checkpoint_sha256": current["checkpoint_ref"]["digest"],
            }
            if canary is None or any(
                canary[field] != value for field, value in expected_canary.items()
            ):
                raise DurableCheckpointGateError(
                    "PostCompact canary does not match durable operation"
                )
            canary_sha256 = canary["canary_sha256"]
        return compose_durable_checkpoint_receipt(
            current,
            critical_projection_sha256=restored.manifest[
                "critical_projection_sha256"
            ],
            recovery_kind=self.recovery_kind,
            postcompact_canary_sha256=canary_sha256,
        )
