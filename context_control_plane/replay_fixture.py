"""Versioned replay fixtures and an independent M1-04 admission validator."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from context_control_plane.sanitizer import sanitize_text


SCHEMA_VERSION = "context.replay-fixture/v1alpha1"
VALIDATOR_VERSION = "m1-04-independent/v1alpha1"
MAX_INPUT_BYTES = 4096

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FIXTURE_REF_RE = re.compile(r"^fx_[a-z2-7]{26}$")
_THREAD_REF_RE = re.compile(r"^thr_[a-z2-7]{26}$")
_RANGE_REF_RE = re.compile(r"^rng_[a-z2-7]{26}$")
_RAW_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:/(?:home|Users|private|var/folders)/[^\s'\"]+|[A-Za-z]:\\Users\\[^\s'\"]+)",
    re.IGNORECASE,
)
_SECRET_RE = re.compile(
    r"(?i)(?:authorization\s*:\s*bearer\s+\S+|\b(?:password|passwd|token|secret|api[_-]?key|cookie)\s*[:=]\s*\S+|\b(?:sk|ghp|github_pat|xox[baprs])-[-A-Za-z0-9_]{8,})"
)
_FORBIDDEN_KEYS = {
    "provider_thread_id",
    "raw_thread_id",
    "archive_path",
    "source_path",
    "local_path",
    "transcript_path",
    "reasoning",
}
_SCENARIO_CLASSES = {
    "compaction-recovery",
    "task-routing",
    "stale-decision",
    "skill-drift",
    "evidence-gap",
    "effect-retry",
    "scope-drift",
    "experiment-promotion",
}
_EVENT_KINDS = {"user_message", "compaction_checkpoint"}
_STATE_FIELDS = {
    "active_task",
    "latest_decision",
    "constraints",
    "blocker",
    "return_point",
    "next_action",
    "rejected_decisions",
}
_SOURCE_FIELDS = {
    "provider",
    "source_thread_ref",
    "source_range_ref",
    "archive_sha256",
    "range_sha256",
    "byte_start",
    "byte_end",
    "extractor_version",
}
_INPUT_FIELDS = {"event_kind", "content", "content_sha256"}
_EXTRACTION_FIELDS = {
    "selection_version",
    "normalized_event_sha256",
    "source_fragment_sha256",
    "content_start",
    "content_end",
    "findings_by_category",
    "content_sha256",
}
_SANITIZER_FINDING_CATEGORIES = {
    "secret",
    "pii",
    "machine",
    "license",
    "provider-id",
}
_FIXTURE_FIELDS = {
    "schema_version",
    "fixture_id",
    "project_id",
    "scenario_class",
    "source",
    "input_event",
    "extraction",
    "initial_state",
    "expected_state",
    "expected_gate",
    "evidence_refs",
    "sanitization",
    "fixture_sha256",
}


class ReplayFixtureError(ValueError):
    """Raised when a replay fixture cannot pass independent validation."""


@dataclass(frozen=True)
class ValidationReceipt:
    validator_version: str
    fixture_id: str
    fixture_sha256: str
    validated_at: str
    status: str
    evidence_count: int
    receipt_sha256: str


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _opaque_fixture_id(material: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_bytes(material)).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
    return f"fx_{encoded[:26]}"


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReplayFixtureError(f"{field} must be a non-empty string")
    return value


def _validate_timestamp(value: Any) -> None:
    value = _require_string(value, "validated_at")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReplayFixtureError("validated_at must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ReplayFixtureError("validated_at must include timezone")


def _walk(value: Any):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _all_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _all_strings(child)


def _validate_state(state: Any, field: str) -> None:
    if not isinstance(state, dict) or set(state) != _STATE_FIELDS:
        raise ReplayFixtureError(f"{field} fields do not match the contract")
    for key in ("active_task", "latest_decision", "return_point", "next_action"):
        _require_string(state[key], f"{field}.{key}")
    if state["blocker"] is not None:
        _require_string(state["blocker"], f"{field}.blocker")
    for key in ("constraints", "rejected_decisions"):
        values = state[key]
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item.strip() for item in values
        ):
            raise ReplayFixtureError(f"{field}.{key} must contain non-empty strings")
    if state["latest_decision"] in state["rejected_decisions"]:
        raise ReplayFixtureError(f"{field} revives a rejected decision")


def _validate_extraction(extraction: Any, *, content_sha256: str) -> None:
    if not isinstance(extraction, dict) or set(extraction) != _EXTRACTION_FIELDS:
        raise ReplayFixtureError("extraction fields do not match the contract")
    if extraction["selection_version"] != "anchored-contiguous-fragment/v1":
        raise ReplayFixtureError("unsupported extraction selection_version")
    for key in ("normalized_event_sha256", "source_fragment_sha256", "content_sha256"):
        if not _SHA256_RE.fullmatch(str(extraction[key])):
            raise ReplayFixtureError(f"extraction.{key} must be SHA-256")
    if extraction["content_sha256"] != content_sha256:
        raise ReplayFixtureError("extraction content hash does not match fixture input")
    if (
        not isinstance(extraction["content_start"], int)
        or not isinstance(extraction["content_end"], int)
        or extraction["content_start"] < 0
        or extraction["content_end"] <= extraction["content_start"]
    ):
        raise ReplayFixtureError("extraction content range is invalid")
    findings = extraction["findings_by_category"]
    if not isinstance(findings, dict) or not all(
        category in _SANITIZER_FINDING_CATEGORIES
        and isinstance(count, int)
        and not isinstance(count, bool)
        and count > 0
        for category, count in findings.items()
    ):
        raise ReplayFixtureError("extraction findings are invalid")


def build_replay_fixture(
    *,
    project_id: str,
    scenario_class: str,
    source: dict[str, Any],
    input_event: dict[str, Any],
    extraction: dict[str, Any],
    initial_state: dict[str, Any],
    expected_state: dict[str, Any],
    expected_gate: str,
    evidence_refs: Iterable[str],
    sanitizer_version: str,
) -> dict[str, Any]:
    """Build a deterministic fixture after the primary sanitizer is clean."""
    content = input_event.get("content") if isinstance(input_event, dict) else None
    if not isinstance(content, str):
        raise ReplayFixtureError("input_event.content must be a string")
    sanitized = sanitize_text(content)
    if sanitized.findings or sanitized.text != content:
        raise ReplayFixtureError("fixture input requires a clean primary sanitizer result")
    _validate_extraction(extraction, content_sha256=sanitized.content_sha256)
    evidence_refs = list(evidence_refs)
    identity = {
        "project_id": project_id,
        "scenario_class": scenario_class,
        "source_thread_ref": source.get("source_thread_ref"),
        "source_range_ref": source.get("source_range_ref"),
        "range_sha256": source.get("range_sha256"),
        "content_sha256": input_event.get("content_sha256"),
    }
    fixture = {
        "schema_version": SCHEMA_VERSION,
        "fixture_id": _opaque_fixture_id(identity),
        "project_id": project_id,
        "scenario_class": scenario_class,
        "source": source,
        "input_event": input_event,
        "extraction": extraction,
        "initial_state": initial_state,
        "expected_state": expected_state,
        "expected_gate": expected_gate,
        "evidence_refs": evidence_refs,
        "sanitization": {
            "sanitizer_version": sanitizer_version,
            "status": "clean",
            "content_sha256": sanitized.content_sha256,
        },
    }
    fixture["fixture_sha256"] = _sha256(fixture)
    return fixture


def validate_replay_fixture(
    fixture: dict[str, Any],
    *,
    current_evidence_refs: set[str],
    validated_at: str,
) -> ValidationReceipt:
    """Validate fixture integrity, privacy, state semantics, and current evidence."""
    if not isinstance(fixture, dict) or set(fixture) != _FIXTURE_FIELDS:
        raise ReplayFixtureError("fixture fields do not match the contract")
    if fixture["schema_version"] != SCHEMA_VERSION:
        raise ReplayFixtureError("unsupported fixture schema_version")
    if not _FIXTURE_REF_RE.fullmatch(str(fixture["fixture_id"])):
        raise ReplayFixtureError("fixture_id must be opaque")
    _require_string(fixture["project_id"], "project_id")
    if fixture["scenario_class"] not in _SCENARIO_CLASSES:
        raise ReplayFixtureError("unsupported scenario_class")
    if fixture["expected_gate"] not in {"allow", "veto"}:
        raise ReplayFixtureError("expected_gate must be allow or veto")
    _validate_timestamp(validated_at)

    for key, _ in _walk(fixture):
        if key in _FORBIDDEN_KEYS:
            raise ReplayFixtureError(f"forbidden fixture field: {key}")
    for value in _all_strings(fixture):
        if _RAW_UUID_RE.search(value):
            raise ReplayFixtureError("raw provider UUID is forbidden")
        if _ABSOLUTE_PATH_RE.search(value):
            raise ReplayFixtureError("machine-specific path is forbidden")
        if _SECRET_RE.search(value):
            raise ReplayFixtureError("secret-like content is forbidden")

    source = fixture["source"]
    if not isinstance(source, dict) or set(source) != _SOURCE_FIELDS:
        raise ReplayFixtureError("fixture source fields do not match the contract")
    if source["provider"] not in {"codex", "claude", "cursor", "other"}:
        raise ReplayFixtureError("unsupported source provider")
    if not _THREAD_REF_RE.fullmatch(str(source["source_thread_ref"])):
        raise ReplayFixtureError("source_thread_ref must be opaque")
    if not _RANGE_REF_RE.fullmatch(str(source["source_range_ref"])):
        raise ReplayFixtureError("source_range_ref must be opaque")
    for key in ("archive_sha256", "range_sha256"):
        if not _SHA256_RE.fullmatch(str(source[key])):
            raise ReplayFixtureError(f"source.{key} must be SHA-256")
    if (
        not isinstance(source["byte_start"], int)
        or not isinstance(source["byte_end"], int)
        or source["byte_start"] < 0
        or source["byte_end"] <= source["byte_start"]
    ):
        raise ReplayFixtureError("fixture source byte range is invalid")
    _require_string(source["extractor_version"], "source.extractor_version")

    input_event = fixture["input_event"]
    if not isinstance(input_event, dict) or set(input_event) != _INPUT_FIELDS:
        raise ReplayFixtureError("input_event fields do not match the contract")
    if input_event["event_kind"] not in _EVENT_KINDS:
        raise ReplayFixtureError("unsupported fixture event_kind")
    content = _require_string(input_event["content"], "input_event.content")
    if len(content.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ReplayFixtureError("fixture input exceeds the bounded content limit")
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if input_event["content_sha256"] != content_sha256:
        raise ReplayFixtureError("fixture input content hash mismatch")
    _validate_extraction(fixture["extraction"], content_sha256=content_sha256)

    sanitization = fixture["sanitization"]
    if not isinstance(sanitization, dict) or set(sanitization) != {
        "sanitizer_version",
        "status",
        "content_sha256",
    }:
        raise ReplayFixtureError("sanitization fields do not match the contract")
    _require_string(sanitization["sanitizer_version"], "sanitizer_version")
    if sanitization["status"] != "clean" or sanitization["content_sha256"] != content_sha256:
        raise ReplayFixtureError("fixture sanitization claim does not match content")

    _validate_state(fixture["initial_state"], "initial_state")
    _validate_state(fixture["expected_state"], "expected_state")

    evidence_refs = fixture["evidence_refs"]
    if not isinstance(evidence_refs, list) or not evidence_refs:
        raise ReplayFixtureError("fixture requires current evidence refs")
    if not all(
        isinstance(ref, str)
        and (ref.startswith("artifact://") or ref.startswith("assertion:"))
        and ref in current_evidence_refs
        for ref in evidence_refs
    ):
        raise ReplayFixtureError("fixture evidence is not in the current verified set")

    fixture_payload = {key: value for key, value in fixture.items() if key != "fixture_sha256"}
    fixture_sha256 = _sha256(fixture_payload)
    if fixture["fixture_sha256"] != fixture_sha256:
        raise ReplayFixtureError("fixture digest mismatch")
    expected_fixture_id = _opaque_fixture_id(
        {
            "project_id": fixture["project_id"],
            "scenario_class": fixture["scenario_class"],
            "source_thread_ref": source["source_thread_ref"],
            "source_range_ref": source["source_range_ref"],
            "range_sha256": source["range_sha256"],
            "content_sha256": input_event["content_sha256"],
        }
    )
    if fixture["fixture_id"] != expected_fixture_id:
        raise ReplayFixtureError("fixture_id does not match fixture identity")

    receipt_core = {
        "validator_version": VALIDATOR_VERSION,
        "fixture_id": fixture["fixture_id"],
        "fixture_sha256": fixture_sha256,
        "validated_at": validated_at,
        "status": "passed",
        "evidence_count": len(evidence_refs),
    }
    return ValidationReceipt(**receipt_core, receipt_sha256=_sha256(receipt_core))
