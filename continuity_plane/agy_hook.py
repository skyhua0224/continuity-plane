"""Translate Antigravity hook JSON into the shared lifecycle and back."""

import hashlib
import json
import os
import sys
from pathlib import Path

from .collaboration_registry import _atomic_write, _locked, default_data_root
from .lifecycle import (
    MAX_CONTEXT_BYTES,
    add_host_context,
    handle_event,
    turn_state_checkpoint,
)

# agy-builtin-hooks, Supported Event Types and Input/Output Contract.
TOOL_EVENTS = {"PreToolUse", "PostToolUse"}
EVENTS = {
    "SessionStart": "SessionStart",
    "PreInvocation": "UserPromptSubmit",
    "PostInvocation": "PostInvocation",
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "Stop": "PreCompact",
}


def normalize(native):
    event = native.get("hookEventName")
    session = native.get("conversationId")
    workspace = native.get("workspacePaths")
    cwd = native.get("cwd") or (
        workspace[0] if isinstance(workspace, list) and workspace else None
    )
    if (
        event not in EVENTS
        or not isinstance(session, str)
        or not 0 < len(session) <= 256
        or not isinstance(cwd, str)
        or not Path(cwd).is_absolute()
    ):
        return None
    payload = {
        "hook_event_name": EVENTS[event],
        "session_id": session,
        "cwd": str(Path(cwd).resolve()),
    }
    for name, target in (
        ("transcriptPath", "transcript_path"),
        ("lastUserInput", "prompt"),
    ):
        if isinstance(native.get(name), str):
            payload[target] = native[name]
    if event in TOOL_EVENTS:
        tool = native.get("toolCall")
        if isinstance(tool, dict):
            name, args = tool.get("name"), tool.get("args")
            args = args if isinstance(args, dict) else {}
            if name == "run_command":
                name = "shell"
                args = {"command": args.get("CommandLine") or args.get("command")}
            elif name == "call_mcp_tool":
                server, tool_name = args.get("ServerName"), args.get("ToolName")
                if isinstance(server, str) and isinstance(tool_name, str):
                    server = server.removeprefix("continuity-plane_")
                    name = f"{server}.{tool_name}"
                    args = args.get("Arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except ValueError:
                            args = {}
            payload.update(tool_name=name, tool_input=args)
        payload["tool_response"] = {"isError": bool(native.get("error"))}
    if event == "Stop":
        payload["observation_kind"] = "turn-boundary"
        payload["termination_reason"] = native.get("terminationReason")
    return payload


def _context(result):
    return result.get("hookSpecificOutput", {}).get("additionalContext", "")


def _read_state(path):
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def _respond(native, payload, data, config):
    from .adoption import cli_nudge, merge_context

    event = native["hookEventName"]
    if event in {"PreToolUse", "PostInvocation"}:
        return ""

    def shared(value):
        if config is None:
            output = handle_event(
                value, host="antigravity", data_root=data, meter_injection=False
            )
            turn_state_checkpoint(value, output, host="antigravity", data_root=data)
            return add_host_context(
                output, value, host="antigravity", data_root=data, nudge=False
            )
        from .remote_hook import _record_front_output, forward

        output = forward(value, config, provider="antigravity")
        _record_front_output(value, output, config, provider="antigravity", meter=False)
        if value["hook_event_name"] == "SessionStart":
            from .update_check import merge_session_notice

            merge_session_notice(output, value, MAX_CONTEXT_BYTES, data)
        return output

    if event == "Stop":
        shared(payload)
        return ""

    key = hashlib.sha256(
        (payload["cwd"] + ":" + payload["session_id"]).encode()
    ).hexdigest()
    path = data / "agy-hooks" / (key + ".json")
    if event == "PostToolUse":
        output = shared(payload)
        merge_context(
            output, "PostToolUse", cli_nudge(payload, data), MAX_CONTEXT_BYTES
        )
        text = _context(output)
        if text:
            with _locked(path):
                state = _read_state(path)
                pending = state.get("pending", "")
                combined = (pending + "\n" + text).strip()
                if len(combined.encode()) <= MAX_CONTEXT_BYTES:
                    state["pending"] = combined
                    _atomic_write(path, state)
        return ""

    with _locked(path):
        state = _read_state(path)
        if event == "SessionStart":
            output = shared(
                {
                    **payload,
                    "source": "turn-boundary",
                    "observation_kind": "turn-boundary",
                }
            )
            state["started"] = True
            _atomic_write(path, state)
            return _context(output)

        output = {}
        prompt = payload.get("prompt")
        invocation = native.get("invocationNum", 0)
        if prompt and invocation == 0:
            # A new user step can repeat identical text. At invocation zero the
            # initial step count distinguishes it from a retried callback.
            turn = [
                hashlib.sha256(prompt.encode()).hexdigest(),
                native.get("initialNumSteps"),
            ]
            if state.get("turn") != turn:
                if not state.pop("started", False):
                    # In 1.5.13 --conversation resumes directly at PreInvocation.
                    output = shared(
                        {
                            **payload,
                            "hook_event_name": "SessionStart",
                            "source": "turn-boundary",
                            "observation_kind": "turn-boundary",
                        }
                    )
                plan = shared(payload)
                merge_context(
                    output, "UserPromptSubmit", _context(plan), MAX_CONTEXT_BYTES
                )
                state["turn"] = turn
                pending = state.get("pending", "")
                if (
                    pending
                    and len(_context(output).encode()) + len(pending.encode()) + 1
                    <= MAX_CONTEXT_BYTES
                ):
                    merge_context(
                        output, "UserPromptSubmit", pending, MAX_CONTEXT_BYTES
                    )
                    state.pop("pending")
                _atomic_write(path, state)
                return _context(output)
        pending = state.pop("pending", "")
        if pending:
            _atomic_write(path, state)
        return pending


def respond(native, *, config_path=None):
    if not isinstance(native, dict):
        return {}
    event = native.get("hookEventName")
    # These responses are required even if local state or the bridge fails.
    if event in ("PreToolUse", "Stop"):
        fallback = {"decision": "allow"}
    else:
        fallback = {}
    if event == "PreToolUse" or os.environ.get("CONTINUITY_DISABLED") == "1":
        return fallback
    try:
        payload = normalize(native)
        if payload is None:
            return fallback
        config = None
        if config_path is not None:
            from .remote_hook import read_config

            config = read_config(config_path)
        data = Path((config or {}).get("local_data_root") or default_data_root())
        text = _respond(native, payload, data, config)
        if not text:
            return fallback
        if config is not None:
            from .remote_hook import _record_front_output

            _record_front_output(
                payload,
                {"hookSpecificOutput": {"additionalContext": text}},
                config,
                provider="antigravity",
            )
        else:
            from .usage_meter import record

            record(
                payload["cwd"],
                command="hook:" + event,
                response_bytes=0,
                estimated_grep_bytes=0,
                injected_bytes=len(text.encode()),
                session_id=payload["session_id"],
                data_root=data,
                host="antigravity",
            )
        return {"injectSteps": [{"systemMessage": {"systemMessage": text}}]}
    except (OSError, ValueError, KeyError, TypeError):
        return fallback


def run(config_path=None):
    if config_path is None and sys.argv[1:] == ["--version"]:
        from .release_version import VERSION

        print(VERSION)
        return 0
    try:
        raw = sys.stdin.buffer.read(524289)
        value = json.loads(raw) if len(raw) <= 524288 else None
    except (ValueError, OSError):
        value = None
    print(json.dumps(respond(value, config_path=config_path), ensure_ascii=True))
    return 0
