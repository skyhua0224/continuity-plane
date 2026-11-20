"""Install and inspect the native Antigravity Continuity plugin."""

import json
import os
import shlex
import shutil
import subprocess
import sysconfig
from pathlib import Path

from .agy_hook import EVENTS
from .collaboration_registry import _locked
from .release_version import VERSION

PLUGIN = "continuity-plane"
SERVERS = {"continuity": "continuity-mcp", "continuity-search": "continuity-search-mcp"}
BUNDLE_FILES = (
    "hooks.json",
    "mcp_config.json",
    "skills/continuity-collaboration/SKILL.md",
    "skills/continuity-collaboration/references/collaboration.md",
    "skills/continuity-collaboration/references/sources.md",
    "plugin.json",
)


def bundle():
    checkout = Path(__file__).resolve().parents[1] / "integrations/antigravity" / PLUGIN
    if (checkout / "plugin.json").is_file():
        return checkout
    return Path(sysconfig.get_path("data")) / "share/continuity/agy-plugin"


def customization_root(scope, workspace):
    # agy-builtin-plugins, Directory Structure; agy-builtin-config, Path Resolution.
    if scope == "global":
        return Path.home() / ".gemini/config"
    return Path(workspace).expanduser().resolve() / ".agents"


def install(scope, workspace, *, dry_run=False, bridge_config=None):
    from .worker_dispatch import _write

    customization = customization_root(scope, workspace)
    target = customization / "plugins" / PLUGIN
    source = bundle()
    shared_skill_root = (
        Path(__file__).resolve().parents[1] / "integrations/claude/continuity-plane"
    )
    content = {
        name: (
            (
                shared_skill_root
                if name.startswith("skills/") and shared_skill_root.is_dir()
                else source
            )
            / name
        ).read_text(encoding="utf-8")
        for name in BUNDLE_FILES
    }
    if bridge_config is not None:
        from .remote_hook import read_config

        bridge_config = Path(bridge_config).expanduser().resolve()
        config = read_config(bridge_config)
        if not config.get("host") or not (
            isinstance(config.get("mappings"), list)
            or config.get("local_root")
            and config.get("remote_root")
        ):
            raise ValueError("bridge config requires a host and explicit root mappings")
        config_name = str(bridge_config)
        if os.name == "nt" and any(char in config_name for char in "%!^&|<>"):
            raise ValueError("bridge config path contains a cmd.exe metacharacter")
        argv = ["continuity-remote-hook", "--agy", "--config", config_name]
        quote = subprocess.list2cmdline if os.name == "nt" else shlex.join
        hooks = json.loads(content["hooks.json"])
        for event, handlers in hooks[PLUGIN].items():
            if event in {"PreToolUse", "PostToolUse"}:
                handlers = handlers[0]["hooks"]
            handlers[0]["command"] = quote(argv)
        content["hooks.json"] = json.dumps(hooks, indent=2) + "\n"
    report = {
        "scope": scope,
        "target": str(target),
        "version": json.loads(content["plugin.json"])["version"],
        "bridge_config": str(bridge_config) if bridge_config else None,
        "dry_run": dry_run,
        "files": list(content),
    }
    if dry_run:
        return report
    if customization.is_symlink() or target.parent.is_symlink():
        raise ValueError("plugin customization directory is a symlink")
    with _locked(target.parent / (PLUGIN + ".install")):
        if target.is_symlink():
            raise ValueError("plugin destination is a symlink")
        changed = False
        for name, text in content.items():
            path = target / name
            if any(
                item.is_symlink()
                for item in (path, *path.parents)
                if item.is_relative_to(target)
            ):
                raise ValueError("plugin content destination is a symlink")
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                _write(path, text)
                changed = True
    return {**report, "changed": changed}


