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
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from context_control_plane.document_lifecycle import (  # noqa: E402
    DocumentLifecycleError,
    build_document_control_manifest,
    validate_document_control_manifest,
)
from context_control_plane.document_lifecycle_benchmark import (  # noqa: E402
    DocumentLifecycleBenchmarkError,
    load_document_lifecycle_benchmark_baseline,
    load_document_lifecycle_benchmark_config,
    validate_document_lifecycle_benchmark_receipt,
)

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
IGNORED_REPOSITORY_PARTS = {
    ".git",
    ".venv",
    ".ruff_cache",
    "__pycache__",
    "build",
    "dist",
}


def _is_ignored_path(relative_path: Path) -> bool:
    return bool(IGNORED_REPOSITORY_PARTS.intersection(relative_path.parts)) or any(
        part.endswith(".egg-info") for part in relative_path.parts
    )


def _document_revision(path: Path) -> int:
    match = REVISION_PATTERN.search(path.read_text(encoding="utf-8"))
    if match is None:
        raise ValueError(f"missing document revision: {path}")
    return int(match.group(1))


def validate_registered_instance(instance: object, schema: object, label: str) -> None:
    """Reject instances that do not satisfy their registered strict schema."""
    if not isinstance(schema, dict):
        raise TypeError(f"strict schema is invalid: {label}")
    errors = sorted(
        Draft202012Validator(schema).iter_errors(instance),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = "/".join(str(part) for part in first.absolute_path) or "$"
        raise ValueError(
            f"strict schema validation failed: {label} at {location}: {first.message}"
        )


def _registered_schema(root: Path, schema_entries: list[dict], schema_id: str) -> dict:
    entry = next(
        (
            candidate
            for candidate in schema_entries
            if isinstance(candidate, dict) and candidate.get("schema_id") == schema_id
        ),
        None,
    )
    if entry is None:
        raise ValueError(f"registered schema is missing: {schema_id}")
    schema = json.loads((root / entry["artifact_path"]).read_text(encoding="utf-8"))
    if not isinstance(schema, dict):
        raise TypeError(f"registered schema is invalid: {schema_id}")
    return schema


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
            (
                "MASTER/STATUS revision mismatch: "
                f"MASTER={master_revision}, STATUS={status_revision}"
            )
        ]
    target_state_match = TARGET_STATE_REVISION_PATTERN.search(target_state_text)
    if target_state_match is None:
        return ["target-state governance revision is missing"]
    target_state_revision = int(target_state_match.group(1))
    if master_revision != target_state_revision:
        return [
            (
                "MASTER/target-state revision mismatch: "
                f"MASTER={master_revision}, target-state={target_state_revision}"
            )
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
    active_tasks = re.findall(r"^\| M\d+-\d+ \| 🟡 \|", master_text, re.MULTILINE)
    if len(active_tasks) != 1:
        return ["MASTER must contain exactly one 🟡 task"]

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
                (
                    "schema registry hash mismatch: "
                    f"{entry['artifact_path']} expected={expected_hash} actual={actual_hash}"
                )
            ]

    document_config_path = root / "profiles" / "document-control-config.yaml"
    document_manifest_path = root / "profiles" / "document-control-manifest.yaml"
    lifecycle_registered = any(
        isinstance(entry, dict)
        and entry.get("schema_id") == "context.document-control-manifest"
        for entry in schema_entries
    )
    if lifecycle_registered and (
        not document_config_path.exists() or not document_manifest_path.exists()
    ):
        return ["document lifecycle config and manifest are required"]
    if document_config_path.exists() != document_manifest_path.exists():
        return ["document lifecycle config and manifest must exist together"]
    if document_config_path.exists():
        try:
            document_config = yaml.safe_load(
                document_config_path.read_text(encoding="utf-8")
            )
            document_manifest = yaml.safe_load(
                document_manifest_path.read_text(encoding="utf-8")
            )
            regenerated_manifest = build_document_control_manifest(
                root, document_config
            )
            if regenerated_manifest != document_manifest:
                return [
                    "document lifecycle verification failed: generated manifest drift"
                ]
            manifest_schema = _registered_schema(
                root, schema_entries, "context.document-control-manifest"
            )
            validate_registered_instance(
                document_manifest, manifest_schema, "document control manifest"
            )
            validate_document_control_manifest(root, document_manifest)

            benchmark_schema = _registered_schema(
                root, schema_entries, "context.document-lifecycle-benchmark"
            )
            benchmark_config_schema = _registered_schema(
                root,
                schema_entries,
                "context.document-lifecycle-benchmark-config",
            )
            benchmark_config = load_document_lifecycle_benchmark_config(root)
            validate_registered_instance(
                benchmark_config,
                benchmark_config_schema,
                "document lifecycle benchmark config",
            )
            benchmark_path = (
                root / "experiments" / "state" / "m0-10-document-lifecycle-results.yaml"
            )
            benchmark_receipt = yaml.safe_load(
                benchmark_path.read_text(encoding="utf-8")
            )
            validate_registered_instance(
                benchmark_receipt, benchmark_schema, "document lifecycle benchmark"
            )
            baseline_ref = benchmark_config["baseline_git_ref"]
            baseline_status, baseline_master = (
                load_document_lifecycle_benchmark_baseline(root, baseline_ref)
            )
            validate_document_lifecycle_benchmark_receipt(
                root,
                document_manifest,
                benchmark_receipt,
                baseline_ref=baseline_ref,
                baseline_status=baseline_status,
                baseline_master=baseline_master,
            )
        except (
            OSError,
            TypeError,
            KeyError,
            ValueError,
            yaml.YAMLError,
            DocumentLifecycleError,
            DocumentLifecycleBenchmarkError,
        ) as error:
            return [f"document lifecycle verification failed: {error}"]

    for path in root.rglob("*"):
        relative_path = path.relative_to(root)
        if _is_ignored_path(relative_path):
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
            return [(f"invalid structured data: {relative_path.as_posix()}: {error}")]

    for document_path in root.rglob("*.md"):
        if _is_ignored_path(document_path.relative_to(root)):
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
                return [f"broken local Markdown link: {source} -> {raw_target}"]

        source = document_path.relative_to(root).as_posix()
        if source == "docs/policies/documentation-style.md":
            continue
        for line_number, line in enumerate(document_text.splitlines(), start=1):
            if any(
                pattern.search(line) for pattern in PROHIBITED_DOCUMENTATION_PATTERNS
            ):
                return [f"documentation style violation: {source}:{line_number}"]

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
