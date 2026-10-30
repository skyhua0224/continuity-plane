"""Opt-in SSH hook bridge with explicit root mapping and local recovery fallback."""

import argparse
import json
import re
import subprocess
import sys
import shlex
import hashlib
import os
import time
from pathlib import Path


def _delivery_path(payload, config):
    from .collaboration_registry import default_data_root

    key = hashlib.sha256(
        (str(Path(payload["cwd"]).resolve()) + ":" + payload["session_id"]).encode()
    ).hexdigest()
    return (
        Path(config.get("local_data_root") or default_data_root())
        / "bridge-delivery"
        / (key + ".json")
    )


def _delivery_snapshot(payload, config):
    if os.environ.get("CONTINUITY_METER", "1") == "0":
        return None
    path = _delivery_path(payload, config)
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except (OSError, ValueError):
        return None


def _record_front_output(payload, result, config):
    from .collaboration_registry import _atomic_write, _locked, default_data_root

    data = Path(config.get("local_data_root") or default_data_root())
    root = str(Path(payload["cwd"]).resolve())
    session = payload["session_id"]
    event = payload.get("hook_event_name")
    phase = (
        "saved"
        if event == "PreCompact" and result.get("status") == "saved"
        else "restored"
        if (
            event == "PostCompact"
            or event == "SessionStart"
            and payload.get("source") == "compact"
        )
        and result.get("hookSpecificOutput")
        else None
    )
    if phase:
        try:
            from .handoff_journal import record as record_phase

            key = hashlib.sha256((root + ":" + session).encode()).hexdigest()
            saved = json.loads((data / "handoffs" / (key + ".json")).read_text())
            record_phase(
                root,
                session,
                saved["generation"],
                phase,
                data_root=data,
                kind=payload.get("observation_kind", "native-event"),
            )
        except (OSError, ValueError, KeyError):
            pass
    text = result.get("hookSpecificOutput", {}).get("additionalContext")
    if (
        not isinstance(text, str)
        or not text
        or os.environ.get("CONTINUITY_METER", "1") == "0"
    ):
        return
    path = _delivery_path(payload, config)
    import uuid

    with _locked(path):
        counter = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "session_sha256": hashlib.sha256(session.encode()).hexdigest(),
                "instance_id": uuid.uuid4().hex,
                "calls": 0,
                "hook_injected_bytes": 0,
            }
        )
        counter["calls"] += 1
        counter["hook_injected_bytes"] += len(text.encode())
        _atomic_write(path, counter)
    from .usage_meter import record

    record(
        root,
        command="hook:bridge-output",
        response_bytes=0,
        estimated_grep_bytes=0,
        injected_bytes=len(text.encode()),
        session_id=session,
        data_root=data,
        host="claude-bridge",
    )


