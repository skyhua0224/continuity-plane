"""Read-only code lookup through an explicitly selected exact-root SSH mapping."""

import json
from pathlib import Path

from .remote_cli import forward


def lookup(arguments, config_file):
    with Path(config_file).expanduser().open("rb") as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError("bridge config exceeds 16 KiB")
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError("bridge config must be an object")
    root = Path(arguments["root"]).expanduser()
    if not root.is_absolute():
        raise ValueError("lookup root must be absolute")
    mappings = config.get("mappings", [config])
    if not isinstance(mappings, list):
        raise ValueError("bridge mappings must be a list")
    matches = [m for m in mappings if isinstance(m, dict) and isinstance(m.get("local_root"), str) and Path(m["local_root"]).is_absolute() and Path(m["local_root"]).expanduser().resolve() == root.resolve()]
    if len(matches) != 1:
        raise ValueError("remote lookup requires one explicit exact-root mapping; local fallback is disabled")
    route = {**config, **matches[0]}
    if not isinstance(route.get("host"), str) or not isinstance(route.get("remote_root"), str):
        raise ValueError("remote lookup mapping requires host and absolute remote_root")
    if arguments.get("cache_path") is not None or arguments.get("data_root") is not None:
        raise ValueError("remote lookup does not forward local cache_path/data_root; configure remote_data_root in the mapping")
    argv = ["context", "lookup", "--query", arguments["query"], "--format", arguments.get("format", "brief"), "--max-results", str(arguments.get("max_results", 8)), "--max-output-bytes", str(arguments.get("max_output_bytes", 2048))]
    if route.get("remote_data_root"):
        remote_data = route["remote_data_root"]
        if not isinstance(remote_data, str) or not Path(remote_data).is_absolute():
            raise ValueError("remote_data_root must be an absolute owning-host path")
        argv += ["--data-root", remote_data]
    if arguments.get("path_prefix"):
        argv += ["--path-prefix", arguments["path_prefix"]]
    for extension in arguments.get("extensions", []):
        argv += ["--extension", extension]
    timeout = route.get("lookup_timeout_seconds", 30)
    if type(timeout) not in {int, float} or not 0 < timeout <= 60:
        raise ValueError("lookup_timeout_seconds must be between 0 and 60")
    result = forward(argv, host=route.get("host", ""), root=route.get("remote_root"), endpoint=route.get("remote_cli_endpoint", "continuity-remote-cli"), timeout=timeout)
    if result["return_code"]:
        raise ValueError("mapped remote lookup failed; check the owning host's registered root and runtime")
    text = result.get("stdout")
    if not isinstance(text, str) or len(text.encode()) > arguments.get("max_output_bytes", 2048):
        raise ValueError("remote lookup returned an invalid or over-budget receipt")
    response = {"content": [{"type": "text", "text": text}], "isError": False, "_meta": {"continuity": {"execution": "mapped-remote", "local_fallback": False}}}
    if arguments.get("format", "brief") == "json":
        value = json.loads(text)
        if not isinstance(value, dict) or not isinstance(value.get("matches"), list):
            raise ValueError("invalid remote lookup JSON receipt")
        response["structuredContent"] = value
    return response
