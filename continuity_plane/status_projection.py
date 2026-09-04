"""Deterministic current-only STATUS projections from a recovery packet."""

from __future__ import annotations

from typing import Any


STATUS_PROJECTION_SCHEMA_VERSION = "context.status-projection/v1alpha1"


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _packet(packet: Any) -> dict[str, Any]:
    if not isinstance(packet, dict):
        raise ValueError("status packet must be an object")
    required = {
        "project_id",
        "revision",
        "event_head",
        "active_work",
        "claim",
        "checkpoint_ref",
        "next_action",
        "source_fresh",
        "lease_valid",
        "read_only",
        "open_blockers",
    }
    if not required.issubset(packet):
        raise ValueError("status packet is missing current route fields")
    if not isinstance(packet["revision"], int) or packet["revision"] < 0:
        raise ValueError("status revision is invalid")
    if not isinstance(packet["event_head"], dict):
        raise ValueError("status event head is invalid")
    active_work = packet["active_work"]
    claim = packet["claim"]
    if (active_work is None) != (claim is None):
        raise ValueError("status active work and claim must share one lifecycle")
    if active_work is not None and not isinstance(active_work, dict):
        raise ValueError("status active work is invalid")
    if claim is not None and not isinstance(claim, dict):
        raise ValueError("status claim is invalid")
    if not isinstance(packet["checkpoint_ref"], dict):
        raise ValueError("status checkpoint is missing")
    if not isinstance(packet["open_blockers"], list):
        raise ValueError("status blockers are invalid")
    return packet


def _value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _cell(value: Any) -> str:
    return _value(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", "<br>")


def render_status_projection(packet: Any, *, language: str = "zh-CN") -> str:
    """Render only the current route; historical narrative is intentionally excluded."""
    current = _packet(packet)
    if language not in {"zh-CN", "en"}:
        raise ValueError("status projection language is unsupported")
    work = current["active_work"]
    claim = current["claim"]
    event_head = current["event_head"]
    checkpoint = current["checkpoint_ref"]
    blockers = current["open_blockers"]
    next_action = (
        "continue-project-work-state-sync-pending"
        if current["read_only"]
        else _text(current["next_action"], "next_action")
    )
    blocker_text = "none" if not blockers else "; ".join(
        _text(item.get("reason"), "blocker.reason")
        for item in blockers
        if isinstance(item, dict)
    )
    if language == "zh-CN":
        rows = [
            ("项目", _text(current["project_id"], "project_id")),
            ("状态 revision", _value(current["revision"])),
            ("事件序号", _value(event_head.get("sequence_no"))),
            ("active Work", "none" if work is None else _text(work.get("work_id"), "active_work.work_id")),
            ("Work 标题", "none" if work is None else _text(work.get("title"), "active_work.title")),
            ("Work 状态", "idle" if work is None else _text(work.get("status"), "active_work.status")),
            ("claim", "none" if claim is None else _text(claim.get("claim_id"), "claim.claim_id")),
            ("租约有效", _value(current["lease_valid"])),
            ("来源新鲜", _value(current["source_fresh"])),
            (
                "Continuity State 写入",
                "同步待恢复" if current["read_only"] else "可用",
            ),
            ("普通项目工作", "可继续"),
            ("下一动作", next_action),
            ("checkpoint", _text(checkpoint.get("artifact_uri"), "checkpoint.artifact_uri")),
            ("阻塞", blocker_text),
        ]
        title = "# 当前项目 STATUS"
        note = "本文件由当前恢复包生成。"
    else:
        rows = [
            ("Project", _text(current["project_id"], "project_id")),
            ("State revision", _value(current["revision"])),
            ("Event sequence", _value(event_head.get("sequence_no"))),
            ("Active Work", "none" if work is None else _text(work.get("work_id"), "active_work.work_id")),
            ("Work title", "none" if work is None else _text(work.get("title"), "active_work.title")),
            ("Work status", "idle" if work is None else _text(work.get("status"), "active_work.status")),
            ("Claim", "none" if claim is None else _text(claim.get("claim_id"), "claim.claim_id")),
            ("Lease valid", _value(current["lease_valid"])),
            ("Source fresh", _value(current["source_fresh"])),
            (
                "Continuity State writes",
                "Sync pending" if current["read_only"] else "Ready",
            ),
            ("Ordinary project work", "Continue"),
            ("Next action", next_action),
            ("Checkpoint", _text(checkpoint.get("artifact_uri"), "checkpoint.artifact_uri")),
            ("Blocker", blocker_text),
        ]
        title = "# Current Project STATUS"
        note = "Generated from the current recovery packet."
    table = "\n".join(f"| {_cell(key)} | {_cell(value)} |" for key, value in rows)
    return f"{title}\n\n{note}\n\n| Field | Value |\n|---|---|\n{table}\n"


__all__ = ["STATUS_PROJECTION_SCHEMA_VERSION", "render_status_projection"]
