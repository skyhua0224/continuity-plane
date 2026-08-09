#!/usr/bin/env python3
"""Verify repository governance and admission gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml


REVISION_PATTERN = re.compile(r"^版本：revision (\d+)\s*$", re.MULTILINE)
ACTIVE_WORK_PATTERN = re.compile(r"^\| active work \| (M\d+-\d+)", re.MULTILINE)
TARGET_STATE_REVISION_PATTERN = re.compile(
    r"^governance authority：`MASTER\.md` revision (\d+)\s*$", re.MULTILINE
)
RAW_TRANSCRIPT_SUFFIXES = (".jsonl", ".jsonl.zst", ".rollout")
MARKDOWN_LINK_PATTERN = re.compile(r"\[[^\]]*\]\((<[^>]+>|[^)\s]+)")
PROHIBITED_DOCUMENTATION_PATTERNS = (
    re.compile(r"不是[^\n]{0,80}而是"),
    re.compile(r"第一刀|更聪明|我们在哪|偷偷"),
)


def _document_revision(path: Path) -> int:
    match = REVISION_PATTERN.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"missing document revision: {path}")
    return int(match.group(1))


def verify_repository(root: Path) -> list[str]:
    try:
        master_path = root / "MASTER.md"
        status_path = root / "STATUS.md"
        target_state_path = root / "docs" / "architecture" / "target-state.md"
        master_text = master_path.read_text(encoding="utf-8")
        status_text = status_path.read_text(encoding="utf-8")
        target_state_text = target_state_path.read_text(encoding="utf-8")
        master_revision = _document_revision(master_path)
        status_revision = _document_revision(status_path)
    except (OSError, ValueError) as error:
        return [str(error)]

    if master_revision != status_revision:
        return [
            "MASTER/STATUS revision mismatch: "
            f"MASTER={master_revision}, STATUS={status_revision}"
        ]
    target_state_match = TARGET_STATE_REVISION_PATTERN.search(target_state_text)
    if target_state_match is None:
        return ["target-state governance revision is missing"]
    target_state_revision = int(target_state_match.group(1))
    if master_revision != target_state_revision:
        return [
            "MASTER/target-state revision mismatch: "
            f"MASTER={master_revision}, target-state={target_state_revision}"
        ]
    active_match = ACTIVE_WORK_PATTERN.search(status_text)
    if active_match is None:
        return ["STATUS active work is missing"]
    active_work_id = active_match.group(1)
    master_task_pattern = re.compile(
        rf"^\| {re.escape(active_work_id)} \| ([^|]+) \|", re.MULTILINE
    )
    if master_task_pattern.search(master_text) is None:
        return [f"active leaf {active_work_id} is missing from MASTER"]
    task_match = master_task_pattern.search(master_text)
    if "🟡" not in task_match.group(1):
        return [f"active leaf {active_work_id} must be 🟡 in MASTER"]

    registry_path = root / "schemas" / "registry.yaml"
    try:
        registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
        schema_entries = registry["schemas"]
    except (OSError, TypeError, KeyError, yaml.YAMLError) as error:
        return [f"invalid schema registry: {error}"]

    for entry in schema_entries:
        artifact_path = root / entry["artifact_path"]
        try:
            actual_hash = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
            expected_hash = entry["content_sha256"]
        except (OSError, KeyError, TypeError) as error:
            return [f"invalid schema registry entry: {error}"]
        if actual_hash != expected_hash:
            return [
                "schema registry hash mismatch: "
                f"{entry['artifact_path']} expected={expected_hash} actual={actual_hash}"
            ]

    for path in root.rglob("*"):
        relative_path = path.relative_to(root)
        if ".git" in relative_path.parts:
            continue
        if "raw-conversations" in relative_path.parts or path.name.endswith(
            RAW_TRANSCRIPT_SUFFIXES
        ):
            return [f"raw transcript path is forbidden: {relative_path.as_posix()}"]
        if not path.is_file() or path.suffix.lower() not in {".json", ".yaml", ".yml"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
            if path.suffix.lower() == ".json":
                json.loads(content)
            else:
                yaml.safe_load(content)
        except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as error:
            return [
                "invalid structured data: "
                f"{relative_path.as_posix()}: {error}"
            ]

    for document_path in root.rglob("*.md"):
        if ".git" in document_path.relative_to(root).parts:
            continue
        document_text = document_path.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK_PATTERN.finditer(document_text):
            raw_target = match.group(1).strip("<>")
            if raw_target.startswith(("#", "//")):
                continue
            parsed_target = urlsplit(raw_target)
            if parsed_target.scheme:
                continue
            target_path = document_path.parent / unquote(parsed_target.path)
            if not target_path.exists():
                source = document_path.relative_to(root).as_posix()
                return [
                    f"broken local Markdown link: {source} -> {raw_target}"
                ]

        source = document_path.relative_to(root).as_posix()
        if source == "docs/policies/documentation-style.md":
            continue
        for line_number, line in enumerate(document_text.splitlines(), start=1):
            if any(
                pattern.search(line)
                for pattern in PROHIBITED_DOCUMENTATION_PATTERNS
            ):
                return [
                    f"documentation style violation: {source}:{line_number}"
                ]

    return []


def main() -> int:
    parser = argparse.ArgumentParser(
        description="verify repository governance and admission gates"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path.cwd(),
        help="repository root to verify (default: current directory)",
    )
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error(f"repository root does not exist: {args.root}")

    errors = verify_repository(args.root.resolve())
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1

    print("repository verification: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
