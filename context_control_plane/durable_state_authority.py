"""State MCP authority adapter and receipts for local durable operations."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, ClassVar

from .durable_operation import validate_durable_operation
from .state_mcp import (
    EFFECT_REQUEST_SCHEMA_VERSION_V2,
    EFFECT_TOOL,
    READ_TOOL,
    RequestContext,
    StateMCPService,
)

RECEIPT_SCHEMA_VERSION = "context.durable-state-receipt/v1alpha1"
_RECEIPT_FIELDS = {
    "schema_version",
    "operation_id",
    "action",
    "project_id",
    "work_id",
    "claim_id",
    "effect_id",
    "effect_key",
    "request_id",
    "request_sha256",
    "effect_status",
    "revision",
    "event_head",
    "result_ref",
    "registry_digest",
    "state_response_sha256",
    "reconciled",
    "state_write_authority",
    "provider_native_authority",
    "receipt_sha256",
}
_EVENT_HEAD_FIELDS = {"sequence_no", "event_sha256"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ADAPTER_MANIFEST_FIELDS = {
    "schema_version",
    "adapter_id",
    "adapter_version",
    "authority_interface",
    "receipt_schema_version",
    "source_ref",
    "state_write_authority",
    "provider_native_authority",
}


class DurableStateAuthorityError(RuntimeError):
    """Raised when State MCP cannot prove an operation authority transition."""


def validate_durable_authority_adapter_manifest(manifest: Any) -> dict[str, Any]:
    """Validate one authority adapter identity without granting State authority."""
    if not isinstance(manifest, dict) or set(manifest) != _ADAPTER_MANIFEST_FIELDS:
        raise DurableStateAuthorityError("durable authority adapter manifest is invalid")
    normalized = copy.deepcopy(manifest)
    if normalized["schema_version"] != "context.durable-authority-adapter/v1alpha1":
        raise DurableStateAuthorityError("durable authority adapter version is invalid")
    for field in ("adapter_id", "adapter_version"):
        value = normalized[field]
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            raise DurableStateAuthorityError(
                f"durable authority adapter {field} is invalid"
            )
    if normalized["authority_interface"] != "state-mcp":
        raise DurableStateAuthorityError("durable authority interface must be State MCP")
    if normalized["receipt_schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise DurableStateAuthorityError("durable authority receipt version is invalid")
    _reference(normalized["source_ref"], "durable authority adapter source_ref")
    if normalized["state_write_authority"] is not False:
        raise DurableStateAuthorityError(
            "durable authority adapter cannot claim State authority"
        )
    if normalized["provider_native_authority"] is not False:
        raise DurableStateAuthorityError(
            "durable authority adapter cannot claim provider authority"
        )
    return normalized


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
        raise DurableStateAuthorityError("State authority data is not canonical JSON") from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _receipt_digest(receipt: dict[str, Any]) -> str:
    body = copy.deepcopy(receipt)
    body.pop("receipt_sha256", None)
    return _digest(body)


def _reference(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(character in value for character in "\r\n\x00")
    ):
        raise DurableStateAuthorityError(f"{field} is invalid")
    return value


def validate_durable_state_receipt(
    receipt: Any,
    *,
    operation: dict[str, Any] | None = None,
    expected_action: str | None = None,
) -> dict[str, Any]:
    """Validate one content-addressed State MCP authority receipt."""
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        raise DurableStateAuthorityError("durable State receipt fields are invalid")
    normalized = copy.deepcopy(receipt)
    if normalized["schema_version"] != RECEIPT_SCHEMA_VERSION:
        raise DurableStateAuthorityError("durable State receipt version is invalid")
    for field in (
        "operation_id",
        "project_id",
        "work_id",
        "claim_id",
        "effect_id",
        "effect_key",
        "request_id",
    ):
        value = normalized[field]
        if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
            raise DurableStateAuthorityError(f"durable State receipt {field} is invalid")
    if normalized["action"] not in {"authorize", "complete"}:
        raise DurableStateAuthorityError("durable State receipt action is invalid")
    if expected_action is not None and normalized["action"] != expected_action:
        raise DurableStateAuthorityError("durable State receipt action does not match")
    for field in (
        "request_sha256",
        "registry_digest",
        "state_response_sha256",
        "receipt_sha256",
    ):
        value = normalized[field]
        if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
            raise DurableStateAuthorityError(f"durable State receipt {field} is invalid")
    if type(normalized["revision"]) is not int or normalized["revision"] < 0:
        raise DurableStateAuthorityError("durable State receipt revision is invalid")
    head = normalized["event_head"]
    if not isinstance(head, dict) or set(head) != _EVENT_HEAD_FIELDS:
        raise DurableStateAuthorityError("durable State receipt event head is invalid")
    if type(head["sequence_no"]) is not int or head["sequence_no"] <= 0:
        raise DurableStateAuthorityError("durable State receipt event sequence is invalid")
    if not isinstance(head["event_sha256"], str) or _SHA_RE.fullmatch(
        head["event_sha256"]
    ) is None:
        raise DurableStateAuthorityError("durable State receipt event hash is invalid")
    if normalized["effect_status"] not in {"authorized", "started", "succeeded"}:
        raise DurableStateAuthorityError("durable State receipt effect_status is invalid")
    _reference(normalized["result_ref"], "durable State receipt result_ref", optional=True)
    if normalized["effect_status"] == "succeeded":
        if normalized["result_ref"] is None:
            raise DurableStateAuthorityError("succeeded receipt requires a result_ref")
    elif normalized["result_ref"] is not None:
        raise DurableStateAuthorityError("pending receipt cannot contain a result_ref")
    if normalized["action"] == "complete" and normalized["effect_status"] != "succeeded":
        raise DurableStateAuthorityError("complete receipt requires a succeeded effect")
    if type(normalized["reconciled"]) is not bool:
        raise DurableStateAuthorityError("durable State receipt reconciled is invalid")
    if normalized["state_write_authority"] is not False:
        raise DurableStateAuthorityError("durable State receipt cannot grant State authority")
    if normalized["provider_native_authority"] is not False:
        raise DurableStateAuthorityError("durable State receipt cannot grant provider authority")
    if normalized["receipt_sha256"] != _receipt_digest(normalized):
        raise DurableStateAuthorityError("durable State receipt digest mismatch")
    if operation is not None:
        current = validate_durable_operation(copy.deepcopy(operation))
        effect = current["effect"]
        expected = {
            "operation_id": current["operation_id"],
            "project_id": current["project_id"],
            "work_id": current["work_id"],
            "claim_id": current["claim_id"],
            "effect_id": effect["effect_id"],
            "effect_key": effect["effect_key"],
        }
        for field, value in expected.items():
            if normalized[field] != value:
                raise DurableStateAuthorityError(
                    f"durable State receipt {field} does not match operation"
                )
        if normalized["action"] == "complete" and (
            normalized["result_ref"] != current["result_ref"]
        ):
            raise DurableStateAuthorityError(
                "durable State receipt result does not match operation"
            )
    return normalized


def compose_durable_state_receipt(
    operation: dict[str, Any],
    *,
    action: str,
    request_id: str,
    request_sha256: str,
    revision: int,
    event_head: dict[str, Any],
    result_ref: str | None,
    registry_digest: str,
    state_response_sha256: str,
    reconciled: bool,
    effect_status: str | None = None,
) -> dict[str, Any]:
    """Compose one validated receipt from a State MCP adapter response."""
    current = validate_durable_operation(copy.deepcopy(operation))
    receipt = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "operation_id": current["operation_id"],
        "action": action,
        "project_id": current["project_id"],
        "work_id": current["work_id"],
        "claim_id": current["claim_id"],
        "effect_id": current["effect"]["effect_id"],
        "effect_key": current["effect"]["effect_key"],
        "request_id": request_id,
        "request_sha256": request_sha256,
        "effect_status": effect_status
        or ("succeeded" if action == "complete" else "authorized"),
        "revision": revision,
        "event_head": copy.deepcopy(event_head),
        "result_ref": result_ref,
        "registry_digest": registry_digest,
        "state_response_sha256": state_response_sha256,
        "reconciled": reconciled,
        "state_write_authority": False,
        "provider_native_authority": False,
        "receipt_sha256": "0" * 64,
    }
    receipt["receipt_sha256"] = _receipt_digest(receipt)
    return validate_durable_state_receipt(
        receipt, operation=current, expected_action=action
    )


def durable_state_receipt_ref(receipt: dict[str, Any]) -> str:
    normalized = validate_durable_state_receipt(receipt)
    return "state-receipt://sha256/" + normalized["receipt_sha256"]


class StateMCPDurableAuthorityAdapter:
    """Authorize and complete durable effects exclusively through State MCP."""

    capability_manifest: ClassVar[dict[str, Any]] = {
        "schema_version": "context.durable-authority-adapter/v1alpha1",
        "adapter_id": "context.state-mcp/v2",
        "adapter_version": "1.1.0-alpha.1",
        "authority_interface": "state-mcp",
        "receipt_schema_version": RECEIPT_SCHEMA_VERSION,
        "source_ref": "component://context.state-mcp",
        "state_write_authority": False,
        "provider_native_authority": False,
    }

    def __init__(self, service: StateMCPService, *, context: RequestContext) -> None:
        if not isinstance(service, StateMCPService):
            raise TypeError("service must be a StateMCPService")
        if not isinstance(context, RequestContext):
            raise TypeError("context must be a RequestContext")
        self._service = service
        self._context = context

    @staticmethod
    def _request_id(operation: dict[str, Any], action: str) -> str:
        digest = hashlib.sha256(
            f"{operation['operation_id']}:{action}".encode()
        ).hexdigest()
        return f"durable-{action}-{digest[:32]}"

    def _read(self, operation: dict[str, Any], action: str) -> dict[str, Any]:
        request_id = self._request_id(operation, f"read-{action}")
        response = self._service.call_tool(
            READ_TOOL,
            {
                "schema_version": "context.state-mcp-request/v1alpha1",
                "request_id": request_id,
                "project_id": operation["project_id"],
            },
            context=self._context,
        )
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise DurableStateAuthorityError("State MCP read failed during reconciliation")
        return response

    @staticmethod
    def _matching_effect(
        operation: dict[str, Any], response: dict[str, Any], *, action: str
    ) -> dict[str, Any]:
        try:
            result = response["result"]
            snapshot = result["snapshot"]
            revision = result["revision"]
            event_head = result["event_head"]
            registry_digest = result["registry_digest"]
        except (KeyError, TypeError) as exc:
            raise DurableStateAuthorityError("State MCP response is incomplete") from exc
        if (
            snapshot["project"]["project_id"] != operation["project_id"]
            or snapshot["project"]["revision"] != revision
            or not isinstance(event_head, dict)
            or not isinstance(registry_digest, str)
            or _SHA_RE.fullmatch(registry_digest) is None
        ):
            raise DurableStateAuthorityError("State MCP response authority is inconsistent")
        effect = next(
            (
                item
                for item in snapshot["effects"]
                if item["effect_id"] == operation["effect"]["effect_id"]
            ),
            None,
        )
        if effect is None:
            raise DurableStateAuthorityError("State MCP effect is absent")
        expected = {
            "effect_key": operation["effect"]["effect_key"],
            "work_id": operation["work_id"],
            "claim_id": operation["claim_id"],
            "operation": operation["effect"]["operation"],
            "scope_ref": operation["effect"]["scope_ref"],
            "request_sha256": operation["effect"]["request_sha256"],
        }
        for field, value in expected.items():
            if effect.get(field) != value:
                raise DurableStateAuthorityError(
                    f"State MCP effect {field} does not match operation"
                )
        if action == "authorize":
            if effect["status"] not in {"authorized", "started", "succeeded"}:
                raise DurableStateAuthorityError("State MCP effect is not authorized")
        elif effect["status"] != "succeeded" or effect["result_ref"] != operation["result_ref"]:
            raise DurableStateAuthorityError("State MCP effect completion does not match")
        return effect

    def _receipt(
        self,
        operation: dict[str, Any],
        *,
        action: str,
        request: dict[str, Any],
        response: dict[str, Any],
        reconciled: bool,
    ) -> dict[str, Any]:
        effect = self._matching_effect(operation, response, action=action)
        result = response["result"]
        return compose_durable_state_receipt(
            operation,
            action=action,
            request_id=request["request_id"],
            request_sha256=_digest(request),
            revision=result["revision"],
            event_head=result["event_head"],
            effect_status=effect["status"],
            result_ref=effect["result_ref"],
            registry_digest=result["registry_digest"],
            state_response_sha256=_digest(response),
            reconciled=reconciled,
        )

    def _request(
        self,
        operation: dict[str, Any],
        *,
        action: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        effect = operation["effect"]
        return {
            "schema_version": EFFECT_REQUEST_SCHEMA_VERSION_V2,
            "request_id": self._request_id(operation, action),
            "project_id": operation["project_id"],
            "expected_revision": expected_revision,
            "action": action,
            "effect_id": effect["effect_id"],
            "effect_key": effect["effect_key"],
            "work_id": operation["work_id"],
            "claim_id": operation["claim_id"],
            "operation": effect["operation"],
            "scope_ref": copy.deepcopy(effect["scope_ref"]),
            "request_sha256": effect["request_sha256"],
            "result_ref": operation["result_ref"] if action == "complete" else None,
            "evidence_ids": [],
            "causation_ref": f"operation:{operation['operation_id']}",
            "correlation_ref": operation["trace_binding"]["correlation_id"],
        }

    def _commit(self, operation: dict[str, Any], *, action: str) -> dict[str, Any]:
        operation = validate_durable_operation(copy.deepcopy(operation))
        if action == "authorize":
            expected_revision = operation["authority"]["project_revision"]
        else:
            if operation["phase"] != "effect-settled" or operation["result_ref"] is None:
                raise DurableStateAuthorityError(
                    "State completion requires a settled durable operation"
                )
            current = self._read(operation, action)
            try:
                self._matching_effect(operation, current, action=action)
            except DurableStateAuthorityError:
                expected_revision = current["result"]["revision"]
            else:
                request = self._request(
                    operation,
                    action=action,
                    expected_revision=current["result"]["revision"],
                )
                return self._receipt(
                    operation,
                    action=action,
                    request=request,
                    response=current,
                    reconciled=True,
                )
        request = self._request(
            operation, action=action, expected_revision=expected_revision
        )
        response = self._service.call_tool(
            EFFECT_TOOL, request, context=self._context
        )
        if isinstance(response, dict) and response.get("ok") is True:
            return self._receipt(
                operation,
                action=action,
                request=request,
                response=response,
                reconciled=False,
            )
        if not isinstance(response, dict) or response.get("error", {}).get("code") != "conflict":
            raise DurableStateAuthorityError(f"State MCP effect {action} failed")
        reconciled = self._read(operation, action)
        return self._receipt(
            operation,
            action=action,
            request=request,
            response=reconciled,
            reconciled=True,
        )

    def commit_intent(self, operation: dict[str, Any]) -> dict[str, Any]:
        return self._commit(operation, action="authorize")

    def commit_state(self, operation: dict[str, Any]) -> dict[str, Any]:
        return self._commit(operation, action="complete")
