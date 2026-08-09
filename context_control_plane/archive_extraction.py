"""Bounded, metadata-first extraction from provider transcript archives.

The observed Codex JSONL layout is adapter input, not a stable public schema.
Inventory objects never retain event bodies. Material text is exposed only by
an explicit, hash-verified byte-range read.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ADAPTER_VERSION = "codex-rollout-observed/v1alpha1"
DEFAULT_MAX_CANDIDATE_LINE_BYTES = 128 * 1024

_FORBIDDEN_EVENT_TYPES = {
    ("event_msg", "agent_reasoning"),
    ("response_item", "reasoning"),
    ("response_item", "custom_tool_call"),
    ("response_item", "custom_tool_call_output"),
    ("response_item", "function_call"),
    ("response_item", "function_call_output"),
}


class ArchiveExtractionError(ValueError):
    """Raised when an archive or requested byte range fails an extraction gate."""


@dataclass(frozen=True)
class CandidateRange:
    event_kind: str
    line_number: int
    byte_start: int
    byte_end: int
    size_bytes: int
    range_sha256: str
    top_type: str
    payload_type: str | None


@dataclass(frozen=True)
class RolloutInventory:
    adapter_version: str
    archive_sha256: str
    size_bytes: int
    line_count: int
    max_line_bytes: int
    event_counts: dict[str, int]
    candidates: tuple[CandidateRange, ...]
    quarantined_count: int
    oversized_candidate_count: int
    schema_quarantine_count: int


@dataclass(frozen=True)
class CandidateEvent:
    event_kind: str
    text: str
    range_sha256: str
    metadata: dict[str, Any]


def _event_identity(item: dict[str, Any]) -> tuple[str, str | None]:
    top_type = item.get("type")
    if not isinstance(top_type, str) or not top_type:
        return "<missing>", None
    payload = item.get("payload")
    payload_type = payload.get("type") if isinstance(payload, dict) else None
    return top_type, payload_type if isinstance(payload_type, str) else None


def _event_count_key(top_type: str, payload_type: str | None) -> str:
    return f"{top_type}:{payload_type}" if payload_type is not None else top_type


def _material_kind(item: dict[str, Any]) -> tuple[str | None, bool]:
    """Return material kind and whether a recognized material shape drifted."""
    top_type, payload_type = _event_identity(item)
    if (top_type, payload_type) == ("event_msg", "user_message"):
        payload = item.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return "user_message", False
        return None, True
    if top_type == "compacted":
        if isinstance(item.get("message"), str):
            return "compaction_checkpoint", False
        payload = item.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return "compaction_checkpoint", False
        return None, True
    return None, False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_codex_rollout(
    path: Path | str,
    *,
    max_candidate_line_bytes: int = DEFAULT_MAX_CANDIDATE_LINE_BYTES,
) -> RolloutInventory:
    """Stream one observed Codex rollout and return a content-free inventory."""
    path = Path(path)
    if not path.is_file():
        raise ArchiveExtractionError("rollout path must be a file")
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

            top_type, payload_type = _event_identity(item)
            event_counts[_event_count_key(top_type, payload_type)] += 1
            event_kind, shape_drifted = _material_kind(item)
            if shape_drifted:
                quarantined_count += 1
                schema_quarantine_count += 1
                continue
            if (top_type, payload_type) in _FORBIDDEN_EVENT_TYPES:
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
                    payload_type=payload_type,
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


def read_bounded_jsonl_range(
    path: Path | str,
    *,
    start: int,
    end: int,
    max_bytes: int,
    expected_archive_sha256: str,
) -> tuple[dict[str, Any], ...]:
    """Read a complete-line JSONL range after size, alignment, and hash gates."""
    path = Path(path)
    if not path.is_file():
        raise ArchiveExtractionError("rollout path must be a file")
    size = path.stat().st_size
    if (
        not isinstance(start, int)
        or not isinstance(end, int)
        or start < 0
        or end <= start
        or end > size
    ):
        raise ArchiveExtractionError("byte range is invalid")
    if not isinstance(max_bytes, int) or max_bytes <= 0 or end - start > max_bytes:
        raise ArchiveExtractionError("byte range exceeds max_bytes")
    if _sha256_file(path) != expected_archive_sha256:
        raise ArchiveExtractionError("archive hash changed after inventory")

    with path.open("rb") as stream:
        if start > 0:
            stream.seek(start - 1)
            if stream.read(1) != b"\n":
                raise ArchiveExtractionError("byte range start is not line-aligned")
        stream.seek(end - 1)
        if stream.read(1) != b"\n":
            raise ArchiveExtractionError("byte range end is not line-aligned")
        stream.seek(start)
        raw = stream.read(end - start)
    if len(raw) != end - start:
        raise ArchiveExtractionError("byte range could not be read completely")

    items: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            item = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArchiveExtractionError("byte range contains invalid JSONL") from exc
        if not isinstance(item, dict):
            raise ArchiveExtractionError("byte range JSONL items must be objects")
        items.append(item)
    if not items:
        raise ArchiveExtractionError("byte range contains no JSONL items")
    return tuple(items)


def read_candidate_event(
    path: Path | str,
    candidate: CandidateRange,
    *,
    expected_archive_sha256: str,
) -> CandidateEvent:
    """Read and normalize one inventory-selected material event."""
    path = Path(path)
    if not path.is_file():
        raise ArchiveExtractionError("rollout path must be a file")
    if _sha256_file(path) != expected_archive_sha256:
        raise ArchiveExtractionError("archive hash changed after inventory")
    return _read_verified_candidate_event(
        path,
        candidate,
        expected_archive_sha256=expected_archive_sha256,
    )


def _read_verified_candidate_event(
    path: Path,
    candidate: CandidateRange,
    *,
    expected_archive_sha256: str,
) -> CandidateEvent:
    """Read one candidate after the caller has verified the archive digest."""
    if not isinstance(candidate, CandidateRange):
        raise ArchiveExtractionError("candidate must come from rollout inventory")
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
    event_kind, shape_drifted = _material_kind(item)
    if shape_drifted or event_kind != candidate.event_kind:
        raise ArchiveExtractionError("candidate event shape changed after inventory")

    if event_kind == "user_message":
        text = item["payload"]["message"]
    elif event_kind == "compaction_checkpoint":
        text = item.get("message")
        if not isinstance(text, str):
            text = item["payload"]["message"]
    else:
        raise ArchiveExtractionError("candidate event kind is not admissible")
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


def read_candidate_events(
    path: Path | str,
    candidates: tuple[CandidateRange, ...] | list[CandidateRange],
    *,
    expected_archive_sha256: str,
    max_total_bytes: int,
) -> tuple[CandidateEvent, ...]:
    """Read a bounded candidate batch after one archive-integrity check."""
    path = Path(path)
    if not path.is_file():
        raise ArchiveExtractionError("rollout path must be a file")
    if not isinstance(max_total_bytes, int) or max_total_bytes <= 0:
        raise ArchiveExtractionError("max_total_bytes must be positive")
    candidates = tuple(candidates)
    if not all(isinstance(candidate, CandidateRange) for candidate in candidates):
        raise ArchiveExtractionError("candidates must come from rollout inventory")
    if sum(candidate.size_bytes for candidate in candidates) > max_total_bytes:
        raise ArchiveExtractionError("candidate batch exceeds max_total_bytes")
    if _sha256_file(path) != expected_archive_sha256:
        raise ArchiveExtractionError("archive hash changed after inventory")

    ordered = sorted(candidates, key=lambda candidate: (candidate.byte_start, candidate.byte_end))
    return tuple(
        _read_verified_candidate_event(
            path,
            candidate,
            expected_archive_sha256=expected_archive_sha256,
        )
        for candidate in ordered
    )
