#!/usr/bin/env python3
"""Small stdio MCP adapter exposing the bounded local resume packet."""

from __future__ import annotations

import atexit
import json
import hashlib
import subprocess
import sys
import time
from pathlib import Path

from .light_observability import PolicyConfigError, SessionProbe, load_policy


def _reply(request_id: object, result: dict) -> None:
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)


def _error(request_id: object, code: int, message: str) -> None:
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        ),
        flush=True,
    )


def _binding_from_output(output: str) -> dict | None:
    try:
        envelope = json.loads(output)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version")
        not in {
            "context.recovery-envelope/v1alpha1",
            "context.resume-packet/v1alpha1",
        }
    ):
        return None
    active_work = envelope.get("active_work")
    claim = envelope.get("claim")
    idle = active_work is None and claim is None
    if not idle and (
        not isinstance(active_work, dict) or not isinstance(claim, dict)
    ):
        return None
    if idle and envelope.get("next_action") != "activate-next-work":
        return None
    return {
        "project_id": envelope.get("project_id"),
        "mode": "idle" if idle else "active",
        "work_id": None if idle else active_work.get("work_id"),
        "claim_id": None if idle else claim.get("claim_id"),
        "actor_ref": None if idle else claim.get("actor_ref"),
        "read_only": envelope.get("read_only") is not False,
        "source_fresh": envelope.get("source_fresh") is True,
        "checkpoint_verified": envelope.get("checkpoint_verified") is True,
        "lease_valid": envelope.get("lease_valid") is True,
        "next_action": envelope.get("next_action"),
    }


