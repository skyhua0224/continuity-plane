#!/usr/bin/env python3
"""Codex lifecycle bridge with bounded output and sanitized local telemetry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MAX_PACKET_BYTES = 8 * 1024
MAX_CONTEXT_BYTES = 12 * 1024
MAX_TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 3
RECOVERY_WINDOW_SECONDS = 5 * 60
RECOVERY_READ_BUDGET_BYTES = 12 * 1024
EFFECT_INTENT_SECONDS = 2 * 60
RECOVERY_RULE_IDS = [
    "continuity.answer.bounded",
    "continuity.answer.direct",
    "continuity.answer.no-recovery-narration",
    "continuity.effect.read-only",
    "continuity.question.no-advance",
    "continuity.resume.bounded-read",
    "continuity.resume.current-state",
    "continuity.work.sticky",
]
SESSION_BINDING_SCHEMA = "context.codex-session-project-bindings/v1alpha1"
CONTINUITY_RESUME_TOOL = "mcp__continuity__continuity_resume"
DELIVERY_WORKSPACE_REGISTRY_SCHEMA = (
    "context.delivery-workspace-registry/v1alpha1"
)

_EFFECT_PATTERNS = (
    (
        "source-control",
        re.compile(
            r"(?:^|[;&|]\s*)(?:git\s+(?:add|commit|push|merge|rebase|reset)\b|"
            r"git\s+tag\s+(?!(?:-l|--list|--contains|--points-at|--merged|"
            r"--no-merged|--sort|--format)\b)|"
            r"tea\s+(?:pulls?\s+(?:create|merge)|releases?)|"
            r"gh\s+(?:pr\s+(?:create|merge)|release\s+(?:create|delete|edit|upload))|"
            r"glab\s+(?:mr\s+(?:create|merge)|release\s+(?:create|delete|update|upload)))\b",
            re.IGNORECASE,
        ),
    ),
    (
        "deployment",
        re.compile(
            r"(?:^|[;&|]\s*)(?:kubectl\s+(?:apply|delete|patch|replace|rollout|scale|set)|"
            r"helm\s+(?:install|upgrade|uninstall|rollback)|"
            r"docker\s+compose\s+(?:up|down|restart)|"
            r"systemctl\s+(?:start|stop|restart|enable|disable)|"
            r"terraform\s+(?:apply|destroy|import)|ansible-playbook\b|"
            r"azd\s+(?:up|deploy)|wrangler\s+deploy|vercel\s+(?:deploy|--prod)|"
            r"(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
            r"(?:\S*/)?deploy(?:[-_][a-z0-9.-]+)?\.sh\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "remote-effect",
        re.compile(r"(?:^|[;&|]\s*)(?:ssh|scp)\b", re.IGNORECASE),
    ),
    (
        "package-publish",
        re.compile(
            r"(?:^|[;&|]\s*)(?:npm\s+publish|pnpm\s+publish|yarn\s+npm\s+publish|"
            r"twine\s+upload|cargo\s+publish|python\s+-m\s+twine\s+upload)\b",
            re.IGNORECASE,
        ),
    ),
)

_RECOVERY_SOURCE_RE = re.compile(
    r"(?:^|[/\\\s'\"])(?:MASTER|STATUS(?:\.current)?|AGENTS)\.md"
    r"(?:$|[/\\\s'\"])|"
    r"(?:^|[/\\\s'\"])SKILL\.md(?:$|[/\\\s'\"])",
    re.IGNORECASE,
)
_WHOLE_FILE_READERS = {"cat", "less", "more"}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _session_binding_path(payload: dict[str, Any]) -> Path | None:
    data = os.environ.get("PLUGIN_DATA")
    session_id = payload.get("session_id")
    if not data or not isinstance(session_id, str) or not session_id:
        return None
    return Path(data) / "session-bindings" / f"{_hash(session_id)}.json"


def _read_session_binding(payload: dict[str, Any]) -> dict[str, Any] | None:
    path = _session_binding_path(payload)
    session_id = payload.get("session_id")
    if path is None or not isinstance(session_id, str):
        return None
    try:
        binding = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(binding, dict):
        return None
    digest = binding.get("binding_sha256")
    expected = _hash(
        _canonical(
            {
                key: value
                for key, value in binding.items()
                if key != "binding_sha256"
            }
        )
    )
    if not isinstance(session_id, str) or not session_id or digest != expected:
        return None
    if binding.get("session_sha256") != _hash(session_id):
        return None
    fields = {
        "schema_version",
        "session_sha256",
        "active_project_root",
        "projects",
        "binding_sha256",
    }
    if binding.get("schema_version") != SESSION_BINDING_SCHEMA or set(binding) != fields:
        legacy_fields = {
            "schema_version",
            "session_sha256",
            "project_id",
            "project_root",
            "profile_sha256",
            "binding_sha256",
        }
        if binding.get("schema_version") != "context.codex-session-project-binding/v1alpha1" or set(binding) != legacy_fields:
            return None
        binding = {
            "schema_version": SESSION_BINDING_SCHEMA,
            "session_sha256": binding["session_sha256"],
            "active_project_root": binding["project_root"],
            "projects": [
                {
                    "project_id": binding["project_id"],
                    "project_root": binding["project_root"],
                    "profile_sha256": binding["profile_sha256"],
                }
            ],
            "binding_sha256": "",
        }
        binding["binding_sha256"] = _hash(
            _canonical(
                {
                    key: value
                    for key, value in binding.items()
                    if key != "binding_sha256"
                }
            )
        )
    return binding


def _session_bound_roots(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    binding = _read_session_binding(payload)
    if binding is None:
        return None
    projects = binding.get("projects")
    active_root = binding.get("active_project_root")
    if (
        not isinstance(projects, list)
        or not projects
        or not isinstance(active_root, str)
        or not Path(active_root).is_absolute()
    ):
        return None
    roots: list[dict[str, Any]] = []
    for item in projects:
        if (
            not isinstance(item, dict)
            or set(item) != {"project_id", "project_root", "profile_sha256"}
            or not isinstance(item.get("project_id"), str)
            or not item["project_id"]
            or not isinstance(item.get("project_root"), str)
            or not Path(item["project_root"]).is_absolute()
            or not isinstance(item.get("profile_sha256"), str)
        ):
            return None
        root = Path(item["project_root"]).resolve()
        profile = root / ".continuity/project.yaml"
        if not profile.is_file() or _file_hash(profile) != item["profile_sha256"]:
            return None
        roots.append(
            {
                "project_id": item["project_id"],
                "project_root": root,
                "profile_sha256": item["profile_sha256"],
            }
        )
    if not any(item["project_root"] == Path(active_root).resolve() for item in roots):
        return None
    return roots


def _session_bound_root(payload: dict[str, Any]) -> Path | None:
    roots = _session_bound_roots(payload)
    binding = _read_session_binding(payload)
    if roots is None or binding is None:
        return None
    active_root = Path(binding["active_project_root"]).resolve()
    return active_root if any(item["project_root"] == active_root for item in roots) else None


def _resume_tool_packet(payload: dict[str, Any]) -> dict[str, Any] | None:
    response = payload.get("tool_response")
    if not isinstance(response, dict) or response.get("isError") is True:
        return None
    candidates: list[Any] = [response.get("structuredContent"), response]
    content = response.get("content")
    if isinstance(content, list):
        candidates.extend(
            item.get("text")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        )
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except json.JSONDecodeError:
                continue
        if not isinstance(candidate, dict):
            continue
        packet = _load_resume_packet(_canonical(candidate).encode("utf-8"))
        if packet is not None:
            return packet
    return None


def _record_resume_binding(payload: dict[str, Any]) -> str:
    if payload.get("tool_name") != CONTINUITY_RESUME_TOOL:
        return "ignored"
    packet = _resume_tool_packet(payload)
    tool_input = payload.get("tool_input")
    if packet is None or not isinstance(tool_input, dict):
        return "ignored"
    root_value = tool_input.get("root")
    if not isinstance(root_value, str) or not root_value:
        return "ignored"
    existing_binding = _read_session_binding(payload)
    existing_roots = _session_bound_roots(payload)
    if existing_binding is not None and existing_roots is None:
        # A successful explicit resume is allowed to repair a stale profile digest.
        existing_roots = [
            {
                "project_id": item["project_id"],
                "project_root": Path(item["project_root"]).resolve(),
                "profile_sha256": item["profile_sha256"],
            }
            for item in existing_binding.get("projects", [])
            if isinstance(item, dict)
            and isinstance(item.get("project_id"), str)
            and isinstance(item.get("project_root"), str)
            and isinstance(item.get("profile_sha256"), str)
        ]
    requested = Path(root_value)
    if not requested.is_absolute():
        cwd = payload.get("cwd")
        base = _session_bound_root(payload) or (Path(cwd) if isinstance(cwd, str) else None)
        if base is None:
            return "ignored"
        requested = base / requested
    try:
        root = requested.resolve()
    except OSError:
        return "ignored"
    profile = root / ".continuity/project.yaml"
    profile_sha256 = _file_hash(profile)
    project_id = packet.get("project_id")
    if (
        profile_sha256 is None
        or not isinstance(project_id, str)
        or not project_id
    ):
        return "ignored"
    path = _session_binding_path(payload)
    session_id = payload.get("session_id")
    if path is None or not isinstance(session_id, str):
        return "ignored"
    for item in existing_roots or []:
        if item["project_root"] == root and item["project_id"] != project_id:
            return "conflict"
    projects = [
        {
            "project_id": item["project_id"],
            "project_root": str(item["project_root"]),
            "profile_sha256": item["profile_sha256"],
        }
        for item in existing_roots or []
        if item["project_root"] != root
    ]
    projects.append(
        {
            "project_id": project_id,
            "project_root": str(root),
            "profile_sha256": profile_sha256,
        }
    )
    projects.sort(key=lambda item: item["project_root"])
    document = {
        "schema_version": SESSION_BINDING_SCHEMA,
        "session_sha256": _hash(session_id),
        "active_project_root": str(root),
        "projects": projects,
        "binding_sha256": "",
    }
    document["binding_sha256"] = _hash(
        _canonical(
            {
                key: value
                for key, value in document.items()
                if key != "binding_sha256"
            }
        )
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(_canonical(document) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return "recorded"


def _message_text(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(parts)


def _tail_json(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            offset = max(0, size - MAX_TRANSCRIPT_TAIL_BYTES)
            source.seek(offset)
            data = source.read(MAX_TRANSCRIPT_TAIL_BYTES)
    except OSError:
        return []
    if offset:
        newline = data.find(b"\n")
        data = b"" if newline < 0 else data[newline + 1 :]
    events = []
    for line in data.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def derive_recent_interaction_cursor(path: Path) -> dict[str, Any] | None:
    """Hash the latest Codex user/visible-output pair without retaining transcript text."""
    current: dict[str, Any] | None = None
    for event in _tail_json(path):
        if event.get("type") != "response_item":
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "message":
            continue
        role = payload.get("role")
        text = _message_text(payload.get("content"))
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if role == "user" and text:
            content_sha = _hash(text)
            current = {
                "current_input_ref": f"input://sha256/{content_sha}",
                "current_input_sha256": content_sha,
                "current_turn_sha256": _hash(turn_id) if isinstance(turn_id, str) else None,
                "confirmed_input_refs": [],
                "visible_output_high_watermark_sha256": None,
                "visible_output_phase": None,
                "response_mode": "answer-current-input",
                "no_restate": False,
            }
        elif role == "assistant" and text and current is not None:
            phase = payload.get("phase")
            current["visible_output_high_watermark_sha256"] = _hash(text)
            current["visible_output_phase"] = (
                phase if phase in {"commentary", "final_answer"} else "other"
            )
            current["no_restate"] = True
            if phase == "final_answer":
                current["confirmed_input_refs"] = [current["current_input_ref"]]
                current["response_mode"] = "continue-silently"
            else:
                current["response_mode"] = "continue-without-restatement"
    if current is None:
        return None
    cursor = {
        "schema_version": "context.interaction-cursor/v1alpha1",
        **current,
        "raw_transcript_admission": False,
        "state_write_authority": False,
        "completion_authority": False,
        "cursor_sha256": "",
    }
    cursor["cursor_sha256"] = _hash(
        _canonical({key: value for key, value in cursor.items() if key != "cursor_sha256"})
    )
    return cursor


def _project_root(cwd: str) -> Path | None:
    start = Path(cwd).resolve()
    candidates = [start, *start.parents]
    common_dir: Path | None = None
    try:
        completed = subprocess.run(
            [
                "git", "-C", str(start), "rev-parse", "--path-format=absolute",
                "--show-toplevel", "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode == 0:
            lines = completed.stdout.splitlines()
            candidates.insert(0, Path(lines[0]).resolve())
            common_dir = Path(lines[1]).resolve()
    except (OSError, subprocess.SubprocessError):
        pass
    if common_dir is not None:
        binding_path = common_dir / "continuity-plane/project-root.json"
        try:
            binding = json.loads(binding_path.read_text(encoding="utf-8"))
            digest = binding.get("binding_sha256")
            expected = _hash(
                json.dumps(
                    {
                        key: value
                        for key, value in binding.items()
                        if key != "binding_sha256"
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            bound_root = Path(binding["control_root"]).resolve()
            profile = bound_root / ".continuity/project.yaml"
            if (
                binding.get("schema_version")
                == "context.git-workspace-binding/v1alpha1"
                and digest == expected
                and profile.is_file()
                and _file_hash(profile) == binding.get("profile_sha256")
            ):
                bound_common = subprocess.run(
                    [
                        "git", "-C", str(bound_root), "rev-parse",
                        "--path-format=absolute", "--git-common-dir",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                if (
                    bound_common.returncode == 0
                    and Path(bound_common.stdout.strip()).resolve() == common_dir
                ):
                    return bound_root
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            listed = subprocess.run(
                ["git", "-C", str(start), "worktree", "list", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            discovered = [
                Path(line.removeprefix("worktree ")).resolve()
                for line in listed.stdout.splitlines()
                if line.startswith("worktree ")
                and (
                    Path(line.removeprefix("worktree ")).resolve()
                    / ".continuity/project.yaml"
                ).is_file()
            ]
            if len(discovered) == 1:
                return discovered[0]
            if len(discovered) > 1:
                raise RuntimeError(
                    "multiple Continuity roots share this Git repository"
                )
        except OSError:
            pass
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / ".continuity/project.yaml").is_file():
            return candidate
    return None


def _command(arguments: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("continuity")
    candidates: list[list[str]] = []
    if executable:
        candidates.append([executable, *arguments, "--root", str(root)])
    candidates.extend(
        [
            [sys.executable, "-m", "continuity_plane.cli", *arguments, "--root", str(root)],
            [
                sys.executable,
                "-m",
                "continuity_plane.cli",
                *arguments,
                "--root",
                str(root),
            ],
        ]
    )
    last: subprocess.CompletedProcess[str] | None = None
    for command in candidates:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
        last = completed
        if completed.returncode == 0:
            return completed
        if "No module named" not in completed.stderr:
            return completed
    if last is not None:
        return last
    raise RuntimeError("Continuity CLI is unavailable")


def _observation_path(payload: dict[str, Any]) -> Path | None:
    data = os.environ.get("PLUGIN_DATA")
    session_id = payload.get("session_id")
    if not data or not isinstance(session_id, str) or not session_id:
        return None
    directory = Path(data) / "live-events"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_hash(session_id)}.jsonl"


def _cursor_path(payload: dict[str, Any]) -> Path | None:
    data = os.environ.get("PLUGIN_DATA")
    session_id = payload.get("session_id")
    if not data or not isinstance(session_id, str) or not session_id:
        return None
    directory = Path(data) / "interaction-cursors"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{_hash(session_id)}.json"


def _skill_lock_path() -> Path | None:
    data = os.environ.get("PLUGIN_DATA")
    root = os.environ.get("PLUGIN_ROOT")
    if not data or not root:
        return None
    skill = Path(root) / "skills/continuity-plane/SKILL.md"
    try:
        skill_bytes = skill.read_bytes()
    except OSError:
        return None
    compiled = hashlib.sha256(
        skill_bytes + b"\n" + _canonical(RECOVERY_RULE_IDS).encode("utf-8")
    ).hexdigest()
    document = {
        "status": "measured",
        "selected_rule_ids": RECOVERY_RULE_IDS,
        "compiled_packet_sha256": compiled,
        "unavailable_reason": None,
    }
    directory = Path(data) / "skill-locks"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / "continuity-plane.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(_canonical(document) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return target


def _write_cursor(payload: dict[str, Any]) -> dict[str, Any] | None:
    transcript = payload.get("transcript_path")
    target = _cursor_path(payload)
    if not isinstance(transcript, str) or not transcript or target is None:
        return None
    cursor = derive_recent_interaction_cursor(Path(transcript))
    if cursor is None:
        return None
    temporary = target.with_suffix(".tmp")
    temporary.write_text(_canonical(cursor) + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return cursor


def _read_cursor(payload: dict[str, Any]) -> dict[str, Any] | None:
    target = _cursor_path(payload)
    if target is None:
        return None
    try:
        cursor = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(cursor, dict):
        return None
    digest = cursor.get("cursor_sha256")
    expected = _hash(
        _canonical({key: value for key, value in cursor.items() if key != "cursor_sha256"})
    )
    if not isinstance(digest, str) or digest != expected:
        return None
    return cursor


def _observe(
    payload: dict[str, Any],
    root: Path,
    *,
    event_type: str,
    success: bool,
    canary_passed: bool | None = None,
    source_refreshed: bool = False,
    tool_name: str | None = None,
    effect_class: str | None = None,
    decision: str | None = None,
    recovery_read_bytes: int | None = None,
    recovery_read_budget_bytes: int | None = None,
    tool_output_bytes: int | None = None,
    context_admitted: bool | None = None,
) -> None:
    path = _observation_path(payload)
    if path is None:
        return
    record = {
        "schema_version": "context.codex-hook-observation/v1alpha1",
        "event_type": event_type,
        "observed_at": datetime.now(UTC).isoformat(),
        "session_sha256": _hash(str(payload.get("session_id", ""))),
        "turn_sha256": (
            _hash(str(payload["turn_id"])) if payload.get("turn_id") is not None else None
        ),
        "project_root_sha256": _hash(str(root)),
        "trigger": payload.get("trigger") or payload.get("source"),
        "model_id": payload.get("model"),
        "success": success,
        "canary_passed": canary_passed,
        "raw_transcript_admission": False,
        "state_write_authority": False,
        "completion_authority": False,
        "source_refreshed": source_refreshed,
        "plugin_loaded": True,
        "tool_name": tool_name,
        "effect_class": effect_class,
        "decision": decision,
        "recovery_read_bytes": recovery_read_bytes,
        "recovery_read_budget_bytes": recovery_read_budget_bytes,
        "tool_output_bytes": tool_output_bytes,
        "context_admitted": context_admitted,
        "plugin_manifest_sha256": _file_hash(
            Path(os.environ.get("PLUGIN_ROOT", "")) / ".codex-plugin/plugin.json"
        ),
        "hook_contract_sha256": _file_hash(
            Path(os.environ.get("PLUGIN_ROOT", "")) / "hooks/hooks.json"
        ),
    }
    serialized = _canonical(record) + "\n"
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, serialized.encode("utf-8"))
    finally:
        os.close(descriptor)


def _stop(reason: str) -> None:
    print(
        _canonical(
            {
                "continue": False,
                "stopReason": reason,
                "systemMessage": reason,
            }
        )
    )


def _continuation_context(packet: dict[str, Any]) -> str | None:
    context = (
        "Continuity recovery contract. Continue silently: do not mention recovery, "
        "compaction, hooks, or reloading. Do not answer acknowledged input again. "
        "Do not restate text already visible before this continuation. "
        "Use the packet as the current authority boundary; perform only its next_action "
        "after the checkpoint canary. Packet: "
        + _canonical(packet)
    )
    if len(context.encode("utf-8")) > MAX_CONTEXT_BYTES:
        return None
    return context


def _autorun_command(payload: dict[str, Any], root: Path) -> subprocess.CompletedProcess[str]:
    session_id = payload.get("session_id")
    suffix = _hash(str(session_id))[:32]
    command = ["autorun", "--session-id", f"hook-{suffix}"]
    last = subprocess.CompletedProcess(command, 1, "", "autorun failed")
    for attempt in range(3):
        last = _command(command, root)
        if last.returncode == 0:
            return last
        message = f"{last.stdout}\n{last.stderr}".lower()
        if attempt == 2 or not any(
            marker in message
            for marker in (
                "transport closed",
                "connection reset",
                "broken pipe",
                "timed out",
                "temporarily unavailable",
            )
        ):
            return last
    return last


def _autorun_packet(payload: dict[str, Any], root: Path) -> dict[str, Any] | None:
    completed = _autorun_command(payload, root)
    if completed.returncode != 0:
        return None
    try:
        result = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        return None
    packet = result.get("resume_packet") if isinstance(result, dict) else None
    return packet if _load_resume_packet(_canonical(packet).encode("utf-8")) else None


def _is_stage_test_command(command: str) -> bool:
    return bool(
        re.search(
            r"(?:^|[;&|]\s*)(?:pytest\b|python(?:3)?\s+-m\s+(?:unittest|pytest)\b|"
            r"(?:npm|pnpm|yarn)\s+test\b|cargo\s+test\b|go\s+test\b|"
            r"cmake\s+--build\b)",
            command,
            re.IGNORECASE,
        )
    )


def _load_resume_packet(encoded: bytes) -> dict[str, Any] | None:
    if not encoded or len(encoded) > MAX_PACKET_BYTES:
        return None
    try:
        packet = json.loads(encoded)
    except json.JSONDecodeError:
        return None
    if not isinstance(packet, dict):
        return None
    required = {
        "project_id",
        "revision",
        "active_work",
        "claim",
        "next_action",
        "source_fresh",
        "read_only",
    }
    if not required.issubset(packet):
        return None
    if not isinstance(packet["revision"], int) or packet["revision"] < 0:
        return None
    active = packet["active_work"]
    claim = packet["claim"]
    if (active is None) != (claim is None):
        return None
    if active is not None and (
        not isinstance(active, dict) or not isinstance(claim, dict)
    ):
        return None
    if not isinstance(packet["source_fresh"], bool) or not isinstance(
        packet["read_only"], bool
    ):
        return None
    return packet


def _shell_command(payload: dict[str, Any]) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    command = tool_input.get("command")
    if isinstance(command, str):
        return command
    if isinstance(command, list) and all(isinstance(item, str) for item in command):
        return " ".join(command)
    return ""


def _recovery_database_path() -> Path | None:
    data = os.environ.get("PLUGIN_DATA")
    if not data:
        return None
    directory = Path(data)
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "recovery-budget.sqlite3"


def _recovery_database() -> sqlite3.Connection | None:
    path = _recovery_database_path()
    if path is None:
        return None
    connection = sqlite3.connect(path, timeout=1.0)
    connection.execute("PRAGMA busy_timeout = 1000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS recovery_windows (
            session_sha256 TEXT PRIMARY KEY,
            project_root_sha256 TEXT NOT NULL,
            started_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            budget_bytes INTEGER NOT NULL,
            admitted_bytes INTEGER NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS effect_intents_v2 (
            intent_key TEXT PRIMARY KEY,
            resource_key TEXT NOT NULL,
            session_sha256 TEXT NOT NULL,
            tool_use_sha256 TEXT NOT NULL,
            claim_sha256 TEXT NOT NULL,
            effect_class TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            host_id TEXT NOT NULL,
            owner_sha256 TEXT NOT NULL,
            repository_sha256 TEXT NOT NULL,
            worktree_sha256 TEXT NOT NULL,
            branch_sha256 TEXT NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS effect_intents_v2_resource_idx "
        "ON effect_intents_v2(resource_key)"
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return connection


def _start_recovery_window(
    payload: dict[str, Any], root: Path, *, budget_bytes: int
) -> None:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    connection = _recovery_database()
    if connection is None:
        return
    now = time.time()
    with connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO recovery_windows (
                session_sha256, project_root_sha256, started_at, expires_at,
                budget_bytes, admitted_bytes
            ) VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                _hash(session_id),
                _hash(str(root)),
                now,
                now + RECOVERY_WINDOW_SECONDS,
                budget_bytes,
            ),
        )
    connection.close()


def _active_recovery_budget(
    payload: dict[str, Any], root: Path
) -> tuple[int, int] | None:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    connection = _recovery_database()
    if connection is None:
        return None
    row = connection.execute(
        """
        SELECT budget_bytes, admitted_bytes, expires_at, project_root_sha256
        FROM recovery_windows WHERE session_sha256 = ?
        """,
        (_hash(session_id),),
    ).fetchone()
    connection.close()
    if row is None or row[2] < time.time() or row[3] != _hash(str(root)):
        return None
    return int(row[0]), int(row[1])


def _admit_recovery_output(
    payload: dict[str, Any], root: Path, *, output_bytes: int
) -> tuple[bool, int, int] | None:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    connection = _recovery_database()
    if connection is None:
        return None
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT budget_bytes, admitted_bytes, expires_at, project_root_sha256
            FROM recovery_windows WHERE session_sha256 = ?
            """,
            (_hash(session_id),),
        ).fetchone()
        if row is None or row[2] < time.time() or row[3] != _hash(str(root)):
            connection.rollback()
            return None
        budget = int(row[0])
        admitted = int(row[1])
        if admitted + output_bytes > budget:
            connection.rollback()
            return False, budget, admitted
        admitted += output_bytes
        connection.execute(
            "UPDATE recovery_windows SET admitted_bytes = ? WHERE session_sha256 = ?",
            (admitted, _hash(session_id)),
        )
        connection.commit()
        return True, budget, admitted
    finally:
        connection.close()


