"""Lazy optional mapping from durable workflow contracts to Temporal."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .durable_workflow import (
    DurableWorkflowError,
    validate_workflow_definition,
    validate_workflow_rollover,
    validate_workflow_run,
)
from .workflow_orchestration import WorkflowRuntimeUnavailable

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_BINDING_SCHEMA_VERSION = "context.workflow-backend-binding/v1alpha1"
_RUN_RECEIPT_SCHEMA_VERSION = "context.workflow-run-receipt/v1alpha1"
_DISPATCH_RECEIPT_SCHEMA_VERSION = "context.effect-dispatch-receipt/v1alpha1"
MAX_TEMPORAL_PAYLOAD_BYTES = 262_144
_DISPATCH_RECEIPT_FIELDS = {
    "schema_version",
    "request_id",
    "project_id",
    "project_revision",
    "work_id",
    "claim_id",
    "claim_revision",
    "lease_epoch",
    "fence",
    "effect_id",
    "effect_key",
    "request_sha256",
    "operation",
    "scope_ref",
    "dispatch_started_at",
    "receipt_sha256",
}
_STATE_MCP_RESULT_FIELDS = {
    "schema_version",
    "operation",
    "status",
    "project_revision",
    "effect",
    "receipt",
    "transition",
}
_TRANSITION_FIELDS = {
    "schema_version",
    "operation",
    "project_id",
    "project_revision_before",
    "project_revision_after",
    "actor_ref",
    "observed_at",
    "previous_transition_sha256",
    "changes",
    "transition_id",
    "transition_sha256",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        raise DurableWorkflowError(f"{field} is invalid")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise DurableWorkflowError(f"{field} must be a lowercase SHA-256")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise DurableWorkflowError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DurableWorkflowError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DurableWorkflowError(f"{field} requires a timezone")
    return value


def _load_temporal() -> object:
    return importlib.import_module("temporalio")


def _bounded_payload(run: dict[str, Any]) -> dict[str, Any]:
    current = validate_workflow_run(run)
    payload = {
        "schema_version": "context.temporal-workflow-input/v1alpha1",
        "workflow_id": current["workflow_id"],
        "chain_id": current["chain_id"],
        "run_id": current["run_id"],
        "generation": current["generation"],
        "previous_run_id": current["previous_run_id"],
        "previous_run_event_sha256": current["previous_run_event_sha256"],
        "project_id": current["project_id"],
        "root_work_id": current["root_work_id"],
        "request_id": current["request_id"],
        "workflow_type": current["workflow_type"],
        "definition_version": current["definition_version"],
        "implementation_sha256": current["implementation_sha256"],
        "authority": copy.deepcopy(current["authority"]),
        "checkpoint_ref": current["checkpoint_ref"],
        "checkpoint_sha256": current["checkpoint_sha256"],
        "continuation_sha256": current["continuation_sha256"],
        "active_patch_ids": copy.deepcopy(current["active_patch_ids"]),
        "pending_inputs": [
            {
                "input_id": item["input_id"],
                "input_sha256": item["input_sha256"],
                "status": item["status"],
            }
            for item in current["pending_inputs"]
        ],
        "input_ack_count": current["input_ack_count"],
        "input_ack_sha256": current["input_ack_sha256"],
        "effect_statuses": [
            {
                "effect_id": item["effect_id"],
                "effect_key": item["effect_key"],
                "request_sha256": item["request_sha256"],
                "status": item["status"],
            }
            for item in current["effects"]
        ],
        "effect_settlement_count": current["effect_settlement_count"],
        "effect_settlement_sha256": current["effect_settlement_sha256"],
        "run_sha256": current["run_sha256"],
        "state_write_authority": False,
        "effect_dispatch_authority": False,
        "provider_native_authority": False,
    }
    if len(_canonical(payload)) > MAX_TEMPORAL_PAYLOAD_BYTES:
        raise DurableWorkflowError("Temporal workflow input exceeds payload limit")
    return payload


def bind_temporal_payload_receipt(
    payload: dict[str, Any], *, receipt_field: str, receipt_sha256: str
) -> dict[str, Any]:
    """Attach one authority receipt before enforcing the final payload limit."""
    if receipt_field not in {
        "backend_binding_receipt_sha256",
        "rollover_receipt_sha256",
    }:
        raise DurableWorkflowError("Temporal payload receipt field is invalid")
    _sha256(receipt_sha256, "Temporal payload receipt_sha256")
    if not isinstance(payload, dict) or receipt_field in payload:
        raise DurableWorkflowError("Temporal workflow payload is invalid")
    bound = copy.deepcopy(payload)
    bound[receipt_field] = receipt_sha256
    if len(_canonical(bound)) > MAX_TEMPORAL_PAYLOAD_BYTES:
        raise DurableWorkflowError("Temporal workflow input exceeds payload limit")
    return bound


def workflow_backend_binding_intent(
    run: dict[str, Any], *, backend_id: str
) -> dict[str, str]:
    """Derive the exact State MCP effect identity for a remote workflow start."""
    current = validate_workflow_run(run)
    _identifier(backend_id, "backend_id")
    operation = "workflow-backend-start"
    request_sha256 = _digest(
        {
            "schema_version": "context.workflow-backend-intent/v1alpha1",
            "backend_id": backend_id,
            "workflow_id": current["workflow_id"],
            "chain_id": current["chain_id"],
            "run_id": current["run_id"],
            "run_sha256": current["run_sha256"],
            "project_revision": current["authority"]["project_revision"],
            "event_head_sha256": current["authority"]["event_head"]["event_sha256"],
        }
    )
    return {
        "effect_id": "workflow-backend-"
        + _digest([current["run_id"], backend_id])[:32],
        "effect_key": f"workflow-backend:{backend_id}:{current['run_id']}",
        "request_sha256": request_sha256,
        "operation": operation,
    }


def _validate_dispatch_receipt(
    receipt: Any, *, run: dict[str, Any], backend_id: str
) -> dict[str, Any]:
    if not isinstance(receipt, dict) or set(receipt) != _DISPATCH_RECEIPT_FIELDS:
        raise DurableWorkflowError("State MCP dispatch receipt fields are invalid")
    normalized = copy.deepcopy(receipt)
    if normalized["schema_version"] != _DISPATCH_RECEIPT_SCHEMA_VERSION:
        raise DurableWorkflowError("State MCP dispatch receipt version is invalid")
    for field in (
        "request_id",
        "project_id",
        "work_id",
        "claim_id",
        "effect_id",
        "effect_key",
        "operation",
    ):
        _identifier(normalized[field], f"State MCP {field}")
    for field in ("request_sha256", "receipt_sha256"):
        _sha256(normalized[field], f"State MCP {field}")
    for field in (
        "project_revision",
        "claim_revision",
        "lease_epoch",
        "fence",
    ):
        if type(normalized[field]) is not int or normalized[field] <= 0:
            raise DurableWorkflowError(f"State MCP {field} is invalid")
    if normalized["fence"] != normalized["lease_epoch"]:
        raise DurableWorkflowError("State MCP dispatch fence is invalid")
    scope = normalized["scope_ref"]
    if scope != {"scope_kind": "capability", "scope_ref": "workflow-backend"}:
        raise DurableWorkflowError("State MCP dispatch scope is invalid")
    _timestamp(normalized["dispatch_started_at"], "State MCP dispatch_started_at")
    expected_sha256 = _digest(
        {key: value for key, value in normalized.items() if key != "receipt_sha256"}
    )
    if normalized["receipt_sha256"] != expected_sha256:
        raise DurableWorkflowError("State MCP dispatch receipt hash mismatch")
    current = validate_workflow_run(run)
    intent = workflow_backend_binding_intent(current, backend_id=backend_id)
    expected = {
        "project_id": current["project_id"],
        "project_revision": current["authority"]["project_revision"] + 1,
        "work_id": current["root_work_id"],
        **intent,
    }
    if any(normalized[field] != value for field, value in expected.items()):
        raise DurableWorkflowError("State MCP dispatch receipt does not match workflow")
    return normalized


def _validate_state_mcp_dispatch_receipt(
    result: Any, *, run: dict[str, Any], backend_id: str
) -> dict[str, Any]:
    if not isinstance(result, dict) or set(result) != _STATE_MCP_RESULT_FIELDS:
        raise DurableWorkflowError("State MCP dispatch result fields are invalid")
    normalized_result = copy.deepcopy(result)
    if (
        normalized_result["schema_version"] != _DISPATCH_RECEIPT_SCHEMA_VERSION
        or normalized_result["operation"] != "start-effect-dispatch"
        or normalized_result["status"] != "accepted"
    ):
        raise DurableWorkflowError("State MCP dispatch result is not committed")
    current = validate_workflow_run(run)
    normalized = _validate_dispatch_receipt(
        normalized_result["receipt"], run=current, backend_id=backend_id
    )
    if normalized_result["project_revision"] != normalized["project_revision"]:
        raise DurableWorkflowError("State MCP dispatch result revision mismatch")
    effect = normalized_result["effect"]
    if not isinstance(effect, dict):
        raise DurableWorkflowError("State MCP dispatch effect is invalid")
    effect_expected = {
        "effect_id": normalized["effect_id"],
        "effect_key": normalized["effect_key"],
        "work_id": normalized["work_id"],
        "claim_id": normalized["claim_id"],
        "status": "started",
        "operation": normalized["operation"],
        "scope_ref": normalized["scope_ref"],
        "expected_project_revision": normalized["project_revision"],
        "request_sha256": normalized["request_sha256"],
        "lease_epoch": normalized["lease_epoch"],
        "dispatch_receipt_sha256": normalized["receipt_sha256"],
        "dispatch_started_at": normalized["dispatch_started_at"],
    }
    if any(effect.get(field) != value for field, value in effect_expected.items()):
        raise DurableWorkflowError("State MCP dispatch effect does not match receipt")
    transition = normalized_result["transition"]
    if not isinstance(transition, dict) or set(transition) != _TRANSITION_FIELDS:
        raise DurableWorkflowError("State MCP dispatch transition fields are invalid")
    if (
        transition["schema_version"] != "context.work-claim-transition/v1alpha1"
        or transition["operation"] != "start-effect-dispatch"
        or transition["project_id"] != current["project_id"]
        or transition["project_revision_before"]
        != current["authority"]["project_revision"]
        or transition["project_revision_after"] != normalized["project_revision"]
        or transition["observed_at"] != normalized["dispatch_started_at"]
    ):
        raise DurableWorkflowError(
            "State MCP dispatch transition does not match receipt"
        )
    _identifier(transition["actor_ref"], "State MCP transition actor_ref")
    _identifier(transition["transition_id"], "State MCP transition_id")
    if transition["previous_transition_sha256"] is not None:
        _sha256(
            transition["previous_transition_sha256"],
            "State MCP previous_transition_sha256",
        )
    _sha256(transition["transition_sha256"], "State MCP transition_sha256")
    if transition["transition_sha256"] != _digest(
        {
            key: value
            for key, value in transition.items()
            if key not in {"transition_id", "transition_sha256"}
        }
    ):
        raise DurableWorkflowError("State MCP dispatch transition hash mismatch")
    changes = transition["changes"]
    if not isinstance(changes, dict) or changes.get("effect_after") != effect:
        raise DurableWorkflowError("State MCP dispatch transition effect mismatch")
    return normalized_result


def build_workflow_backend_binding(
    run: dict[str, Any],
    *,
    backend_id: str,
    state_mcp_result: dict[str, Any],
    state_mcp_resolver: Callable[[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Compose a binding proof from a complete State MCP dispatch receipt."""
    current = validate_workflow_run(run)
    _identifier(backend_id, "backend_id")
    state_result = _validate_state_mcp_dispatch_receipt(
        state_mcp_result, run=current, backend_id=backend_id
    )
    state_receipt = state_result["receipt"]
    if not callable(state_mcp_resolver):
        raise DurableWorkflowError("trusted State MCP resolver is required")
    try:
        resolved_result = state_mcp_resolver(
            current["project_id"], state_receipt["request_id"]
        )
    except Exception as exc:
        raise DurableWorkflowError("State MCP committed result is unavailable") from exc
    if resolved_result != state_result:
        raise DurableWorkflowError(
            "State MCP committed result does not match authority"
        )
    state_mcp_receipt_sha256 = state_receipt["receipt_sha256"]
    receipt = {
        "schema_version": _BINDING_SCHEMA_VERSION,
        "binding_id": "wfb_"
        + _digest(
            [
                current["workflow_id"],
                current["run_id"],
                backend_id,
                state_mcp_receipt_sha256,
            ]
        )[:32],
        "backend_id": backend_id,
        "workflow_id": current["workflow_id"],
        "chain_id": current["chain_id"],
        "run_id": current["run_id"],
        "run_sha256": current["run_sha256"],
        "authority_project_revision": current["authority"]["project_revision"],
        "authority_event_head_sha256": current["authority"]["event_head"][
            "event_sha256"
        ],
        "committed_project_revision": state_receipt["project_revision"],
        "state_mcp_receipt": state_receipt,
        "state_mcp_receipt_sha256": state_mcp_receipt_sha256,
        "status": "committed",
        "committed_at": state_receipt["dispatch_started_at"],
        "state_write_authority": False,
        "receipt_sha256": "",
    }
    receipt["receipt_sha256"] = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    return receipt


