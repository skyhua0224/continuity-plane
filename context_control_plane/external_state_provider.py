"""Read-only external projection boundary for authoritative State MCP data."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
from typing import Any

from .durable_state_migration import (
    V5_SCHEMA_VERSION,
    DurableStateMigrationError,
    canonical_typed_state_migration_bytes,
)
from .shared_state_migration import (
    V6_SCHEMA_VERSION,
    DurableStateV6MigrationError,
    canonical_shared_state_migration_bytes,
)
from .state_mcp import READ_TOOL, RequestContext
from .state_mcp import REQUEST_SCHEMA_VERSION as STATE_REQUEST_SCHEMA_VERSION
from .state_mcp import RESPONSE_SCHEMA_VERSION as STATE_RESPONSE_SCHEMA_VERSION
from .typed_state import TypedStateError, canonical_state_bytes

EXTERNAL_READ_TOOL = "context.external-state.read"
EXTERNAL_REQUEST_SCHEMA_VERSION = "context.external-state-request/v1alpha1"
EXTERNAL_RESPONSE_SCHEMA_VERSION = "context.external-state-response/v1alpha1"
EXTERNAL_PROJECTION_SCHEMA_VERSION = "context.external-state-projection/v1alpha1"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUEST_FIELDS = {
    "schema_version",
    "request_id",
    "project_id",
    "expected_revision",
}
_SOURCE_RESPONSE_FIELDS = {
    "schema_version",
    "request_id",
    "tool",
    "ok",
    "result",
    "error",
}
_SOURCE_RESULT_FIELDS = {
    "snapshot",
    "revision",
    "event_head",
    "registry_digest",
    "capabilities",
}
_PROJECTION_FIELDS = {
    "schema_version",
    "provider_id",
    "project_id",
    "state_revision",
    "source",
    "snapshot",
    "state_sha256",
    "state_write_authority",
    "controlled_action_authority",
    "provider_authority",
    "external_effect_authority",
    "projection_sha256",
    "signature",
}
_PROJECTION_SOURCE_FIELDS = {
    "response_schema_version",
    "tool",
    "revision",
    "event_head",
    "registry_digest",
    "capabilities_sha256",
}


class ExternalStateProjectionError(ValueError):
    """Raised when an external State projection violates its binding."""


class HMACExternalStateProjectionSigner:
    """Sign and verify external projection envelopes with one trusted HMAC key."""

    def __init__(self, *, key_id: str, secret: bytes) -> None:
        self._key_id = _identifier(key_id, "key_id")
        if not isinstance(secret, bytes) or len(secret) < 16:
            raise ValueError("secret must contain at least 16 bytes")
        self._secret = secret

    def sign(self, payload: Any) -> dict[str, str]:
        value = hmac.new(
            self._secret,
            _canonical_bytes(payload),
            hashlib.sha256,
        ).hexdigest()
        return {
            "algorithm": "hmac-sha256",
            "key_id": self._key_id,
            "value": value,
        }

    def verify(self, payload: Any, signature: Any) -> bool:
        if (
            not isinstance(signature, dict)
            or set(signature) != {"algorithm", "key_id", "value"}
            or signature.get("algorithm") != "hmac-sha256"
            or signature.get("key_id") != self._key_id
            or not isinstance(signature.get("value"), str)
            or _SHA256_RE.fullmatch(signature["value"]) is None
        ):
            return False
        expected = self.sign(payload)["value"]
        return hmac.compare_digest(expected, signature["value"])


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a bounded identifier")
    return value


def _response(
    *,
    request_id: str | None,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    ok = error_code is None
    return {
        "schema_version": EXTERNAL_RESPONSE_SCHEMA_VERSION,
        "request_id": request_id,
        "tool": EXTERNAL_READ_TOOL,
        "ok": ok,
        "result": copy.deepcopy(result) if ok else None,
        "error": (
            None
            if ok
            else {
                "code": error_code,
                "message": error_message or "request failed",
            }
        ),
    }


def _request_id(arguments: Any) -> str | None:
    if isinstance(arguments, dict):
        value = arguments.get("request_id")
        if (
            isinstance(value, str)
            and len(value) <= 200
            and _IDENTIFIER_RE.fullmatch(value) is not None
        ):
            return value
    return None


def _validate_request(arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return "arguments must be an object"
    if set(arguments) != _REQUEST_FIELDS:
        return "request fields do not match the external read contract"
    if arguments.get("schema_version") != EXTERNAL_REQUEST_SCHEMA_VERSION:
        return "unsupported external read request schema"
    try:
        _identifier(arguments.get("request_id"), "request_id")
        _identifier(arguments.get("project_id"), "project_id")
    except ValueError as exc:
        return str(exc)
    if len(arguments["request_id"]) > 200 or len(arguments["project_id"]) > 200:
        return "request_id and project_id must not exceed 200 characters"
    expected_revision = arguments.get("expected_revision")
    if expected_revision is not None and (
        type(expected_revision) is not int or expected_revision < 0
    ):
        return "expected_revision must be null or a non-negative integer"
    return None


def _validate_event_head(value: Any) -> bool:
    return value is None or (
        isinstance(value, dict)
        and set(value) == {"sequence_no", "event_sha256"}
        and type(value["sequence_no"]) is int
        and value["sequence_no"] >= 1
        and isinstance(value["event_sha256"], str)
        and _SHA256_RE.fullmatch(value["event_sha256"]) is not None
    )


def _canonical_typed_snapshot(snapshot: Any) -> bytes:
    if not isinstance(snapshot, dict):
        raise ExternalStateProjectionError("typed snapshot must be an object")
    try:
        if snapshot.get("schema_version") == V6_SCHEMA_VERSION:
            return canonical_shared_state_migration_bytes(snapshot)
        if snapshot.get("schema_version") == V5_SCHEMA_VERSION:
            return canonical_typed_state_migration_bytes(snapshot)
        return canonical_state_bytes(snapshot)
    except (
        DurableStateMigrationError,
        DurableStateV6MigrationError,
        TypedStateError,
    ) as exc:
        raise ExternalStateProjectionError("typed snapshot validation failed") from exc


def typed_state_snapshot_sha256(snapshot: Any) -> str:
    """Validate one supported typed snapshot and return its canonical digest."""
    return hashlib.sha256(_canonical_typed_snapshot(snapshot)).hexdigest()


def _validated_source_result(
    response: Any,
    *,
    source_request_id: str,
    project_id: str,
) -> dict[str, Any] | None:
    if not isinstance(response, dict) or set(response) != _SOURCE_RESPONSE_FIELDS:
        return None
    if (
        response.get("schema_version") != STATE_RESPONSE_SCHEMA_VERSION
        or response.get("request_id") != source_request_id
        or response.get("tool") != READ_TOOL
        or response.get("ok") is not True
        or response.get("error") is not None
    ):
        return None
    result = response.get("result")
    if not isinstance(result, dict) or set(result) != _SOURCE_RESULT_FIELDS:
        return None
    snapshot = result.get("snapshot")
    revision = result.get("revision")
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("project"), dict)
        or snapshot["project"].get("project_id") != project_id
        or type(revision) is not int
        or revision < 0
        or snapshot["project"].get("revision") != revision
        or not _validate_event_head(result.get("event_head"))
        or not isinstance(result.get("registry_digest"), str)
        or _SHA256_RE.fullmatch(result["registry_digest"]) is None
        or not isinstance(result.get("capabilities"), dict)
    ):
        return None
    try:
        typed_state_snapshot_sha256(snapshot)
    except ExternalStateProjectionError:
        return None
    return copy.deepcopy(result)


def _validated_source_error(
    response: Any,
    *,
    source_request_id: str,
) -> str | None:
    if not isinstance(response, dict) or set(response) != _SOURCE_RESPONSE_FIELDS:
        return None
    error = response.get("error")
    if (
        response.get("schema_version") != STATE_RESPONSE_SCHEMA_VERSION
        or response.get("request_id") != source_request_id
        or response.get("tool") != READ_TOOL
        or response.get("ok") is not False
        or response.get("result") is not None
        or not isinstance(error, dict)
        or set(error) != {"code", "message"}
        or not isinstance(error.get("code"), str)
        or not error["code"]
        or not isinstance(error.get("message"), str)
        or not error["message"]
    ):
        return None
    return error["code"]


def validate_external_state_projection(
    projection: Any,
    *,
    signer: HMACExternalStateProjectionSigner,
) -> dict[str, Any]:
    """Validate hashes, revision binding and zero-authority projection fields."""
    if not isinstance(projection, dict) or set(projection) != _PROJECTION_FIELDS:
        raise ExternalStateProjectionError(
            "projection fields do not match the contract"
        )
    if projection.get("schema_version") != EXTERNAL_PROJECTION_SCHEMA_VERSION:
        raise ExternalStateProjectionError("unsupported projection schema")
    try:
        _identifier(projection.get("provider_id"), "provider_id")
        project_id = _identifier(projection.get("project_id"), "project_id")
    except ValueError as exc:
        raise ExternalStateProjectionError(str(exc)) from exc
    revision = projection.get("state_revision")
    source = projection.get("source")
    snapshot = projection.get("snapshot")
    if type(revision) is not int or revision < 0:
        raise ExternalStateProjectionError("state_revision is invalid")
    if not isinstance(source, dict) or set(source) != _PROJECTION_SOURCE_FIELDS:
        raise ExternalStateProjectionError("projection source is invalid")
    if (
        source.get("response_schema_version") != STATE_RESPONSE_SCHEMA_VERSION
        or source.get("tool") != READ_TOOL
        or source.get("revision") != revision
        or not _validate_event_head(source.get("event_head"))
        or not isinstance(source.get("registry_digest"), str)
        or _SHA256_RE.fullmatch(source["registry_digest"]) is None
        or not isinstance(source.get("capabilities_sha256"), str)
        or _SHA256_RE.fullmatch(source["capabilities_sha256"]) is None
    ):
        raise ExternalStateProjectionError("projection source binding is invalid")
    if (
        not isinstance(snapshot, dict)
        or not isinstance(snapshot.get("project"), dict)
        or snapshot["project"].get("project_id") != project_id
        or snapshot["project"].get("revision") != revision
    ):
        raise ExternalStateProjectionError("snapshot revision binding is invalid")
    try:
        state_sha256 = typed_state_snapshot_sha256(snapshot)
    except ExternalStateProjectionError as exc:
        raise ExternalStateProjectionError("typed snapshot validation failed") from exc
    if projection.get("state_sha256") != state_sha256:
        raise ExternalStateProjectionError("state digest mismatch")
    if (
        projection.get("state_write_authority") is not False
        or projection.get("controlled_action_authority") is not False
        or type(projection.get("provider_authority")) is not int
        or projection["provider_authority"] != 0
        or type(projection.get("external_effect_authority")) is not int
        or projection["external_effect_authority"] != 0
    ):
        raise ExternalStateProjectionError("projection authority must remain zero")
    body = {
        key: value
        for key, value in projection.items()
        if key not in {"projection_sha256", "signature"}
    }
    if projection.get("projection_sha256") != _digest(body):
        raise ExternalStateProjectionError("projection digest mismatch")
    if not isinstance(signer, HMACExternalStateProjectionSigner) or not signer.verify(
        {**body, "projection_sha256": projection["projection_sha256"]},
        projection.get("signature"),
    ):
        raise ExternalStateProjectionError("projection signature mismatch")
    return copy.deepcopy(projection)


class ExternalStateProjectionProvider:
    """Expose authorized State MCP reads as immutable, revision-bound projections."""

    def __init__(
        self,
        state_mcp: Any,
        *,
        provider_id: str,
        signer: HMACExternalStateProjectionSigner,
    ) -> None:
        if not callable(getattr(state_mcp, "call_tool", None)):
            raise TypeError("state_mcp must provide call_tool")
        self._state_mcp = state_mcp
        self._provider_id = _identifier(provider_id, "provider_id")
        if not isinstance(signer, HMACExternalStateProjectionSigner):
            raise TypeError("signer must be an HMACExternalStateProjectionSigner")
        self._signer = signer

    def call_tool(
        self,
        tool: str,
        arguments: Any,
        *,
        context: RequestContext,
    ) -> dict[str, Any]:
        """Read one authorized projection without adding state authority."""
        request_id = _request_id(arguments)
        if tool != EXTERNAL_READ_TOOL:
            return _response(
                request_id=request_id,
                error_code="unsupported",
                error_message="unsupported external State provider tool",
            )
        request_error = _validate_request(arguments)
        if request_error is not None:
            return _response(
                request_id=request_id,
                error_code="invalid_request",
                error_message=request_error,
            )

        source_request_id = "source:" + _digest(
            {
                "provider_id": self._provider_id,
                "request_id": arguments["request_id"],
                "project_id": arguments["project_id"],
            }
        )
        source_request = {
            "schema_version": STATE_REQUEST_SCHEMA_VERSION,
            "request_id": source_request_id,
            "project_id": arguments["project_id"],
        }
        try:
            source_response = self._state_mcp.call_tool(
                READ_TOOL,
                source_request,
                context=context,
            )
        except Exception:  # noqa: BLE001
            return _response(
                request_id=request_id,
                error_code="source_unavailable",
                error_message="authoritative state source is unavailable",
            )

        if isinstance(source_response, dict) and source_response.get("ok") is False:
            source_code = _validated_source_error(
                source_response,
                source_request_id=source_request_id,
            )
            if source_code is None:
                return _response(
                    request_id=request_id,
                    error_code="source_integrity_error",
                    error_message="authoritative state response failed integrity checks",
                )
            if source_code == "permission_denied":
                return _response(
                    request_id=request_id,
                    error_code="permission_denied",
                    error_message="request is not authorized",
                )
            if source_code == "not_found":
                return _response(
                    request_id=request_id,
                    error_code="not_found",
                    error_message="authorized project state was not found",
                )
            return _response(
                request_id=request_id,
                error_code="source_unavailable",
                error_message="authoritative state source is unavailable",
            )

        source_result = _validated_source_result(
            source_response,
            source_request_id=source_request_id,
            project_id=arguments["project_id"],
        )
        if source_result is None:
            return _response(
                request_id=request_id,
                error_code="source_integrity_error",
                error_message="authoritative state response failed integrity checks",
            )

        revision = source_result["revision"]
        if (
            arguments["expected_revision"] is not None
            and arguments["expected_revision"] != revision
        ):
            return _response(
                request_id=request_id,
                error_code="stale_view",
                error_message="requested state revision is no longer current",
            )

        snapshot = source_result["snapshot"]
        source = {
            "response_schema_version": STATE_RESPONSE_SCHEMA_VERSION,
            "tool": READ_TOOL,
            "revision": revision,
            "event_head": source_result["event_head"],
            "registry_digest": source_result["registry_digest"],
            "capabilities_sha256": _digest(source_result["capabilities"]),
        }
        body = {
            "schema_version": EXTERNAL_PROJECTION_SCHEMA_VERSION,
            "provider_id": self._provider_id,
            "project_id": arguments["project_id"],
            "state_revision": revision,
            "source": source,
            "snapshot": snapshot,
            "state_sha256": typed_state_snapshot_sha256(snapshot),
            "state_write_authority": False,
            "controlled_action_authority": False,
            "provider_authority": 0,
            "external_effect_authority": 0,
        }
        projection_sha256 = _digest(body)
        projection = validate_external_state_projection(
            {
                **body,
                "projection_sha256": projection_sha256,
                "signature": self._signer.sign(
                    {**body, "projection_sha256": projection_sha256}
                ),
            },
            signer=self._signer,
        )
        return _response(request_id=request_id, result=projection)


__all__ = [
    "EXTERNAL_PROJECTION_SCHEMA_VERSION",
    "EXTERNAL_READ_TOOL",
    "EXTERNAL_REQUEST_SCHEMA_VERSION",
    "EXTERNAL_RESPONSE_SCHEMA_VERSION",
    "ExternalStateProjectionError",
    "ExternalStateProjectionProvider",
    "HMACExternalStateProjectionSigner",
    "typed_state_snapshot_sha256",
    "validate_external_state_projection",
]
