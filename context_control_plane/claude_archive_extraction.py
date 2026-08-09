"""Bounded extraction for the observed Claude Code JSONL archive shape."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .archive_extraction import (
    ArchiveExtractionError,
    CandidateEvent,
    CandidateRange,
    RolloutInventory,
    _sha256_file,
)


ADAPTER_VERSION = "claude-code-jsonl-observed/v1alpha1"
DEFAULT_MAX_CANDIDATE_LINE_BYTES = 128 * 1024
_RUNTIME_ENVELOPE_PREFIXES = (
    "another claude session sent a message:",
    "<teammate-message",
    "<task-notification",
    "<task-id>",
)


def _content_text(item: dict[str, Any]) -> tuple[str | None, bool, bool]:
    """Return normalized text, shape drift, and forbidden-content status."""
    top_type = item.get("type")
    if top_type == "assistant":
        return None, False, True
    if top_type != "user":
        return None, False, False
    if item.get("isMeta") is True:
        return None, False, True
    origin = item.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in {None, "human"}:
        return None, False, True
    if item.get("promptSource") == "system":
        return None, False, True
    message = item.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None, True, False
    content = message.get("content")
    if isinstance(content, str):
        if content.lstrip().lower().startswith(_RUNTIME_ENVELOPE_PREFIXES):
            return None, False, True
        return content, False, False
    if isinstance(content, list):
        if any(
            isinstance(part, dict) and part.get("type") in {"tool_result", "tool_use"}
            for part in content
        ):
            return None, False, True
        if content and all(
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            for part in content
        ):
            return "\n".join(part["text"] for part in content), False, False
    return None, True, False


def _material_kind(item: dict[str, Any]) -> tuple[str | None, bool, bool]:
    text, shape_drifted, forbidden = _content_text(item)
    if text is None:
        return None, shape_drifted, forbidden
    if item.get("isCompactSummary") is True:
        return "compaction_checkpoint", False, False
    return "user_message", False, False


def inspect_claude_archive(
    path: Path | str,
    *,
    max_candidate_line_bytes: int = DEFAULT_MAX_CANDIDATE_LINE_BYTES,
) -> RolloutInventory:
    """Stream one observed Claude archive and return a content-free inventory."""
    path = Path(path)
    if not path.is_file():
        raise ArchiveExtractionError("archive path must be a file")
    if not isinstance(max_candidate_line_bytes, int) or max_candidate_line_bytes <= 0:
        raise ArchiveExtractionError("max_candidate_line_bytes must be positive")

    archive_digest = hashlib.sha256()
    event_counts: Counter[str] = Counter()
    candidates: list[CandidateRange] = []
    offset = 0
    line_count = 0
    max_line_bytes = 0
    quarantined_count = 0
    oversized_candidate_count = 0
    schema_quarantine_count = 0

    with path.open("rb") as stream:
        for line_count, raw in enumerate(stream, 1):
            byte_start = offset
            offset += len(raw)
            archive_digest.update(raw)
            max_line_bytes = max(max_line_bytes, len(raw))
            try:
                item = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                event_counts["<invalid-json>"] += 1
                quarantined_count += 1
                schema_quarantine_count += 1
                continue
            if not isinstance(item, dict):
                event_counts["<non-object>"] += 1
                quarantined_count += 1
                schema_quarantine_count += 1
                continue

            top_type = item.get("type")
            top_type = top_type if isinstance(top_type, str) and top_type else "<missing>"
            message = item.get("message")
            role = message.get("role") if isinstance(message, dict) else None
            role = role if isinstance(role, str) else None
            event_counts[f"{top_type}:{role}" if role else top_type] += 1
            event_kind, shape_drifted, forbidden = _material_kind(item)
            if shape_drifted:
                quarantined_count += 1
                schema_quarantine_count += 1
                continue
            if forbidden:
                quarantined_count += 1
                continue
            if event_kind is None:
                continue
            if len(raw) > max_candidate_line_bytes:
                quarantined_count += 1
                oversized_candidate_count += 1
                continue
            candidates.append(
                CandidateRange(
                    event_kind=event_kind,
                    line_number=line_count,
                    byte_start=byte_start,
                    byte_end=offset,
                    size_bytes=len(raw),
                    range_sha256=hashlib.sha256(raw).hexdigest(),
                    top_type=top_type,
                    payload_type=role,
                )
            )

    return RolloutInventory(
        adapter_version=ADAPTER_VERSION,
        archive_sha256=archive_digest.hexdigest(),
        size_bytes=offset,
        line_count=line_count,
        max_line_bytes=max_line_bytes,
        event_counts=dict(sorted(event_counts.items())),
        candidates=tuple(candidates),
        quarantined_count=quarantined_count,
        oversized_candidate_count=oversized_candidate_count,
        schema_quarantine_count=schema_quarantine_count,
    )


def read_claude_candidate_event(
    path: Path | str,
    candidate: CandidateRange,
    *,
    expected_archive_sha256: str,
) -> CandidateEvent:
    """Read and normalize one inventory-selected Claude material event."""
    path = Path(path)
    if not path.is_file():
        raise ArchiveExtractionError("archive path must be a file")
    if _sha256_file(path) != expected_archive_sha256:
        raise ArchiveExtractionError("archive hash changed after inventory")
    return _read_verified_claude_candidate_event(
        path,
        candidate,
        expected_archive_sha256=expected_archive_sha256,
    )


def _read_verified_claude_candidate_event(
    path: Path,
    candidate: CandidateRange,
    *,
    expected_archive_sha256: str,
) -> CandidateEvent:
    """Read one Claude candidate after archive-level integrity verification."""
    if not isinstance(candidate, CandidateRange):
        raise ArchiveExtractionError("candidate must come from archive inventory")
    if (
        candidate.byte_start < 0
        or candidate.byte_end <= candidate.byte_start
        or candidate.byte_end > path.stat().st_size
        or candidate.size_bytes != candidate.byte_end - candidate.byte_start
    ):
        raise ArchiveExtractionError("candidate byte range is invalid")
    with path.open("rb") as stream:
        if candidate.byte_start > 0:
            stream.seek(candidate.byte_start - 1)
            if stream.read(1) != b"\n":
                raise ArchiveExtractionError("candidate range start is not line-aligned")
        stream.seek(candidate.byte_end - 1)
        if stream.read(1) != b"\n":
            raise ArchiveExtractionError("candidate range end is not line-aligned")
        stream.seek(candidate.byte_start)
        raw_range = stream.read(candidate.size_bytes)
    if len(raw_range) != candidate.size_bytes:
        raise ArchiveExtractionError("candidate range could not be read completely")
    if raw_range.count(b"\n") != 1:
        raise ArchiveExtractionError("candidate range must contain exactly one event")
    range_sha256 = hashlib.sha256(raw_range).hexdigest()
    if range_sha256 != candidate.range_sha256:
        raise ArchiveExtractionError("candidate range hash mismatch")
    try:
        item = json.loads(raw_range)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveExtractionError("candidate range contains invalid JSONL") from exc
    if not isinstance(item, dict):
        raise ArchiveExtractionError("candidate range JSONL item must be an object")
    event_kind, shape_drifted, forbidden = _material_kind(item)
    if shape_drifted or forbidden or event_kind != candidate.event_kind:
        raise ArchiveExtractionError("candidate event shape changed after inventory")
    text, _, _ = _content_text(item)
    if text is None:
        raise ArchiveExtractionError("candidate event has no admissible text")
    return CandidateEvent(
        event_kind=event_kind,
        text=text,
        range_sha256=range_sha256,
        metadata={
            "adapter_version": ADAPTER_VERSION,
            "line_number": candidate.line_number,
            "byte_start": candidate.byte_start,
            "byte_end": candidate.byte_end,
            "size_bytes": candidate.size_bytes,
            "archive_sha256": expected_archive_sha256,
        },
    )


def read_claude_candidate_events(
    path: Path | str,
    candidates: tuple[CandidateRange, ...] | list[CandidateRange],
    *,
    expected_archive_sha256: str,
    max_total_bytes: int,
) -> tuple[CandidateEvent, ...]:
    """Read a bounded Claude candidate batch after one archive hash check."""
    path = Path(path)
    if not path.is_file():
        raise ArchiveExtractionError("archive path must be a file")
    if not isinstance(max_total_bytes, int) or max_total_bytes <= 0:
        raise ArchiveExtractionError("max_total_bytes must be positive")
    candidates = tuple(candidates)
    if not all(isinstance(candidate, CandidateRange) for candidate in candidates):
        raise ArchiveExtractionError("candidates must come from archive inventory")
    if sum(candidate.size_bytes for candidate in candidates) > max_total_bytes:
        raise ArchiveExtractionError("candidate batch exceeds max_total_bytes")
    if _sha256_file(path) != expected_archive_sha256:
        raise ArchiveExtractionError("archive hash changed after inventory")

    ordered = sorted(candidates, key=lambda candidate: (candidate.byte_start, candidate.byte_end))
    return tuple(
        _read_verified_claude_candidate_event(
            path,
            candidate,
            expected_archive_sha256=expected_archive_sha256,
        )
        for candidate in ordered
    )
