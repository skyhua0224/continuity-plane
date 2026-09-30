"""Provider-neutral durable workflow contracts with optional backends."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .durable_operation_runner import LocalDurableOperationRunner


class WorkflowOrchestrationError(ValueError):
    """Raised when a workflow contract or backend selection is unsafe."""


class WorkflowRuntimeUnavailable(RuntimeError):
    """Report whether an unavailable backend may already be executing."""

    def __init__(self, reason: str, *, remote_execution_possible: bool) -> None:
        super().__init__(reason)
        self.reason = reason
        self.remote_execution_possible = remote_execution_possible


class LocalWorkflowRuntimeAdapter:
    """Expose the synchronous M8-01 runner through the runtime contract."""

    def __init__(self, runner: LocalDurableOperationRunner) -> None:
        if not isinstance(runner, LocalDurableOperationRunner):
            raise TypeError("runner must be a LocalDurableOperationRunner")
        self.runner = runner
        self.capability_manifest = {
            "backend_id": "local-durable-operation",
            "execution_scope": "authority-instance",
            "distributed_execution": False,
            "state_write_authority": False,
            "claim_authority": False,
            "effect_authority": False,
        }

    async def start(
        self,
        prepared: dict[str, Any],
        *,
        request_id: str,
        request_sha256: str,
        binding_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id:
            raise WorkflowOrchestrationError("local runtime request_id is invalid")
        if (
            not isinstance(request_sha256, str)
            or len(request_sha256) != 64
            or any(
                not (character.isdigit() or "a" <= character <= "f")
                for character in request_sha256
            )
        ):
            raise WorkflowOrchestrationError("local runtime request_sha256 is invalid")
        if binding_receipt is not None:
            raise WorkflowOrchestrationError(
                "local runtime does not accept a remote backend binding"
            )
        return self.runner.run(prepared)


@dataclass(frozen=True)
class WorkflowRuntimeSelection:
    """One explicit runtime selection and its non-authoritative receipt."""

    runtime: Any
    receipt: dict[str, Any]

    async def start(
        self,
        prepared: dict[str, Any],
        *,
        request_id: str,
        request_sha256: str,
        binding_receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await self.runtime.start(
            prepared,
            request_id=request_id,
            request_sha256=request_sha256,
            binding_receipt=binding_receipt,
        )
        if not isinstance(result, dict):
            raise WorkflowOrchestrationError(
                "workflow runtime returned an invalid result"
            )
        return result


def _load_temporal() -> object:
    return importlib.import_module("temporalio")


def select_workflow_backend(
    profile: str,
    *,
    temporal_loader: Callable[[], object] | None = None,
) -> dict[str, Any]:
    """Select an explicit workflow backend without adding a default dependency."""
    if profile == "local-embedded":
        return {
            "schema_version": "context.workflow-backend-capabilities/v1alpha1",
            "backend_id": "local-reference",
            "profile": profile,
            "requires_sdk": False,
            "requires_service": False,
            "replay": True,
            "patching": True,
            "continue_as_new": True,
            "state_write_authority": False,
            "provider_native_authority": False,
        }
    if profile != "temporal":
        raise WorkflowOrchestrationError("workflow backend profile is unsupported")
    try:
        (temporal_loader or _load_temporal)()
    except (ImportError, ModuleNotFoundError) as exc:
        raise WorkflowOrchestrationError("Temporal SDK is unavailable") from exc
    return {
        "schema_version": "context.workflow-backend-capabilities/v1alpha1",
        "backend_id": "temporal",
        "profile": profile,
        "requires_sdk": True,
        "requires_service": True,
        "replay": True,
        "patching": True,
        "continue_as_new": True,
        "state_write_authority": False,
        "provider_native_authority": False,
    }


def _runtime_capabilities(runtime: Any) -> dict[str, Any]:
    if not callable(getattr(runtime, "start", None)):
        raise WorkflowOrchestrationError("workflow runtime must provide async start")
    manifest = getattr(runtime, "capability_manifest", None)
    required = {
        "backend_id",
        "execution_scope",
        "distributed_execution",
        "state_write_authority",
        "claim_authority",
        "effect_authority",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise WorkflowOrchestrationError("workflow runtime capabilities are invalid")
    if manifest["execution_scope"] not in {"authority-instance", "distributed"}:
        raise WorkflowOrchestrationError("workflow runtime execution scope is invalid")
    if manifest["distributed_execution"] is not (
        manifest["execution_scope"] == "distributed"
    ):
        raise WorkflowOrchestrationError(
            "workflow runtime distribution claim is invalid"
        )
    for field in ("state_write_authority", "claim_authority", "effect_authority"):
        if manifest[field] is not False:
            raise WorkflowOrchestrationError("workflow runtime cannot claim authority")
    return dict(manifest)


def _selection(runtime: Any, *, reason: str) -> WorkflowRuntimeSelection:
    manifest = _runtime_capabilities(runtime)
    receipt = {
        "schema_version": "context.workflow-runtime-selection/v1alpha1",
        "selected_backend_id": manifest["backend_id"],
        "selection_reason": reason,
        "execution_scope": manifest["execution_scope"],
        "distributed_execution": manifest["distributed_execution"],
        "backend_binding_committed": False,
        "state_write_authority": False,
        "claim_authority": False,
        "effect_authority": False,
    }
    return WorkflowRuntimeSelection(runtime=runtime, receipt=receipt)


def select_workflow_runtime(
    *,
    local_runtime: Any,
    temporal_factory: Callable[[], Any] | None = None,
    mode: str = "local",
) -> WorkflowRuntimeSelection:
    """Select before backend binding; ambiguous remote starts never fall back."""
    if mode not in {"local", "prefer-temporal", "require-temporal"}:
        raise WorkflowOrchestrationError("workflow runtime selection mode is invalid")
    if mode == "local":
        return _selection(local_runtime, reason="default-local")
    if temporal_factory is None:
        unavailable = WorkflowRuntimeUnavailable(
            "temporal_factory_unavailable", remote_execution_possible=False
        )
        if mode == "require-temporal":
            raise unavailable
        return _selection(local_runtime, reason="fallback-before-binding")
    try:
        temporal = temporal_factory()
    except WorkflowRuntimeUnavailable as exc:
        if mode == "require-temporal" or exc.remote_execution_possible:
            raise
        return _selection(local_runtime, reason="fallback-before-binding")
    return _selection(temporal, reason="temporal-available-before-binding")