def forward(payload, config, *, runner=subprocess.run):
    from .lifecycle import handle_event

    if not isinstance(payload, dict) or not isinstance(config, dict):
        raise ValueError("hook payload/config must be objects")
    if isinstance(config.get("mappings"), list):
        cwd = Path(payload.get("cwd", "")).resolve()
        mapping = next(
            (
                m
                for m in config["mappings"]
                if isinstance(m, dict)
                and isinstance(m.get("local_root"), str)
                and Path(m["local_root"]).expanduser().resolve() == cwd
            ),
            None,
        )
        if mapping is None:
            from .adoption import record_hook

            record_hook(payload, "claude", config.get("local_data_root"))
            return handle_event(
                payload,
                host="claude-bridge",
                data_root=config.get("local_data_root"),
                meter_injection=False,
                record_protocol=False,
            )
        config = {**config, **mapping}
    host = config.get("host", "")
    if not isinstance(host, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", host
    ):
        raise ValueError("host must be a configured SSH alias or user@host")
    local = Path(config.get("local_root", "")).expanduser()
    remote = config.get("remote_root")
    if (
        not local.is_absolute()
        or not isinstance(remote, str)
        or not remote.startswith("/")
    ):
        raise ValueError("explicit absolute local_root and remote_root are required")
    if Path(payload.get("cwd", "")).resolve() != local.resolve():
        return {"continue": True, "status": "root-not-mapped"}
    from .adoption import drain_spool, spool_tool_call

    adoption_spool = (
        Path(config.get("local_data_root") or Path.home() / ".local/state/continuity")
        / "bridge-adoption"
        / (hashlib.sha256(str(local).encode()).hexdigest() + ".jsonl")
    )
    # Count before coalescing so rapid tool calls are not lost.
    spool_tool_call(adoption_spool, payload, "claude")
    if payload.get("hook_event_name") == "PostToolUse":
        from .collaboration_registry import _atomic_write, _locked

        key = hashlib.sha256(
            (str(local) + str(payload.get("session_id", ""))).encode()
        ).hexdigest()
        poll = (
            Path(
                config.get("local_data_root") or Path.home() / ".local/state/continuity"
            )
            / "bridge-polls"
            / (key + ".json")
        )
        with _locked(poll):
            previous = json.loads(poll.read_text()) if poll.exists() else {}
            now = time.time()
            if now - previous.get("at", 0) < 3:
                return {"continue": True, "status": "poll-coalesced"}
            _atomic_write(poll, {"at": now})
    timeout = config.get("timeout_seconds", 5)
    if type(timeout) not in {int, float} or not 0 < timeout <= 15:
        raise ValueError("bridge timeout must be between 0 and 15 seconds")
    # Only bounded host event metadata crosses SSH; never transcript paths/bodies.
    allowed = (
        "hook_event_name",
        "session_id",
        "source",
        "trigger",
        "next_action",
        "prompt",
        "observation_kind",
        "agent_id",
        "agent_type",
    )
    outgoing = {k: payload[k] for k in allowed if k in payload}
    outgoing["cwd"] = remote
    adoption_entries = drain_spool(adoption_spool)
    if adoption_entries:
        outgoing["adoption"] = adoption_entries
    if payload.get("hook_event_name") in {"SubagentStart", "SubagentStop", "SessionStart", "SessionEnd"}:
        from .runtime_origin import current
        from .native_subagents import boot_identity

        outgoing["collection_origin"] = current("claude")
        outgoing["boot_id_sha256"] = boot_identity()
    if not config.get("forward_checklists", True):
        outgoing.pop("prompt", None)
    elif (
        isinstance(outgoing.get("prompt"), str)
        and len(outgoing["prompt"].encode()) > 8192
    ):
        outgoing["prompt_omitted_sha256"] = hashlib.sha256(outgoing["prompt"].encode()).hexdigest()
        outgoing.pop("prompt")
        outgoing["prompt_omitted"] = True
    if os.environ.get("CONTINUITY_ASSIGNEE") or config.get("assignee"):
        outgoing["assignee"] = (
            os.environ.get("CONTINUITY_ASSIGNEE") or config["assignee"]
        )
    outgoing["continuity_role"] = os.environ.get("CONTINUITY_ROLE") or "controller"
    outgoing["controller"] = (
        os.environ.get("CONTINUITY_CONTROLLER")
        or config.get("controller")
        or (
            outgoing.get("assignee")
            if outgoing["continuity_role"] == "controller"
            else None
        )
    )
    if os.environ.get("CONTINUITY_TASK_ID"):
        outgoing["task_id"] = os.environ["CONTINUITY_TASK_ID"]
    fence = os.environ.get("CONTINUITY_CONTROLLER_TOKEN")
    if fence:
        if not re.fullmatch(r"[0-9a-f]{32}", fence):
            raise ValueError("controller fence must be the project lease token, never a provider credential")
        outgoing["controller_fence"] = fence
    # Notices are computed on the state host, so the local watch list must travel
    # with the event; a mapping's own list keeps it per project.
    watch = os.environ.get("CONTINUITY_WATCH_PROJECTS") or config.get("watch_projects")
    if isinstance(watch, str):
        watch = watch.split(",")
    if isinstance(watch, list):
        names = [str(name).strip() for name in watch if str(name).strip()]
        if names:
            outgoing["watch_projects"] = names[:32]
    from .provider_usage import capture_hook

    usage = capture_hook(payload, "claude", data_root=config.get("local_data_root"))
    if usage:
        outgoing["usage"] = usage
    bridge_usage = _delivery_snapshot(payload, config)
    if bridge_usage:
        outgoing["bridge_usage"] = bridge_usage
    if len(json.dumps(outgoing).encode()) > 16384:
        raise ValueError("forwarded hook metadata exceeds 16 KiB")
    local_result = handle_event(
        payload,
        host="claude-bridge",
        data_root=config.get("local_data_root"),
        meter_injection=False,
        record_protocol=False,
    )
    try:
        endpoint = config.get("remote_endpoint", "continuity-remote-receive")
        if (
            not isinstance(endpoint, str)
            or Path(endpoint).name != "continuity-remote-receive"
            or any(c in endpoint for c in "\n\r\x00")
        ):
            raise ValueError("remote_endpoint must name continuity-remote-receive")
        control = Path.home() / ".cache/continuity-ssh"
        control.mkdir(parents=True, exist_ok=True, mode=0o700)
        response = runner(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=3",
                "-o",
                "ClearAllForwardings=yes",
                "-o",
                "ControlMaster=auto",
                "-o",
                "ControlPersist=60",
                "-o",
                "ControlPath=" + str(control / "%C"),
                host,
                shlex.quote(endpoint),
            ],
            input=json.dumps(outgoing),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if response.returncode != 0:
            raise ValueError("remote hook failed")
        if len(response.stdout.encode()) > 16384:
            raise ValueError("remote hook output exceeds 16 KiB")
        output = (
            json.loads(response.stdout)
            if response.stdout.strip()
            else {"continue": True, "status": "remote-quiet"}
        )
        if (
            not isinstance(output, dict)
            or output.get("continue") is False
            or output.get("decision") == "block"
        ):
            raise ValueError("remote hook attempted a blocking response")
        context = output.get("hookSpecificOutput", {}).get("additionalContext", "")
        if not isinstance(context, str) or len(context.encode()) > 4096:
            raise ValueError("remote context exceeds budget")
        if (
            payload.get("hook_event_name") == "PreCompact"
            and output.get("status") != "saved"
        ):
            raise ValueError("remote checkpoint was not acknowledged")
        if payload.get("hook_event_name") == "PreCompact" and output.get(
            "portable_handoff"
        ):
            from .lifecycle import _digest
            from .collaboration_registry import _atomic_write, default_data_root

            saved = output["portable_handoff"]
            if (
                not isinstance(saved, dict)
                or saved.get("root") != remote
                or saved.get("session") != payload.get("session_id")
                or saved.get("digest") != _digest(saved)
            ):
                raise ValueError(
                    "remote handoff identity/digest does not match this session"
                )
            local_saved = {
                **saved,
                "root": str(local.resolve()),
                "remote_source": {"root": remote, "digest": saved["digest"]},
            }
            local_saved["digest"] = _digest(local_saved)
            key = hashlib.sha256(
                (str(local.resolve()) + ":" + payload["session_id"]).encode()
            ).hexdigest()
            cache = (
                Path(config.get("local_data_root") or default_data_root())
                / "handoffs"
                / (key + ".json")
            )
            _atomic_write(cache, local_saved)
        if (
            payload.get("hook_event_name") in {"SessionStart", "PostCompact"}
            and not context
            and local_result.get("hookSpecificOutput")
        ):
            return local_result
        return output
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {
            **local_result,
            "continue": True,
            "bridge_status": "unavailable-local-fallback",
        }


