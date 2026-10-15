"""Scoped vocabulary memory shared by collaborating agent sessions."""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .collaboration_registry import (
    project_data_dir,
    write_json_atomic,
)


VOCABULARY_SCHEMA = "context.vocabulary-memory/v1alpha1"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_SCOPE = {"global", "project"}
_CONFIDENCE = {"candidate", "confirmed"}
_SECRET_RE = re.compile(
    r"(?:api[_-]?key|token|secret|password|passwd|authorization|bearer"
    r"|private[_-]?key|access[_-]?key|credential)[\"']?\s*[:=]",
    re.IGNORECASE,
)


class VocabularyMemoryError(ValueError):
    """A vocabulary entry or lookup is invalid."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: Any, field: str, limit: int = 4096) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > limit
        or "\n" in value
        or "\r" in value
    ):
        raise VocabularyMemoryError(f"{field} is invalid")
    return value


def _normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _alias_matches(alias: str, text: str) -> bool:
    # Latin identifiers need word boundaries; CJK phrases need no spaces.
    alias = _normalize(alias)
    left = r"(?<![A-Za-z0-9_.-])" if alias and alias[0].isascii() and (alias[0].isalnum() or alias[0] in "_.-") else ""
    right = r"(?![A-Za-z0-9_.-])" if alias and alias[-1].isascii() and (alias[-1].isalnum() or alias[-1] in "_.-") else ""
    parts = alias.split(" ")
    pattern = re.escape(parts[0])
    for previous, following in zip(parts, parts[1:]):
        cjk = lambda char: "\u3400" <= char <= "\u9fff"
        separator = r" *" if cjk(previous[-1]) or cjk(following[0]) else r" +"
        pattern += separator + re.escape(following)
    return bool(re.search(left + pattern + right, text))


def _aliases(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise VocabularyMemoryError("aliases must be a non-empty list")
    result = []
    for alias in value:
        text = _normalize(_text(alias, "alias", 256))
        if text not in result:
            result.append(text)
    return result


def _path(scope: str, data_root: Path, project_id: str | None) -> Path:
    if scope == "global":
        return data_root / "vocabulary.json"
    if project_id is None:
        raise VocabularyMemoryError("project vocabulary requires project_id")
    return project_data_dir(data_root, project_id) / "vocabulary.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        document = {"schema_version": VOCABULARY_SCHEMA, "entities": [], "updated_at": _now()}
    except (OSError, json.JSONDecodeError) as exc:
        raise VocabularyMemoryError(f"vocabulary memory is unreadable: {exc}") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != VOCABULARY_SCHEMA
        or not isinstance(document.get("entities"), list)
    ):
        raise VocabularyMemoryError("vocabulary memory schema is unsupported")
    return document


def _validate(entity: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "entity_id",
        "aliases",
        "statement",
        "scope",
        "confidence",
        "source",
        "created_at",
        "updated_at",
        "superseded_by",
    }
    if not isinstance(entity, dict) or set(entity) != fields:
        raise VocabularyMemoryError("entity fields are invalid")
    if _ID_RE.fullmatch(entity["entity_id"]) is None:
        raise VocabularyMemoryError("entity_id is invalid")
    _aliases(entity["aliases"])
    _text(entity["statement"], "statement", 2048)
    if entity["scope"] not in _SCOPE or entity["confidence"] not in _CONFIDENCE:
        raise VocabularyMemoryError("entity scope or confidence is invalid")
    _text(entity["source"], "source", 256)
    _text(entity["created_at"], "created_at", 64)
    _text(entity["updated_at"], "updated_at", 64)
    if entity["superseded_by"] is not None and _ID_RE.fullmatch(
        entity["superseded_by"]
    ) is None:
        raise VocabularyMemoryError("superseded_by is invalid")
    return entity


def add_entity(
    data_root: Path | str,
    *,
    entity_id: str,
    aliases: list[str],
    statement: str,
    scope: str,
    source: str,
    project_id: str | None = None,
    confidence: str = "candidate",
) -> dict[str, Any]:
    """Add or replace an alias entry without storing secret values."""
    root = Path(data_root)
    if scope not in _SCOPE or confidence not in _CONFIDENCE:
        raise VocabularyMemoryError("scope or confidence is invalid")
    if _SECRET_RE.search(statement) or any(_SECRET_RE.search(alias) for alias in aliases):
        raise VocabularyMemoryError(
            "secret values are forbidden; store a secret-manager reference instead"
        )
    timestamp = _now()
    entity = {
        "entity_id": entity_id,
        "aliases": _aliases(aliases),
        "statement": _text(statement, "statement", 2048),
        "scope": scope,
        "confidence": confidence,
        "source": _text(source, "source", 256),
        "created_at": timestamp,
        "updated_at": timestamp,
        "superseded_by": None,
    }
    _validate(entity)
    target = _path(scope, root, project_id)
    document = _load(target)
    for old in document["entities"]:
        if old.get("entity_id") == entity_id and old.get("superseded_by") is None:
            old["superseded_by"] = entity_id
            old["updated_at"] = timestamp
    document["entities"] = [item for item in document["entities"] if item != entity]
    document["entities"].append(entity)
    document["updated_at"] = timestamp
    target.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(target, document)
    return entity


def list_entities(
    data_root: Path | str,
    *,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = [_load(_path("global", Path(data_root), None))]
    if project_id is not None:
        documents.append(_load(_path("project", Path(data_root), project_id)))
    return [item for document in documents for item in document["entities"]]


def resolve(
    data_root: Path | str,
    query: str,
    *,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Resolve normalized phrases without partial Latin identifier matches."""
    if not isinstance(query, str) or len(query.encode("utf-8")) > 16384:
        raise VocabularyMemoryError("query is invalid")
    if not query.strip():
        return []
    text = _normalize(_text(query, "query", 16384))
    matches: list[dict[str, Any]] = []
    project_aliases: set[str] = set()
    for entity in list_entities(data_root, project_id=project_id):
        if entity.get("superseded_by") is not None:
            continue
        if not any(_alias_matches(alias, text) for alias in entity["aliases"]):
            continue
        if entity["scope"] == "project":
            project_aliases.update(_normalize(alias) for alias in entity["aliases"])
        matches.append(entity)
    return [
        entity
        for entity in matches
        if entity["scope"] == "global"
        and not {_normalize(alias) for alias in entity["aliases"]} & project_aliases
        or entity["scope"] == "project"
    ]
