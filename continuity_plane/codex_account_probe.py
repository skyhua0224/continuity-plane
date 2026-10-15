"""Read-only app-server quota metadata, using the existing CLI login."""

import json
import queue
import subprocess
import threading
import time


def read_limits(executable="codex", timeout=15, *, expected_origin=None):
    from .runtime_origin import current, require_local

    origin = current("codex")
    if expected_origin is not None:
        require_local(expected_origin, "codex")
    process = subprocess.Popen(
        [executable, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    messages = queue.Queue(maxsize=128)

    def reader():
        for _ in range(128):
            line = process.stdout.readline(65537)
            if not line:
                return
            if len(line) > 65536:
                messages.put({"probe_error": "account response exceeds budget"})
                return
            try:
                messages.put(json.loads(line))
            except (ValueError, UnicodeError):
                continue

    threading.Thread(target=reader, daemon=True).start()
    deadline = time.monotonic() + timeout

    def send(value):
        process.stdin.write(json.dumps(value).encode() + b"\n")
        process.stdin.flush()

    def receive(identity):
        while time.monotonic() < deadline:
            try:
                value = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if not isinstance(value, dict):
                continue
            if value.get("probe_error"):
                raise ValueError(value["probe_error"])
            if value.get("id") == identity:
                if "error" in value:
                    raise ValueError(
                        "native quota unavailable; existing login/provider does not expose subscription limits"
                    )
                return value.get("result", {})
        raise ValueError("native account quota probe timed out")

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "continuity-quota", "version": "1"},
                    "capabilities": {},
                },
            }
        )
        receive(0)
        send({"jsonrpc": "2.0", "method": "initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "account/rateLimits/read",
                "params": {},
            }
        )
        result = receive(1)
        return {
            k: result[k] for k in ("rateLimits", "rateLimitsByLimitId") if k in result
        } | {"collection_origin": origin}
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()
