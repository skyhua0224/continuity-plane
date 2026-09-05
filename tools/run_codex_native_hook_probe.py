"""Run an isolated, ephemeral Codex task through the host's real compact API."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def scoped_overrides(config, hook_config):
    overrides = {"hooks": hook_config, "features.hooks": True}
    for plugin in config.get("plugins", {}):
        overrides[f"plugins.{plugin}.enabled"] = False
    for server in config.get("mcp_servers", {}):
        overrides[f"mcp_servers.{server}.enabled"] = False
    return overrides


def summarize(events):
    result = {"native_compaction_observed": False, "hook_counts": {}, "lookup_calls": 0,
              "hook_context_bytes": 0, "tool_calls": 0, "usage": None}
    for event in events:
        method = event.get("method")
        params = event.get("params", {})
        if method == "hook/completed":
            run = params.get("run", {})
            key = f"{run.get('eventName')}:{run.get('status')}"
            result["hook_counts"][key] = result["hook_counts"].get(key, 0) + 1
            result["hook_context_bytes"] += sum(len(e.get("text", "").encode()) for e in run.get("entries", []) if e.get("kind") == "context")
        if method == "thread/compacted":
            result["native_compaction_observed"] = True
        if method == "thread/tokenUsage/updated":
            result["usage"] = params.get("tokenUsage")
        if method == "item/completed":
            item = params.get("item", {})
            if item.get("type") == "contextCompaction":
                result["native_compaction_observed"] = True
            if item.get("type") == "commandExecution":
                result["tool_calls"] += 1
                command = item.get("command", "")
                result["lookup_calls"] += bool(re.search(r"\bcontinuity\s+context\s+(?:lookup|search)\b", command))
    return result


class Host:
    def __init__(self, environment, project=None):
        self.process = subprocess.Popen(
            ["codex", "--dangerously-bypass-hook-trust", "app-server", "--stdio"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=environment, cwd=ROOT,
        )
        self.messages = queue.Queue(maxsize=4096)
        self.events = []
        self.sequence = 0
        self.rpc_error = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                if len(line) <= 4 * 1024 * 1024:
                    self.messages.put(json.loads(line))
        finally:
            self.messages.put(None)

    def receive(self, timeout):
        if timeout <= 0:
            raise RuntimeError("host_timeout")
        try:
            event = self.messages.get(timeout=max(0.01, timeout))
        except queue.Empty:
            raise RuntimeError("host_timeout") from None
        if event is None:
            raise RuntimeError("host_transport_closed")
        if "method" in event:
            if "id" in event:
                # Unexpected host approval requests are refused, not automatically granted.
                self.send({"id": event["id"], "error": {"code": -32601, "message": "unsupported probe request"}})
            else:
                self.events.append(event)
        return event

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def call(self, method, params, timeout=60):
        self.sequence += 1
        request_id = self.sequence
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            event = self.receive(deadline - time.monotonic())
            if event.get("id") == request_id:
                if "error" in event:
                    error = event["error"]
                    self.rpc_error = error
                    raise RuntimeError(f"host_rpc_{method.replace('/', '_')}_{error.get('code')}")
                return event["result"]

    def until(self, predicate, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            event = self.receive(deadline - time.monotonic())
            if predicate(event):
                return event

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    config_path = Path.home() / ".codex/config.toml"
    original_config = config_path.read_bytes()
    config = tomllib.loads(original_config.decode())
    receipt = {"observed_at": datetime.now(UTC).isoformat(), "status": "failed",
               "measurement_source": "codex-app-server-native", "ephemeral": True,
               "business_state_writes": 0, "installed_plugin_acceptance": False,
               "model": config.get("model"), "context_window": config.get("model_context_window"),
               "config_unchanged": False, "phases": []}
    host = None
    probe_parent = ROOT / ".continuity/local"
    probe_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-probe-", dir=probe_parent) as directory:
        temp = Path(directory)
        project = temp / "project"
        project.mkdir()
        source = ROOT / "integrations/codex/continuity-plane/scripts/continuity-hook.py"
        shutil.copy2(source, project / "continuity_hook.py")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "continuity_hook.py"], check=True)
        cli = [sys.executable, "-m", "context_control_plane.cli"]
        for command in (
            ["init", "--project-id", "native-probe"],
            ["work", "activate", "--work-id", "inspect-hook", "--work-title", "Inspect hook behavior",
             "--owner-ref", "probe-agent", "--claim-id", "probe-claim", "--scope", "capability:hook-analysis"],
        ):
            result = subprocess.run([*cli, *command, "--root", str(project)], cwd=ROOT, capture_output=True, text=True)
            if result.returncode:
                receipt["failed_gate"] = "probe_initialization"
                break
        else:
            try:
                # Only these already-reviewed local scripts bypass trust in this disposable host.
                environment = {**os.environ, "PLUGIN_ROOT": str(source.parent.parent),
                               "PLUGIN_DATA": str(temp / "plugin-data"), "CONTINUITY_EFFECT_POLICY": "auto",
                               "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"]}
                hook_config = json.loads((source.parent.parent / "hooks/hooks.json").read_text())["hooks"]
                (project / ".codex").mkdir()
                (project / ".codex/config.toml").write_text("# Isolated probe configuration layer.\n")
                (project / ".codex/hooks.json").write_text(json.dumps({"hooks": hook_config}))
                host = Host(environment, project)
                host.call("initialize", {"clientInfo": {"name": "continuity-native-probe", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
                host.send({"method": "initialized", "params": {}})
                listed = host.call("hooks/list", {"cwds": [str(project)]})
                cfg = host.call("config/read", {"cwd": str(project), "includeLayers": True})
                receipt["discovery_diagnostics"] = {
                    "layer_count": len(cfg.get("layers") or []),
                    "layers": [{"type": layer.get("name", {}).get("type"), "disabled": bool(layer.get("disabledReason"))} for layer in cfg.get("layers") or []],
                    "errors": sum(len(entry.get("errors", [])) for entry in listed.get("data", [])),
                    "warnings": sum(len(entry.get("warnings", [])) for entry in listed.get("data", [])),
                }
                found = [h for entry in listed.get("data", []) for h in entry.get("hooks", []) if h.get("source") == "project"]
                receipt["preflight_hooks"] = [{k: h.get(k) for k in ("eventName", "enabled", "trustStatus", "source")} for h in found]
                if len(found) != 6:
                    raise RuntimeError("host_hook_discovery")
                if args.preflight_only:
                    receipt["status"] = "preflight-passed"
                    raise RuntimeError("preflight_only_no_model_invocation")
                overrides = scoped_overrides(config, {})
                overrides.pop("hooks")
                started = host.call("thread/start", {"cwd": str(project), "ephemeral": True,
                                                     "approvalPolicy": "never", "sandbox": "read-only", "config": overrides})
                thread_id = started["thread"]["id"]
                receipt["effective_model"] = started.get("model")
                prompts = [
                    "Read-only: locate advisory_main and report its supported event names in one sentence. "
                    "After that the next task is inspect _operation_is_sampled, but do not do that next task yet. "
                    "Keep marker quiet-river-42 for your final response on the next task. Do not edit files.",
                    "Continue the saved next task and include the saved marker. Do not repeat the previous answer. Do not edit files.",
                ]
                for index, prompt in enumerate(prompts):
                    if index:
                        prior = len(host.events)
                        host.call("thread/compact/start", {"threadId": thread_id})
                        if not summarize(host.events[prior:])["native_compaction_observed"]:
                            host.until(lambda e: summarize([e])["native_compaction_observed"])
                    prior = len(host.events)
                    host.call("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": prompt}]})
                    completed = host.until(lambda e: e.get("method") == "turn/completed")
                    events = host.events[prior:]
                    phase = summarize(events)
                    answers = [e["params"]["item"].get("text", "") for e in events if e.get("method") == "item/completed" and e.get("params", {}).get("item", {}).get("type") == "agentMessage"]
                    phase["marker_retained"] = any("quiet-river-42" in answer for answer in answers)
                    phase["turn_status"] = completed["params"]["turn"]["status"]
                    receipt["phases"].append(phase)
                    if phase["turn_status"] != "completed":
                        raise RuntimeError("provider_turn_failed")
                receipt.update(summarize(host.events))
                receipt["status"] = "passed" if receipt["native_compaction_observed"] and receipt["phases"][-1]["marker_retained"] and all(p["lookup_calls"] > 0 for p in receipt["phases"]) else "failed"
                if receipt["status"] != "passed":
                    receipt["failed_gate"] = "native_adoption_or_memory"
            except (RuntimeError, OSError, KeyError, ValueError) as exc:
                receipt["failed_gate"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                if host is not None and host.rpc_error is not None:
                    # Protocol setup diagnostics contain no model output or credentials.
                    print(json.dumps({"protocol_error": host.rpc_error}), file=sys.stderr)
            finally:
                if host is not None:
                    receipt["host_observations"] = summarize(host.events)
                    host.close()
    receipt["config_unchanged"] = hashlib.sha256(config_path.read_bytes()).digest() == hashlib.sha256(original_config).digest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps(receipt, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
