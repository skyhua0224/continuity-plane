"""Versioned candidate attachment for existing project governance documents."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .effect_scope_gate import validate_scope


SCHEMA_VERSION = "context.canonical-attach-proposal/v1alpha1"
_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_WORK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_KINDS = {"master", "status"}


class CanonicalAttachError(ValueError):
    """Raised when a canonical-plan attachment is invalid or stale."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CanonicalAttachError("proposal is not canonical JSON") from exc


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise CanonicalAttachError(f"{field} must be lowercase SHA-256")
    return value


def _digest(proposal: dict[str, Any]) -> str:
    unsigned = {key: value for key, value in proposal.items() if key != "proposal_sha256"}
    return hashlib.sha256(_canonical(unsigned)).hexdigest()


def _source_path(root: Path, raw: str) -> tuple[Path, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise CanonicalAttachError("source path is invalid")
    path = Path(raw)
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if not resolved.is_file():
        raise CanonicalAttachError(f"source is missing: {raw}")
    try:
        display = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        display = str(resolved)
    return resolved, display


def _source_entry(root: Path, kind: str, raw: str) -> dict[str, str]:
    if kind not in _SOURCE_KINDS:
        raise CanonicalAttachError("source kind is invalid")
    path, display = _source_path(root, raw)
    return {
        "kind": kind,
        "path": display,
        "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _work(work_id: str, title: str, owner_ref: str, scopes: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(work_id, str) or _WORK_ID_RE.fullmatch(work_id) is None:
        raise CanonicalAttachError("work_id is invalid")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise CanonicalAttachError("work title is invalid")
    if not isinstance(owner_ref, str) or not owner_ref.strip() or len(owner_ref) > 200:
        raise CanonicalAttachError("owner_ref is invalid")
    if not isinstance(scopes, list) or not scopes:
        raise CanonicalAttachError("work scopes are invalid")
    normalized: list[dict[str, str]] = []
    for scope in scopes:
        try:
            validate_scope(scope)
        except ValueError as exc:
            raise CanonicalAttachError("work scope is invalid") from exc
        normalized.append(dict(scope))
    if len({(scope["scope_kind"], scope["scope_ref"]) for scope in normalized}) != len(normalized):
        raise CanonicalAttachError("work scopes must be unique")
    return {
        "work_id": work_id,
        "title": title.strip(),
        "owner_ref": owner_ref,
        "scope_refs": normalized,
    }


def build_attach_proposal(
    *,
    root: Path,
    project_id: str,
    master_path: str,
    status_path: str,
    work_id: str,
    work_title: str,
    owner_ref: str,
    scope_refs: list[dict[str, str]],
    created_at: str,
) -> dict[str, Any]:
    """Bind existing governance sources to one explicit candidate Work."""
    if not isinstance(project_id, str) or _ID_RE.fullmatch(project_id) is None:
        raise CanonicalAttachError("project_id is invalid")
    if not isinstance(created_at, str) or "T" not in created_at or "+" not in created_at:
        raise CanonicalAttachError("created_at is invalid")
    proposal = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "created_at": created_at,
        "sources": [
            _source_entry(root, "master", master_path),
            _source_entry(root, "status", status_path),
        ],
        "work": _work(work_id, work_title, owner_ref, scope_refs),
        "state_write_authority": False,
        "proposal_sha256": "0" * 64,
    }
    proposal["proposal_sha256"] = _digest(proposal)
    validate_attach_proposal(root, proposal, verify_sources=True)
    return proposal


def validate_attach_proposal(
    root: Path,
    proposal: Any,
    *,
    verify_sources: bool,
) -> dict[str, Any]:
    """Validate structure, digest, and optionally current source hashes."""
    if not isinstance(proposal, dict) or set(proposal) != {
        "schema_version",
        "project_id",
        "created_at",
        "sources",
        "work",
        "state_write_authority",
        "proposal_sha256",
    }:
        raise CanonicalAttachError("proposal fields are invalid")
    if proposal["schema_version"] != SCHEMA_VERSION:
        raise CanonicalAttachError("proposal schema_version is unsupported")
    if not isinstance(proposal["project_id"], str) or _ID_RE.fullmatch(proposal["project_id"]) is None:
        raise CanonicalAttachError("project_id is invalid")
    if not isinstance(proposal["created_at"], str) or "T" not in proposal["created_at"]:
        raise CanonicalAttachError("created_at is invalid")
    if proposal["state_write_authority"] is not False:
        raise CanonicalAttachError("proposal cannot claim State write authority")
    _sha(proposal["proposal_sha256"], "proposal_sha256")
    if proposal["proposal_sha256"] != _digest(proposal):
        raise CanonicalAttachError("proposal digest is invalid")
    sources = proposal["sources"]
    if not isinstance(sources, list) or len(sources) != 2:
        raise CanonicalAttachError("proposal sources are invalid")
    kinds: set[str] = set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"kind", "path", "content_sha256"}:
            raise CanonicalAttachError("proposal source fields are invalid")
        kind = source["kind"]
        if kind not in _SOURCE_KINDS or kind in kinds:
            raise CanonicalAttachError("proposal source kind is invalid")
        kinds.add(kind)
        _sha(source["content_sha256"], "proposal source hash")
        if verify_sources:
            path, _ = _source_path(root, source["path"])
            if hashlib.sha256(path.read_bytes()).hexdigest() != source["content_sha256"]:
                raise CanonicalAttachError("proposal source changed since plan")
    if kinds != _SOURCE_KINDS:
        raise CanonicalAttachError("proposal source set is invalid")
    work = proposal["work"]
    if not isinstance(work, dict) or set(work) != {"work_id", "title", "owner_ref", "scope_refs"}:
        raise CanonicalAttachError("proposal work fields are invalid")
    _work(work["work_id"], work["title"], work["owner_ref"], work["scope_refs"])
    return proposal


__all__ = [
    "CanonicalAttachError",
    "SCHEMA_VERSION",
    "build_attach_proposal",
    "validate_attach_proposal",
]