def _validate_backend_binding(
    receipt: Any, *, run: dict[str, Any], backend_id: str
) -> dict[str, Any]:
    fields = {
        "schema_version",
        "binding_id",
        "backend_id",
        "workflow_id",
        "chain_id",
        "run_id",
        "run_sha256",
        "authority_project_revision",
        "authority_event_head_sha256",
        "committed_project_revision",
        "state_mcp_receipt",
        "state_mcp_receipt_sha256",
        "status",
        "committed_at",
        "state_write_authority",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise DurableWorkflowError("workflow backend binding fields are invalid")
    if receipt["schema_version"] != _BINDING_SCHEMA_VERSION:
        raise DurableWorkflowError("workflow backend binding version is invalid")
    for field in ("binding_id", "backend_id", "workflow_id", "chain_id", "run_id"):
        _identifier(receipt[field], field)
    for field in (
        "run_sha256",
        "authority_event_head_sha256",
        "state_mcp_receipt_sha256",
        "receipt_sha256",
    ):
        _sha256(receipt[field], field)
    if (
        type(receipt["authority_project_revision"]) is not int
        or receipt["authority_project_revision"] < 0
        or type(receipt["committed_project_revision"]) is not int
        or receipt["committed_project_revision"] <= 0
    ):
        raise DurableWorkflowError("workflow backend binding revision is invalid")
    if receipt["status"] != "committed":
        raise DurableWorkflowError("workflow backend binding is not committed")
    _timestamp(receipt["committed_at"], "committed_at")
    if receipt["state_write_authority"] is not False:
        raise DurableWorkflowError("workflow backend binding cannot claim authority")
    current = validate_workflow_run(run)
    expected = {
        "backend_id": backend_id,
        "workflow_id": current["workflow_id"],
        "chain_id": current["chain_id"],
        "run_id": current["run_id"],
        "run_sha256": current["run_sha256"],
        "authority_project_revision": current["authority"]["project_revision"],
        "authority_event_head_sha256": current["authority"]["event_head"][
            "event_sha256"
        ],
        "committed_project_revision": current["authority"]["project_revision"] + 1,
    }
    if any(receipt[field] != value for field, value in expected.items()):
        raise DurableWorkflowError("workflow backend binding does not match run")
    state_receipt = _validate_dispatch_receipt(
        receipt["state_mcp_receipt"], run=current, backend_id=backend_id
    )
    if state_receipt["receipt_sha256"] != receipt["state_mcp_receipt_sha256"]:
        raise DurableWorkflowError(
            "workflow backend binding State MCP receipt mismatch"
        )
    expected_hash = _digest(
        {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    )
    if receipt["receipt_sha256"] != expected_hash:
        raise DurableWorkflowError("workflow backend binding hash mismatch")
    return copy.deepcopy(receipt)


class TemporalWorkflowAdapter:
    """Start Temporal workflows without making the SDK a core dependency."""

    def __init__(
        self,
        *,
        client: Any,
        namespace: str,
        task_queue: str,
        workflow_name: str,
        adapter_version: str,
        implementation_sha256: str,
        worker_deployment: str,
        worker_build_id: str,
        sdk_loader: Callable[[], object] | None = None,
        state_mcp_resolver: Callable[[str, str], dict[str, Any]] | None = None,
    ) -> None:
        if not callable(getattr(client, "start_workflow", None)):
            raise TypeError("Temporal client must provide start_workflow")
        for field, value in (
            ("namespace", namespace),
            ("task_queue", task_queue),
            ("workflow_name", workflow_name),
            ("adapter_version", adapter_version),
            ("worker_deployment", worker_deployment),
            ("worker_build_id", worker_build_id),
        ):
            _identifier(value, field)
        _sha256(implementation_sha256, "implementation_sha256")
        if getattr(client, "namespace", None) != namespace:
            raise DurableWorkflowError("Temporal client namespace mismatch")
        try:
            sdk = (sdk_loader or _load_temporal)()
        except (ImportError, ModuleNotFoundError) as exc:
            raise WorkflowRuntimeUnavailable(
                "sdk_unavailable", remote_execution_possible=False
            ) from exc
        sdk_version = getattr(sdk, "__version__", None)
        common = getattr(sdk, "common", None)
        conflict_policy = getattr(
            getattr(common, "WorkflowIDConflictPolicy", None), "USE_EXISTING", None
        )
        reuse_policy = getattr(
            getattr(common, "WorkflowIDReusePolicy", None), "REJECT_DUPLICATE", None
        )
        if not isinstance(sdk_version, str) or not sdk_version:
            raise WorkflowRuntimeUnavailable(
                "sdk_version_unavailable", remote_execution_possible=False
            )
        if conflict_policy is None or reuse_policy is None:
            raise WorkflowRuntimeUnavailable(
                "sdk_conflict_policy_unavailable", remote_execution_possible=False
            )
        self.client = client
        self.namespace = namespace
        self.task_queue = task_queue
        self.workflow_name = workflow_name
        self.state_mcp_resolver = state_mcp_resolver
        self._id_conflict_policy = conflict_policy
        self._id_reuse_policy = reuse_policy
        self.capability_manifest = {
            "schema_version": "context.workflow-adapter-manifest/v1alpha1",
            "backend_id": "temporal",
            "adapter_version": adapter_version,
            "implementation_sha256": implementation_sha256,
            "sdk_version": sdk_version,
            "namespace": namespace,
            "task_queue": task_queue,
            "workflow_name": workflow_name,
            "execution_scope": "distributed",
            "distributed_execution": True,
            "requires_sdk": True,
            "requires_service": True,
            "history_replay": True,
            "patching": True,
            "patch_contract": "temporal.workflow.patched",
            "worker_versioning_supported": True,
            "worker_versioning_configured": False,
            "worker_deployment": worker_deployment,
            "worker_build_id": worker_build_id,
            "continue_as_new": True,
            "state_write_authority": False,
            "claim_authority": False,
            "effect_authority": False,
            "effect_dispatch_authority": False,
            "provider_native_authority": False,
        }

    async def start(
        self,
        run: dict[str, Any],
        *,
        request_id: str,
        request_sha256: str,
        binding_receipt: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Start once after State MCP has committed the backend binding."""
        current = validate_workflow_run(run)
        _identifier(request_id, "request_id")
        _sha256(request_sha256, "request_sha256")
        if current["generation"] != 1:
            raise DurableWorkflowError(
                "Temporal direct start only accepts the first generation"
            )
        if (
            current["implementation_sha256"]
            != self.capability_manifest["implementation_sha256"]
        ):
            raise DurableWorkflowError("workflow implementation does not match worker")
        binding = _validate_backend_binding(
            binding_receipt, run=current, backend_id="temporal"
        )
        if not callable(self.state_mcp_resolver):
            raise DurableWorkflowError("trusted State MCP resolver is required")
        state_receipt = binding["state_mcp_receipt"]
        try:
            resolved_result = self.state_mcp_resolver(
                current["project_id"], state_receipt["request_id"]
            )
        except Exception as exc:
            raise DurableWorkflowError(
                "State MCP committed result is unavailable"
            ) from exc
        resolved = _validate_state_mcp_dispatch_receipt(
            resolved_result, run=current, backend_id="temporal"
        )
        if resolved["receipt"] != state_receipt:
            raise DurableWorkflowError(
                "workflow backend binding does not match State MCP authority"
            )
        payload = bind_temporal_payload_receipt(
            _bounded_payload(current),
            receipt_field="backend_binding_receipt_sha256",
            receipt_sha256=binding["receipt_sha256"],
        )
        try:
            handle = await self.client.start_workflow(
                self.workflow_name,
                payload,
                id=current["workflow_id"],
                task_queue=self.task_queue,
                id_conflict_policy=self._id_conflict_policy,
                id_reuse_policy=self._id_reuse_policy,
            )
        except Exception as exc:
            raise WorkflowRuntimeUnavailable(
                "start_outcome_unknown", remote_execution_possible=True
            ) from exc
        provider_workflow_id = getattr(handle, "id", None)
        provider_run_id = getattr(handle, "result_run_id", None)
        first_execution_run_id = getattr(handle, "first_execution_run_id", None)
        if provider_workflow_id != current["workflow_id"]:
            raise WorkflowRuntimeUnavailable(
                "provider_identity_mismatch", remote_execution_possible=True
            )
        for field, value in (
            ("provider_run_id", provider_run_id),
            ("first_execution_run_id", first_execution_run_id),
        ):
            try:
                _identifier(value, field)
            except DurableWorkflowError as exc:
                raise WorkflowRuntimeUnavailable(
                    "provider_identity_unavailable", remote_execution_possible=True
                ) from exc
        receipt = {
            "schema_version": _RUN_RECEIPT_SCHEMA_VERSION,
            "request_id": request_id,
            "request_sha256": request_sha256,
            "adapter_id": "temporal",
            "adapter_version": self.capability_manifest["adapter_version"],
            "sdk_version": self.capability_manifest["sdk_version"],
            "namespace": self.namespace,
            "task_queue": self.task_queue,
            "workflow_id": current["workflow_id"],
            "chain_id": current["chain_id"],
            "core_run_id": current["run_id"],
            "provider_run_id": provider_run_id,
            "first_execution_run_id": first_execution_run_id,
            "generation": current["generation"],
            "definition_version": current["definition_version"],
            "implementation_sha256": current["implementation_sha256"],
            "worker_deployment": self.capability_manifest["worker_deployment"],
            "worker_build_id": self.capability_manifest["worker_build_id"],
            "worker_versioning_configured": self.capability_manifest[
                "worker_versioning_configured"
            ],
            "active_patch_ids": copy.deepcopy(current["active_patch_ids"]),
            "run_sha256": current["run_sha256"],
            "backend_binding_receipt_sha256": binding["receipt_sha256"],
            "backend_binding_committed": True,
            "state_write_authority": False,
            "effect_dispatch_authority": False,
            "provider_native_authority": False,
            "receipt_sha256": "",
        }
        receipt["receipt_sha256"] = _digest(
            {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        )
        return validate_workflow_run_receipt(
            receipt,
            run=current,
            binding_receipt=binding,
            request_id=request_id,
            request_sha256=request_sha256,
            adapter_manifest=self.capability_manifest,
            provider_run_id=provider_run_id,
            first_execution_run_id=first_execution_run_id,
        )


def validate_workflow_run_receipt(
    receipt: Any,
    *,
    run: dict[str, Any],
    binding_receipt: dict[str, Any],
    request_id: str,
    request_sha256: str,
    adapter_manifest: dict[str, Any],
    provider_run_id: str,
    first_execution_run_id: str,
) -> dict[str, Any]:
    """Validate the adapter receipt against the core run and State binding."""
    fields = {
        "schema_version",
        "request_id",
        "request_sha256",
        "adapter_id",
        "adapter_version",
        "sdk_version",
        "namespace",
        "task_queue",
        "workflow_id",
        "chain_id",
        "core_run_id",
        "provider_run_id",
        "first_execution_run_id",
        "generation",
        "definition_version",
        "implementation_sha256",
        "worker_deployment",
        "worker_build_id",
        "worker_versioning_configured",
        "active_patch_ids",
        "run_sha256",
        "backend_binding_receipt_sha256",
        "backend_binding_committed",
        "state_write_authority",
        "effect_dispatch_authority",
        "provider_native_authority",
        "receipt_sha256",
    }
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise DurableWorkflowError("workflow run receipt fields are invalid")
    normalized = copy.deepcopy(receipt)
    if normalized["schema_version"] != _RUN_RECEIPT_SCHEMA_VERSION:
        raise DurableWorkflowError("workflow run receipt version is invalid")
    for field in (
        "request_id",
        "adapter_id",
        "adapter_version",
        "sdk_version",
        "namespace",
        "task_queue",
        "workflow_id",
        "chain_id",
        "core_run_id",
        "provider_run_id",
        "first_execution_run_id",
        "worker_deployment",
        "worker_build_id",
    ):
        _identifier(normalized[field], field)
    for field in (
        "request_sha256",
        "implementation_sha256",
        "run_sha256",
        "backend_binding_receipt_sha256",
        "receipt_sha256",
    ):
        _sha256(normalized[field], field)
    if type(normalized["generation"]) is not int or normalized["generation"] <= 0:
        raise DurableWorkflowError("workflow run receipt generation is invalid")
    if (
        type(normalized["definition_version"]) is not int
        or normalized["definition_version"] <= 0
    ):
        raise DurableWorkflowError("workflow run receipt definition is invalid")
    if type(normalized["worker_versioning_configured"]) is not bool:
        raise DurableWorkflowError("workflow run receipt worker versioning is invalid")
    if normalized["active_patch_ids"] != sorted(set(normalized["active_patch_ids"])):
        raise DurableWorkflowError("workflow run receipt patch set is invalid")
    if normalized["backend_binding_committed"] is not True:
        raise DurableWorkflowError("workflow run receipt backend binding is absent")
    for field in (
        "state_write_authority",
        "effect_dispatch_authority",
        "provider_native_authority",
    ):
        if normalized[field] is not False:
            raise DurableWorkflowError("workflow run receipt cannot claim authority")
    if normalized["receipt_sha256"] != _digest(
        {key: value for key, value in normalized.items() if key != "receipt_sha256"}
    ):
        raise DurableWorkflowError("workflow run receipt hash mismatch")
    current = validate_workflow_run(run)
    _identifier(request_id, "expected request_id")
    _sha256(request_sha256, "expected request_sha256")
    _identifier(provider_run_id, "expected provider_run_id")
    _identifier(first_execution_run_id, "expected first_execution_run_id")
    if (
        normalized["request_id"] != request_id
        or normalized["request_sha256"] != request_sha256
    ):
        raise DurableWorkflowError("workflow run receipt does not match request")
    required_manifest = {
        "backend_id",
        "adapter_version",
        "sdk_version",
        "namespace",
        "task_queue",
        "implementation_sha256",
        "worker_deployment",
        "worker_build_id",
        "worker_versioning_configured",
    }
    if not isinstance(adapter_manifest, dict) or not required_manifest.issubset(
        adapter_manifest
    ):
        raise DurableWorkflowError("workflow adapter manifest is invalid")
    binding = _validate_backend_binding(
        binding_receipt, run=current, backend_id=adapter_manifest["backend_id"]
    )
    expected = {
        "adapter_id": adapter_manifest["backend_id"],
        "adapter_version": adapter_manifest["adapter_version"],
        "sdk_version": adapter_manifest["sdk_version"],
        "namespace": adapter_manifest["namespace"],
        "task_queue": adapter_manifest["task_queue"],
        "workflow_id": current["workflow_id"],
        "chain_id": current["chain_id"],
        "core_run_id": current["run_id"],
        "generation": current["generation"],
        "definition_version": current["definition_version"],
        "implementation_sha256": adapter_manifest["implementation_sha256"],
        "worker_deployment": adapter_manifest["worker_deployment"],
        "worker_build_id": adapter_manifest["worker_build_id"],
        "worker_versioning_configured": adapter_manifest[
            "worker_versioning_configured"
        ],
        "provider_run_id": provider_run_id,
        "first_execution_run_id": first_execution_run_id,
        "active_patch_ids": current["active_patch_ids"],
        "run_sha256": current["run_sha256"],
        "backend_binding_receipt_sha256": binding["receipt_sha256"],
    }
    if any(normalized[field] != value for field, value in expected.items()):
        raise DurableWorkflowError("workflow run receipt does not match run")
    return normalized


def resolve_temporal_patch(
    run: dict[str, Any],
    *,
    definition: dict[str, Any],
    patch_id: str,
    workflow_api: Any,
) -> bool:
    """Bind Temporal's replay marker decision to the core patch projection."""
    current = validate_workflow_run(run)
    current_definition = validate_workflow_definition(definition)
    _identifier(patch_id, "patch_id")
    patch = next(
        (
            item
            for item in current_definition["patches"]
            if item["patch_id"] == patch_id
        ),
        None,
    )
    if patch is None:
        raise DurableWorkflowError("Temporal patch is absent from workflow definition")
    deprecated = patch["deprecated_in_version"]
    if deprecated is not None and current["definition_version"] >= deprecated:
        deprecate_patch = getattr(workflow_api, "deprecate_patch", None)
        if not callable(deprecate_patch):
            raise TypeError("Temporal workflow API must provide deprecate_patch")
        deprecate_patch(patch_id)
        if patch_id in current["active_patch_ids"]:
            raise DurableWorkflowError(
                "deprecated Temporal patch retained a core marker"
            )
        return True
    patched = getattr(workflow_api, "patched", None)
    if not callable(patched):
        raise TypeError("Temporal workflow API must provide patched")
    provider_decision = patched(patch_id)
    if type(provider_decision) is not bool:
        raise DurableWorkflowError("Temporal patch decision is invalid")
    core_decision = patch_id in current["active_patch_ids"]
    if current["definition_version"] < patch["introduced_in_version"] and core_decision:
        raise DurableWorkflowError("Temporal patch was used before introduced version")
    if provider_decision != core_decision:
        raise DurableWorkflowError("Temporal patch decision does not match core marker")
    return provider_decision


def invoke_temporal_continue_as_new(
    receipt: dict[str, Any],
    *,
    closed_run: dict[str, Any],
    next_run: dict[str, Any],
    workflow_api: Any,
) -> None:
    """Invoke Continue-As-New only from a validated safe-point receipt."""
    current_run = validate_workflow_run(next_run)
    current_receipt = validate_workflow_rollover(
        receipt,
        closed_run=closed_run,
        next_run=current_run,
    )
    continue_as_new = getattr(workflow_api, "continue_as_new", None)
    if not callable(continue_as_new):
        raise TypeError("Temporal workflow API must provide continue_as_new")
    payload = bind_temporal_payload_receipt(
        _bounded_payload(current_run),
        receipt_field="rollover_receipt_sha256",
        receipt_sha256=current_receipt["receipt_sha256"],
    )
    continue_as_new(payload)
    raise DurableWorkflowError("Temporal continue_as_new returned unexpectedly")
