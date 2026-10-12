"""Small project rules and unresolved commitments sidecars."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .collaboration_registry import CollaborationRegistryError, _ID_RE, _atomic_write, _locked, _text, default_data_root, project_data_dir


def _document(data_root: Path | str | None, project_id: str, name: str) -> tuple[Path, list[dict[str, Any]]]:
    directory = project_data_dir(data_root or default_data_root(), project_id)
    path = directory / name
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    except (OSError, json.JSONDecodeError) as exc:
        raise CollaborationRegistryError(f"{name} is invalid") from exc
    if not isinstance(value, list):
        raise CollaborationRegistryError(f"{name} is invalid")
    return path, value


def add_rule(data_root: Path | str | None, *, project_id: str, rule_id: str, text: str, audience: str = "all") -> dict[str, Any]:
    if _ID_RE.fullmatch(rule_id) is None:
        raise CollaborationRegistryError("rule_id is invalid")
    _text(text, "rule", 4096)
    path, rows = _document(data_root, project_id, "rules.json")
    item = {"rule_id": rule_id, "text": text, "audience": audience, "status": "active"}
    with _locked(path):
        if any(row.get("rule_id") == rule_id for row in rows):
            raise CollaborationRegistryError("rule_id already exists")
        rows.append(item)
        _atomic_write(path, rows)
    return item


def list_rules(data_root: Path | str | None, *, project_id: str, audience: str = "all") -> list[dict[str, Any]]:
    _, rows = _document(data_root, project_id, "rules.json")
    return [row for row in rows if row.get("status") == "active" and row.get("audience") in {"all", audience}]


def add_commitment(data_root: Path | str | None, *, project_id: str, commitment_id: str, text: str) -> dict[str, Any]:
    if _ID_RE.fullmatch(commitment_id) is None:
        raise CollaborationRegistryError("commitment_id is invalid")
    _text(text, "commitment", 4096)
    path, rows = _document(data_root, project_id, "commitments.json")
    item = {"commitment_id": commitment_id, "text": text, "status": "open"}
    with _locked(path):
        if any(row.get("commitment_id") == commitment_id for row in rows):
            raise CollaborationRegistryError("commitment_id already exists")
        rows.append(item)
        _atomic_write(path, rows)
    return item


def close_commitment(data_root: Path | str | None, *, project_id: str, commitment_id: str) -> dict[str, Any]:
    path, rows = _document(data_root, project_id, "commitments.json")
    for row in rows:
        if row.get("commitment_id") == commitment_id:
            row["status"] = "closed"
            with _locked(path):
                _atomic_write(path, rows)
            return row
    raise CollaborationRegistryError("commitment_id does not exist")


def list_commitments(data_root: Path | str | None, *, project_id: str) -> list[dict[str, Any]]:
    _, rows = _document(data_root, project_id, "commitments.json")
    return [row for row in rows if row.get("status") == "open"]
