"""Provider-neutral MCP adapter for bounded incremental code lookup."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

try:
    from .code_index import lookup_code_index
    from .release_version import VERSION, runtime_version_issue
except ImportError:  # pragma: no cover - exercised by the direct plugin launcher
    package_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package_root))
    __package__ = "continuity_plane"
    from continuity_plane.code_index import lookup_code_index
    from continuity_plane.release_version import VERSION, runtime_version_issue


_ANNOTATIONS = {
    # Transparent derived-cache refreshes do not change project or external state.
    "readOnlyHint": True,
    "openWorldHint": False,
    "destructiveHint": False,
}


def _reply(request_id: object, result: dict) -> None:
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False
        ),
        flush=True,
    )


def _error(request_id: object, code: int, message: str) -> None:
    print(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": code, "message": message},
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def _tool() -> dict:
    return {
        "name": "continuity_context_lookup",
        "description": (
            "Return bounded hash-bound symbol and path references from an incremental Git-tracked "
            "code index. Its transparent cache never changes project State or source. "
            "An empty result is a normal miss; continue with bounded CLI or narrow search."
        ),
        "annotations": _ANNOTATIONS,
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["root", "query"],
            "properties": {
                "root": {"type": "string", "minLength": 1},
                "query": {"type": "string", "minLength": 1, "maxLength": 1024},
                "cache_path": {"type": "string", "minLength": 1},
                "format": {"enum": ["brief", "json"], "default": "brief"},
                "data_root": {"type": "string"},
                "bridge_config": {"type": "string", "minLength": 1, "description": "Opt-in SSH bridge config with an exact local_root/remote_root repository mapping; no native account data is forwarded"},
                "path_prefix": {"type": "string"},
                "extensions": {"type": "array", "items": {"type": "string"}},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                "max_output_bytes": {
                    "type": "integer",
                    "minimum": 1024,
                    "maximum": 65536,
                },
            },
        },
    }


def _call_lookup(request_id: object, arguments: object) -> None:
    if not isinstance(arguments, dict):
        _error(request_id, -32602, "arguments must be an object")
        return
    root_value = arguments.get("root")
    query = arguments.get("query")
    cache_path = arguments.get("cache_path")
    max_results = arguments.get("max_results", 20)
    max_output_bytes = arguments.get("max_output_bytes", 8192)
    format = arguments.get("format", "brief")
    bridge = arguments.get("bridge_config", os.environ.get("CONTINUITY_BRIDGE_CONFIG"))
    from .usage_meter import adaptive_defaults

    default_count, default_budget = (
        adaptive_defaults(
            root_value, data_root=arguments.get("data_root"), format=format
        )
        if isinstance(root_value, str) and not bridge
        else ((8, 2048) if bridge else (20, 8192))
    )
    if "max_results" not in arguments:
        max_results = default_count
    if "max_output_bytes" not in arguments:
        max_output_bytes = default_budget
    if (
        not isinstance(format, str)
        or format not in {"brief", "json"}
        or not isinstance(root_value, str)
        or not root_value
        or not isinstance(query, str)
        or not query
        or (
            cache_path is not None
            and (not isinstance(cache_path, str) or not cache_path)
        )
        or type(max_results) is not int
        or type(max_output_bytes) is not int
        or not 1 <= max_results <= 200
        or not 1024 <= max_output_bytes <= 65536
        or (bridge is not None and (not isinstance(bridge, str) or not bridge))
        or (arguments.get("path_prefix") is not None and not isinstance(arguments["path_prefix"], str))
        or (arguments.get("extensions") is not None and (not isinstance(arguments["extensions"], list) or not all(isinstance(v, str) for v in arguments["extensions"])))
    ):
        _error(request_id, -32602, "lookup arguments are invalid")
        return
    try:
        if bridge:
            from .remote_lookup import lookup

            _reply(request_id, lookup({**arguments, "max_results": max_results, "max_output_bytes": max_output_bytes}, bridge))
            return
        root = Path(root_value).expanduser().resolve()
        result = lookup_code_index(
            root,
            query=query,
            cache_path=cache_path,
            max_results=max_results,
            max_output_bytes=max_output_bytes,
            data_root=arguments.get("data_root"),
            record_usage=format == "json",
            path_prefix=arguments.get("path_prefix"),
            extensions=arguments.get("extensions"),
        )
    except (OSError, ValueError) as exc:
        _error(request_id, -32000, str(exc))
        return
    if format == "brief":
        text = (
            "\n".join(
                f"{m['path']}:{m['line']} {m['kind']} {m['name']}"
                for m in result["matches"]
            )
            or "No matches; use a narrower query or scoped rg."
        )
        if result.get("truncated"):
            text += "\n[more results; narrow query or increase result/budget limit]"
        text = text.encode()[:max_output_bytes].decode("utf-8", "ignore")
        try:
            from .usage_meter import record

            record(
                root,
                command="lookup:mcp-brief",
                response_bytes=len(text.encode()),
                estimated_grep_bytes=sum(
                    len(m["path"].encode()) + len(m["name"].encode()) + 32
                    for m in result["matches"]
                ),
                data_root=arguments.get("data_root"),
                cache_status=result.get("cache_status"),
            )
        except (OSError, ValueError):
            pass
        _reply(
            request_id, {"content": [{"type": "text", "text": text}], "isError": False}
        )
        return
    _reply(
        request_id,
        {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False, sort_keys=True),
                }
            ],
            "structuredContent": result,
            # Keep guidance outside the hashed, byte-bounded receipt.
            "_meta": {
                "continuity": {
                    "lookup_status": "hit" if result["matches"] else "empty",
                    "fallback": "none" if result["matches"] else "narrow-search",
                }
            },
            "isError": False,
        },
    )


def main() -> int:
    for line in sys.stdin:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(request, dict):
            continue
        request_id = request.get("id")
        method = request.get("method")
        if request_id is not None:
            issue = runtime_version_issue()
            if issue:
                _error(request_id, -32000, issue)
                continue
        if method == "initialize":
            params = request.get("params")
            protocol = (
                params.get("protocolVersion", "2025-06-18")
                if isinstance(params, dict)
                else "2025-06-18"
            )
            _reply(
                request_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "continuity-search", "version": VERSION},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _reply(request_id, {"tools": [_tool()]})
        elif method == "tools/call":
            params = request.get("params")
            if (
                not isinstance(params, dict)
                or params.get("name") != "continuity_context_lookup"
            ):
                _error(request_id, -32602, "unknown tool")
                continue
            _call_lookup(request_id, params.get("arguments", {}))
        else:
            _error(request_id, -32601, "method not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
