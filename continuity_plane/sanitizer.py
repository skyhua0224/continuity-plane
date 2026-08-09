"""Deterministic M1-03 sanitizer and replay-admission gate.

The sanitizer is intentionally conservative. It reports category and location
metadata, while never retaining the matched value in a finding.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable


class SanitizationError(ValueError):
    """Raised when sanitized content cannot pass admission."""


@dataclass(frozen=True)
class SanitizationFinding:
    category: str
    start: int
    end: int


@dataclass(frozen=True)
class SanitizationResult:
    text: str
    findings: tuple[SanitizationFinding, ...]
    content_sha256: str

    @property
    def findings_by_category(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for finding in self.findings:
            counts[finding.category] = counts.get(finding.category, 0) + 1
        return counts


_SECRET_PATTERNS = (
    re.compile(
        r"(?im)^\s*(?:password|passwd|token|secret|api[_-]?key|cookie|set-cookie)\s*[:=]\s*.+$"
    ),
    re.compile(
        r"(?i)\b(?:password|passwd|token|secret|api[_-]?key|cookie|set-cookie)\s*[:=]\s*[^\s'\"<>]+"
    ),
    re.compile(
        r"(?i)\b(?:authorization\s*:\s*bearer|bearer)\s+[A-Za-z0-9._~+/=-]{8,}"
    ),
    re.compile(r"\b(?:sk|ghp|github_pat|xox[baprs])-[-A-Za-z0-9_]{8,}\b", re.IGNORECASE),
    re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
    ),
)
_PROVIDER_ID_PATTERNS = (
    re.compile(
        r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
        re.IGNORECASE,
    ),
)
_PII_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{8,}\d)(?!\d)"),
)
_MACHINE_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])/(?:home|Users|private|var/folders)/[^\s'\"]+"),
    re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:\\Users\\[^\s'\"]+"),
)
_SPDX_PATTERN = re.compile(
    r"(?im)(SPDX-License-Identifier:\s*)([A-Za-z0-9.+-]+(?:\s+WITH\s+[A-Za-z0-9.+-]+)?)"
)


def _replace_matches(
    text: str,
    pattern: re.Pattern[str],
    category: str,
    replacement: str | Callable[[re.Match[str]], str],
) -> tuple[str, list[SanitizationFinding]]:
    findings: list[SanitizationFinding] = []

    def replace(match: re.Match[str]) -> str:
        findings.append(SanitizationFinding(category, match.start(), match.end()))
        return replacement(match) if callable(replacement) else replacement

    return pattern.sub(replace, text), findings


def sanitize_text(text: str, *, allowed_licenses: Iterable[str] = ()) -> SanitizationResult:
    """Redact known secret, PII and machine-specific values deterministically."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")

    sanitized = text
    findings: list[SanitizationFinding] = []
    for pattern in _SECRET_PATTERNS:
        sanitized, matched = _replace_matches(
            sanitized, pattern, "secret", "[REDACTED:secret]"
        )
        findings.extend(matched)
    for pattern in _PII_PATTERNS:
        sanitized, matched = _replace_matches(sanitized, pattern, "pii", "[REDACTED:pii]")
        findings.extend(matched)
    for pattern in _PROVIDER_ID_PATTERNS:
        sanitized, matched = _replace_matches(
            sanitized, pattern, "provider-id", "[REDACTED:provider-id]"
        )
        findings.extend(matched)
    for pattern in _MACHINE_PATTERNS:
        sanitized, matched = _replace_matches(
            sanitized, pattern, "machine", "[REDACTED:machine]"
        )
        findings.extend(matched)

    allowed = {license_id.strip() for license_id in allowed_licenses if license_id.strip()}

    def replace_license(match: re.Match[str]) -> str:
        license_id = match.group(2)
        if license_id in allowed:
            return match.group(0)
        findings.append(SanitizationFinding("license", match.start(2), match.end(2)))
        return f"{match.group(1)}[REDACTED:license]"

    sanitized = _SPDX_PATTERN.sub(replace_license, sanitized)
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
    return SanitizationResult(sanitized, tuple(findings), digest)


def validate_admission(result: SanitizationResult, *, provenance_valid: bool) -> None:
    """Permit replay/typed admission only for clean content with valid provenance."""
    if not isinstance(result, SanitizationResult):
        raise SanitizationError("admission requires a SanitizationResult")
    if result.findings:
        raise SanitizationError("sanitizer findings must be resolved before admission")
    if not provenance_valid:
        raise SanitizationError("valid provenance is required before admission")