def main(argv=None):
    if os.environ.get("CONTINUITY_DISABLED") == "1":
        return 0
    parser = argparse.ArgumentParser(
        description="Forward Claude lifecycle metadata over SSH; no transcript transfer or work gate"
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    try:
        path = Path(args.config).expanduser()
        with path.open("rb") as stream:
            config = json.loads(stream.read(16385))
        raw = sys.stdin.buffer.read(524289)
        if len(raw) > 524288:
            return 0
        payload = json.loads(raw)
        result = forward(payload, config)
        from .adoption import cli_nudge, merge_context

        merge_context(
            result,
            "PostToolUse",
            cli_nudge(
                payload,
                config.get("local_data_root")
                or Path.home() / ".local/state/continuity",
            ),
        )
        if result.get("hookSpecificOutput"):
            result["injected_bytes"] = len(
                result["hookSpecificOutput"]["additionalContext"].encode()
            )
            print(
                json.dumps(
                    {"hookSpecificOutput": result["hookSpecificOutput"]},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        elif (
            result.get("bridge_status")
            and os.environ.get("CONTINUITY_HOOK_DEBUG") == "1"
        ):
            print(
                "Continuity bridge unavailable; local recovery remains available; ordinary work continues.",
                file=sys.stderr,
            )
        _record_front_output(payload, result, config)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        if os.environ.get("CONTINUITY_HOOK_DEBUG") == "1":
            print(
                f"Continuity bridge: {str(exc)[:180]}; ordinary work continues.",
                file=sys.stderr,
            )
    return 0


def receive_main():
    """Return an explicit checkpoint receipt to the SSH bridge on every event."""
    from .lifecycle import handle_event

    if os.environ.get("CONTINUITY_DISABLED") == "1":
        print(json.dumps({"continue": True, "status": "disabled"}))
        return 0

    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError("remote hook input exceeds 16 KiB")
        payload = json.loads(raw)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("cwd"), str)
            or not Path(payload["cwd"]).is_absolute()
        ):
            raise ValueError("absolute mapped project root is required")
        from .collaboration_registry import resolve_project

        if resolve_project(payload["cwd"]) is None:
            raise ValueError("remote project root is not registered")
        event = payload.get("hook_event_name")
        actor = payload.get("assignee")
        if actor:
            from .collaboration_registry import _ASSIGNEE_RE

            if not isinstance(actor, str) or not _ASSIGNEE_RE.fullmatch(actor):
                raise ValueError("invalid actor identity")
            os.environ["CONTINUITY_ASSIGNEE"] = actor
        if payload.get("continuity_role") in {"worker", "reviewer", "controller"}:
            os.environ["CONTINUITY_ROLE"] = payload["continuity_role"]
        controller = payload.get("controller")
        if controller:
            from .collaboration_registry import _ASSIGNEE_RE

            if not isinstance(controller, str) or not _ASSIGNEE_RE.fullmatch(
                controller
            ):
                raise ValueError("invalid controller identity")
            os.environ["CONTINUITY_CONTROLLER"] = controller
        if payload.get("watch_projects"):
            from .collaboration_registry import _ID_RE

            watch = payload["watch_projects"]
            if not isinstance(watch, list) or not all(
                isinstance(name, str) and _ID_RE.fullmatch(name) for name in watch
            ):
                raise ValueError("invalid watch project list")
            os.environ["CONTINUITY_WATCH_PROJECTS"] = ",".join(watch)
        if payload.get("controller_fence"):
            fence = payload["controller_fence"]
            if not isinstance(fence, str) or not re.fullmatch(r"[0-9a-f]{32}", fence):
                raise ValueError("invalid project controller fence")
            os.environ["CONTINUITY_CONTROLLER_TOKEN"] = fence
        if isinstance(payload.get("task_id"), str):
            os.environ["CONTINUITY_TASK_ID"] = payload["task_id"]
        if payload.get("usage"):
            from .provider_usage import import_snapshot

            import_snapshot(payload["cwd"], payload["usage"])
        if payload.get("bridge_usage"):
            from .usage_meter import import_hook_snapshot

            import_hook_snapshot(payload["cwd"], payload["bridge_usage"])
        if payload.get("adoption"):
            from .adoption import record_forwarded

            record_forwarded(payload["adoption"], payload.get("cwd"))
        result = handle_event(payload, host="claude-remote", meter_injection=False)
        if event == "PreCompact" and result.get("status") == "saved":
            from .collaboration_registry import default_data_root

            key = hashlib.sha256(
                (
                    str(Path(payload["cwd"]).resolve()) + ":" + payload["session_id"]
                ).encode()
            ).hexdigest()
            saved = default_data_root() / "handoffs" / (key + ".json")
            result["portable_handoff"] = json.loads(saved.read_text())
        if (
            event in {"UserPromptSubmit", "SessionStart"}
            and payload.get("source") != "compact"
        ):
            from .planning_adoption import context

            base = result.get("hookSpecificOutput", {}).get("additionalContext", "")
            hint = context(
                payload,
                host="claude-remote",
                max_bytes=4096 - len(base.encode()) - 1,
            )
            if hint:
                combined = (base + "\n" + hint).strip()
                if len(combined.encode()) <= 4096:
                    result["hookSpecificOutput"] = {
                        "hookEventName": event,
                        "additionalContext": combined,
                    }
        from .controller_awareness import notice

        base = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        note = notice(payload, max_bytes=min(1024, 4096 - len(base.encode()) - 1))
        if note:
            result["hookSpecificOutput"] = {
                "hookEventName": event,
                "additionalContext": (base + "\n" + note).strip(),
            }
        if payload.get("prompt_omitted"):
            from .planning_adoption import omitted_checklist_hint

            base = result.get("hookSpecificOutput", {}).get("additionalContext", "")
            reminder = omitted_checklist_hint(payload, max_bytes=4096 - len(base.encode()) - 1)
            if reminder and len((base + "\n" + reminder).encode()) <= 4096:
                result["hookSpecificOutput"] = {
                    "hookEventName": event,
                    "additionalContext": (base + "\n" + reminder).strip(),
                }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(
            json.dumps(
                {"continue": True, "status": "remote-error", "reason": str(exc)[:256]}
            )
        )
        return 2


def install_profile(profile, config_file, executable):
    """Install only this profile's hooks, preserving existing fields and a byte backup."""
    from .collaboration_registry import _atomic_write, _locked

    profile = Path(profile).expanduser().resolve()
    if not profile.is_dir():
        raise ValueError("profile directory does not exist")
    config_file = Path(config_file).expanduser().resolve()
    config = json.loads(config_file.read_text())
    if (
        not isinstance(config, dict)
        or not config.get("host")
        or not isinstance(config.get("mappings"), list)
    ):
        raise ValueError("bridge config requires host and explicit root mappings")
    executable = Path(executable).resolve()
    if not executable.is_file():
        raise ValueError("bridge executable does not exist")
    previous_configs = config.get("supersedes_config_files", [])
    if not isinstance(previous_configs, list) or not all(
        isinstance(path, str) and Path(path).is_absolute() for path in previous_configs
    ):
        raise ValueError("supersedes_config_files must be an array of absolute paths")
    managed_configs = {str(config_file), *previous_configs}
    settings = profile / "settings.json"
    command = shlex.join([str(executable), "--config", str(config_file)])
    events = {
        "SessionStart": "startup|resume|compact",
        "PreCompact": "manual|auto",
        "PostCompact": "manual|auto",
        "UserPromptSubmit": "",
        "PostToolUse": "",
        "Stop": "",
        "SubagentStart": "",
        "SubagentStop": "",
        "SessionEnd": "",
    }
    with _locked(settings):
        original = settings.read_bytes() if settings.exists() else b"{}\n"
        document = json.loads(original)
        if not isinstance(document, dict) or not isinstance(
            document.get("hooks", {}), dict
        ):
            raise ValueError("settings hooks must be an object")
        before = json.dumps(document, sort_keys=True)
        disabled_duplicates = []
        for plugin in ("continuity-plane@continuity-plane",):
            if document.get("enabledPlugins", {}).get(plugin) is True:
                document["enabledPlugins"][plugin] = False
                disabled_duplicates.append(plugin)
        hooks = document.setdefault("hooks", {})
        for event, matcher in events.items():
            entries = hooks.setdefault(event, [])
            if not isinstance(entries, list):
                raise ValueError("existing event hooks must be arrays")
            managed = False
            remove_entries = set()
            for entry in entries:
                if not isinstance(entry, dict) or not isinstance(
                    entry.get("hooks"), list
                ):
                    continue
                kept = []
                had_hooks = bool(entry["hooks"])
                for hook in entry["hooks"]:
                    try:
                        tokens = (
                            shlex.split(hook.get("command", ""))
                            if isinstance(hook, dict)
                            else []
                        )
                    except ValueError:
                        tokens = []
                    owned = bool(
                        tokens
                        and Path(tokens[0]).name == "continuity-remote-hook"
                        and "--config" in tokens
                        and tokens.index("--config") + 1 < len(tokens)
                        and tokens[tokens.index("--config") + 1] in managed_configs
                    )
                    if owned:
                        if managed:
                            continue
                        hook = {**hook, "command": command, "timeout": 15}
                        managed = True
                    kept.append(hook)
                entry["hooks"] = kept
                if had_hooks and not kept:
                    remove_entries.add(id(entry))
            entries[:] = [entry for entry in entries if id(entry) not in remove_entries]
            if managed:
                continue
            entries.append(
                {
                    "matcher": matcher,
                    "hooks": [{"type": "command", "command": command, "timeout": 15}],
                }
            )
        changed = before != json.dumps(document, sort_keys=True)
        backup = None
        if changed:
            backup = settings.with_name(
                "settings.json.before-continuity-"
                + hashlib.sha256(original).hexdigest()[:16]
            )
            if not backup.exists():
                with backup.open("xb") as stream:
                    stream.write(original)
            _atomic_write(settings, document)
    return {
        "changed": changed,
        "profile": str(profile),
        "backup": str(backup) if backup else None,
        "events": list(events),
        "command": command,
        "disabled_duplicate_plugins": disabled_duplicates,
        "mcp_update": _install_mcp_endpoints(profile, config),
        "skill_update": _install_skill(profile, config.get("skill_source")),
        "guidance_update": _install_guidance(profile, config.get("profile_guidance")),
    }


def _install_guidance(profile, note):
    if not note:
        return {"changed": False}
    if not isinstance(note, str) or len(note.encode()) > 4096:
        raise ValueError("profile guidance must be at most 4 KiB")
    from .worker_dispatch import _write
    from .collaboration_registry import _locked

    path = profile / "CLAUDE.md"
    with _locked(path):
        original = path.read_text() if path.exists() else ""
        block = (
            "<!-- continuity:profile-guidance -->\n"
            + note.strip()
            + "\n<!-- /continuity:profile-guidance -->"
        )
        pattern = r"<!-- continuity:profile-guidance -->.*?<!-- /continuity:profile-guidance -->"
        updated = (
            re.sub(pattern, lambda _: block, original, flags=re.S)
            if re.search(pattern, original, re.S)
            else original.rstrip() + "\n\n" + block + "\n"
        )
        if updated == original:
            return {"changed": False}
        backup = (
            profile
            / "continuity/backups"
            / ("CLAUDE-" + hashlib.sha256(original.encode()).hexdigest()[:16] + ".md")
        )
        if not backup.exists():
            _write(backup, original)
        _write(path, updated)
    return {"changed": True, "backup": str(backup), "target": str(path)}


def _install_skill(profile, source):
    if not source:
        return {"changed": False}
    import shutil

    source = Path(source).resolve()
    if not (source / "SKILL.md").is_file():
        raise ValueError("skill source has no SKILL.md")
    files = [p for p in source.rglob("*") if p.is_file()]
    if (
        any(p.is_symlink() for p in source.rglob("*"))
        or sum(p.stat().st_size for p in files) > 1048576
    ):
        raise ValueError("skill source must be a small local bundle without symlinks")
    target = profile / "skills/continuity-collaboration"
    changed = any(
        not (target / p.relative_to(source)).is_file()
        or (target / p.relative_to(source)).read_bytes() != p.read_bytes()
        for p in files
    )
    backup = None
    if changed:
        if target.exists():
            digest = hashlib.sha256(
                b"".join(
                    p.read_bytes() for p in sorted(target.rglob("*")) if p.is_file()
                )
            ).hexdigest()[:16]
            backup = profile / "continuity/backups" / ("skill-" + digest)
            if not backup.exists():
                shutil.copytree(target, backup)
        shutil.copytree(source, target, dirs_exist_ok=True)
    return {
        "changed": changed,
        "target": str(target),
        "backup": str(backup) if backup else None,
    }


def _install_mcp_endpoints(profile, config):
    from .collaboration_registry import _atomic_write, _locked

    endpoints = config.get("mcp_endpoints")
    if not endpoints:
        return {"changed": False}
    path = profile / ".claude.json"
    if not path.exists():
        return {"changed": False, "reason": "profile MCP settings file not present"}
    with _locked(path):
        original = path.read_bytes()
        document = json.loads(original)
        changed = []
        for name, endpoint in endpoints.items():
            if name not in {"continuity", "continuity-search"} or Path(
                endpoint
            ).name not in {"continuity-mcp", "continuity-search-mcp"}:
                raise ValueError("invalid continuity MCP endpoint")
            server = document.get("mcpServers", {}).get(name)
            if not isinstance(server, dict) or server.get("command") != "ssh":
                continue
            argv = [
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ClearAllForwardings=yes",
                config["host"],
                shlex.quote(endpoint),
            ]
            if server.get("args") != argv:
                server["args"] = argv
                changed.append(name)
        backup = None
        if changed:
            backup = path.with_name(
                ".claude.json.before-continuity-"
                + hashlib.sha256(original).hexdigest()[:16]
            )
            if not backup.exists():
                with backup.open("xb") as stream:
                    stream.write(original)
            _atomic_write(path, document)
    return {
        "changed": bool(changed),
        "servers": changed,
        "backup": str(backup) if backup else None,
        "reconnect_required": bool(changed),
    }


def register_cli(commands):
    parser = commands.add_parser(
        "bridge",
        help="Install an explicitly configured SSH hook bridge in one Claude profile",
    )
    sub = parser.add_subparsers(dest="bridge_action", required=True)
    install = sub.add_parser("install")
    install.add_argument("--profile", required=True)
    install.add_argument("--config-file", required=True)
    install.add_argument(
        "--executable",
        required=True,
        help="Absolute continuity-remote-hook executable in the controller environment",
    )
    install.set_defaults(handler=lambda args: _install_command(args))


def _install_command(args):
    print(
        json.dumps(
            install_profile(args.profile, args.config_file, args.executable),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
