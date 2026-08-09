"""Deterministic, sanitizer-gated fragment extraction for replay fixtures."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .archive_extraction import CandidateEvent
from .sanitizer import sanitize_text


SELECTION_VERSION = "anchored-contiguous-fragment/v1"


class FragmentExtractionError(ValueError):
    """Raised when a source event cannot yield an admissible minimal fragment."""


@dataclass(frozen=True)
class FragmentReceipt:
    selection_version: str
    normalized_event_sha256: str
    source_fragment_sha256: str
    content_start: int
    content_end: int
    findings_by_category: dict[str, int]
    content_sha256: str


@dataclass(frozen=True)
class SanitizedFragment:
    content: str
    receipt: FragmentReceipt


def _anchor_span(text: str, anchors: tuple[str, ...]) -> tuple[int, int]:
    positions: list[tuple[int, int]] = []
    for anchor in anchors:
        if not isinstance(anchor, str) or not anchor:
            raise FragmentExtractionError("anchors must be non-empty strings")
        match = re.search(re.escape(anchor), text, re.IGNORECASE)
        if match is None:
            raise FragmentExtractionError(f"source event is missing anchor: {anchor}")
        positions.append(match.span())
    return min(start for start, _ in positions), max(end for _, end in positions)


def _paragraph_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    paragraph_start = text.rfind("\n\n", 0, start)
    paragraph_start = 0 if paragraph_start < 0 else paragraph_start + 2
    paragraph_end = text.find("\n\n", end)
    paragraph_end = len(text) if paragraph_end < 0 else paragraph_end
    return paragraph_start, paragraph_end


def _bounded_bounds(
    text: str,
    anchor_start: int,
    anchor_end: int,
    *,
    max_bytes: int,
) -> tuple[int, int]:
    start, end = _paragraph_bounds(text, anchor_start, anchor_end)
    if len(text[start:end].encode("utf-8")) <= max_bytes:
        return start, end
    if len(text[anchor_start:anchor_end].encode("utf-8")) > max_bytes:
        raise FragmentExtractionError("required anchor span exceeds max_bytes")

    start, end = anchor_start, anchor_end
    while True:
        changed = False
        if start > 0:
            candidate = text[start - 1 : end]
            if len(candidate.encode("utf-8")) <= max_bytes:
                start -= 1
                changed = True
        if end < len(text):
            candidate = text[start : end + 1]
            if len(candidate.encode("utf-8")) <= max_bytes:
                end += 1
                changed = True
        if not changed:
            break
    return start, end


def extract_sanitized_fragment(
    event: CandidateEvent,
    *,
    anchors: tuple[str, ...],
    max_bytes: int,
) -> SanitizedFragment:
    """Select one contiguous source fragment and return only sanitized content."""
    if not isinstance(event, CandidateEvent):
        raise FragmentExtractionError("event must be a normalized candidate event")
    if not isinstance(max_bytes, int) or max_bytes <= 0:
        raise FragmentExtractionError("max_bytes must be positive")
    anchors = tuple(anchors)
    if not anchors:
        raise FragmentExtractionError("at least one anchor is required")

    anchor_start, anchor_end = _anchor_span(event.text, anchors)
    content_start, content_end = _bounded_bounds(
        event.text,
        anchor_start,
        anchor_end,
        max_bytes=max_bytes,
    )
    selected_fragment = event.text[content_start:content_end]
    content_start += len(selected_fragment) - len(selected_fragment.lstrip())
    content_end -= len(selected_fragment) - len(selected_fragment.rstrip())
    source_fragment = event.text[content_start:content_end]
    if not source_fragment:
        raise FragmentExtractionError("selected source fragment is empty")
    sanitized = sanitize_text(source_fragment)
    if not sanitized.text.strip() or len(sanitized.text.encode("utf-8")) > max_bytes:
        raise FragmentExtractionError("sanitized fragment exceeds the content gate")
    for anchor in anchors:
        if re.search(re.escape(anchor), sanitized.text, re.IGNORECASE) is None:
            raise FragmentExtractionError("sanitization removed a required anchor")

    return SanitizedFragment(
        content=sanitized.text,
        receipt=FragmentReceipt(
            selection_version=SELECTION_VERSION,
            normalized_event_sha256=hashlib.sha256(event.text.encode("utf-8")).hexdigest(),
            source_fragment_sha256=hashlib.sha256(source_fragment.encode("utf-8")).hexdigest(),
            content_start=content_start,
            content_end=content_end,
            findings_by_category=sanitized.findings_by_category,
            content_sha256=sanitized.content_sha256,
        ),
    )
