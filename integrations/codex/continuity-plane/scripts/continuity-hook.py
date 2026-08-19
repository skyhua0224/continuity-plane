#!/usr/bin/env python3
"""Codex lifecycle bridge with bounded output and sanitized local telemetry."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


MAX_PACKET_BYTES = 8 * 1024
MAX_CONTEXT_BYTES = 12 * 1024
MAX_TRANSCRIPT_TAIL_BYTES = 2 * 1024 * 1024
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


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    try:
        completed = subprocess.run(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode == 0:
            candidates.insert(0, Path(completed.stdout.strip()).resolve())
    except (OSError, subprocess.SubprocessError):
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
                "context_control_plane.cli",
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
            timeout=8,
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
        print(_canonical({"continue": True}))
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
    _observe(payload, root, event_type="session-start", success=success)
    if not success:
        _stop("Continuity resume failed; keep this project read-only.")
        return 0
    encoded = completed.stdout.strip().encode("utf-8")
    if not encoded or len(encoded) > MAX_PACKET_BYTES:
        _stop("Continuity resume packet is missing or exceeds its byte budget.")
        return 0
    try:
        packet = json.loads(encoded)
    except json.JSONDecodeError:
        _stop("Continuity resume packet is not valid JSON.")
        return 0
    context = (
        "Continuity recovery contract. Continue silently: do not mention recovery, "
        "compaction, hooks, or reloading. Do not answer acknowledged input again. "
        "Do not restate text already visible before this continuation. "
        "Use the packet as the current authority boundary; perform only its next_action "
        "after the checkpoint canary. Packet: "
        + _canonical(packet)
    )
    if len(context.encode("utf-8")) > MAX_CONTEXT_BYTES:
        _stop("Continuity recovery context exceeds its byte budget.")
        return 0
    print(
        _canonical(
            {
                "continue": True,
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": context,
                },
            }
        )
    )
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, TypeError):
        return 0
    if not isinstance(payload, dict) or not isinstance(payload.get("cwd"), str):
        return 0
    root = _project_root(payload["cwd"])
    if root is None:
        return 0
    event = payload.get("hook_event_name")
    try:
        if event == "PreCompact":
            return _precompact(payload, root)
        if event == "PostCompact":
            return _postcompact(payload, root)
        if event == "SessionStart":
            return _session_start(payload, root)
    except (OSError, RuntimeError, subprocess.SubprocessError):
        _observe(payload, root, event_type="hook-error", success=False)
        _stop("Continuity lifecycle hook failed; keep this project read-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
