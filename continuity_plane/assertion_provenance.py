"""Current-evidence provenance records for bearing assertions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "context.assertion-provenance/v1alpha1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_ARTIFACT_REF_RE = re.compile(r"^artifact://sha256/[0-9a-f]{64}$")
_REPO_REF_RE = re.compile(
    r"^repo://(?P<repository>[A-Za-z0-9][A-Za-z0-9._-]{0,255})/"
    r"(?P<path>[^#]+?)(?:#(?P<fragment>L[1-9][0-9]*|bytes=[0-9]+:[0-9]+))?$"
)
_WORKTREE_REVISION_RE = re.compile(r"^worktree:sha256:(?P<sha256>[0-9a-f]{64})$")
_GIT_REVISION_RE = re.compile(r"^git:(?P<revision>[0-9a-f]{7,40})$")
_STATE_REF_RE = re.compile(r"^state://[A-Za-z0-9][A-Za-z0-9._:/-]{0,2040}$")
_CURRENT_AUTHORITIES = {
    "current_code",
    "current_state",
    "industry_standard",
    "os_official",
    "software_official",
}
_AUTHORITY_KINDS = _CURRENT_AUTHORITIES | {"memory_candidate", "historical_report"}
_LOCAL_AUTHORITIES = {"current_code", "current_state"}
_EVIDENCE_FIELDS = {
    "evidence_id",
    "authority_kind",
    "source_ref",
    "revision",
    "sha256",
    "valid_at",
    "retrieval_receipt_ref",
}
_RECORD_FIELDS = {
    "schema_version",
    "assertion_id",
    "assertion_text",
    "assertion_sha256",
    "bearing",
    "evidence",
    "provenance_coverage",
    "asserted_at",
    "valid_until",
    "state_write_authority",
    "record_sha256",
}


class AssertionProvenanceError(ValueError):
    """Raised when a bearing assertion lacks current, valid evidence."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _record_digest(record: dict[str, Any]) -> str:
    unsigned = copy.deepcopy(record)
    unsigned.pop("record_sha256", None)
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _safe(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SAFE_ID_RE.fullmatch(value) is None:
        raise AssertionProvenanceError(f"{field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int = 16_384) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > maximum
        or "\x00" in value
    ):
        raise AssertionProvenanceError(f"{field} is invalid")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise AssertionProvenanceError(f"{field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AssertionProvenanceError(f"{field} is invalid") from exc
    if parsed.tzinfo is None:
        raise AssertionProvenanceError(f"{field} requires a timezone")
    return parsed


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise AssertionProvenanceError(f"{field} is invalid")
    return value


def _resolved_bytes(value: Any, field: str) -> bytes:
    if value is None:
        raise AssertionProvenanceError(f"{field} does not resolve")
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise AssertionProvenanceError(f"{field} resolver returned invalid content")
    return bytes(value)


def _validate_fragment(fragment: str | None, payload: bytes) -> None:
    if fragment is None:
        return
    if fragment.startswith("L"):
        line = int(fragment[1:])
        if line > len(payload.splitlines()):
            raise AssertionProvenanceError("source_ref line does not resolve")
        return
    offset_text, length_text = fragment.removeprefix("bytes=").split(":", 1)
    offset = int(offset_text)
    length = int(length_text)
    if length <= 0 or offset + length > len(payload):
        raise AssertionProvenanceError("source_ref byte range does not resolve")


def _resolve_repo_evidence(evidence: dict[str, Any], root: str | Path) -> bytes:
    match = _REPO_REF_RE.fullmatch(evidence["source_ref"])
    if match is None:
        raise AssertionProvenanceError("current_code source_ref is invalid")
    resolved_root = Path(root).resolve()
    if match.group("repository") != resolved_root.name:
        raise AssertionProvenanceError("current_code source_ref repository mismatch")
    relative = Path(match.group("path"))
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in match.group("path")
    ):
        raise AssertionProvenanceError("current_code source_ref is invalid")
    source_path = (resolved_root / relative).resolve()
    if resolved_root not in source_path.parents:
        raise AssertionProvenanceError("current_code source_ref is outside repository")
    revision = evidence["revision"]
    worktree_match = _WORKTREE_REVISION_RE.fullmatch(revision)
    git_match = _GIT_REVISION_RE.fullmatch(revision)
    if worktree_match is not None:
        try:
            payload = source_path.read_bytes()
        except OSError as exc:
            raise AssertionProvenanceError(
                "current_code source_ref does not resolve"
            ) from exc
        if worktree_match.group("sha256") != hashlib.sha256(payload).hexdigest():
            raise AssertionProvenanceError(
                "current_code revision does not bind resolved content"
            )
    elif git_match is not None:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(resolved_root),
                "show",
                f"{git_match.group('revision')}:{relative.as_posix()}",
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionProvenanceError(
                "current_code revision or source_ref does not resolve"
            )
        payload = completed.stdout
    else:
        raise AssertionProvenanceError(
            "current_code revision must bind Git or worktree content"
        )
    _validate_fragment(match.group("fragment"), payload)
    return payload


def _resolve_evidence(
    evidence: dict[str, Any],
    *,
    root: str | Path | None,
    evidence_resolver: Callable[
        [str, str], bytes | bytearray | memoryview | None
    ]
    | None,
) -> bytes:
    if evidence["authority_kind"] == "current_state":
        if _STATE_REF_RE.fullmatch(evidence["source_ref"]) is None:
            raise AssertionProvenanceError("current_state source_ref is invalid")
        if evidence_resolver is None:
            raise AssertionProvenanceError("current_state evidence requires a resolver")
    if evidence["authority_kind"] == "current_code" and root is not None:
        return _resolve_repo_evidence(evidence, root)
    if evidence_resolver is not None:
        try:
            return _resolved_bytes(
                evidence_resolver(evidence["source_ref"], evidence["revision"]),
                "current evidence",
            )
        except AssertionProvenanceError:
            raise
        except Exception as exc:
            raise AssertionProvenanceError("current evidence resolver failed") from exc
    raise AssertionProvenanceError("current evidence requires a resolver")


def _validate_resolved_bearing_evidence(
    evidence: dict[str, Any],
    *,
    root: str | Path | None,
    evidence_resolver: Callable[
        [str, str], bytes | bytearray | memoryview | None
    ]
    | None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None],
) -> None:
    payload = _resolve_evidence(
        evidence, root=root, evidence_resolver=evidence_resolver
    )
    if hashlib.sha256(payload).hexdigest() != evidence["sha256"]:
        raise AssertionProvenanceError("current evidence digest mismatch")
    receipt_ref = evidence["retrieval_receipt_ref"]
    try:
        receipt = _resolved_bytes(
            artifact_resolver(receipt_ref), "retrieval receipt"
        )
    except AssertionProvenanceError:
        raise
    except Exception as exc:
        raise AssertionProvenanceError("retrieval receipt resolver failed") from exc
    expected_receipt_sha256 = receipt_ref.rsplit("/", 1)[-1]
    if hashlib.sha256(receipt).hexdigest() != expected_receipt_sha256:
        raise AssertionProvenanceError("retrieval receipt digest mismatch")
    try:
        parsed_receipt = json.loads(receipt)
        from .retrieval_routing import validate_retrieval_receipt

        validate_retrieval_receipt(parsed_receipt)
    except Exception as exc:
        raise AssertionProvenanceError("retrieval receipt is invalid") from exc


