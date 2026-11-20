"""Read native CLI metadata on the machine that owns the profile."""

import json
import re
import shlex
import shutil
import subprocess
import time
from datetime import UTC, datetime

EXECUTABLE = "agy"
INSPECT_COMMANDS = ("model", "effort", "config", "hooks", "skills", "agents", "credits", "usage")
# /usage sometimes answers from a stale copy that shows less use (agy 1.5.13 on Linux,
# 2026-11-20: one read in six reported 1.27 % of the 5-hour window instead of 2.15 %).
USAGE_SAMPLES = 3
_ID_EFFORT = re.compile(r"-(low|medium|high|xhigh)$")
_LABEL_EFFORT = re.compile(r" \((?:low|medium|high|xhigh)\)$", re.I)


def _read(executable, *args, cwd=None):
    # python-314-subprocess, Using the subprocess Module.
    try:
        process = subprocess.run(
            [executable, *args],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"agy {args[0]} timed out") from exc
    if process.returncode:
        raise ValueError(f"agy {' '.join(args)} exited {process.returncode}")
    return process.stdout


def models(executable=EXECUTABLE):
    entries = []
    for line in _read(executable, "models").splitlines():
        model_id, _, label = line.partition("\t")
        if model_id.strip() and label.strip():
            entries.append({"id": model_id.strip(), "label": label.strip()})
    if not entries:
        raise ValueError("agy models returned no model IDs and labels")
    return entries


def model_effort(model_id):
    match = _ID_EFFORT.search(model_id)
    return match[1] if match else None


EFFORTS = ("low", "medium", "high", "xhigh")


def model_efforts(model, entries):
    """Efforts the model offers, from the suffixed IDs that share its base ID."""
    base = _ID_EFFORT.sub("", model)
    found = {
        match[1]
        for entry in entries
        if _ID_EFFORT.sub("", entry["id"]) == base and (match := _ID_EFFORT.search(entry["id"]))
    }
    return [effort for effort in EFFORTS if effort in found]


def clamp_effort(effort, available):
    """The requested effort, or the highest offered one below it (Claude models stop at high)."""
    if not available or effort in available or effort not in EFFORTS:
        return effort
    lower = [e for e in available if EFFORTS.index(e) < EFFORTS.index(effort)]
    return (lower or available)[-1]


def model_entry(model, entries):
    for entry in entries:
        if model == entry["id"]:
            return entry
    for entry in entries:
        if model == _ID_EFFORT.sub("", entry["id"]):
            return entry
    raise ValueError(f"provider model {model!r} is absent from agy models")


def slash(name, executable=EXECUTABLE, *, cwd=None):
    # agy-headless, Slash commands: the command envelope is local metadata.
    if name not in INSPECT_COMMANDS:
        raise ValueError("unsupported agy inspection command")
    raw = _read(executable, "-p", "/" + name, "--output-format", "json", cwd=cwd)
    try:
        # python-314-json, json.loads.
        value = json.loads(raw)
        command = value["command"]
        if (
            value.get("status") == "SUCCESS"
            and value.get("num_turns") == 0
            and command["name"] == name
            and isinstance(command["data"], dict)
        ):
            return command["data"]
    except (KeyError, TypeError, ValueError):
        pass
    raise ValueError(f"agy /{name} returned no native command data")


def quota_window(label, usage, *, now=None):
    """Pick the least remaining share across the model's active windows."""
    # agy-usage, Model quotas (/usage): windows are shared by each model group.
    now = now or datetime.now(UTC)
    label = _LABEL_EFFORT.sub("", label).casefold()
    groups = usage.get("groups")
    windows = []
    for group in groups if isinstance(groups, list) else ():
        if not isinstance(group, dict):
            continue
        description = str(group.get("description", ""))
        _, _, names = description.casefold().partition("models within this group:")
        if label != str(group.get("name", "")).casefold() and label not in {
            name.strip() for name in names.split(",")
        }:
            continue
        buckets = group.get("buckets")
        for bucket in buckets if isinstance(buckets, list) else ():
            if not isinstance(bucket, dict):
                continue
            fraction = bucket.get("remaining_fraction")
            if type(fraction) not in (int, float) or not 0 <= fraction <= 1:
                continue
            try:
                # python-314-datetime, datetime.fromisoformat.
                reset = datetime.fromisoformat(bucket["reset_time"])
            except (KeyError, TypeError, ValueError):
                continue
            if reset.tzinfo and reset > now:
                windows.append((fraction, reset))
    if not windows:
        raise ValueError("agy /usage returned no active quota window for the configured model")
    return min(windows, key=lambda window: (window[0], -window[1].timestamp()))


# One /usage answer covers every model group and each agy start takes seconds, so
# the adapters checked in one pass share the reads for a short time.
CACHE_SECONDS = 60
_cache = {}


def _cached(kind, executable, read):
    key = (kind, shutil.which(executable) or executable)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = read()
    _cache[key] = (now, value)
    return value


def forget():
    """Drop shared reads, e.g. after a run that has just used quota."""
    _cache.clear()


def cached_models(executable=EXECUTABLE):
    return _cached("models", executable, lambda: models(executable))


def tightest_window(label, executable=EXECUTABLE, samples=USAGE_SAMPLES):
    """The least remaining share over several /usage reads, so a stale read cannot admit work."""
    readings = _cached(
        "usage", executable, lambda: [slash("usage", executable) for _ in range(samples)]
    )
    return min(quota_window(label, usage) for usage in readings)


def command(args):
    if args.agy_action in {"install", "status"}:
        from .agy_integration import install, status

        result = (
            install(args.scope, args.workspace, dry_run=args.dry_run, bridge_config=args.bridge_config)
            if args.agy_action == "install"
            else status(args.workspace, args.executable)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.agy_action == "mcp-install":
        # agy-mcp, MCP configuration structure: register a stdio command.
        argv = [args.executable, "mcp", "add", "continuity", "continuity-mcp"]
        if args.dry_run:
            # python-314-shlex, shlex.join: display the argv for a Unix shell.
            print(shlex.join(argv))
        else:
            return subprocess.run(argv, check=False).returncode
    else:
        value = {
            "version": _read(args.executable, "--version").strip(),
            "models": models(args.executable),
            **{name: slash(name, args.executable) for name in INSPECT_COMMANDS},
        }
        if args.json:
            print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            for name, data in value.items():
                text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                print(f"{name} {text}")
    return 0


def register_cli(commands):
    parser = commands.add_parser("agy", help="Inspect the native CLI or register the Continuity MCP server")
    actions = parser.add_subparsers(dest="agy_action", required=True)
    inspect = actions.add_parser("inspect")
    inspect.add_argument("--json", action="store_true")
    install = actions.add_parser("mcp-install")
    install.add_argument("--dry-run", action="store_true")
    plugin = actions.add_parser("install", help="Install the native Continuity plugin")
    plugin.add_argument("--dry-run", action="store_true")
    plugin.add_argument("--scope", choices=("workspace", "global"), default="workspace")
    plugin.add_argument("--bridge-config", help="Explicit SSH hook bridge config")
    status = actions.add_parser("status", help="Inspect plugin discovery, hooks and MCP versions")
    for action in (plugin, status):
        action.add_argument("--workspace", default=".")
    for action in (inspect, install, plugin, status):
        action.set_defaults(handler=command)
        action.add_argument("--executable", default=EXECUTABLE)
