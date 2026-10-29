"""Alpha36 stable MCP proxy: tool list stays fixed while the remote drops."""

from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from continuity_plane.mcp_proxy import McpProxy, parse_proxy_target

REPO = Path(__file__).resolve().parents[1]

FAKE_REMOTE = textwrap.dedent(
    """
    import json, os, sys
    state = os.environ["FAKE_STATE"]
    version = open(os.path.join(state, "version")).read().strip()
    tools = [
        {"name": "read_tool", "annotations": {"readOnlyHint": True}, "inputSchema": {"type": "object"}},
        {"name": "write_tool", "annotations": {"readOnlyHint": False}, "inputSchema": {"type": "object"}},
    ]
    if version != "1":
        tools.append({"name": "added_later", "inputSchema": {"type": "object"}})
    for line in sys.stdin:
        request = json.loads(line)
        method, rid = request.get("method"), request.get("id")
        if rid is None:
            continue
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake", "version": version}}
        elif method == "tools/list":
            result = {"tools": tools}
        else:
            name = request["params"]["name"]
            with open(os.path.join(state, "executed"), "a") as log:
                log.write(name + "\\n")
            crash = os.path.join(state, "crash")
            if os.path.exists(crash):
                os.remove(crash)
                os._exit(1)
            result = {"content": [{"type": "text", "text": name + " ok"}], "isError": False}
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
    """
)


