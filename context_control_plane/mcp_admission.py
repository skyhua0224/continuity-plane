"""Pinned MCP Registry discovery and non-invoking tool admission decisions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

SNAPSHOT_SCHEMA_VERSION = "context.mcp-registry-snapshot/v1alpha1"
DECISION_SCHEMA_VERSION = "context.mcp-admission-decision/v1alpha1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SCOPES = {"read", "evidence_write", "state_write", "external_effect"}
_AUTH_KINDS = {"none", "token", "oauth2", "local_process"}
_REGISTRY_KINDS = {"fixture", "official"}
_TRUSTED_REGISTRY_ANCHOR_FIELDS = {
    "registry_kind",
    "registry_url",
    "registry_revision",
    "registry_sha256",
}
_SNAPSHOT_FIELDS = {
    "schema_version",
    "snapshot_id",
    "registry_kind",
    "registry_url",
    "registry_revision",
    "registry_sha256",
    "retrieved_at",
    "servers",
}
_SERVER_FIELDS = {
    "server_id",
    "publisher",
    "publisher_verified",
    "license_ref",
    "license_verified",
    "source_url",
    "source_revision",
    "source_sha256",
    "auth_kind",
    "tools",
}
_TOOL_FIELDS = {"name", "scope", "requires_state_mcp"}
_REQUEST_FIELDS = {
    "request_id",
    "project_id",
    "operation_id",
    "requested_server_ids",
    "granted_scopes",
    "available_auth_kinds",
    "allow_external_effects",
    "state_mcp_route",
    "expected_registry_revision",
    "expected_registry_sha256",
}
_DECISION_FIELDS = {
    "schema_version",
    "decision_id",
    "request_id",
    "project_id",
    "operation_id",
    "request_sha256",
    "state_mcp_route_anchor_sha256",
    "registry_snapshot_id",
    "registry_kind",
    "registry_revision",
    "registry_sha256",
    "registry_anchor_sha256",
    "decided_at",
    "authorized_scopes",
    "allow_external_effects",
    "servers",
    "tool_invocation_performed",
    "state_write_authority",
    "decision_sha256",
}
_SERVER_DECISION_FIELDS = {
    "server_id",
    "status",
    "reasons",
    "active_tools",
    "denied_tools",
}
_ACTIVE_TOOL_FIELDS = {"name", "scope", "invocation_route"}
_DENIED_TOOL_FIELDS = {"name", "scope", "reason"}


class MCPAdmissionError(ValueError):
    """Raised when registry provenance or tool authorization is invalid."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def mcp_registry_snapshot_sha256(snapshot: dict[str, Any]) -> str:
    """Return the registry content digest without retrieval-envelope time."""
    if not isinstance(snapshot, dict):
        raise MCPAdmissionError("MCP registry snapshot is invalid")
    unsigned = copy.deepcopy(snapshot)
    unsigned.pop("registry_sha256", None)
    unsigned.pop("retrieved_at", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _digest(value: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(value)
    unsigned.pop("decision_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _anchor_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _safe(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise MCPAdmissionError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int = 1024) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or any(character in value for character in "\x00\r\n")
    ):
        raise MCPAdmissionError(f"{field} is invalid")
    return value


def _https_url(value: Any, field: str) -> str:
    _text(value, field, 4096)
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise MCPAdmissionError(f"{field} must be an https URL without userinfo")
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MCPAdmissionError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MCPAdmissionError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise MCPAdmissionError(f"{field} requires a timezone")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise MCPAdmissionError(f"{field} is invalid")
    return value


def _git_revision(value: Any, field: str) -> str:
    if not isinstance(value, str) or _GIT_REVISION_RE.fullmatch(value) is None:
        raise MCPAdmissionError(f"{field} must be a fixed git revision")
    return value


def _validate_trusted_registry_anchor(
    snapshot: dict[str, Any], trusted_registry_anchor: dict[str, Any] | None
) -> str | None:
    if trusted_registry_anchor is None:
        if snapshot["registry_kind"] == "official":
            raise MCPAdmissionError("official registry requires a trusted anchor")
        return None
    if (
        not isinstance(trusted_registry_anchor, dict)
        or set(trusted_registry_anchor) != _TRUSTED_REGISTRY_ANCHOR_FIELDS
    ):
        raise MCPAdmissionError("trusted registry anchor fields are invalid")
    if trusted_registry_anchor["registry_kind"] not in _REGISTRY_KINDS:
        raise MCPAdmissionError("trusted registry kind is invalid")
    _https_url(trusted_registry_anchor["registry_url"], "trusted registry URL")
    _git_revision(
        trusted_registry_anchor["registry_revision"], "trusted registry revision"
    )
    _sha256(trusted_registry_anchor["registry_sha256"], "trusted registry sha256")
    expected = {
        field: snapshot[field] for field in _TRUSTED_REGISTRY_ANCHOR_FIELDS
    }
    if trusted_registry_anchor != expected:
        raise MCPAdmissionError("trusted registry anchor mismatch")
    return _anchor_digest(trusted_registry_anchor)


def validate_mcp_registry_snapshot(
    snapshot: Any,
    *,
    trusted_registry_anchor: dict[str, Any] | None = None,
) -> str | None:
    if not isinstance(snapshot, dict) or set(snapshot) != _SNAPSHOT_FIELDS:
        raise MCPAdmissionError("MCP registry snapshot fields are invalid")
    if snapshot["schema_version"] != SNAPSHOT_SCHEMA_VERSION:
        raise MCPAdmissionError("MCP registry schema_version is invalid")
    _safe(snapshot["snapshot_id"], "snapshot_id")
    if snapshot["registry_kind"] not in _REGISTRY_KINDS:
        raise MCPAdmissionError("registry_kind is invalid")
    _https_url(snapshot["registry_url"], "registry_url")
    _git_revision(snapshot["registry_revision"], "registry_revision")
    _sha256(snapshot["registry_sha256"], "registry_sha256")
    _timestamp(snapshot["retrieved_at"], "retrieved_at")
    servers = snapshot["servers"]
    if not isinstance(servers, list) or not servers or len(servers) > 1024:
        raise MCPAdmissionError("MCP servers are invalid")
    server_ids: list[str] = []
    for server_index, server in enumerate(servers):
        if not isinstance(server, dict) or set(server) != _SERVER_FIELDS:
            raise MCPAdmissionError(f"server {server_index} fields are invalid")
        server_id = _safe(server["server_id"], "server_id")
        server_ids.append(server_id)
        _text(server["publisher"], "publisher", 512)
        if type(server["publisher_verified"]) is not bool or type(server["license_verified"]) is not bool:
            raise MCPAdmissionError("publisher/license verification flags are invalid")
        _safe(server["license_ref"], "license_ref")
        _https_url(server["source_url"], "source_url")
        _git_revision(server["source_revision"], "source_revision")
        _sha256(server["source_sha256"], "source_sha256")
        if server["auth_kind"] not in _AUTH_KINDS:
            raise MCPAdmissionError("auth_kind is invalid")
        tools = server["tools"]
        if not isinstance(tools, list) or not tools or len(tools) > 4096:
            raise MCPAdmissionError("server tools are invalid")
        names: list[str] = []
        for tool_index, tool in enumerate(tools):
            if not isinstance(tool, dict) or set(tool) != _TOOL_FIELDS:
                raise MCPAdmissionError(f"tool {tool_index} fields are invalid")
            names.append(_safe(tool["name"], "tool name"))
            if tool["scope"] not in _SCOPES:
                raise MCPAdmissionError("tool scope is invalid")
            if type(tool["requires_state_mcp"]) is not bool:
                raise MCPAdmissionError("requires_state_mcp is invalid")
            expected_state_mcp = tool["scope"] in {"state_write", "external_effect"}
            if tool["requires_state_mcp"] is not expected_state_mcp:
                raise MCPAdmissionError("tool scope and State MCP requirement mismatch")
        if len(set(names)) != len(names):
            raise MCPAdmissionError("tool names must be unique per server")
    if len(set(server_ids)) != len(server_ids):
        raise MCPAdmissionError("server IDs must be unique")
    if snapshot["registry_sha256"] != mcp_registry_snapshot_sha256(snapshot):
        raise MCPAdmissionError("MCP registry snapshot digest mismatch")
    return _validate_trusted_registry_anchor(snapshot, trusted_registry_anchor)


def _validate_request(request: Any) -> None:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise MCPAdmissionError("MCP admission request fields are invalid")
    for field in ("request_id", "project_id", "operation_id"):
        _safe(request[field], field)
    ids = request["requested_server_ids"]
    if not isinstance(ids, list) or not ids or len(ids) > 256 or len(set(ids)) != len(ids):
        raise MCPAdmissionError("requested_server_ids are invalid")
    for server_id in ids:
        _safe(server_id, "requested server ID")
    scopes = request["granted_scopes"]
    if not isinstance(scopes, list) or len(scopes) > len(_SCOPES) or len(set(scopes)) != len(scopes):
        raise MCPAdmissionError("granted_scopes are invalid")
    if not set(scopes) <= _SCOPES:
        raise MCPAdmissionError("granted_scopes contain an unknown scope")
    auth = request["available_auth_kinds"]
    if not isinstance(auth, list) or len(set(auth)) != len(auth) or not set(auth) <= _AUTH_KINDS:
        raise MCPAdmissionError("available_auth_kinds are invalid")
    if type(request["allow_external_effects"]) is not bool:
        raise MCPAdmissionError("allow_external_effects is invalid")
    route = request["state_mcp_route"]
    if route is not None and (
        not isinstance(route, str) or not route.startswith("state-mcp://") or len(route) > 2048
    ):
        raise MCPAdmissionError("state_mcp_route is invalid")
    _git_revision(request["expected_registry_revision"], "expected_registry_revision")
    _sha256(request["expected_registry_sha256"], "expected_registry_sha256")


def mcp_admission_request_sha256(request: dict[str, Any]) -> str:
    """Return the canonical digest of a validated MCP admission request."""
    _validate_request(request)
    return hashlib.sha256(_canonical(request)).hexdigest()


def _trusted_state_mcp_route_anchor_sha256(
    request: dict[str, Any], trusted_state_mcp_routes: dict[str, str] | None
) -> str | None:
    route = request["state_mcp_route"]
    if route is None:
        return None
    project_id = request["project_id"]
    if not isinstance(trusted_state_mcp_routes, dict):
        raise MCPAdmissionError("state_mcp_route requires a trusted project route")
    trusted_route = trusted_state_mcp_routes.get(project_id)
    if not isinstance(trusted_route, str):
        raise MCPAdmissionError("trusted State MCP project route is absent")
    parsed = urlparse(trusted_route)
    if (
        parsed.scheme != "state-mcp"
        or parsed.netloc != "project"
        or parsed.path != f"/{project_id}"
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise MCPAdmissionError("trusted State MCP route project identity mismatch")
    if route != trusted_route:
        raise MCPAdmissionError("state_mcp_route does not match the trusted project route")
    return _anchor_digest({"project_id": project_id, "route": trusted_route})


def decide_mcp_admission(
    snapshot: dict[str, Any],
    request: dict[str, Any],
    *,
    trusted_state_mcp_routes: dict[str, str] | None = None,
    trusted_registry_anchor: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return an auditable activation decision without invoking any server tool."""
    registry_anchor_sha256 = validate_mcp_registry_snapshot(
        snapshot, trusted_registry_anchor=trusted_registry_anchor
    )
    _validate_request(request)
    state_mcp_route_anchor_sha256 = _trusted_state_mcp_route_anchor_sha256(
        request, trusted_state_mcp_routes
    )
    if request["expected_registry_revision"] != snapshot["registry_revision"]:
        raise MCPAdmissionError("registry revision mismatch")
    if request["expected_registry_sha256"] != snapshot["registry_sha256"]:
        raise MCPAdmissionError("registry digest mismatch")
    by_id = {server["server_id"]: server for server in snapshot["servers"]}
    if not set(request["requested_server_ids"]) <= set(by_id):
        raise MCPAdmissionError("requested server is absent from the fixed snapshot")
    decisions: list[dict[str, Any]] = []
    for server_id in request["requested_server_ids"]:
        server = by_id[server_id]
        reasons: list[str] = []
        if not server["publisher_verified"]:
            reasons.append("publisher_unverified")
        if not server["license_verified"]:
            reasons.append("license_unverified")
        if server["auth_kind"] != "none" and server["auth_kind"] not in request["available_auth_kinds"]:
            reasons.append("auth_unavailable")
        active: list[dict[str, Any]] = []
        denied: list[dict[str, Any]] = []
        for tool in server["tools"]:
            scope = tool["scope"]
            reason: str | None = None
            if reasons:
                reason = "server_quarantined"
            elif scope not in request["granted_scopes"]:
                reason = "scope_not_granted"
            elif scope == "external_effect" and not request["allow_external_effects"]:
                reason = "external_effect_not_allowed"
            elif tool["requires_state_mcp"] and request["state_mcp_route"] is None:
                reason = "state_mcp_route_required"
            if reason is not None:
                denied.append({"name": tool["name"], "scope": scope, "reason": reason})
                continue
            route = (
                request["state_mcp_route"]
                if tool["requires_state_mcp"]
                else f"mcp://{server_id}/{tool['name']}"
            )
            active.append({"name": tool["name"], "scope": scope, "invocation_route": route})
        decisions.append(
            {
                "server_id": server_id,
                "status": "quarantined" if reasons else "admitted",
                "reasons": reasons,
                "active_tools": active,
                "denied_tools": denied,
            }
        )
    decision = {
        "schema_version": DECISION_SCHEMA_VERSION,
        "decision_id": f"decision/{request['request_id']}",
        "request_id": request["request_id"],
        "project_id": request["project_id"],
        "operation_id": request["operation_id"],
        "request_sha256": mcp_admission_request_sha256(request),
        "state_mcp_route_anchor_sha256": state_mcp_route_anchor_sha256,
        "registry_snapshot_id": snapshot["snapshot_id"],
        "registry_kind": snapshot["registry_kind"],
        "registry_revision": snapshot["registry_revision"],
        "registry_sha256": snapshot["registry_sha256"],
        "registry_anchor_sha256": registry_anchor_sha256,
        "decided_at": snapshot["retrieved_at"],
        "authorized_scopes": list(request["granted_scopes"]),
        "allow_external_effects": request["allow_external_effects"],
        "servers": decisions,
        "tool_invocation_performed": False,
        "state_write_authority": False,
        "decision_sha256": "",
    }
    decision["decision_sha256"] = _digest(decision)
    validate_mcp_admission_decision(decision)
    return decision


def validate_mcp_admission_decision(
    decision: Any,
    *,
    expected_request: dict[str, Any] | None = None,
    expected_snapshot: dict[str, Any] | None = None,
    trusted_state_mcp_routes: dict[str, str] | None = None,
    trusted_registry_anchor: dict[str, Any] | None = None,
) -> None:
    if not isinstance(decision, dict) or set(decision) != _DECISION_FIELDS:
        raise MCPAdmissionError("MCP admission decision fields are invalid")
    if decision["schema_version"] != DECISION_SCHEMA_VERSION:
        raise MCPAdmissionError("MCP admission decision schema_version is invalid")
    for field in (
        "decision_id",
        "request_id",
        "project_id",
        "operation_id",
        "registry_snapshot_id",
    ):
        _safe(decision[field], field)
    if decision["decision_id"] != f"decision/{decision['request_id']}":
        raise MCPAdmissionError("decision_id does not bind request_id")
    _sha256(decision["request_sha256"], "request_sha256")
    if decision["state_mcp_route_anchor_sha256"] is not None:
        _sha256(
            decision["state_mcp_route_anchor_sha256"],
            "state_mcp_route_anchor_sha256",
        )
    if decision["registry_kind"] not in _REGISTRY_KINDS:
        raise MCPAdmissionError("registry_kind is invalid")
    _git_revision(decision["registry_revision"], "registry_revision")
    _sha256(decision["registry_sha256"], "registry_sha256")
    if decision["registry_anchor_sha256"] is not None:
        _sha256(decision["registry_anchor_sha256"], "registry_anchor_sha256")
    if (
        decision["registry_kind"] == "official"
        and decision["registry_anchor_sha256"] is None
    ):
        raise MCPAdmissionError("official registry decision requires a trusted anchor")
    _timestamp(decision["decided_at"], "decided_at")
    scopes = decision["authorized_scopes"]
    if not isinstance(scopes, list) or len(set(scopes)) != len(scopes) or not set(scopes) <= _SCOPES:
        raise MCPAdmissionError("authorized_scopes are invalid")
    if type(decision["allow_external_effects"]) is not bool:
        raise MCPAdmissionError("allow_external_effects is invalid")
    servers = decision["servers"]
    if not isinstance(servers, list) or not servers:
        raise MCPAdmissionError("server decisions are invalid")
    server_ids: list[str] = []
    for server in servers:
        if not isinstance(server, dict) or set(server) != _SERVER_DECISION_FIELDS:
            raise MCPAdmissionError("server decision fields are invalid")
        server_ids.append(_safe(server["server_id"], "server_id"))
        if server["status"] not in {"admitted", "quarantined"}:
            raise MCPAdmissionError("server status is invalid")
        reasons = server["reasons"]
        if not isinstance(reasons, list) or len(set(reasons)) != len(reasons):
            raise MCPAdmissionError("server reasons are invalid")
        for reason in reasons:
            _safe(reason, "server reason")
        active = server["active_tools"]
        denied = server["denied_tools"]
        if not isinstance(active, list) or not isinstance(denied, list):
            raise MCPAdmissionError("tool decisions are invalid")
        if server["status"] == "quarantined":
            if not reasons:
                raise MCPAdmissionError("quarantined server requires reasons")
            if active:
                raise MCPAdmissionError("quarantined server cannot activate tools")
        elif reasons:
            raise MCPAdmissionError("admitted server cannot carry quarantine reasons")
        tool_names: list[str] = []
        for tool in active:
            if not isinstance(tool, dict) or set(tool) != _ACTIVE_TOOL_FIELDS:
                raise MCPAdmissionError("active tool fields are invalid")
            tool_names.append(_safe(tool["name"], "active tool name"))
            scope = tool["scope"]
            if scope not in scopes:
                raise MCPAdmissionError("active tool scope was not authorized")
            route = tool["invocation_route"]
            if scope in {"state_write", "external_effect"}:
                if decision["state_mcp_route_anchor_sha256"] is None:
                    raise MCPAdmissionError("write/effect tool lacks a trusted route anchor")
                if not isinstance(route, str) or not route.startswith("state-mcp://"):
                    raise MCPAdmissionError("write/effect tool bypasses State MCP")
                if scope == "external_effect" and not decision["allow_external_effects"]:
                    raise MCPAdmissionError("external effect activated without its gate")
            else:
                if not isinstance(route, str) or not route.startswith("mcp://"):
                    raise MCPAdmissionError("read tool route is invalid")
        for tool in denied:
            if not isinstance(tool, dict) or set(tool) != _DENIED_TOOL_FIELDS:
                raise MCPAdmissionError("denied tool fields are invalid")
            tool_names.append(_safe(tool["name"], "denied tool name"))
            if tool["scope"] not in _SCOPES:
                raise MCPAdmissionError("denied tool scope is invalid")
            _safe(tool["reason"], "denied tool reason")
        if len(set(tool_names)) != len(tool_names):
            raise MCPAdmissionError("tool names must be unique across each server decision")
    if len(set(server_ids)) != len(server_ids):
        raise MCPAdmissionError("server decision IDs must be unique")
    if decision["tool_invocation_performed"] is not False or decision["state_write_authority"] is not False:
        raise MCPAdmissionError("admission decisions cannot invoke tools or grant state authority")
    _sha256(decision["decision_sha256"], "decision_sha256")
    if decision["decision_sha256"] != _digest(decision):
        raise MCPAdmissionError("MCP admission decision digest mismatch")

    if expected_request is not None:
        _validate_request(expected_request)
        expected_state_anchor = _trusted_state_mcp_route_anchor_sha256(
            expected_request, trusted_state_mcp_routes
        )
        if decision["state_mcp_route_anchor_sha256"] != expected_state_anchor:
            raise MCPAdmissionError("trusted State MCP route replay mismatch")
        expected_request_sha256 = mcp_admission_request_sha256(expected_request)
        if decision["request_sha256"] != expected_request_sha256:
            raise MCPAdmissionError("request replay digest mismatch")
        request_bindings = {
            "request_id": expected_request["request_id"],
            "project_id": expected_request["project_id"],
            "operation_id": expected_request["operation_id"],
            "authorized_scopes": expected_request["granted_scopes"],
            "allow_external_effects": expected_request["allow_external_effects"],
            "registry_revision": expected_request["expected_registry_revision"],
            "registry_sha256": expected_request["expected_registry_sha256"],
        }
        for field, expected in request_bindings.items():
            if decision[field] != expected:
                raise MCPAdmissionError(f"request replay {field} mismatch")
        if [server["server_id"] for server in decision["servers"]] != expected_request[
            "requested_server_ids"
        ]:
            raise MCPAdmissionError("request replay server set mismatch")

    if expected_snapshot is not None:
        expected_registry_anchor = validate_mcp_registry_snapshot(
            expected_snapshot, trusted_registry_anchor=trusted_registry_anchor
        )
        if decision["registry_anchor_sha256"] != expected_registry_anchor:
            raise MCPAdmissionError("trusted registry anchor replay mismatch")
        snapshot_bindings = {
            "registry_snapshot_id": expected_snapshot["snapshot_id"],
            "registry_kind": expected_snapshot["registry_kind"],
            "registry_revision": expected_snapshot["registry_revision"],
            "registry_sha256": expected_snapshot["registry_sha256"],
            "decided_at": expected_snapshot["retrieved_at"],
        }
        for field, expected in snapshot_bindings.items():
            if decision[field] != expected:
                raise MCPAdmissionError(f"registry replay {field} mismatch")
        snapshot_servers = {
            server["server_id"]: server for server in expected_snapshot["servers"]
        }
        for server in decision["servers"]:
            snapshot_server = snapshot_servers.get(server["server_id"])
            if snapshot_server is None:
                raise MCPAdmissionError("server replay identity mismatch")
            decided_tools = {
                tool["name"]: tool["scope"]
                for tool in server["active_tools"] + server["denied_tools"]
            }
            snapshot_tools = {
                tool["name"]: tool["scope"] for tool in snapshot_server["tools"]
            }
            if decided_tools != snapshot_tools:
                raise MCPAdmissionError("server tool replay mismatch")

    if expected_request is not None and expected_snapshot is not None:
        replayed = decide_mcp_admission(
            expected_snapshot,
            expected_request,
            trusted_state_mcp_routes=trusted_state_mcp_routes,
            trusted_registry_anchor=trusted_registry_anchor,
        )
        if decision != replayed:
            raise MCPAdmissionError("decision replay mismatch")


def canonical_mcp_admission_decision_bytes(decision: dict[str, Any]) -> bytes:
    validate_mcp_admission_decision(decision)
    return _canonical(decision)