def _is_recovery_read(command: str) -> bool:
    return bool(_RECOVERY_SOURCE_RE.search(command))


def _is_unbounded_recovery_read(command: str) -> bool:
    if not _is_recovery_read(command):
        return False
    for segment in re.split(r"(?:&&|\|\||[;|])", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        while words and ("=" in words[0] or words[0] in {"command", "env", "sudo"}):
            words.pop(0)
        if words and Path(words[0]).name in _WHOLE_FILE_READERS:
            return True
    return False


def _tool_response_bytes(payload: dict[str, Any]) -> int:
    response = payload.get("tool_response")
    if isinstance(response, str):
        return len(response.encode("utf-8"))
    return len(_canonical(response).encode("utf-8"))


def _repository_sha256(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        value = Path(completed.stdout.strip())
        if not value.is_absolute():
            value = root / value
        try:
            return _hash(str(value.resolve()))
        except OSError:
            pass
    return _hash(str(root.resolve()))


def _delivery_workspace_registry(
    root: Path,
    project_id: str,
) -> dict[str, Any] | None:
    path = root / ".continuity/local/delivery-workspaces.json"
    profile = root / ".continuity/project.yaml"
    try:
        if path.is_symlink():
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fields = {
        "schema_version",
        "project_id",
        "project_profile_sha256",
        "workspaces",
        "registry_sha256",
    }
    if (
        not isinstance(document, dict)
        or set(document) != fields
        or document.get("schema_version") != DELIVERY_WORKSPACE_REGISTRY_SCHEMA
        or document.get("project_id") != project_id
        or document.get("project_profile_sha256") != _file_hash(profile)
        or document.get("registry_sha256")
        != _hash(
            _canonical(
                {
                    key: value
                    for key, value in document.items()
                    if key != "registry_sha256"
                }
            )
        )
        or not isinstance(document.get("workspaces"), list)
    ):
        return None
    seen: set[str] = set()
    for item in document["workspaces"]:
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "workspace_id",
                "workspace_root",
                "repository_sha256",
                "allowed_effects",
            }
            or not isinstance(item.get("workspace_id"), str)
            or not item["workspace_id"]
            or item["workspace_id"] in seen
            or not isinstance(item.get("workspace_root"), str)
            or not Path(item["workspace_root"]).is_absolute()
            or not isinstance(item.get("repository_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", item["repository_sha256"])
            or not isinstance(item.get("allowed_effects"), list)
            or not item["allowed_effects"]
            or item["allowed_effects"] != sorted(set(item["allowed_effects"]))
        ):
            return None
        seen.add(item["workspace_id"])
    return document


def _tool_workdir(payload: dict[str, Any], root: Path) -> Path | None:
    tool_input = payload.get("tool_input")
    value = tool_input.get("workdir") if isinstance(tool_input, dict) else None
    if value is None:
        value = payload.get("cwd")
    if not isinstance(value, str) or not value:
        return root
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        return path.resolve()
    except OSError:
        return None


def _git_toplevel(root: Path) -> Path | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout.strip():
        return None
    try:
        return Path(completed.stdout.strip()).resolve()
    except OSError:
        return None


def _source_control_effect_root(
    payload: dict[str, Any],
    root: Path,
    claim: dict[str, Any],
    *,
    project_id: str,
    effect_action: str,
) -> Path | None:
    workdir = _tool_workdir(payload, root)
    governance_repository = _git_toplevel(root)
    effect_repository = _git_toplevel(workdir) if workdir is not None else None
    if governance_repository is None or effect_repository is None:
        return None
    scopes = claim.get("scope_owners")
    repo_scopes = {
        item.get("scope_ref")
        for item in scopes or []
        if isinstance(item, dict) and item.get("scope_kind") == "repo"
    }
    if _repository_sha256(effect_repository) == _repository_sha256(
        governance_repository
    ):
        return effect_repository if not repo_scopes else None
    registry = _delivery_workspace_registry(root, project_id)
    if registry is None:
        return None
    repository_sha256 = _repository_sha256(effect_repository)
    for item in registry["workspaces"]:
        workspace = Path(item["workspace_root"]).resolve()
        try:
            workdir.relative_to(workspace)
        except (TypeError, ValueError):
            continue
        if (
            item["repository_sha256"] == repository_sha256
            and f"repo://{item['workspace_id']}" in repo_scopes
            and effect_action in item["allowed_effects"]
        ):
            return workspace
    return None


def _git_branch(root: Path) -> str:
    try:
        symbolic = subprocess.run(
            ["git", "-C", str(root), "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if symbolic.returncode == 0 and symbolic.stdout.strip():
            return symbolic.stdout.strip()
        detached = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if detached.returncode == 0 and detached.stdout.strip():
            return f"detached:{detached.stdout.strip()}"
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def _effect_resource_identity(payload: dict[str, Any], root: Path) -> dict[str, str] | None:
    session_id = payload.get("session_id")
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    if not isinstance(tool_use_id, str) or not tool_use_id:
        tool_use_id = _hash(_shell_command(payload))
    provider = payload.get("provider") or os.environ.get("CONTINUITY_PROVIDER") or "codex"
    host = payload.get("host_id") or os.environ.get("CONTINUITY_HOST_ID") or socket.gethostname()
    repository = _repository_sha256(root)
    worktree = _hash(str(root.resolve()))
    branch = _hash(_git_branch(root))
    resource_key = _hash(
        _canonical(
            {
                "provider": str(provider),
                "host": str(host),
                "repository": repository,
                "worktree": worktree,
                "branch": branch,
            }
        )
    )
    return {
        "session_sha256": _hash(session_id),
        "tool_use_sha256": _hash(tool_use_id),
        "resource_key": resource_key,
        "provider_id": _hash(str(provider)),
        "host_id": _hash(str(host)),
        "repository_sha256": repository,
        "worktree_sha256": worktree,
        "branch_sha256": branch,
    }


def _effect_identity(
    payload: dict[str, Any], root: Path, claim: dict[str, Any]
) -> dict[str, str] | None:
    identity = _effect_resource_identity(payload, root)
    owner = claim.get("actor_ref")
    if identity is None or not isinstance(owner, str) or not owner:
        return None
    intent_key = _hash(
        _canonical(
            {
                "provider": identity["provider_id"],
                "host": identity["host_id"],
                "owner": _hash(owner),
                "repository": identity["repository_sha256"],
                "worktree": identity["worktree_sha256"],
                "branch": identity["branch_sha256"],
            }
        )
    )
    return {**identity, "intent_key": intent_key, "owner_sha256": _hash(owner)}


def _acquire_effect_intent(
    payload: dict[str, Any],
    root: Path,
    *,
    effect_class: str,
    claim: dict[str, Any],
) -> bool:
    identity = _effect_identity(payload, root, claim)
    claim_id = claim.get("claim_id")
    if identity is None or not isinstance(claim_id, str) or not claim_id:
        return False
    connection = _recovery_database()
    if connection is None:
        return False
    now = time.time()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT session_sha256, expires_at
            FROM effect_intents_v2 WHERE resource_key = ?
            """,
            (identity["resource_key"],),
        ).fetchone()
        if (
            row is not None
            and row[1] >= now
            and row[0] != identity["session_sha256"]
        ):
            connection.rollback()
            return False
        connection.execute(
            """
            INSERT OR REPLACE INTO effect_intents_v2 (
                intent_key, resource_key, session_sha256, tool_use_sha256,
                claim_sha256, effect_class, provider_id, host_id, owner_sha256,
                repository_sha256, worktree_sha256, branch_sha256, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                identity["intent_key"],
                identity["resource_key"],
                identity["session_sha256"],
                identity["tool_use_sha256"],
                _hash(claim_id),
                effect_class,
                identity["provider_id"],
                identity["host_id"],
                identity["owner_sha256"],
                identity["repository_sha256"],
                identity["worktree_sha256"],
                identity["branch_sha256"],
                now + EFFECT_INTENT_SECONDS,
            ),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def _release_effect_intent(payload: dict[str, Any]) -> None:
    session_id = payload.get("session_id")
    tool_use_id = payload.get("tool_use_id")
    if not isinstance(session_id, str) or not session_id:
        return
    if not isinstance(tool_use_id, str) or not tool_use_id:
        tool_use_id = _hash(_shell_command(payload))
    connection = _recovery_database()
    if connection is None:
        return
    with connection:
        connection.execute(
            """
            DELETE FROM effect_intents_v2
            WHERE session_sha256 = ? AND tool_use_sha256 = ?
            """,
            (
                _hash(session_id),
                _hash(tool_use_id),
            ),
        )
    connection.close()


def _effect_class(command: str) -> str | None:
    for effect_class, pattern in _EFFECT_PATTERNS:
        if pattern.search(command):
            return effect_class
        if effect_class == "remote-effect" and _rsync_has_remote_endpoint(command):
            return effect_class
    return None


def _rsync_has_remote_endpoint(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return bool(re.search(r"(?:^|[;&|]\s*)rsync\b", command, re.IGNORECASE))
    in_rsync = False
    for token in tokens:
        if token in {";", "&&", "||", "|"}:
            in_rsync = False
            continue
        if Path(token).name.lower() == "rsync":
            in_rsync = True
            continue
        if not in_rsync or token.startswith("-"):
            continue
        if token.lower().startswith("rsync://"):
            return True
        if re.match(r"^[A-Za-z]:[\\/]", token):
            continue
        if ":" not in token:
            continue
        authority = token.split(":", 1)[0]
        if authority and "/" not in authority and "\\" not in authority:
            return True
    return False


def _effect_action(command: str, effect_class: str) -> str:
    lowered = command.lower()
    if effect_class == "source-control":
        if re.search(r"\bgit\s+(?:rebase|reset)\b", lowered) or re.search(
            r"\bgit\s+commit\b[^;&|]*\s--amend\b",
            lowered,
        ):
            return "source-control.history-rewrite"
        if re.search(r"\bgit\s+push\b", lowered):
            return "source-control.push"
        if re.search(
            r"\b(?:tea\s+pulls?\s+create|gh\s+pr\s+create|glab\s+mr\s+create)\b",
            lowered,
        ):
            return "source-control.pr"
        if re.search(
            r"\b(?:git\s+merge|tea\s+pulls?\s+merge|gh\s+pr\s+merge|glab\s+mr\s+merge)\b",
            lowered,
        ):
            return "source-control.merge"
        if re.search(r"\b(?:git\s+tag|tea\s+releases?|gh\s+release|glab\s+release)\b", lowered):
            return "source-control.release"
        return "source-control.local"
    if effect_class == "deployment":
        return "deployment.deploy"
    if effect_class == "remote-effect":
        return "remote-effect.install-verification"
    if effect_class == "package-publish":
        return "package-publish.publish"
    return effect_class


def _scope_allows(
    effect_class: str, effect_action: str, claim: dict[str, Any]
) -> bool:
    scopes = claim.get("scope_owners")
    if not isinstance(scopes, list) or not scopes:
        return False
    effect_scopes = {
        scope.get("scope_ref")
        for scope in scopes
        if isinstance(scope, dict) and scope.get("scope_kind") == "effect"
    }
    if effect_scopes:
        if effect_action in effect_scopes:
            return True
        return effect_action == "source-control.local" and any(
            isinstance(scope_ref, str) and scope_ref.startswith("source-control.")
            for scope_ref in effect_scopes
        )
    if effect_action == "source-control.local":
        return all(
            isinstance(scope, dict)
            and isinstance(scope.get("scope_kind"), str)
            and isinstance(scope.get("scope_ref"), str)
            and bool(scope["scope_ref"])
            for scope in scopes
        )
    return False


def _deny_tool(reason: str) -> None:
    print(
        _canonical(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def _pretooluse(payload: dict[str, Any], root: Path) -> int:
    tool_name = payload.get("tool_name")
    if tool_name != "Bash":
        return 0
    command = _shell_command(payload)
    recovery_budget = _active_recovery_budget(payload, root)
    if recovery_budget is not None and _is_unbounded_recovery_read(command):
        budget, admitted = recovery_budget
        _observe(
            payload,
            root,
            event_type="recovery-read",
            success=False,
            tool_name="Bash",
            decision="deny",
            recovery_read_bytes=admitted,
            recovery_read_budget_bytes=budget,
            tool_output_bytes=0,
            context_admitted=False,
        )
        _deny_tool(
            "Continuity blocked an unbounded recovery read; use the current bounded "
            "projection or an explicit line/range limit."
        )
        return 0
    effect_class = _effect_class(command)
    if effect_class is None:
        return 0
    effect_action = _effect_action(command, effect_class)
    completed = _command(["resume"], root)
    packet = (
        _load_resume_packet(completed.stdout.strip().encode("utf-8"))
        if completed.returncode == 0
        else None
    )
    active = packet.get("active_work") if packet is not None else None
    claim = packet.get("claim") if packet is not None else None
    writable = (
        packet is not None
        and packet.get("read_only") is False
        and packet.get("source_fresh") is True
        and packet.get("checkpoint_verified") is True
        and packet.get("lease_valid") is True
        and isinstance(active, dict)
        and isinstance(claim, dict)
        and claim.get("status", "active") == "active"
    )
    if not writable:
        reason = (
            f"Continuity blocked {effect_class}: an active Work, active claim, "
            "fresh source, valid lease, and verified checkpoint are required."
        )
        _observe(
            payload,
            root,
            event_type="pretooluse",
            success=False,
            tool_name="Bash",
            effect_class=effect_class,
            decision="deny",
        )
        _deny_tool(reason)
        return 0
    assert isinstance(claim, dict)
    if not _scope_allows(effect_class, effect_action, claim):
        reason = (
            f"Continuity blocked {effect_class}: the active claim scope does not "
            f"authorize {effect_action}."
        )
        _observe(
            payload,
            root,
            event_type="pretooluse",
            success=False,
            tool_name="Bash",
            effect_class=effect_class,
            decision="deny",
        )
        _deny_tool(reason)
        return 0
    effect_root = root
    if effect_class == "source-control":
        project_id = packet.get("project_id") if packet is not None else None
        effect_root = (
            _source_control_effect_root(
                payload,
                root,
                claim,
                project_id=project_id,
                effect_action=effect_action,
            )
            if isinstance(project_id, str) and project_id
            else None
        )
        if effect_root is None:
            reason = (
                "Continuity blocked source-control: the command workdir is not "
                "the governance repository or a registered and claimed delivery "
                "workspace."
            )
            _observe(
                payload,
                root,
                event_type="pretooluse",
                success=False,
                tool_name="Bash",
                effect_class=effect_class,
                decision="deny-workspace",
            )
            _deny_tool(reason)
            return 0
    if not _acquire_effect_intent(
        payload, effect_root, effect_class=effect_class, claim=claim
    ):
        reason = (
            f"Continuity blocked {effect_class}: another active session holds the "
            "repository effect intent; wait for its result or coordinate the action."
        )
        _observe(
            payload,
            root,
            event_type="pretooluse",
            success=False,
            tool_name="Bash",
            effect_class=effect_class,
            decision="deny-conflict",
        )
        _deny_tool(reason)
        return 0
    _observe(
        payload,
        root,
        event_type="pretooluse",
        success=True,
        tool_name="Bash",
        effect_class=effect_class,
        decision="allow",
    )
    return 0


def _posttooluse(payload: dict[str, Any], root: Path) -> int:
    if payload.get("tool_name") != "Bash":
        return 0
    command = _shell_command(payload)
    if _effect_class(command) is not None:
        _release_effect_intent(payload)
    if not _is_recovery_read(command):
        response = payload.get("tool_response")
        success = isinstance(response, dict) and response.get("exit_code") == 0
        if success and _is_stage_test_command(command):
            packet = _autorun_packet(payload, root)
            if packet is None:
                _observe(payload, root, event_type="autorun", success=False)
                _stop("Continuity autorun failed after a successful stage test; continuation was stopped.")
                return 0
            context = _continuation_context(packet)
            if context is None:
                _observe(payload, root, event_type="autorun", success=False)
                _stop("Continuity autorun packet exceeds its byte budget; continuation was stopped.")
                return 0
            _observe(payload, root, event_type="autorun", success=True, canary_passed=True)
            print(
                _canonical(
                    {
                        "continue": True,
                        "hookSpecificOutput": {
                            "hookEventName": "PostToolUse",
                            "additionalContext": context,
                        },
                    }
                )
            )
        return 0
    output_bytes = _tool_response_bytes(payload)
    result = _admit_recovery_output(payload, root, output_bytes=output_bytes)
    if result is None:
        return 0
    admitted, budget, cumulative = result
    _observe(
        payload,
        root,
        event_type="recovery-read",
        success=admitted,
        tool_name="Bash",
        decision="allow" if admitted else "deny",
        recovery_read_bytes=cumulative,
        recovery_read_budget_bytes=budget,
        tool_output_bytes=output_bytes,
        context_admitted=admitted,
    )
    if not admitted:
        _stop(
            "Continuity recovery read budget exceeded; use the current bounded "
            "projection or a smaller explicit range."
        )
    return 0


def _precompact(payload: dict[str, Any], root: Path) -> int:
    _write_cursor(payload)
    _skill_lock_path()
    completed = _command(["checkpoint", "create"], root)
    success = completed.returncode == 0
    _observe(payload, root, event_type="precompact", success=success)
    if not success:
        _stop("Continuity checkpoint creation failed; compaction was stopped.")
    return 0


def _postcompact(payload: dict[str, Any], root: Path) -> int:
    completed = _command(["checkpoint", "verify"], root)
    success = completed.returncode == 0
    _observe(
        payload,
        root,
        event_type="postcompact",
        success=success,
        canary_passed=success,
    )
    if not success:
        _stop("Continuity checkpoint verification failed; continuation was stopped.")
    else:
        packet = _autorun_packet(payload, root)
        if packet is None:
            _observe(payload, root, event_type="autorun", success=False)
            _stop("Continuity autorun failed after checkpoint verification; continuation was stopped.")
            return 0
        context = _continuation_context(packet)
        if context is None:
            _observe(payload, root, event_type="autorun", success=False)
            _stop("Continuity autorun packet exceeds its byte budget; continuation was stopped.")
            return 0
        _observe(payload, root, event_type="autorun", success=True, canary_passed=True)
        print(
            _canonical(
                {
                    "continue": True,
                    "hookSpecificOutput": {
                        "hookEventName": "PostCompact",
                        "additionalContext": context,
                    },
                }
            )
        )
    return 0


def _session_start(payload: dict[str, Any], root: Path) -> int:
    cursor_path = _cursor_path(payload)
    skill_lock_path = _skill_lock_path()
    arguments = ["resume"]
    if cursor_path is not None and cursor_path.is_file():
        arguments.extend(["--interaction-cursor", str(cursor_path)])
    if skill_lock_path is not None:
        arguments.extend(["--skill-lock", str(skill_lock_path)])
    completed = _command(arguments, root)
    success = completed.returncode == 0
    if not success:
        _observe(payload, root, event_type="session-start", success=False)
        _stop("Continuity resume failed; keep this project read-only.")
        return 0
    encoded = completed.stdout.strip().encode("utf-8")
    packet = _load_resume_packet(encoded)
    if packet is None:
        _observe(payload, root, event_type="session-start", success=False)
        _stop("Continuity resume packet is invalid or exceeds its byte budget.")
        return 0
    source_refreshed = False
    if packet.get("source_fresh") is False:
        refreshed = _command(["attach", "refresh"], root)
        if refreshed.returncode != 0:
            _observe(
                payload,
                root,
                event_type="session-start",
                success=False,
                source_refreshed=False,
            )
            _stop("Continuity source refresh failed; keep this project read-only.")
            return 0
        source_refreshed = True
        _observe(
            payload,
            root,
            event_type="session-start",
            success=False,
            source_refreshed=True,
        )
        _stop(
            "Continuity source proposal refreshed. Explicit governance approval is required "
            "before checkpoint creation; keep this project read-only."
        )
        return 0
    _observe(
        payload,
        root,
        event_type="session-start",
        success=True,
        source_refreshed=source_refreshed,
    )
    if payload.get("source") == "compact":
        requested_budget = packet.get("recovery_read_budget_bytes")
        budget = (
            requested_budget
            if isinstance(requested_budget, int)
            and 0 < requested_budget <= RECOVERY_READ_BUDGET_BYTES
            else RECOVERY_READ_BUDGET_BYTES
        )
        _start_recovery_window(payload, root, budget_bytes=budget)
    context = _continuation_context(packet)
    if context is None:
        _stop("Continuity recovery context exceeds its byte budget.")
        return 0
    response = {
        "continue": True,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        },
    }
    if payload.get("source") == "startup":
        project_id = packet.get("project_id")
        if not isinstance(project_id, str) or not project_id:
            project_id = "project"
        response["systemMessage"] = (
            f"Continuity active · {project_id} · revision {packet['revision']}"
        )
    print(_canonical(response))
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(payload, dict) or not isinstance(payload.get("cwd"), str):
        return 0
    event = payload.get("hook_event_name")
    if event == "PostToolUse" and payload.get("tool_name") == CONTINUITY_RESUME_TOOL:
        binding_result = _record_resume_binding(payload)
        if binding_result == "conflict":
            print(
                _canonical(
                    {
                        "decision": "block",
                        "reason": (
                            "Continuity session binding conflicts with the requested "
                            "project root; the existing project identity was preserved."
                        ),
                    }
                )
            )
        return 0
    binding_path = _session_binding_path(payload)
    bound_root = _session_bound_root(payload)
    if binding_path is not None and binding_path.exists() and bound_root is None:
        if event == "PreToolUse":
            _deny_tool(
                "Continuity session project binding is invalid; external effects "
                "remain blocked."
            )
        else:
            _stop(
                "Continuity session project binding is invalid; keep this session "
                "read-only until an explicit project resume succeeds."
            )
        return 0
    root = bound_root or _project_root(payload["cwd"])
    if root is None:
        return 0
    try:
        if event == "PreCompact":
            return _precompact(payload, root)
        if event == "PostCompact":
            return _postcompact(payload, root)
        if event == "SessionStart":
            return _session_start(payload, root)
        if event == "PreToolUse":
            return _pretooluse(payload, root)
        if event == "PostToolUse":
            return _posttooluse(payload, root)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        _observe(payload, root, event_type="hook-error", success=False)
        if event == "PreToolUse":
            _deny_tool(
                "Continuity authority is unavailable; external effects remain blocked."
            )
            return 0
        _stop("Continuity lifecycle hook failed; keep this project read-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