def _json_file(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _mcp_version(executable, server):
    # mcp-lifecycle-2025-03-26, Initialization and Version Negotiation.
    # Both launchers use initialize. See mcp-versioning-2026-07-28,
    # Backward Compatibility with Initialization-Based Versions.
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "clientInfo": {"name": "continuity-doctor", "version": VERSION},
        },
    }
    try:
        completed = subprocess.run(
            [executable],
            input=json.dumps(request) + "\n",
            capture_output=True,
            encoding="utf-8",
            timeout=5,
            check=True,
        )
        info = json.loads(completed.stdout.splitlines()[0])["result"]["serverInfo"]
        return info["version"] if info["name"] == server else None
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        IndexError,
        subprocess.SubprocessError,
    ):
        return None


def status(workspace, executable):
    from .agy_probe import _read, slash
    from .update_check import parse_version

    workspace = Path(workspace).expanduser().resolve()
    global_root = customization_root("global", workspace)
    local = customization_root("workspace", workspace) / "plugins" / PLUGIN
    global_plugin = global_root / "plugins" / PLUGIN
    target = local if (local / "plugin.json").is_file() else global_plugin
    plugin = _json_file(target / "plugin.json")
    report = {
        "status": "missing" if not plugin else "degraded",
        "scope": "workspace" if target == local else "global",
        "plugin": {
            "path": str(target),
            "version": plugin.get("version"),
            "cli_version": VERSION,
        },
        "hooks": {
            "discovered_events": [],
            "required_events": sorted(EVENTS.keys() - {"PostInvocation"}),
        },
        "mcp": {},
    }
    expected = parse_version(VERSION)
    hook_command = {"continuity-agy-hook", "continuity-remote-hook"}
    launchers = set()
    try:
        report["agy_version"] = _read(executable, "--version").strip()
        hooks = slash("hooks", executable, cwd=workspace)
        actions = [
            action
            for item in hooks.get("hooks", [])
            if item["name"] == PLUGIN
            and item["enabled"]
            and Path(item["source"]).resolve() == target / "hooks.json"
            for action in item["actions"]
        ]
        for action in actions:
            command = shlex.split(action["command"], posix=os.name != "nt")
            launcher = Path(command[0].strip('"')).stem if command else ""
            if launcher in hook_command and shutil.which(launcher):
                launchers.add(launcher)
                report["hooks"]["discovered_events"].append(action["event"])
        report["hooks"]["discovered_events"] = sorted(
            set(report["hooks"]["discovered_events"])
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report["host_error"] = str(exc)

    versions = {}
    for launcher in sorted(launchers):
        try:
            versions[launcher] = _read(launcher, "--version").strip()
        except (OSError, ValueError):
            versions[launcher] = None
    report["hooks"]["launcher_versions"] = versions
    report["hooks"]["versions_match"] = bool(versions) and all(
        parse_version(version) == expected for version in versions.values()
    )
    installed = _json_file(target / "mcp_config.json").get("mcpServers", {})
    duplicates = _json_file(global_root / "mcp_config.json").get("mcpServers", {})
    for name, command in SERVERS.items():
        entry = installed.get(name) if isinstance(installed, dict) else None
        known = (
            isinstance(entry, dict)
            and entry.get("command") == command
            and entry.get("args", []) == []
        )
        launcher = shutil.which(command) if known else None
        version = _mcp_version(launcher, name) if launcher else None
        report["mcp"][name] = {
            "configured": isinstance(entry, dict),
            "stdio_version": version,
            "version_matches": version is not None
            and parse_version(version) == expected,
            "duplicate_global_registration": isinstance(duplicates, dict)
            and name in duplicates,
        }
    report["plugin"]["version_matches"] = (
        parse_version(plugin.get("version")) == expected
    )
    if (
        report["plugin"]["version_matches"]
        and report["hooks"]["versions_match"]
        and report["hooks"]["discovered_events"] == report["hooks"]["required_events"]
        and all(
            s["version_matches"] and not s["duplicate_global_registration"]
            for s in report["mcp"].values()
        )
    ):
        report["status"] = "active"
    return report