class _KillingStdin:
    """Kill the remote just before a tools/call reaches it (an in-flight drop)."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self.armed = True
        self._stdin = process.stdin

    def write(self, data: str) -> int:
        if self.armed and '"tools/call"' in data:
            self.armed = False
            self.process.kill()
            self.process.wait()
        return self._stdin.write(data)

    def flush(self) -> None:
        self._stdin.flush()

    def close(self) -> None:
        self._stdin.close()


class ProxyCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.cache_root = self.tmp / "local"
        self.remote_data = self.tmp / "remote"
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "version").write_text("1")
        self.processes: list[subprocess.Popen[str]] = []
        self.arm_kill_on_call = False
        self.proxies: list[McpProxy] = []

    def _env(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CONTINUITY_")
        }
        env.update(
            CONTINUITY_DATA_ROOT=str(self.remote_data),
            FAKE_STATE=str(self.state),
            PYTHONPATH=str(REPO),
            TMPDIR=str(self.tmp),
        )
        return env

    def _spawn(self, argv: list[str]) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            argv,
            cwd=REPO,
            env=self._env(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.processes.append(process)
        if self.arm_kill_on_call:
            self.arm_kill_on_call = False
            process.stdin = _KillingStdin(process)  # type: ignore[assignment]
        return process

    def real_remote(self) -> subprocess.Popen[str]:
        return self._spawn([sys.executable, "-m", "continuity_plane.codex_mcp_server"])

    def fake_remote(self) -> subprocess.Popen[str]:
        script = self.tmp / "fake_remote.py"
        script.write_text(FAKE_REMOTE)
        return self._spawn([sys.executable, str(script)])

    def kill_remote(self) -> None:
        process = self.processes[-1]
        process.kill()
        process.wait()

    def start(self, transport, label: str = "test-host", **kwargs) -> "Harness":
        proxy = McpProxy(
            transport,
            server="continuity-mcp",
            label=label,
            data_root=self.cache_root,
            connect_timeout=20,
            backoff_start=0.05,
            backoff_max=0.2,
            **kwargs,
        )
        harness = Harness(proxy)
        self.addCleanup(harness.close)
        return harness

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout):
                with contextlib.suppress(Exception):
                    stream.close()


class Harness:
    """Drive a proxy like a host: lines in, lines out, one request at a time."""

    def __init__(self, proxy: McpProxy) -> None:
        self.proxy = proxy
        self.inbox: queue.Queue[str | None] = queue.Queue()
        self.outbox: queue.Queue[str] = queue.Queue()
        self._next_id = 0
        self.thread = threading.Thread(
            target=proxy.serve,
            args=(iter(self.inbox.get, None), self.outbox.put),
            daemon=True,
        )
        self.thread.start()

    def ask(self, method: str, params: dict | None = None, timeout: float = 60) -> str:
        self._next_id += 1
        message: dict = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.inbox.put(json.dumps(message))
        return self.outbox.get(timeout=timeout)

    def result(self, method: str, params: dict | None = None) -> dict:
        return json.loads(self.ask(method, params))["result"]

    def initialize(self) -> dict:
        return self.result("initialize", {"protocolVersion": "2025-06-18"})

    def tools_raw(self) -> str:
        return json.dumps(json.loads(self.ask("tools/list"))["result"], sort_keys=True)

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return self.result("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self) -> None:
        self.inbox.put(None)
        self.thread.join(timeout=10)


class ParseTargetTests(unittest.TestCase):
    def test_host_only_uses_default_command(self) -> None:
        self.assertEqual(
            parse_proxy_target("ssh://state-host", "continuity-mcp"),
            ("state-host", "continuity-mcp"),
        )

    def test_path_and_env_prefix(self) -> None:
        self.assertEqual(
            parse_proxy_target("ssh://me@box/opt/bin/continuity-mcp", "continuity-mcp"),
            ("me@box", "/opt/bin/continuity-mcp"),
        )
        self.assertEqual(
            parse_proxy_target(
                "ssh://h/env%20CONTINUITY_DATA_ROOT=/var/tmp/x%20continuity-mcp",
                "continuity-mcp",
            )[1],
            "env CONTINUITY_DATA_ROOT=/var/tmp/x continuity-mcp",
        )
        self.assertEqual(
            parse_proxy_target("ssh://h/~/.local/bin/continuity-mcp", "continuity-mcp")[1],
            "~/.local/bin/continuity-mcp",
        )

    def test_rejects_other_programs_and_schemes(self) -> None:
        for url in (
            "http://h/continuity-mcp",
            "ssh://h/bin/sh",
            "ssh://h/continuity-mcp%3Brm%20-rf%20x",
            "ssh://h/continuity-search-mcp",
            "ssh://",
            "ssh://h:2222",
            "ssh://-oProxyCommand=x/continuity-mcp",
            "",
        ):
            with self.assertRaises(ValueError, msg=url):
                parse_proxy_target(url, "continuity-mcp")

    def test_search_server_default(self) -> None:
        self.assertEqual(
            parse_proxy_target("ssh://h", "continuity-search-mcp")[1],
            "continuity-search-mcp",
        )


class RealRemoteTests(ProxyCase):
    def test_tools_list_identical_across_three_kills(self) -> None:
        harness = self.start(self.real_remote)
        initialized = harness.initialize()
        first = harness.tools_raw()
        self.assertEqual(initialized["serverInfo"]["name"], "continuity")
        self.assertIn("continuity_help", first)
        for _ in range(3):
            self.kill_remote()
            result = harness.call("continuity_help")
            self.assertFalse(result.get("isError"), result)
            self.assertEqual(harness.tools_raw(), first)
        self.assertEqual(harness.proxy.reconnects, 4)
        self.assertEqual(len(self.processes), 4)

    def test_read_only_call_is_retried_after_in_flight_kill(self) -> None:
        harness = self.start(self.real_remote)
        harness.initialize()
        harness.proxy.session.process.stdin = _KillingStdin(harness.proxy.session.process)
        result = harness.call("continuity_help")
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(len(self.processes), 2)

    def test_second_session_reads_tool_list_from_disk_cache(self) -> None:
        first = self.start(self.real_remote)
        first.initialize()
        listed = first.tools_raw()
        self.assertEqual(len(list((self.cache_root / "mcp-proxy-cache").glob("*.json"))), 1)
        first.close()

        def unreachable():
            raise OSError("no route")

        second = self.start(unreachable, reconnect_wait=0.3)
        self.assertEqual(second.initialize()["serverInfo"]["name"], "continuity")
        self.assertEqual(second.tools_raw(), listed)
        unsent = second.call("continuity_help")
        self.assertTrue(unsent["isError"])
        self.assertEqual(unsent["structuredContent"]["failed_gate"], "transport_unavailable")


class ScriptedRemoteTests(ProxyCase):
    def executed(self) -> list[str]:
        path = self.state / "executed"
        return path.read_text().split() if path.exists() else []

    def test_write_call_during_drop_is_not_repeated(self) -> None:
        harness = self.start(self.fake_remote)
        harness.initialize()
        (self.state / "crash").write_text("")
        result = harness.call("write_tool", {"title": "x"})
        self.assertTrue(result["isError"])
        value = result["structuredContent"]
        self.assertEqual(value["failed_gate"], "transport_interrupted")
        self.assertEqual(value["executed"], "unknown")
        self.assertIn("read_tool", value["message"])
        self.assertEqual(self.executed(), ["write_tool"])
        # The proxy recovers for the next call without replaying the lost one.
        self.assertFalse(harness.call("read_tool")["isError"])
        self.assertEqual(self.executed(), ["write_tool", "read_tool"])

    def test_write_call_with_operation_id_is_retried(self) -> None:
        harness = self.start(self.fake_remote)
        harness.initialize()
        (self.state / "crash").write_text("")
        result = harness.call("write_tool", {"operation_id": "op-1"})
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(self.executed(), ["write_tool", "write_tool"])

    def test_read_only_call_is_retried_once(self) -> None:
        harness = self.start(self.fake_remote)
        harness.initialize()
        (self.state / "crash").write_text("")
        self.assertFalse(harness.call("read_tool")["isError"])
        self.assertEqual(self.executed(), ["read_tool", "read_tool"])

    def test_read_only_call_gives_up_after_second_drop(self) -> None:
        harness = self.start(self.fake_remote)
        harness.initialize()
        original = harness.proxy._connect_once

        def crash_again():
            session = original()
            (self.state / "crash").write_text("")
            return session

        (self.state / "crash").write_text("")
        with mock.patch.object(harness.proxy, "_connect_once", crash_again):
            result = harness.call("read_tool")
        self.assertEqual(result["structuredContent"]["failed_gate"], "transport_interrupted")
        self.assertEqual(self.executed(), ["read_tool", "read_tool"])

    def test_version_change_keeps_cached_list_and_warns_once(self) -> None:
        harness = self.start(self.fake_remote)
        harness.initialize()
        first = harness.tools_raw()
        self.assertNotIn("added_later", first)
        (self.state / "version").write_text("2")
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for _ in range(2):
                self.kill_remote()
                self.assertFalse(harness.call("read_tool")["isError"])
                self.assertEqual(harness.tools_raw(), first)
        self.assertEqual(stderr.getvalue().count("now reports version 2"), 1)
        self.assertEqual(harness.initialize()["serverInfo"]["version"], "1")

    def test_cache_is_keyed_by_remote_version(self) -> None:
        first = self.start(self.fake_remote)
        first.initialize()
        old = first.tools_raw()
        first.close()
        (self.state / "version").write_text("2")
        second = self.start(self.fake_remote)
        second.initialize()
        self.assertNotEqual(second.tools_raw(), old)
        self.assertEqual(len(list((self.cache_root / "mcp-proxy-cache").glob("*.json"))), 2)

    def test_call_waits_for_remote_to_return(self) -> None:
        attempts = {"n": 0}

        def flaky():
            attempts["n"] += 1
            if attempts["n"] == 2:
                raise OSError("flaky")
            return self.fake_remote()

        harness = self.start(flaky, reconnect_wait=10)
        harness.initialize()
        self.kill_remote()
        self.assertFalse(harness.call("read_tool")["isError"])
        self.assertGreaterEqual(attempts["n"], 3)

    def test_protocol_version_is_echoed_and_unknown_method_rejected(self) -> None:
        harness = self.start(self.fake_remote)
        result = harness.result("initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(json.loads(harness.ask("resources/list"))["error"]["code"], -32601)
        self.assertEqual(harness.result("ping"), {})


class SshCommandTests(ProxyCase):
    def test_cli_entry_runs_through_ssh_command(self) -> None:
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        log = self.tmp / "ssh-args"
        ssh = bin_dir / "ssh"
        ssh.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$SSH_ARGS_LOG"\n'
            'while [ $# -gt 0 ]; do case "$1" in -T) shift;; -o) shift 2;; *) break;; esac; done\n'
            'shift\nexec sh -c "$*"\n'
        )
        remote = bin_dir / "continuity-mcp"
        remote.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" -m continuity_plane.codex_mcp_server\n'
        )
        for script in (ssh, remote):
            script.chmod(script.stat().st_mode | stat.S_IEXEC)
        env = self._env()
        env.update(
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            HOME=str(self.tmp),
            SSH_ARGS_LOG=str(log),
            CONTINUITY_DATA_ROOT=str(self.cache_root),
        )
        requests = "\n".join(
            json.dumps(item)
            for item in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "continuity_help", "arguments": {}}},
            )
        )
        completed = subprocess.run(
            [sys.executable, "-m", "continuity_plane.codex_mcp_server", "--proxy", "ssh://fakehost"],
            cwd=REPO,
            env=env,
            input=requests + "\n",
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3])
        self.assertFalse(replies[2]["result"].get("isError"))
        arguments = log.read_text().split()
        for option in (
            "BatchMode=yes",
            "ServerAliveInterval=15",
            "ServerAliveCountMax=3",
            "ControlMaster=auto",
            "ControlPersist=60",
        ):
            self.assertIn(option, arguments)
        self.assertEqual(arguments[-2:], ["fakehost", "continuity-mcp"])
        self.assertIn("-T", arguments)

    def test_bad_target_exits_with_usage_error(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "continuity_plane.codex_mcp_server", "--proxy", "http://x"],
            cwd=REPO,
            env=self._env(),
            input="",
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--proxy expects ssh://", completed.stderr)


if __name__ == "__main__":
    unittest.main()
