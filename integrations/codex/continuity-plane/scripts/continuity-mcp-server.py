#!/usr/bin/env python3
"""Small stdio MCP adapter exposing the bounded local resume packet."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _reply(request_id: object, result: dict) -> None:
    print(json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}), flush=True)


def _error(request_id: object, code: int, message: str) -> None:
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        ),
        flush=True,
    )


def _session_root() -> Path | None:
    start = Path.cwd().resolve()
    completed = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    candidates = [
        Path(completed.stdout.strip()).resolve()
        if completed.returncode == 0 and completed.stdout.strip()
        else start,
        start,
        *start.parents,
    ]
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / ".continuity/project.yaml").is_file():
            return candidate
    return None


def _binding(root: Path) -> dict | None:
    result = subprocess.run(
        ["continuity", "resume", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema_version")
        not in {
            "context.recovery-envelope/v1alpha1",
            "context.resume-packet/v1alpha1",
        }
        or not isinstance(envelope.get("active_work"), dict)
        or not isinstance(envelope.get("claim"), dict)
    ):
        return None
    return {
        "project_id": envelope.get("project_id"),
        "work_id": envelope["active_work"].get("work_id"),
        "claim_id": envelope["claim"].get("claim_id"),
        "actor_ref": envelope["claim"].get("actor_ref"),
        "read_only": envelope.get("read_only") is not False,
    }


def _requested_root(value: object, session_root: Path) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        resolved = (Path.cwd() / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    except OSError:
        return None
    return resolved if resolved == session_root else None


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


def main() -> int:
    session_root = _session_root()
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = request.get("method")
        request_id = request.get("id")
        if method == "initialize":
            _reply(
                request_id,
                {
                    "protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "continuity", "version": "0.1.0"},
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
                            "description": "读取本地项目的有界 active Work 恢复包 / Read the bounded active Work resume packet for a local project.",
                            "inputSchema": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["root"],
                                "properties": {"root": {"type": "string", "minLength": 1}},
                            },
                        },
                        {
                            "name": "continuity_checkpoint",
                            "description": "创建或验证本地项目 immutable checkpoint / Create or verify an immutable checkpoint for a local project.",
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
                            "description": "用 checkpoint-bound evidence 完成本地 Work 并释放 claim / Complete local Work and release its claim with checkpoint-bound evidence.",
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
                            "name": "continuity_work_activate",
                            "description": "添加并认领下一个 source-bound 本地 Work / Add and claim the next source-bound local Work.",
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
                                },
                            },
                        },
                        {
                            "name": "continuity_claim_recover",
                            "description": "主动续租或用新 identity 恢复本地 claim / Heartbeat or reclaim a local claim with a new identity.",
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
            tool_name = params.get("name")
            if tool_name not in {
                "continuity_resume",
                "continuity_checkpoint",
                "continuity_work_complete",
                "continuity_work_activate",
                "continuity_claim_recover",
            }:
                _error(request_id, -32602, "unknown tool")
                continue
            arguments = params.get("arguments", {})
            root = arguments.get("root")
            if not isinstance(root, str) or not root:
                _error(request_id, -32602, "root is required")
                continue
            if session_root is None or _requested_root(root, session_root) is None:
                _error(request_id, -32000, "root does not match this MCP session project")
                continue
            canonical_root = str(session_root)
            binding = _binding(session_root)
            command = ["continuity", "resume", "--root", canonical_root]
            if tool_name == "continuity_checkpoint":
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
            elif tool_name == "continuity_work_activate":
                required = ("work_id", "work_title", "owner_ref", "claim_id")
                values = [arguments.get(field) for field in required]
                scope = arguments.get("scope")
                if (
                    any(not isinstance(value, str) or not value for value in values)
                    or not isinstance(scope, list)
                    or not scope
                    or len(scope) > 128
                    or any(not isinstance(value, str) or not value for value in scope)
                ):
                    _error(request_id, -32602, "activation arguments are invalid")
                    continue
                if _write_binding_error(
                    request_id,
                    binding=binding,
                    actor_ref=arguments["owner_ref"],
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
                ]
                for scope_ref in scope:
                    command.extend(["--scope", scope_ref])
            elif tool_name == "continuity_claim_recover":
                action = arguments.get("action")
                claim_id = arguments.get("claim_id")
                actor_ref = arguments.get("actor_ref")
                new_claim_id = arguments.get("new_claim_id")
                lease_ttl_ms = arguments.get("lease_ttl_ms", 28_800_000)
                if (
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
                if _write_binding_error(
                    request_id,
                    binding=binding,
                    actor_ref=actor_ref,
                    claim_id=claim_id,
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
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
            text = result.stdout or result.stderr or "resume failed"
            _reply(
                request_id,
                {
                    "content": [{"type": "text", "text": text}],
                    "isError": result.returncode != 0,
                },
            )
        elif request_id is not None:
            _error(request_id, -32601, f"method not found: {method}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
