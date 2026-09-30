"""Read-only inventory for local Agent Skills directories."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


DEFAULT_DESCRIPTION_BUDGET = 8000


class SkillsInventoryError(ValueError):
    """A skill inventory input is invalid."""


@dataclass(frozen=True)
class SkillRecord:
    name: str
    path: str
    scope: str
    description: str
    description_bytes: int
    body_bytes: int
    implicit: bool
    issues: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "scope": self.scope,
            "description_bytes": self.description_bytes,
            "body_bytes": self.body_bytes,
            "implicit": self.implicit,
            "issues": list(self.issues),
        }


def _frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = re.fullmatch(r"---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
    if match is None:
        return {}, text
    try:
        document = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillsInventoryError(f"invalid skill frontmatter: {exc}") from exc
    if not isinstance(document, dict):
        return {}, match.group(2)
    return document, match.group(2)


def _implicit(path: Path) -> bool:
    policy = path.parent / "agents" / "openai.yaml"
    try:
        document = yaml.safe_load(policy.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return True
    if not isinstance(document, dict):
        return True
    value = document.get("policy", {}).get("allow_implicit_invocation")
    return value is not False


def audit_skills(
    roots: list[Path | str],
    *,
    description_budget: int = DEFAULT_DESCRIPTION_BUDGET,
) -> dict[str, Any]:
    """Audit skill metadata without changing host configuration."""
    if type(description_budget) is not int or description_budget < 1:
        raise SkillsInventoryError("description_budget must be positive")
    records: list[SkillRecord] = []
    seen_paths: set[Path] = set()
    for root_value in roots:
        root = Path(root_value).expanduser()
        if not root.is_dir():
            continue
        scope = str(root)
        for path in sorted(root.rglob("SKILL.md")):
            resolved = path.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            metadata, body = _frontmatter(text)
            name = metadata.get("name")
            description = metadata.get("description")
            issues: list[str] = []
            if not isinstance(name, str) or not name.strip():
                name = path.parent.name
                issues.append("missing-valid-name")
            if not isinstance(description, str) or not description.strip():
                description = ""
                issues.append("missing-valid-description")
            if description == ">-":
                issues.append("literal-yaml-fold-marker")
            if re.search(r"always[ -]active", description, re.IGNORECASE):
                issues.append("always-active")
            if len(description.encode("utf-8")) > 500:
                issues.append("description-over-500-bytes")
            if len(body.encode("utf-8")) > 20_000:
                issues.append("body-over-20kb")
            record = SkillRecord(
                name=name,
                path=str(path),
                scope=scope,
                description=description,
                description_bytes=len(description.encode("utf-8")),
                body_bytes=len(body.encode("utf-8")),
                implicit=_implicit(path),
                issues=tuple(issues),
            )
            records.append(record)
    by_name: dict[str, list[SkillRecord]] = {}
    for record in records:
        by_name.setdefault(record.name, []).append(record)
    duplicates = {
        name: [record.as_dict() for record in copies]
        for name, copies in by_name.items()
        if len(copies) > 1
    }
    recommendations: list[dict[str, str]] = []
    for record in records:
        action: str | None = None
        if "missing-valid-description" in record.issues or "literal-yaml-fold-marker" in record.issues:
            action = "repair-or-disable"
        elif "always-active" in record.issues:
            action = "remove-always-active-or-make-explicit"
        elif len(by_name[record.name]) > 1:
            action = "keep-one-scope-and-disable-duplicates"
        elif record.description_bytes > 500:
            action = "shorten-description"
        elif record.body_bytes > 20_000:
            action = "move-detail-to-references"
        if action is not None:
            recommendations.append({"path": record.path, "action": action})
    return {
        "schema_version": "context.skills-inventory/v1alpha1",
        "dry_run": True,
        "skill_count": len(records),
        "unique_names": len(by_name),
        "implicit_count": sum(record.implicit for record in records),
        "description_bytes": sum(record.description_bytes for record in records),
        "description_budget": description_budget,
        "description_budget_exceeded": sum(
            record.description_bytes for record in records
        ) > description_budget,
        "duplicate_names": duplicates,
        "records": [record.as_dict() for record in records],
        "recommendations": recommendations,
        "cleanup_policy": "report-only; host configuration is not modified",
    }
