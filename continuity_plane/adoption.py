"""Count how agents reach Continuity: MCP tools versus CLI commands.

Three sources append to one monthly JSONL file under the data root:

- ``hook``: the host hook sees each tool call an agent makes (PostToolUse) and
  records whether it was a Continuity MCP tool or a shell command running
  ``continuity``. This is the per-session view used for adoption targets.
- ``mcp``: the MCP server records every ``tools/call`` it answers.
- ``cli``: the console entry point records every CLI invocation with a caller
  class derived from the environment.

Only counts, operation names, durations and outcome are stored. Arguments,
bodies, prompts and transcripts are never written. Recording must never slow
down or fail the call it observes, so every write swallows its own errors.
Set ``CONTINUITY_ADOPTION_TELEMETRY=0`` to turn recording off.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "context.adoption-event/v1alpha1"
_SHELL_TOOLS = {"bash", "shell", "local_shell", "exec_command", "container.exec"}
# Claude names tools mcp__<server>__<tool>; plugin servers become
# mcp__plugin_<plugin>_<server>__<tool>. Codex hooks may report <server>.<tool>.
_MCP_TOOL = re.compile(
    r"^(?:mcp__(?:plugin_[\w-]*?_)?)?(?P<server>continuity(?:[-_]search)?)(?:__|\.|/)(?P<tool>[\w.-]+)$"
)
_CONTINUITY_WORD = re.compile(r"(?:^|[\s;&|(`'\"/])continuity(?=\s|$)")


def enabled() -> bool:
    return os.environ.get("CONTINUITY_ADOPTION_TELEMETRY", "1") != "0"


def _data_root(data_root: Path | str | None) -> Path:
    if data_root:
        return Path(data_root)
    from .collaboration_registry import default_data_root

    return default_data_root()


def _path(data_root: Path | str | None, now: datetime) -> Path:
    return _data_root(data_root) / "adoption" / f"events-{now:%Y-%m}.jsonl"


def record(source: str, data_root: Path | str | None = None, **fields) -> None:
    if not enabled():
        return
    now = datetime.now(timezone.utc)
    event = {
        "schema_version": SCHEMA,
        "at": now.isoformat(),
        "source": source,
        **{k: v for k, v in fields.items() if v is not None},
    }
    try:
        path = _path(data_root, now)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError):
        pass


# --- Operation names -------------------------------------------------------


def _operation_table() -> dict[tuple[str, ...], str]:
    from .planning_api import CLI_HELP, OPERATIONS

    table: dict[tuple[str, ...], str] = {}
    for action, argv in {**CLI_HELP, **OPERATIONS}.items():
        table[tuple(argv)] = action
    # `continuity task …` is an alias family for `continuity collaboration task …`.
    for argv, action in list(table.items()):
        if argv[:2] == ("collaboration", "task"):
            table.setdefault(("task",) + argv[2:], action)
    return table


def operation(words: list[str]) -> str | None:
    """Map CLI words after `continuity` to the planning action name."""
    tokens: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
            continue
        if word.startswith("--"):
            # Global options before the command take a value unless `=` is used.
            skip = "=" not in word and word not in {"--version", "--help"}
            continue
        if word.startswith("-") or not re.fullmatch(r"[a-z][a-z0-9.-]*", word):
            break
        tokens.append(word)
        if len(tokens) == 4:
            break
    if not tokens:
        return None
    table = _operation_table()
    for size in range(len(tokens), 0, -1):
        action = table.get(tuple(tokens[:size]))
        if action:
            return action
    return ".".join(tokens[:2])


def mcp_equivalent(action: str | None) -> str | None:
    """Name the MCP tool call that already performs this action, if any."""
    if not action:
        return None
    from .planning_api import ALIASES, OPERATIONS

    for tool, alias in ALIASES.items():
        if alias == action:
            return tool
    if action.startswith("context.lookup") or action.startswith("context.search"):
        return "continuity_context_lookup"
    if action in OPERATIONS:
        return f"continuity_plan(action={action})"
    return None


def shell_operations(command: str) -> list[str]:
    """Find `continuity …` invocations inside one shell command string."""
    if not isinstance(command, str) or "continuity" not in command:
        return []
    found: list[str] = []
    for match in _CONTINUITY_WORD.finditer(command):
        rest = command[match.end() :]
        segment = re.split(r"[;&|`)\n]", rest, maxsplit=1)[0]
        try:
            words = shlex.split(segment.strip().strip("'\""))
        except ValueError:
            words = segment.split()
        action = operation(words)
        if action:
            found.append(action)
    return found


def classify_tool_call(tool_name: object, tool_input: object) -> list[dict]:
    """Return adoption entries for one agent tool call (empty when unrelated)."""
    if not isinstance(tool_name, str):
        return []
    match = _MCP_TOOL.match(tool_name)
    if match:
        tool = match["tool"]
        action = None
        if isinstance(tool_input, dict) and isinstance(tool_input.get("action"), str):
            action = tool_input["action"]
        return [{"channel": "mcp", "tool": tool, "op": action or tool}]
    if tool_name.lower() not in _SHELL_TOOLS:
        return []
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    return [
        {"channel": "cli", "op": action, "mcp_equivalent": mcp_equivalent(action)}
        for action in shell_operations(command or "")
    ]


def caller(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    if env.get("CLAUDECODE") == "1" or env.get("CLAUDE_CODE_ENTRYPOINT"):
        return "claude-code"
    if any(key.startswith("CODEX_") for key in env):
        return "codex"
    if env.get("SSH_CONNECTION"):
        return "ssh"
    return "interactive"


# --- Recording helpers used by the entry points ---------------------------


def record_hook(payload: dict, host: str, data_root: Path | str | None = None) -> None:
    if not enabled() or payload.get("hook_event_name") != "PostToolUse":
        return
    entries = classify_tool_call(payload.get("tool_name"), payload.get("tool_input"))
    if not entries:
        return
    response = payload.get("tool_response")
    failed = isinstance(response, dict) and bool(
        response.get("isError") or response.get("is_error")
    )
    for entry in entries:
        record(
            "hook",
            data_root,
            host=host,
            session=str(payload.get("session_id") or "")[:128] or None,
            cwd=payload.get("cwd"),
            ok=not failed,
            **entry,
        )


def record_cli(argv: list[str], code: int, seconds: float) -> None:
    if not enabled():
        return
    record(
        "cli",
        op=operation(argv),
        caller=caller(),
        ok=code == 0,
        ms=round(seconds * 1000, 1),
    )


def record_mcp(
    server: str,
    tool: object,
    arguments: object,
    ok: bool,
    seconds: float,
    client: str | None,
) -> None:
    if not enabled():
        return
    args = arguments if isinstance(arguments, dict) else {}
    action = args.get("action") if isinstance(args.get("action"), str) else None
    record(
        "mcp",
        server=server,
        tool=str(tool)[:128],
        op=action or str(tool)[:128],
        project=args.get("project_id") if isinstance(args.get("project_id"), str) else None,
        client=client,
        ok=ok,
        ms=round(seconds * 1000, 1),
    )


# --- Report -----------------------------------------------------------------


def load(
    data_root: Path | str | None = None, since: datetime | None = None
) -> list[dict]:
    root = _data_root(data_root) / "adoption"
    events: list[dict] = []
    if not root.is_dir():
        return events
    for path in sorted(root.glob("events-*.jsonl")):
        if since and path.stem[7:] < f"{since:%Y-%m}":
            continue
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since:
                    try:
                        if datetime.fromisoformat(event["at"]) < since:
                            continue
                    except (KeyError, ValueError):
                        continue
                events.append(event)
    return events


def _share(mcp: int, cli: int) -> float | None:
    total = mcp + cli
    return round(100 * mcp / total, 1) if total else None


def summarize(events: list[dict], cwd_prefix: str | None = None) -> dict:
    hook = [e for e in events if e.get("source") == "hook"]
    if cwd_prefix:
        hook = [e for e in hook if str(e.get("cwd") or "").startswith(cwd_prefix)]
    sessions: dict[str, Counter] = defaultdict(Counter)
    hosts: dict[str, Counter] = defaultdict(Counter)
    cli_with_tool: Counter = Counter()
    cli_only: Counter = Counter()
    mcp_tools: Counter = Counter()
    for event in hook:
        channel = event.get("channel")
        sessions[event.get("session") or "?"][channel] += 1
        hosts[event.get("host") or "?"][channel] += 1
        if channel == "mcp":
            mcp_tools[event.get("op") or event.get("tool")] += 1
        elif event.get("mcp_equivalent"):
            cli_with_tool[(event.get("op"), event["mcp_equivalent"])] += 1
        else:
            cli_only[event.get("op")] += 1
    mcp_total = sum(c["mcp"] for c in sessions.values())
    cli_total = sum(c["cli"] for c in sessions.values())
    server = [e for e in events if e.get("source") in {"mcp", "cli"}]
    server_failures = Counter(
        (e.get("source"), e.get("op")) for e in server if e.get("ok") is False
    )
    return {
        "schema_version": "context.adoption-report/v1alpha1",
        "agent_view": {
            "sessions": len(sessions),
            "sessions_using_mcp": sum(1 for c in sessions.values() if c["mcp"]),
            "mcp_calls": mcp_total,
            "cli_calls": cli_total,
            "mcp_share_percent": _share(mcp_total, cli_total),
            "by_host": {
                host: {
                    "mcp": c["mcp"],
                    "cli": c["cli"],
                    "mcp_share_percent": _share(c["mcp"], c["cli"]),
                }
                for host, c in sorted(hosts.items())
            },
            "top_mcp_operations": mcp_tools.most_common(10),
            "cli_with_mcp_tool": [
                {"op": op, "mcp": tool, "count": n}
                for (op, tool), n in cli_with_tool.most_common(10)
            ],
            "cli_without_mcp_tool": cli_only.most_common(10),
        },
        "server_view": {
            "mcp_calls": sum(1 for e in server if e["source"] == "mcp"),
            "mcp_by_client": Counter(
                e.get("client") or "?" for e in server if e["source"] == "mcp"
            ).most_common(),
            "cli_calls": sum(1 for e in server if e["source"] == "cli"),
            "cli_by_caller": Counter(
                e.get("caller") or "?" for e in server if e["source"] == "cli"
            ).most_common(),
            "failures": [
                {"source": s, "op": op, "count": n}
                for (s, op), n in server_failures.most_common(10)
            ],
        },
    }


def brief(report: dict) -> str:
    agent, server = report["agent_view"], report["server_view"]
    share = agent["mcp_share_percent"]
    lines = [
        "Continuity adoption",
        f"Agent sessions: {agent['sessions']} ({agent['sessions_using_mcp']} used MCP)"
        f" · calls via MCP {agent['mcp_calls']}, via CLI {agent['cli_calls']}"
        f" · MCP share {'n/a' if share is None else str(share) + '%'}",
    ]
    for host, value in agent["by_host"].items():
        host_share = value["mcp_share_percent"]
        lines.append(
            f"  {host}: MCP {value['mcp']} / CLI {value['cli']}"
            f" ({'n/a' if host_share is None else str(host_share) + '%'})"
        )
    if agent["cli_with_mcp_tool"]:
        lines.append("CLI calls that already have an MCP tool:")
        lines += [
            f"  {item['op']} ×{item['count']} → {item['mcp']}"
            for item in agent["cli_with_mcp_tool"]
        ]
    if agent["cli_without_mcp_tool"]:
        lines.append("CLI calls with no MCP tool yet:")
        lines += [f"  {op} ×{n}" for op, n in agent["cli_without_mcp_tool"]]
    lines.append(
        f"Server: {server['mcp_calls']} MCP calls "
        + ", ".join(f"{c} {n}" for c, n in server["mcp_by_client"])
        + f"; {server['cli_calls']} CLI runs "
        + ", ".join(f"{c} {n}" for c, n in server["cli_by_caller"])
    )
    if server["failures"]:
        lines.append(
            "Failures: "
            + ", ".join(
                f"{f['source']}:{f['op']} ×{f['count']}" for f in server["failures"]
            )
        )
    return "\n".join(lines)


def cli_report(args) -> int:
    since = None
    if args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            raise ValueError("--since needs a timezone, e.g. 2026-10-29T00:00:00Z")
    elif args.days:
        since = datetime.fromtimestamp(
            datetime.now(timezone.utc).timestamp() - args.days * 86400, timezone.utc
        )
    report = summarize(load(args.data_root, since), args.cwd_prefix)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(brief(report))
    return 0


def register_cli(observe_commands) -> None:
    parser = observe_commands.add_parser(
        "adoption",
        help="how agents reach Continuity: MCP tool calls versus CLI commands",
    )
    parser.add_argument("--data-root", default=None)
    parser.add_argument("--since", default=None, help="timezone-aware ISO-8601 start")
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument(
        "--cwd-prefix", default=None, help="only agent sessions under this directory"
    )
    parser.add_argument("--format", choices=["brief", "json"], default="brief")
    parser.set_defaults(handler=cli_report)


class CallTracker:
    """Pair MCP `tools/call` requests with their replies inside one server."""

    def __init__(self, server: str):
        self.server = server
        self.client: str | None = None
        self._pending: dict[str, tuple[object, object, float]] = {}

    def observe_request(self, request: dict) -> None:
        import time

        method = request.get("method")
        if method == "initialize":
            params = request.get("params")
            info = params.get("clientInfo") if isinstance(params, dict) else None
            if isinstance(info, dict) and isinstance(info.get("name"), str):
                self.client = info["name"][:64]
        elif method == "tools/call" and request.get("id") is not None:
            params = request.get("params")
            params = params if isinstance(params, dict) else {}
            self._pending[repr(request["id"])] = (
                params.get("name"),
                params.get("arguments"),
                time.monotonic(),
            )

    def finish(self, request_id: object, ok: bool) -> None:
        import time

        pending = self._pending.pop(repr(request_id), None)
        if pending is None:
            return
        tool, arguments, started = pending
        record_mcp(
            self.server, tool, arguments, ok, time.monotonic() - started, self.client
        )


# --- Bridge spool: SSH-forwarded hooks are coalesced, so counts travel later --


def spool_tool_call(spool: Path, payload: dict, host: str) -> None:
    """Classify one forwarded PostToolUse locally and keep it until the next send."""
    if not enabled() or payload.get("hook_event_name") != "PostToolUse":
        return
    entries = classify_tool_call(payload.get("tool_name"), payload.get("tool_input"))
    if not entries:
        return
    now = datetime.now(timezone.utc).isoformat()
    session = str(payload.get("session_id") or "")[:128] or None
    try:
        spool.parent.mkdir(parents=True, exist_ok=True)
        with spool.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(
                    json.dumps(
                        {"at": now, "host": host, "session": session, **entry},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    except OSError:
        pass


def drain_spool(spool: Path, limit: int = 30) -> list[dict]:
    """Take up to `limit` spooled entries; the rest stay for the next send."""
    try:
        lines = spool.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    taken, rest = lines[:limit], lines[limit:]
    try:
        if rest:
            spool.write_text("\n".join(rest) + "\n", encoding="utf-8")
        else:
            spool.unlink()
    except OSError:
        return []
    entries = []
    for line in taken:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def record_forwarded(entries: object, cwd: str | None) -> int:
    """Record bridge entries on the state host; returns how many were kept."""
    if not isinstance(entries, list):
        return 0
    allowed = {"at", "host", "session", "channel", "tool", "op", "mcp_equivalent"}
    kept = 0
    for entry in entries[:200]:
        if not isinstance(entry, dict) or entry.get("channel") not in {"mcp", "cli"}:
            continue
        clean = {
            k: v for k, v in entry.items() if k in allowed and isinstance(v, str)
        }
        observed_at = clean.pop("at", None)
        record("hook", None, cwd=cwd, ok=True, observed_at=observed_at, **clean)
        kept += 1
    return kept


# --- Steering: one reminder when an agent shells out for something MCP does --

MAX_NUDGES_PER_SESSION = 3


def cli_nudge(payload: dict, data_root: Path | str | None = None) -> str | None:
    """Name the MCP tool once when an agent runs an equivalent CLI command."""
    if not enabled() or payload.get("hook_event_name") != "PostToolUse":
        return None
    if os.environ.get("CONTINUITY_MCP_NUDGE", "1") == "0":
        return None
    entries = [
        e
        for e in classify_tool_call(payload.get("tool_name"), payload.get("tool_input"))
        if e["channel"] == "cli" and e.get("mcp_equivalent")
    ]
    session = str(payload.get("session_id") or "")
    if not entries or not session:
        return None
    import hashlib

    key = hashlib.sha256(session.encode()).hexdigest()
    path = _data_root(data_root) / "adoption-nudges" / f"{key}.json"
    try:
        seen = json.loads(path.read_text()) if path.exists() else []
    except (OSError, ValueError):
        seen = []
    if len(seen) >= MAX_NUDGES_PER_SESSION:
        return None
    fresh = [e for e in entries if e["op"] not in seen]
    if not fresh:
        return None
    entry = fresh[0]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(seen + [entry["op"]]))
    except OSError:
        return None
    return (
        f"continuity: `{entry['op']}` is available as the MCP tool "
        f"{entry['mcp_equivalent']}; prefer it to the shell while the "
        "continuity MCP server is connected (typed arguments, no quoting, shared guards)."
    )


def merge_context(output: dict, event: str, text: str | None, limit: int = 4096) -> dict:
    """Append one line to a hook's additionalContext without exceeding `limit`."""
    if not text:
        return output
    current = output.get("hookSpecificOutput", {}).get("additionalContext", "")
    combined = (current + "\n" + text).strip()
    if len(combined.encode()) > limit:
        return output
    output["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": combined}
    return output