def _binding(root: Path) -> dict | None:
    try:
        result = subprocess.run(
            ["continuity", "resume", "--root", str(root)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _binding_from_output(result.stdout)


def _requested_root(value: object, active_root: Path | None) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        path = Path(value)
        if path.is_absolute():
            resolved = path.resolve()
        else:
            if active_root is None:
                return None
            resolved = (active_root / path).resolve()
    except OSError:
        return None
    if not (resolved / ".continuity/project.yaml").is_file():
        return None
    return resolved


def _run_cli_with_retry(command: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    last = subprocess.CompletedProcess(command, 1, "", "operation failed")
    for attempt in range(3):
        try:
            last = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last = subprocess.CompletedProcess(command, 1, "", str(exc))
        if last.returncode == 0:
            return last
        message = f"{last.stdout}\n{last.stderr}".lower()
        transient = any(
            marker in message
            for marker in (
                "transport closed",
                "connection reset",
                "broken pipe",
                "timed out",
                "temporarily unavailable",
            )
        )
        if not transient or attempt == 2:
            break
        if _binding(root) is None:
            break
    return last


def _write_binding_error(
    request_id: object,
    *,
    binding: dict | None,
    actor_ref: object | None = None,
    claim_id: object | None = None,
    work_id: object | None = None,
) -> bool:
    if binding is None:
        _error(request_id, -32001, "session binding is unavailable; write tools are disabled")
        return True
    if binding["read_only"]:
        _error(request_id, -32002, "session binding is read-only; write tools are disabled")
        return True
    if actor_ref is not None and actor_ref != binding["actor_ref"]:
        _error(request_id, -32003, "actor binding does not match the session claim")
        return True
    if claim_id is not None and claim_id != binding["claim_id"]:
        _error(request_id, -32003, "claim binding does not match the session claim")
        return True
    if work_id is not None and work_id != binding["work_id"]:
        _error(request_id, -32003, "Work binding does not match the session claim")
        return True
    return False


def _write_transition_binding_error(
    request_id: object,
    *,
    binding: dict | None,
    actor_ref: object,
    work_id: object,
    claim_id: object,
    return_work_id: object,
    successor_claim_id: object,
) -> bool:
    if binding is None:
        _error(request_id, -32001, "session binding is unavailable; write tools are disabled")
        return True
    if binding["read_only"]:
        _error(request_id, -32002, "session binding is read-only; write tools are disabled")
        return True
    if actor_ref != binding["actor_ref"]:
        _error(request_id, -32003, "actor binding does not match the session claim")
        return True
    current_path = work_id == binding["work_id"] and claim_id == binding["claim_id"]
    replay_path = (
        return_work_id == binding["work_id"]
        and successor_claim_id == binding["claim_id"]
    )
    if not current_path and not replay_path:
        _error(request_id, -32003, "transition binding does not match the active or returned claim")
        return True
    return False


def _write_activation_binding_error(
    request_id: object,
    *,
    binding: dict | None,
) -> bool:
    if binding is None:
        _error(request_id, -32001, "session binding is unavailable; write tools are disabled")
        return True
    if binding["read_only"]:
        _error(request_id, -32002, "session binding is read-only; write tools are disabled")
        return True
    if binding["mode"] != "idle":
        _error(request_id, -32003, "successor activation requires an idle session binding")
        return True
    return False


def _claim_recovery_binding_error(
    request_id: object,
    *,
    binding: dict | None,
    action: str,
    actor_ref: str,
    claim_id: str,
    new_claim_id: str | None,
) -> bool:
    if binding is None:
        _error(request_id, -32001, "session binding is unavailable; write tools are disabled")
        return True
    if binding["mode"] != "active":
        _error(request_id, -32003, "claim recovery requires an active session binding")
        return True
    if actor_ref != binding["actor_ref"]:
        _error(request_id, -32003, "actor binding does not match the session claim")
        return True
    if action == "heartbeat":
        if claim_id != binding["claim_id"]:
            _error(request_id, -32003, "claim binding does not match the session claim")
            return True
        if binding["read_only"]:
            stale_source_recovery = (
                not binding["source_fresh"]
                and binding["checkpoint_verified"]
                and binding["lease_valid"]
                and binding["next_action"] == "remain-read-only"
            )
            if not stale_source_recovery:
                _error(
                    request_id,
                    -32002,
                    "read-only heartbeat requires only a stale canonical source with a valid bound claim and verified checkpoint",
                )
                return True
        return False
    if binding["read_only"]:
        expired_reclaim = (
            claim_id == binding["claim_id"]
            and new_claim_id != claim_id
            and binding["checkpoint_verified"]
            and not binding["lease_valid"]
            and binding["next_action"] == "remain-read-only"
        )
        if not expired_reclaim:
            _error(
                request_id,
                -32002,
                "read-only session allows only recovery of its expired bound claim",
            )
            return True
        return False
    replay = (
        claim_id != binding["claim_id"]
        and new_claim_id == binding["claim_id"]
    )
    if not replay:
        _error(
            request_id,
            -32003,
            "reclaim binding does not match an expired claim or its active successor",
        )
        return True
    return False


def main() -> int:
    session_roots: dict[str, Path] = {}
    session_bindings: dict[str, dict] = {}
    active_root: Path | None = None
    probes: dict[str, SessionProbe] = {}

    def close_probes() -> None:
        for probe in probes.values():
            probe.close()

    atexit.register(close_probes)
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            initialize_params = request.get("params")
            if not isinstance(initialize_params, dict):
                initialize_params = {}
            _reply(
                request_id,
                {
                    "protocolVersion": initialize_params.get(
                        "protocolVersion", "2024-11-05"
                    ),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "continuity", "version": "0.1.0-alpha.10"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _reply(
                request_id,
                {
                    "tools": [
                        {
                            "name": "continuity_resume",
                            "description": "读取有界恢复包并绑定本 MCP Session 的项目根；全局插件首次调用需传绝对路径 / Read the bounded packet and bind this MCP session to the project root; use an absolute path for the first global-plugin call.",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["root"],
                                "properties": {"root": {"type": "string", "minLength": 1}},
                            },
                        },
                        {
                            "name": "continuity_autorun",
                            "description": "从已验证 checkpoint 继续当前 Work；同一 checkpoint 幂等，lease 临近或过期时按受控路径续租或换签 / Continue the current Work from a verified checkpoint; idempotent per checkpoint with controlled heartbeat or reclaim.",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["root"],
                                "properties": {"root": {"type": "string", "minLength": 1}},
                            },
                        },
                        {
                            "name": "continuity_checkpoint",
                            "description": "创建或验证 immutable checkpoint；本 MCP Session 须先调用 continuity_resume / Create or verify a checkpoint after continuity_resume binds this MCP session.",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["root", "action"],
                                "properties": {
                                    "root": {"type": "string", "minLength": 1},
                                    "action": {"enum": ["create", "verify"]},
                                },
                            },
                        },
                        {
                            "name": "continuity_work_complete",
                            "description": "用 checkpoint-bound evidence 完成 Work 并释放 claim；须先调用 continuity_resume / Complete Work and release its claim after continuity_resume binds this MCP session.",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "root",
                                    "work_id",
                                    "claim_id",
                                    "actor_ref",
                                    "evidence_files",
                                ],
                                "properties": {
                                    "root": {"type": "string", "minLength": 1},
                                    "work_id": {"type": "string", "minLength": 1},
                                    "claim_id": {"type": "string", "minLength": 1},
                                    "actor_ref": {"type": "string", "minLength": 1},
                                    "evidence_files": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 32,
                                        "items": {"type": "string", "minLength": 1},
                                    },
                                },
                            },
                        },
                        {
                            "name": "continuity_work_transition",
                            "description": "完成当前依赖 Work，并在单一 State 事件中释放 claim、解析依赖 blocker、激活预声明 return point、签发新 claim 和刷新 checkpoint / Complete the active dependency and atomically return to its predeclared Work with a fresh claim and checkpoint.",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "root",
                                    "work_id",
                                    "claim_id",
                                    "actor_ref",
                                    "return_work_id",
                                    "successor_claim_id",
                                    "successor_scope",
                                    "resolved_blocker_id",
                                    "workspace_root",
                                    "expected_head",
                                    "evidence_files",
                                ],
                                "properties": {
                                    "root": {"type": "string", "minLength": 1},
                                    "work_id": {"type": "string", "minLength": 1},
                                    "claim_id": {"type": "string", "minLength": 1},
                                    "actor_ref": {"type": "string", "minLength": 1},
                                    "return_work_id": {"type": "string", "minLength": 1},
                                    "successor_claim_id": {"type": "string", "minLength": 1},
                                    "successor_scope": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 128,
                                        "items": {"type": "string", "minLength": 1},
                                    },
                                    "resolved_blocker_id": {"type": "string", "minLength": 1},
                                    "remaining_blocker_id": {"type": "string", "minLength": 1},
                                    "remaining_blocker_reason": {"type": "string", "minLength": 1},
                                    "workspace_root": {"type": "string", "minLength": 1},
                                    "expected_head": {
                                        "type": "string",
                                        "pattern": "^[0-9a-f]{40}$",
                                    },
                                    "expected_ref": {"type": "string", "minLength": 1},
                                    "evidence_files": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 32,
                                        "items": {"type": "string", "minLength": 1},
                                    },
                                },
                            },
                        },
                        {
                            "name": "continuity_work_activate",
                            "description": "添加并认领下一个 source-bound Work；须先调用 continuity_resume / Add and claim the next Work after continuity_resume binds this MCP session.",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "root",
                                    "work_id",
                                    "work_title",
                                    "owner_ref",
                                    "claim_id",
                                    "scope",
                                ],
                                "properties": {
                                    "root": {"type": "string", "minLength": 1},
                                    "work_id": {"type": "string", "minLength": 1},
                                    "work_title": {"type": "string", "minLength": 1},
                                    "owner_ref": {"type": "string", "minLength": 1},
                                    "claim_id": {"type": "string", "minLength": 1},
                                    "scope": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 128,
                                        "items": {"type": "string", "minLength": 1},
                                    },
                                    "execution_class": {
                                        "enum": ["standard", "delivery"]
                                    },
                                    "source_ref": {"type": "string", "minLength": 1},
                                    "predecessor_work_id": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "implementation_evidence_ids": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 32,
                                        "items": {"type": "string", "minLength": 1},
                                    },
                                    "workspace_id": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "workspace_root": {
                                        "type": "string",
                                        "minLength": 1,
                                    },
                                    "expected_head": {
                                        "type": "string",
                                        "pattern": "^[0-9a-f]{40}$",
                                    },
                                    "expected_ref": {"type": "string", "minLength": 1},
                                    "allow_effects": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 16,
                                        "items": {"type": "string", "minLength": 1},
                                    },
                                },
                            },
                        },
                        {
                            "name": "continuity_claim_recover",
                            "description": "续租或恢复 claim，并在同一调用内受控刷新已变更的 canonical source 与 checkpoint；任一步失败均保持只读 / Heartbeat or reclaim and, when narrowly authorized, refresh changed canonical sources and the checkpoint in one call.",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "root",
                                    "action",
                                    "claim_id",
                                    "actor_ref"
                                ],
                                "properties": {
                                    "root": {"type": "string", "minLength": 1},
                                    "action": {"enum": ["heartbeat", "reclaim"]},
                                    "claim_id": {"type": "string", "minLength": 1},
                                    "new_claim_id": {"type": ["string", "null"]},
                                    "actor_ref": {"type": "string", "minLength": 1},
                                    "lease_ttl_ms": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 604800000
                                    }
                                }
                            }
                        }
                    ]
                },
            )
        elif method == "tools/call":
            params = request.get("params", {})
            if not isinstance(params, dict):
                _error(request_id, -32602, "params must be an object")
                continue
            tool_name = params.get("name")
            if tool_name not in {
                "continuity_resume",
                "continuity_autorun",
                "continuity_checkpoint",
                "continuity_work_complete",
                "continuity_work_transition",
                "continuity_work_activate",
                "continuity_claim_recover",
            }:
                _error(request_id, -32602, "unknown tool")
                continue
            arguments = params.get("arguments", {})
            if not isinstance(arguments, dict):
                _error(request_id, -32602, "arguments must be an object")
                continue
            root = arguments.get("root")
            if not isinstance(root, str) or not root:
                _error(request_id, -32602, "root is required")
                continue
            requested_root = _requested_root(root, active_root)
            if requested_root is None:
                _error(request_id, -32000, "root does not match this MCP session project")
                continue
            root_key = str(requested_root)
            if tool_name != "continuity_resume" and root_key not in session_roots:
                _error(
                    request_id,
                    -32001,
                    "session binding is unavailable; call continuity_resume first",
                )
                continue
            canonical_root = root_key
            try:
                probe = probes.get(root_key)
                if probe is None:
                    probe = SessionProbe(requested_root, load_policy(requested_root))
                    probes[root_key] = probe
            except PolicyConfigError:
                _error(
                    request_id,
                    -32004,
                    "Continuity policy is invalid; this MCP session remains read-only",
                )
                continue
            binding = (
                None
                if tool_name == "continuity_resume"
                else session_bindings.get(root_key)
            )
            if tool_name in {"continuity_autorun", "continuity_claim_recover"}:
                refresh_started = time.perf_counter()
                binding = _binding(requested_root)
                refresh_duration_ms = (time.perf_counter() - refresh_started) * 1000
                probe.record_call(
                    "continuity_binding_refresh",
                    duration_ms=refresh_duration_ms,
                    success=binding is not None,
                )
                if binding is None:
                    session_bindings.pop(root_key, None)
                else:
                    session_bindings[root_key] = binding
            command = ["continuity", "resume", "--root", canonical_root]
            if tool_name == "continuity_autorun":
                if _write_binding_error(request_id, binding=binding):
                    continue
                command = [
                    "continuity",
                    "autorun",
                    "--root",
                    canonical_root,
                    "--session-id",
                    "mcp-" + hashlib.sha256(canonical_root.encode()).hexdigest()[:32],
                ]
            elif tool_name == "continuity_checkpoint":
                action = arguments.get("action")
                if action not in {"create", "verify"}:
                    _error(request_id, -32602, "action must be create or verify")
                    continue
                if action == "create" and _write_binding_error(
                    request_id, binding=binding
                ):
                    continue
                command = [
                    "continuity",
                    "checkpoint",
                    action,
                    "--root",
                    canonical_root,
                ]
            elif tool_name == "continuity_work_complete":
                work_id = arguments.get("work_id")
                claim_id = arguments.get("claim_id")
                actor_ref = arguments.get("actor_ref")
                evidence_files = arguments.get("evidence_files")
                if (
                    not all(
                        isinstance(value, str) and value
                        for value in (work_id, claim_id, actor_ref)
                    )
                    or not isinstance(evidence_files, list)
                    or not evidence_files
                    or len(evidence_files) > 32
                    or any(not isinstance(value, str) or not value for value in evidence_files)
                ):
                    _error(request_id, -32602, "completion arguments are invalid")
                    continue
                if _write_binding_error(
                    request_id,
                    binding=binding,
                    actor_ref=actor_ref,
                    claim_id=claim_id,
                    work_id=work_id,
                ):
                    continue
                command = [
                    "continuity",
                    "work",
                    "complete",
                    "--root",
                    canonical_root,
                    "--work-id",
                    work_id,
                    "--claim-id",
                    claim_id,
                    "--actor-ref",
                    actor_ref,
                ]
                for evidence_file in evidence_files:
                    command.extend(["--evidence-file", evidence_file])
            elif tool_name == "continuity_work_transition":
                required = (
                    "work_id",
                    "claim_id",
                    "actor_ref",
                    "return_work_id",
                    "successor_claim_id",
                    "resolved_blocker_id",
                    "workspace_root",
                    "expected_head",
                )
                values = {field: arguments.get(field) for field in required}
                scopes = arguments.get("successor_scope")
                evidence_files = arguments.get("evidence_files")
                remaining_id = arguments.get("remaining_blocker_id")
                remaining_reason = arguments.get("remaining_blocker_reason")
                expected_ref = arguments.get("expected_ref")
                if (
                    any(not isinstance(value, str) or not value for value in values.values())
                    or not isinstance(scopes, list)
                    or not scopes
                    or len(scopes) > 128
                    or any(not isinstance(value, str) or not value for value in scopes)
                    or not isinstance(evidence_files, list)
                    or not evidence_files
                    or len(evidence_files) > 32
                    or any(not isinstance(value, str) or not value for value in evidence_files)
                    or (remaining_id is None) != (remaining_reason is None)
                    or (remaining_id is not None and not isinstance(remaining_id, str))
                    or (remaining_reason is not None and not isinstance(remaining_reason, str))
                    or (expected_ref is not None and not isinstance(expected_ref, str))
                ):
                    _error(request_id, -32602, "transition arguments are invalid")
                    continue
                if _write_transition_binding_error(
                    request_id,
                    binding=binding,
                    actor_ref=values["actor_ref"],
                    work_id=values["work_id"],
                    claim_id=values["claim_id"],
                    return_work_id=values["return_work_id"],
                    successor_claim_id=values["successor_claim_id"],
                ):
                    continue
                command = [
                    "continuity",
                    "work",
                    "transition",
                    "--root",
                    canonical_root,
                    "--work-id",
                    values["work_id"],
                    "--claim-id",
                    values["claim_id"],
                    "--actor-ref",
                    values["actor_ref"],
                    "--return-work-id",
                    values["return_work_id"],
                    "--successor-claim-id",
                    values["successor_claim_id"],
                    "--resolved-blocker-id",
                    values["resolved_blocker_id"],
                    "--workspace-root",
                    values["workspace_root"],
                    "--expected-head",
                    values["expected_head"],
                ]
                for scope_ref in scopes:
                    command.extend(["--successor-scope", scope_ref])
                if remaining_id is not None:
                    command.extend(["--remaining-blocker-id", remaining_id])
                    command.extend(["--remaining-blocker-reason", remaining_reason])
                if expected_ref is not None:
                    command.extend(["--expected-ref", expected_ref])
                for evidence_file in evidence_files:
                    command.extend(["--evidence-file", evidence_file])
            elif tool_name == "continuity_work_activate":
                required = ("work_id", "work_title", "owner_ref", "claim_id")
                values = [arguments.get(field) for field in required]
                scope = arguments.get("scope")
                execution_class = arguments.get("execution_class", "standard")
                source_ref = arguments.get("source_ref")
                predecessor_work_id = arguments.get("predecessor_work_id")
                evidence_ids = arguments.get("implementation_evidence_ids")
                workspace_id = arguments.get("workspace_id")
                workspace_root = arguments.get("workspace_root")
                expected_head = arguments.get("expected_head")
                expected_ref = arguments.get("expected_ref")
                allow_effects = arguments.get("allow_effects")
                allowed_fields = {
                    "root",
                    *required,
                    "scope",
                    "execution_class",
                    "source_ref",
                    "predecessor_work_id",
                    "implementation_evidence_ids",
                    "workspace_id",
                    "workspace_root",
                    "expected_head",
                    "expected_ref",
                    "allow_effects",
                }
                delivery_values = (
                    source_ref,
                    predecessor_work_id,
                    evidence_ids,
                    workspace_id,
                    workspace_root,
                    expected_head,
                    expected_ref,
                    allow_effects,
                )
                if (
                    set(arguments) - allowed_fields
                    or
                    any(not isinstance(value, str) or not value for value in values)
                    or not isinstance(scope, list)
                    or not scope
                    or len(scope) > 128
                    or any(not isinstance(value, str) or not value for value in scope)
                    or execution_class not in {"standard", "delivery"}
                    or (
                        execution_class == "standard"
                        and any(value is not None for value in delivery_values)
                    )
                    or (
                        execution_class == "delivery"
                        and (
                            not isinstance(source_ref, str)
                            or not source_ref
                            or not isinstance(predecessor_work_id, str)
                            or not predecessor_work_id
                            or not isinstance(evidence_ids, list)
                            or not evidence_ids
                            or len(evidence_ids) > 32
                            or any(
                                not isinstance(value, str) or not value
                                for value in evidence_ids
                            )
                            or (
                                workspace_id is not None
                                and (
                                    not isinstance(workspace_id, str)
                                    or not workspace_id
                                )
                            )
                            or not isinstance(workspace_root, str)
                            or not workspace_root
                            or not isinstance(expected_head, str)
                            or len(expected_head) != 40
                            or any(
                                character not in "0123456789abcdef"
                                for character in expected_head
                            )
                            or (
                                expected_ref is not None
                                and (
                                    not isinstance(expected_ref, str)
                                    or not expected_ref
                                )
                            )
                            or not isinstance(allow_effects, list)
                            or not allow_effects
                            or len(allow_effects) > 16
                            or any(
                                not isinstance(value, str) or not value
                                for value in allow_effects
                            )
                        )
                    )
                ):
                    _error(request_id, -32602, "activation arguments are invalid")
                    continue
                if _write_activation_binding_error(
                    request_id,
                    binding=binding,
                ):
                    continue
                command = [
                    "continuity",
                    "work",
                    "activate",
                    "--root",
                    canonical_root,
                    "--work-id",
                    arguments["work_id"],
                    "--work-title",
                    arguments["work_title"],
                    "--owner-ref",
                    arguments["owner_ref"],
                    "--claim-id",
                    arguments["claim_id"],
                    "--execution-class",
                    execution_class,
                ]
                for scope_ref in scope:
                    command.extend(["--scope", scope_ref])
                if execution_class == "delivery":
                    command.extend(["--source-ref", source_ref])
                    command.extend(["--predecessor-work-id", predecessor_work_id])
                    for evidence_id in evidence_ids:
                        command.extend(["--implementation-evidence-id", evidence_id])
                    if workspace_id is not None:
                        command.extend(["--workspace-id", workspace_id])
                    command.extend(["--workspace-root", workspace_root])
                    command.extend(["--expected-head", expected_head])
                    if expected_ref is not None:
                        command.extend(["--expected-ref", expected_ref])
                    for effect in allow_effects:
                        command.extend(["--allow-effect", effect])
            elif tool_name == "continuity_claim_recover":
                action = arguments.get("action")
                claim_id = arguments.get("claim_id")
                actor_ref = arguments.get("actor_ref")
                new_claim_id = arguments.get("new_claim_id")
                lease_ttl_ms = arguments.get("lease_ttl_ms", 28_800_000)
                if (
                    set(arguments)
                    - {
                        "root",
                        "action",
                        "claim_id",
                        "new_claim_id",
                        "actor_ref",
                        "lease_ttl_ms",
                    }
                    or
                    action not in {"heartbeat", "reclaim"}
                    or not isinstance(claim_id, str)
                    or not claim_id
                    or not isinstance(actor_ref, str)
                    or not actor_ref
                    or type(lease_ttl_ms) is not int
                    or lease_ttl_ms <= 0
                    or lease_ttl_ms > 604_800_000
                    or (
                        action == "reclaim"
                        and (not isinstance(new_claim_id, str) or not new_claim_id)
                    )
                    or (action == "heartbeat" and new_claim_id is not None)
                ):
                    _error(request_id, -32602, "claim recovery arguments are invalid")
                    continue
                if _claim_recovery_binding_error(
                    request_id,
                    binding=binding,
                    action=action,
                    actor_ref=actor_ref,
                    claim_id=claim_id,
                    new_claim_id=new_claim_id,
                ):
                    continue
                command = [
                    "continuity",
                    "work",
                    "recover",
                    action,
                    "--root",
                    canonical_root,
                    "--claim-id",
                    claim_id,
                    "--actor-ref",
                    actor_ref,
                    "--lease-ttl-ms",
                    str(lease_ttl_ms),
                ]
                if action == "reclaim":
                    command.extend(["--new-claim-id", new_claim_id])
            started = time.perf_counter()
            result = _run_cli_with_retry(command, requested_root)
            duration_ms = (time.perf_counter() - started) * 1000
            results = [result]
            failed = any(item.returncode != 0 for item in results)
            output = [item.stdout or item.stderr for item in results]
            text = "\n".join(item.rstrip() for item in output if item.strip())
            if not text:
                text = "operation failed" if failed else "operation completed"
            if tool_name == "continuity_resume" and not failed:
                resumed_binding = _binding_from_output(result.stdout)
                if resumed_binding is None:
                    failed = True
                    text = "Continuity resume returned an invalid recovery envelope"
                    session_roots.pop(root_key, None)
                    session_bindings.pop(root_key, None)
                else:
                    session_roots[root_key] = requested_root
                    session_bindings[root_key] = resumed_binding
                    active_root = requested_root
            probe_tool_name = tool_name
            if tool_name == "continuity_checkpoint":
                probe_tool_name = f"{tool_name}:{arguments.get('action')}"
            request_bytes = len(
                json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            response_bytes = len(text.encode("utf-8"))
            probe.record_call(
                probe_tool_name,
                duration_ms=duration_ms,
                success=not failed,
                request_bytes=request_bytes,
                response_bytes=response_bytes,
            )
            if tool_name == "continuity_resume":
                probe.boundary(
                    "resume",
                    success=not failed,
                    duration_ms=duration_ms,
                    extra={
                        "packet_bytes": response_bytes if not failed else None,
                        "duplicate_resumes": probe.duplicate_resumes,
                    },
                )
            elif tool_name in {
                "continuity_autorun",
                "continuity_work_complete",
                "continuity_work_transition",
                "continuity_work_activate",
                "continuity_claim_recover",
            }:
                refresh_started = time.perf_counter()
                refreshed_binding = _binding(requested_root)
                refresh_duration_ms = (time.perf_counter() - refresh_started) * 1000
                probe.record_call(
                    "continuity_binding_refresh",
                    duration_ms=refresh_duration_ms,
                    success=refreshed_binding is not None,
                )
                if refreshed_binding is None:
                    session_bindings.pop(root_key, None)
                else:
                    session_bindings[root_key] = refreshed_binding
            _reply(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": failed,
                },
            )
        elif request_id is not None:
            _error(request_id, -32601, f"method not found: {method}")
    close_probes()
    atexit.unregister(close_probes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