def _validate_evidence(evidence: Any, index: int) -> None:
    if not isinstance(evidence, dict) or set(evidence) != _EVIDENCE_FIELDS:
        raise AssertionProvenanceError(f"evidence {index} fields are invalid")
    _safe(evidence["evidence_id"], "evidence_id")
    if evidence["authority_kind"] not in _AUTHORITY_KINDS:
        raise AssertionProvenanceError("evidence authority_kind is invalid")
    _text(evidence["source_ref"], "source_ref", 2048)
    _text(evidence["revision"], "revision", 512)
    _sha256(evidence["sha256"], "evidence.sha256")
    _timestamp(evidence["valid_at"], "valid_at")
    receipt_ref = _text(
        evidence["retrieval_receipt_ref"], "retrieval_receipt_ref", 2048
    )
    if _ARTIFACT_REF_RE.fullmatch(receipt_ref) is None:
        raise AssertionProvenanceError("retrieval_receipt_ref must be content-addressed")


def compose_assertion_provenance(
    *,
    assertion_id: str,
    assertion_text: str,
    bearing: bool,
    evidence: list[dict[str, Any]],
    asserted_at: str,
    valid_until: str,
    root: str | Path | None = None,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None = None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None]
    | None = None,
) -> dict[str, Any]:
    """Bind a claim to current evidence without granting state authority."""
    _safe(assertion_id, "assertion_id")
    assertion_text = _text(assertion_text, "assertion_text")
    if type(bearing) is not bool:
        raise AssertionProvenanceError("bearing is invalid")
    if not isinstance(evidence, list) or len(evidence) > 256:
        raise AssertionProvenanceError("evidence is invalid")
    for index, item in enumerate(evidence):
        _validate_evidence(item, index)
    if len({item["evidence_id"] for item in evidence}) != len(evidence):
        raise AssertionProvenanceError("evidence IDs must be unique")
    asserted = _timestamp(asserted_at, "asserted_at")
    if bearing:
        if not evidence:
            raise AssertionProvenanceError("bearing assertion requires evidence")
        if any(item["authority_kind"] not in _CURRENT_AUTHORITIES for item in evidence):
            raise AssertionProvenanceError(
                "memory or historical candidate cannot support a bearing assertion"
            )
        if any(item["authority_kind"] in _LOCAL_AUTHORITIES for item in evidence) and (
            root is None and evidence_resolver is None
        ):
            raise AssertionProvenanceError(
                "current code or State bearing evidence requires a resolver"
            )
        if any(item["authority_kind"] in _LOCAL_AUTHORITIES for item in evidence) and (
            artifact_resolver is None
        ):
            raise AssertionProvenanceError(
                "current code or State bearing evidence requires an artifact resolver"
            )
        if any(_timestamp(item["valid_at"], "valid_at") > asserted for item in evidence):
            raise AssertionProvenanceError("bearing evidence postdates the assertion")
    expires = _timestamp(valid_until, "valid_until")
    if expires <= asserted:
        raise AssertionProvenanceError("valid_until must follow asserted_at")
    record = {
        "schema_version": SCHEMA_VERSION,
        "assertion_id": assertion_id,
        "assertion_text": assertion_text,
        "assertion_sha256": hashlib.sha256(assertion_text.encode("utf-8")).hexdigest(),
        "bearing": bearing,
        "evidence": copy.deepcopy(evidence),
        "provenance_coverage": 1.0 if evidence else 0.0,
        "asserted_at": asserted_at,
        "valid_until": valid_until,
        "state_write_authority": False,
        "record_sha256": "",
    }
    record["record_sha256"] = _record_digest(record)
    validate_assertion_provenance(
        record,
        current_time=asserted_at,
        root=root,
        evidence_resolver=evidence_resolver,
        artifact_resolver=artifact_resolver,
    )
    return record


