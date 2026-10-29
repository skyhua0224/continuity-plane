"""Stable stdio MCP proxy for ``continuity-mcp --proxy ssh://HOST``.

The host launches this process locally. ``initialize`` and ``tools/list`` are
answered from a cache so the host's deferred tool list stays byte-identical for
the whole session (a changed list invalidates the host's prompt cache).
``tools/call`` is forwarded to the real MCP server on the remote over SSH; if
that connection drops, the proxy reconnects with backoff and, when safe,
retries the call once.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import unquote, urlparse

CONNECT_ATTEMPT_TIMEOUT = 20.0
RECONNECT_WAIT = 30.0
CALL_TIMEOUT = 300.0
BACKOFF_START = 0.5
BACKOFF_MAX = 10.0
_SERVERS = {"continuity-mcp", "continuity-search-mcp"}

Transport = Callable[[], "subprocess.Popen[str]"]


class RemoteUnavailable(Exception):
    """No remote session could be established within the wait budget."""


class RemoteInterrupted(Exception):
    """The remote session ended while a request was in flight."""


def parse_proxy_target(url: str, server: str) -> tuple[str, str]:
    """Return ``(host, remote_command)`` for ``ssh://HOST[/path]``.

    The path may be ``/env NAME=value /path/to/<server>`` (percent-encoded); the last word must
    still name a Continuity MCP server so the proxy cannot run arbitrary programs.
    """
    parsed = urlparse(url)
    # Use the netloc as written (urlparse lowercases hostname); ssh matches aliases exactly.
    host = parsed.netloc
    if (
        parsed.scheme != "ssh"
        or not re.fullmatch(r"([\w.-]+@)?[\w][\w.-]*", host)
    ):
        raise ValueError(
            "--proxy expects ssh://[user@]HOST[/remote/path/to/" + server + "]; "
            "put ports and jump hosts in ~/.ssh/config"
        )
    path = unquote(parsed.path or "")
    if path in ("", "/"):
        return host, server
    if any(c in path for c in "\n\r\x00"):
        raise ValueError("remote path must be a single line")
    if path.startswith("/~/") or path.startswith("/env "):
        path = path[1:]
    words = shlex.split(path)
    if not words or Path(words[-1]).name != server:
        raise ValueError("remote path must end in " + server)
    return host, " ".join(_quote_word(word) for word in words)


def _quote_word(word: str) -> str:
    # Keep ~/ expandable by the remote shell; quote everything else.
    if word.startswith("~/"):
        return "~/" + shlex.quote(word[2:])
    return shlex.quote(word)


def ssh_transport(host: str, remote_command: str) -> Transport:
    """Build a transport that runs the remote MCP server over ``ssh``."""

    def start() -> subprocess.Popen[str]:
        control = Path.home() / ".cache/continuity-ssh"
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        command = [
            "ssh",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=60",
            "-o",
            "ControlPath=" + str(control / "%C"),
            host,
            remote_command,
        ]
        return subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    return start


class _Session:
    """One live remote process with a reader thread feeding a response queue."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.responses: queue.Queue[dict | None] = queue.Queue()
        self._next_id = 0
        self.eof = False
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        try:
            for line in self.process.stdout:  # type: ignore[union-attr]
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self.responses.put(message)
        except (OSError, ValueError):
            pass
        self.eof = True
        self.responses.put(None)

    def send(self, message: dict) -> None:
        try:
            self.process.stdin.write(json.dumps(message) + "\n")  # type: ignore[union-attr]
            self.process.stdin.flush()  # type: ignore[union-attr]
        except (OSError, ValueError) as error:
            raise RemoteInterrupted(str(error)) from error

    def request(self, method: str, params: dict | None, timeout: float) -> dict:
        self._next_id += 1
        request_id = self._next_id
        message: dict = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RemoteInterrupted("remote response timed out")
            try:
                response = self.responses.get(timeout=remaining)
            except queue.Empty:
                raise RemoteInterrupted("remote response timed out") from None
            if response is None:
                self.responses.put(None)
                raise RemoteInterrupted("remote connection closed")
            if response.get("id") == request_id:
                return response

    def alive(self) -> bool:
        return self.process.poll() is None and not self.eof

    def close(self) -> None:
        for stream in (self.process.stdin, self.process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        if self.process.poll() is None:
            self.process.kill()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


class McpProxy:
    def __init__(
        self,
        transport: Transport,
        *,
        server: str,
        label: str,
        data_root: Path | None = None,
        connect_timeout: float = CONNECT_ATTEMPT_TIMEOUT,
        reconnect_wait: float = RECONNECT_WAIT,
        call_timeout: float = CALL_TIMEOUT,
        backoff_start: float = BACKOFF_START,
        backoff_max: float = BACKOFF_MAX,
    ) -> None:
        self.transport = transport
        self.server = server
        self.label = label
        self.data_root = data_root
        self.connect_timeout = connect_timeout
        self.reconnect_wait = reconnect_wait
        self.call_timeout = call_timeout
        self.backoff_start = backoff_start
        self.backoff_max = backoff_max
        self.session: _Session | None = None
        self.initialize_result: dict | None = None
        self.tools: list[dict] | None = None
        self.read_only: set[str] = set()
        self.reconnects = 0
        self._warned_versions: set[str] = set()
        self._client_params: dict = {"protocolVersion": "2025-06-18"}

    # -- cache -----------------------------------------------------------

    def _cache_dir(self) -> Path:
        root = self.data_root
        if root is None:
            from .collaboration_registry import default_data_root

            root = default_data_root()
        return root / "mcp-proxy-cache"

    def _cache_path(self, version: str) -> Path:
        key = hashlib.sha256(f"{self.label}\0{self.server}\0{version}".encode()).hexdigest()
        return self._cache_dir() / f"{key[:24]}.json"

    def _load_cache(self, version: str | None) -> dict | None:
        try:
            if version is not None:
                path = self._cache_path(version)
            else:
                # Remote unreachable at start: newest cache for this host/server.
                candidates = []
                for path in self._cache_dir().glob("*.json"):
                    entry = json.loads(path.read_text(encoding="utf-8"))
                    if entry.get("label") == self.label and entry.get("server") == self.server:
                        candidates.append((path.stat().st_mtime, entry))
                return max(candidates, key=lambda item: item[0])[1] if candidates else None
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (
            isinstance(entry, dict)
            and entry.get("label") == self.label
            and entry.get("server") == self.server
            and isinstance(entry.get("tools"), list)
            and isinstance(entry.get("initialize"), dict)
        ):
            return entry
        return None

    def _store_cache(self, version: str) -> None:
        path = self._cache_path(version)
        entry = {
            "label": self.label,
            "server": self.server,
            "version": version,
            "initialize": self.initialize_result,
            "tools": self.tools,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(entry), encoding="utf-8")
            os.replace(temporary, path)
        except OSError:
            pass

    def _adopt(self, entry: dict) -> None:
        self.initialize_result = entry["initialize"]
        self.tools = entry["tools"]
        self.read_only = {
            tool["name"]
            for tool in self.tools
            if isinstance(tool, dict)
            and isinstance(tool.get("annotations"), dict)
            and tool["annotations"].get("readOnlyHint") is True
        }

    # -- remote session --------------------------------------------------

    def _connect_once(self) -> _Session:
        session = _Session(self.transport())
        try:
            response = session.request(
                "initialize", dict(self._client_params), self.connect_timeout
            )
            result = response.get("result")
            if not isinstance(result, dict):
                raise RemoteInterrupted("remote initialize failed")
            session.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            version = str(result.get("serverInfo", {}).get("version", ""))
            if self.initialize_result is None:
                cached = self._load_cache(version)
                if cached is None:
                    tools = session.request("tools/list", None, self.connect_timeout)
                    listed = tools.get("result", {}).get("tools")
                    if not isinstance(listed, list):
                        raise RemoteInterrupted("remote tools/list failed")
                    cached = {"initialize": result, "tools": listed}
                    self.initialize_result, self.tools = result, listed
                    self._store_cache(version)
                self._adopt(cached)
            else:
                known = str(self.initialize_result.get("serverInfo", {}).get("version", ""))
                if version != known and version not in self._warned_versions:
                    self._warned_versions.add(version)
                    print(
                        f"continuity-mcp proxy: remote {self.label} now reports version "
                        f"{version} (session started with {known}); keeping the cached "
                        "tool list for this session, restart the host to pick up changes",
                        file=sys.stderr,
                        flush=True,
                    )
            return session
        except BaseException:
            session.close()
            raise

    def _ensure_session(self, wait: float) -> _Session:
        if self.session is not None and self.session.alive():
            return self.session
        if self.session is not None:
            self.session.close()
            self.session = None
        deadline = time.monotonic() + wait
        delay = 0.0
        while True:
            try:
                self.session = self._connect_once()
                self.reconnects += 1
                return self.session
            except (RemoteInterrupted, OSError):
                pass
            delay = self.backoff_start if delay == 0 else min(delay * 2, self.backoff_max)
            if time.monotonic() + delay >= deadline:
                raise RemoteUnavailable("remote unreachable")
            time.sleep(delay)

    def _drop_session(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    # -- request handling ------------------------------------------------

    def handle(self, request: dict) -> dict | None:
        method = request.get("method")
        request_id = request.get("id")
        if request_id is None:
            return None
        if method == "initialize":
            return self._initialize(request_id, request.get("params"))
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            if self.tools is None:
                try:
                    self._ensure_session(self.reconnect_wait)
                except RemoteUnavailable:
                    return self._rpc_error(request_id, -32000, "remote unavailable")
            return self._result(request_id, {"tools": self.tools})
        if method == "tools/call":
            return self._call(request_id, request.get("params"))
        return self._rpc_error(request_id, -32601, f"method not found: {method}")

    def _initialize(self, request_id: object, params: object) -> dict:
        if isinstance(params, dict):
            self._client_params = params
        if self.initialize_result is None:
            try:
                self._ensure_session(self.reconnect_wait)
            except RemoteUnavailable:
                cached = self._load_cache(None)
                if cached is None:
                    return self._rpc_error(request_id, -32000, "remote unavailable")
                self._adopt(cached)
        result = dict(self.initialize_result or {})
        if isinstance(params, dict) and "protocolVersion" in params:
            result["protocolVersion"] = params["protocolVersion"]
        return self._result(request_id, result)

    def _call(self, request_id: object, params: object) -> dict:
        if not isinstance(params, dict):
            return self._rpc_error(request_id, -32602, "tools/call params must be an object")
        name = params.get("name")
        arguments = params.get("arguments")
        retryable = name in self.read_only or (
            isinstance(arguments, dict) and bool(arguments.get("operation_id"))
        )
        attempts = 2 if retryable else 1
        for attempt in range(attempts):
            try:
                session = self._ensure_session(self.reconnect_wait)
            except RemoteUnavailable:
                return self._tool_error(
                    request_id,
                    "The Continuity MCP remote did not come back within "
                    f"{self.reconnect_wait:.0f}s; this call was not sent. Retry it later.",
                    "transport_unavailable",
                )
            try:
                response = session.request("tools/call", params, self.call_timeout)
            except RemoteInterrupted:
                self._drop_session()
                continue
            response["id"] = request_id
            return response
        check = self._check_tool()
        if retryable:
            text = (
                "The connection to the Continuity MCP remote dropped twice during this "
                "call, so it may or may not have run. Check the result with "
                f"{check} before repeating it."
            )
        else:
            text = (
                "The connection to the Continuity MCP remote dropped during this call, "
                "so it may or may not have run. Do not repeat it blindly; check the "
                f"result with {check}, or repeat it with an operation_id if the tool "
                "accepts one."
            )
        return self._tool_error(request_id, text, "transport_interrupted")

    def _check_tool(self) -> str:
        for preferred in ("continuity_task_show", "continuity_inspect", "continuity_help"):
            if preferred in self.read_only:
                return preferred
        return sorted(self.read_only)[0] if self.read_only else "a read-only tool"

    @staticmethod
    def _result(request_id: object, result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _rpc_error(request_id: object, code: int, message: str) -> dict:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def _tool_error(self, request_id: object, text: str, gate: str) -> dict:
        value = {
            "status": "error",
            "failed_gate": gate,
            "message": text,
            "executed": "unknown" if gate == "transport_interrupted" else False,
        }
        return self._result(
            request_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(value, ensure_ascii=False, sort_keys=True)}
                ],
                "structuredContent": value,
                "isError": True,
            },
        )

    def serve(self, lines: Iterable[str], write: Callable[[str], None]) -> int:
        try:
            for line in lines:
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(request, dict):
                    write(json.dumps(self._rpc_error(None, -32600, "request must be an object")))
                    continue
                response = self.handle(request)
                if response is not None:
                    write(json.dumps(response))
        finally:
            self._drop_session()
        return 0


def proxy_main(server: str, argv: list[str]) -> int:
    """Run the proxy for ``server`` from the ``--proxy URL`` entry arguments."""
    url = ""
    for index, argument in enumerate(argv):
        if argument == "--proxy" and index + 1 < len(argv):
            url = argv[index + 1]
        elif argument.startswith("--proxy="):
            url = argument.split("=", 1)[1]
    try:
        host, remote_command = parse_proxy_target(url, server)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    proxy = McpProxy(
        ssh_transport(host, remote_command),
        server=server,
        label=re.sub(r"\s+", " ", f"ssh://{host} {remote_command}"),
        call_timeout=float(os.environ.get("CONTINUITY_MCP_PROXY_CALL_TIMEOUT", CALL_TIMEOUT)),
    )

    def write(text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    return proxy.serve(sys.stdin, write)
