"""Provider-neutral MCP adapter for bounded incremental code lookup."""

from __future__ import annotations

import json
import sys
from pathlib import Path

try:
    from .code_index import lookup_code_index
except ImportError:  # pragma: no cover - exercised by the direct plugin launcher
    package_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package_root))
    from continuity_plane.code_index import lookup_code_index


_ANNOTATIONS = {
    # Transparent derived-cache refreshes do not change project or external state.
    "readOnlyHint": True,
    "openWorldHint": False,
    "destructiveHint": False,
}


def _reply(request_id: object, result: dict) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}, ensure_ascii=False),
        flush=True,
    )


def _error(request_id: object, code: int, message: str) -> None:
    print(
        json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
            ensure_ascii=False,
        ),
        flush=True,
    )


def _tool() -> dict:
    return {
        "name": "continuity_context_lookup",
        "description": (
            "Return bounded hash-bound symbol and path references from an incremental Git-tracked "
            "code index. Its transparent cache never changes project State or source."
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
                "max_results": {"type": "integer", "minimum": 1, "maximum": 200},
                "max_output_bytes": {"type": "integer", "minimum": 1024, "maximum": 65536},
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
    if (
        not isinstance(root_value, str)
        or not root_value
        or not isinstance(query, str)
        or not query
        or (cache_path is not None and (not isinstance(cache_path, str) or not cache_path))
        or type(max_results) is not int
        or type(max_output_bytes) is not int
    ):
        _error(request_id, -32602, "lookup arguments are invalid")
        return
    try:
        root = Path(root_value).expanduser().resolve()
        result = lookup_code_index(
            root,
            query=query,
            cache_path=cache_path,
            max_results=max_results,
            max_output_bytes=max_output_bytes,
        )
    except (OSError, ValueError) as exc:
        _error(request_id, -32000, str(exc))
        return
    _reply(
        request_id,
        {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, sort_keys=True)}],
            "structuredContent": result,
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
        if method == "initialize":
            params = request.get("params")
            protocol = params.get("protocolVersion", "2025-06-18") if isinstance(params, dict) else "2025-06-18"
            _reply(
                request_id,
                {
                    "protocolVersion": protocol,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "continuity-search", "version": "0.1.0-alpha.12"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            _reply(request_id, {"tools": [_tool()]})
        elif method == "tools/call":
            params = request.get("params")
            if not isinstance(params, dict) or params.get("name") != "continuity_context_lookup":
                _error(request_id, -32602, "unknown tool")
                continue
            _call_lookup(request_id, params.get("arguments", {}))
        else:
            _error(request_id, -32601, "method not found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