def validate_assertion_provenance(
    record: Any,
    *,
    current_time: str | None = None,
    root: str | Path | None = None,
    evidence_resolver: Callable[[str, str], bytes | bytearray | memoryview | None]
    | None = None,
    artifact_resolver: Callable[[str], bytes | bytearray | memoryview | None]
    | None = None,
) -> None:
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise AssertionProvenanceError("assertion provenance fields are invalid")
    if record["schema_version"] != SCHEMA_VERSION:
        raise AssertionProvenanceError("assertion provenance schema_version is invalid")
    _safe(record["assertion_id"], "assertion_id")
    text = _text(record["assertion_text"], "assertion_text")
    _sha256(record["assertion_sha256"], "assertion_sha256")
    if record["assertion_sha256"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
        raise AssertionProvenanceError("assertion text digest mismatch")
    if type(record["bearing"]) is not bool:
        raise AssertionProvenanceError("bearing is invalid")
    evidence = record["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 256:
        raise AssertionProvenanceError("evidence is invalid")
    for index, item in enumerate(evidence):
        _validate_evidence(item, index)
    if record["bearing"]:
        if not evidence or any(item["authority_kind"] not in _CURRENT_AUTHORITIES for item in evidence):
            raise AssertionProvenanceError("bearing assertion contains candidate provenance")
        if any(item["authority_kind"] in _LOCAL_AUTHORITIES for item in evidence) and (
            root is None and evidence_resolver is None
        ):
            raise AssertionProvenanceError(
                "current code or State bearing evidence requires a resolver"
            )
        if any(item["authority_kind"] in _LOCAL_AUTHORITIES for item in evidence) and (
            artifact_resolver is None
        ):
            raise AssertionProvenanceError(
                "current code or State bearing evidence requires an artifact resolver"
            )
        for item in evidence:
            if item["authority_kind"] in _LOCAL_AUTHORITIES:
                _validate_resolved_bearing_evidence(
                    item,
                    root=root,
                    evidence_resolver=evidence_resolver,
                    artifact_resolver=artifact_resolver,
                )
        if record["provenance_coverage"] != 1.0:
            raise AssertionProvenanceError("bearing assertion provenance coverage is incomplete")
    elif record["provenance_coverage"] != (1.0 if evidence else 0.0):
        raise AssertionProvenanceError("provenance_coverage is inaccurate")
    asserted = _timestamp(record["asserted_at"], "asserted_at")
    expires = _timestamp(record["valid_until"], "valid_until")
    if expires <= asserted:
        raise AssertionProvenanceError("validity interval is invalid")
    if record["bearing"] and any(
        _timestamp(item["valid_at"], "valid_at") > asserted for item in evidence
    ):
        raise AssertionProvenanceError("bearing evidence postdates the assertion")
    if current_time is not None and _timestamp(current_time, "current_time") > expires:
        raise AssertionProvenanceError("assertion provenance has expired")
    if record["state_write_authority"] is not False:
        raise AssertionProvenanceError("assertion records cannot write State")
    _sha256(record["record_sha256"], "record_sha256")
    if record["record_sha256"] != _record_digest(record):
        raise AssertionProvenanceError("assertion record digest mismatch")


def canonical_assertion_provenance_bytes(record: dict[str, Any]) -> bytes:
    validate_assertion_provenance(record)
    return _canonical(record)
