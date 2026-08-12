"""Deterministic documentation lifecycle validation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit


class DocumentLifecycleError(ValueError):
    """Raised when a managed document violates its lifecycle contract."""


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^版本：revision (\d+)\s*$", re.MULTILINE)
_ACTIVE_WORK_RE = re.compile(
    r"^\| active work \| (M\d+-\d+)[^|]*（([⏳🟡✅🧑‍💻])）\s*\|$",
    re.MULTILINE,
)
_MASTER_TASK_RE_TEMPLATE = r"^\| {task_id} \| ([⏳🟡✅🧑‍💻]) \|"
_STATUS_FIELD_RE_TEMPLATE = r"^\| {field} \| ([^|]+) \|$"
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\((<[^>]+>|[^)\s]+)")
_SUPERSEDES_RE = re.compile(r"^context\.document://([^/]+)/revision/([1-9][0-9]*)$")
_DOCUMENT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_DOCUMENT_CATEGORIES = {
    "governance",
    "routing",
    "policy",
    "architecture",
    "research",
    "migration",
    "report",
    "projection",
}
_ALLOWED_CHANGE_TYPES = {
    "governance": {"correction", "decision", "style"},
    "routing": {"correction", "status", "projection", "style"},
    "policy": {"correction", "decision", "schema", "style"},
    "architecture": {"correction", "decision", "schema", "style"},
    "research": {"correction", "evidence", "style"},
    "migration": {"correction", "evidence", "schema", "style"},
    "report": {"correction", "evidence", "projection", "style"},
    "projection": {"correction", "projection", "style"},
}
_MIN_DUPLICATE_BLOCK_BYTES = 256
_STATUS_MAX_BYTES = 12 * 1024
_MASTER_SECTION_MAX_BYTES = 24 * 1024
_MAX_GIT_PROVENANCE_BLOB_BYTES = 8 * 1024 * 1024
_GIT_PROVENANCE_TIMEOUT_SECONDS = 2
_UNMANAGED_MARKDOWN_PATHS = {"replay/fixtures/README.md"}
_UNMANAGED_MARKDOWN_PARTS = {".git", ".ruff_cache", ".venv", "__pycache__"}
_EXPANDABLE_REFERENCE_CATEGORIES = {"routing", "report", "projection"}
_PROJECTION_TEMPLATE_VERSION = "context.document-projection/v1alpha1"


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise DocumentLifecycleError(f"{field} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DocumentLifecycleError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise DocumentLifecycleError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _safe_relative_path(root: Path, value: Any, field: str) -> tuple[str, Path]:
    if not isinstance(value, str) or not value:
        raise DocumentLifecycleError(f"{field} must be a non-empty repository path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != value:
        raise DocumentLifecycleError(f"{field} must be a canonical repository path")
    path = root / pure
    try:
        path.resolve().relative_to(root.resolve())
    except (OSError, ValueError) as exc:
        raise DocumentLifecycleError(f"{field} escapes repository root") from exc
    return value, path


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _expected_category(path: str) -> str:
    if path == "MASTER.md":
        return "governance"
    if path == "STATUS.md":
        return "routing"
    if path == "AGENTS.md":
        return "policy"
    if path == "README.md":
        return "projection"
    prefixes = {
        "docs/policies/": "policy",
        "docs/architecture/": "architecture",
        "docs/research/": "research",
        "docs/migrations/": "migration",
        "docs/reports/": "report",
        "projections/": "projection",
    }
    for prefix, category in prefixes.items():
        if path.startswith(prefix):
            return category
    if "/" not in path and path.endswith(".md"):
        return "policy"
    if path.endswith(".md"):
        return "policy"
    raise DocumentLifecycleError(f"unmanaged document path: {path}")


def _markdown_metrics(content: str, *, reference_depth: int) -> dict[str, int]:
    local_links = 0
    external_links = 0
    for match in _MARKDOWN_LINK_RE.finditer(content):
        target = match.group(1).strip("<>")
        parsed = urlsplit(target)
        if parsed.scheme in {"http", "https"}:
            external_links += 1
        elif not parsed.scheme and not target.startswith("//"):
            local_links += 1
    return {
        "utf8_bytes": len(content.encode("utf-8")),
        "section_count": sum(
            1 for line in content.splitlines() if line.startswith("## ")
        ),
        "local_link_count": local_links,
        "external_link_count": external_links,
        "reference_depth": reference_depth,
    }


def _managed_reference_depths(
    root: Path,
    contents: dict[str, str],
    categories: dict[str, str],
) -> dict[str, int]:
    graph: dict[str, set[str]] = {}
    for source, content in contents.items():
        targets: set[str] = set()
        source_path = root / source
        for match in _MARKDOWN_LINK_RE.finditer(content):
            raw_target = match.group(1).strip("<>")
            parsed = urlsplit(raw_target)
            if parsed.scheme or raw_target.startswith(("#", "//")) or not parsed.path:
                continue
            target_path = (source_path.parent / unquote(parsed.path)).resolve()
            try:
                target = target_path.relative_to(root).as_posix()
            except ValueError:
                continue
            if target in contents and target != source:
                targets.add(target)
        graph[source] = targets

    depths: dict[str, int] = {}

    def depth(path: str, stack: tuple[str, ...]) -> int:
        if categories[path] not in _EXPANDABLE_REFERENCE_CATEGORIES:
            return 0
        if path in stack:
            cycle = " -> ".join((*stack[stack.index(path) :], path))
            raise DocumentLifecycleError(f"managed reference cycle: {cycle}")
        if path in depths:
            return depths[path]
        next_stack = (*stack, path)
        measured = max(
            (
                1 + depth(target, next_stack)
                if categories[target] in _EXPANDABLE_REFERENCE_CATEGORIES
                else 1
                for target in graph[path]
            ),
            default=0,
        )
        if measured > 2:
            raise DocumentLifecycleError(
                f"reference depth exceeds two hops: {path} ({measured})"
            )
        depths[path] = measured
        return measured

    if "STATUS.md" in contents:
        depth("STATUS.md", ())
    return {path: depths.get(path, 0) for path in contents}


def _second_level_sections(content: str) -> list[tuple[str, int]]:
    sections: list[tuple[str, list[str]]] = []
    current_name = "preamble"
    current_lines: list[str] = []
    for line in content.splitlines(keepends=True):
        if line.startswith("## "):
            sections.append((current_name, current_lines))
            current_name = line[3:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)
    sections.append((current_name, current_lines))
    return [(name, len("".join(lines).encode("utf-8"))) for name, lines in sections]


def _prose_blocks(content: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    fenced = False

    def flush() -> None:
        if not current:
            return
        block = "\n".join(current).strip()
        current.clear()
        if not block or block.startswith(">"):
            return
        normalized = " ".join(block.split())
        if len(normalized.encode("utf-8")) >= _MIN_DUPLICATE_BLOCK_BYTES:
            blocks.append(normalized)

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if not stripped:
            flush()
            continue
        if stripped.startswith("<!--") and stripped.endswith("-->"):
            flush()
            continue
        current.append(line)
    flush()
    return blocks


def _validate_authority(
    root: Path,
    entry: dict[str, Any],
    category: str,
    path: str,
    governance_revision: int,
) -> None:
    authority = entry.get("authority")
    if not isinstance(authority, dict):
        raise DocumentLifecycleError(f"authority contract is missing: {path}")
    expected_keys = {
        "governance_authority",
        "active_state_authority",
        "controlled_action_entry",
    }
    if set(authority) != expected_keys or not all(
        isinstance(authority[key], bool) for key in expected_keys
    ):
        raise DocumentLifecycleError(f"authority contract is invalid: {path}")
    if authority["governance_authority"] != (category == "governance"):
        raise DocumentLifecycleError(
            f"only MASTER may claim governance authority: {path}"
        )
    if authority["active_state_authority"]:
        raise DocumentLifecycleError(
            f"documents cannot claim active state authority: {path}"
        )
    if category in {"report", "projection"} and authority["controlled_action_entry"]:
        raise DocumentLifecycleError(
            f"report/projection cannot claim a controlled action entry: {path}"
        )
    if category in {"report", "projection"}:
        binding = entry.get("projection_binding")
        if not isinstance(binding, dict) or set(binding) != {
            "source_state_revision",
            "master_digest",
            "template_version",
            "content_hash",
            "state_write_authority",
        }:
            raise DocumentLifecycleError(f"projection binding is missing: {path}")
        if (
            not isinstance(binding["source_state_revision"], int)
            or isinstance(binding["source_state_revision"], bool)
            or binding["source_state_revision"] < 1
            or not isinstance(binding["master_digest"], str)
            or not _SHA256_RE.fullmatch(binding["master_digest"])
            or not isinstance(binding["template_version"], str)
            or not isinstance(binding["content_hash"], str)
            or not _SHA256_RE.fullmatch(binding["content_hash"])
            or binding["state_write_authority"] is not False
        ):
            raise DocumentLifecycleError(f"projection binding is invalid: {path}")
        if binding["template_version"] != _PROJECTION_TEMPLATE_VERSION:
            raise DocumentLifecycleError(
                f"projection template version is not registered: {path}"
            )
        expected_master_digest = _sha256_bytes((root / "MASTER.md").read_bytes())
        if binding["source_state_revision"] != governance_revision:
            raise DocumentLifecycleError(
                f"projection source revision does not match governance revision: {path}"
            )
        if binding["master_digest"] != expected_master_digest or binding[
            "content_hash"
        ] != entry.get("content_sha256"):
            raise DocumentLifecycleError(f"projection binding hash drift: {path}")
    elif "projection_binding" in entry:
        raise DocumentLifecycleError(
            f"projection binding is not allowed for {category}: {path}"
        )


def _git_output(root: Path, arguments: list[str]) -> bytes:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            timeout=_GIT_PROVENANCE_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DocumentLifecycleError("supersedes provenance Git lookup failed") from exc


def _git_blob(root: Path, commit: str, path: str) -> bytes:
    object_ref = f"{commit}:{path}"
    object_type = _git_output(root, ["cat-file", "-t", object_ref]).strip()
    if object_type != b"blob":
        raise DocumentLifecycleError(
            f"supersedes provenance does not resolve a blob: {object_ref}"
        )
    size_bytes = _git_output(root, ["cat-file", "-s", object_ref]).strip()
    try:
        parsed_size = int(size_bytes)
    except ValueError as exc:
        raise DocumentLifecycleError(
            f"supersedes provenance blob size is invalid: {object_ref}"
        ) from exc
    if parsed_size > _MAX_GIT_PROVENANCE_BLOB_BYTES:
        raise DocumentLifecycleError(
            f"supersedes provenance blob exceeds 8 MiB: {object_ref}"
        )
    content = _git_output(root, ["show", object_ref])
    if len(content) != parsed_size:
        raise DocumentLifecycleError(
            f"supersedes provenance blob size drift: {object_ref}"
        )
    return content


def _git_object_exists(root: Path, object_ref: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "cat-file", "-e", object_ref],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=_GIT_PROVENANCE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocumentLifecycleError("supersedes provenance Git lookup failed") from exc
    return result.returncode == 0


def _validate_git_commit(root: Path, commit: str) -> None:
    if _git_output(root, ["cat-file", "-t", commit]).strip() != b"commit":
        raise DocumentLifecycleError(
            "supersedes provenance git_commit must identify a commit"
        )
    try:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", commit, "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            timeout=_GIT_PROVENANCE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocumentLifecycleError(
            "supersedes provenance Git ancestry check failed"
        ) from exc
    if result.returncode != 0:
        raise DocumentLifecycleError(
            "supersedes provenance git_commit is not reachable from HEAD"
        )


def _manifest_from_git(root: Path, revision: str) -> dict[str, Any] | None:
    path = "profiles/document-control-manifest.yaml"
    if not _git_object_exists(root, f"{revision}:{path}"):
        return None
    content = _git_blob(root, revision, path)
    try:
        import yaml

        manifest = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeError, ValueError, yaml.YAMLError) as exc:
        raise DocumentLifecycleError(
            f"committed document manifest is invalid: {revision}"
        ) from exc
    if not isinstance(manifest, dict):
        raise DocumentLifecycleError(
            f"committed document manifest is invalid: {revision}"
        )
    return manifest


def _validate_committed_lineage(
    root: Path, manifest: dict[str, Any], entries: list[dict[str, Any]]
) -> None:
    head_manifest = _manifest_from_git(root, "HEAD")
    if head_manifest is None:
        return
    prior_manifest = (
        _manifest_from_git(root, "HEAD^")
        if head_manifest == manifest
        else head_manifest
    )
    if prior_manifest is None:
        return
    prior_entries = prior_manifest.get("documents")
    if not isinstance(prior_entries, list):
        raise DocumentLifecycleError("committed document manifest has no documents")
    current_by_id = {entry["document_id"]: entry for entry in entries}
    prior_by_id = {
        entry["document_id"]: entry
        for entry in prior_entries
        if isinstance(entry, dict) and isinstance(entry.get("document_id"), str)
    }
    removed = set(prior_by_id) - set(current_by_id)
    if removed:
        raise DocumentLifecycleError(
            "committed document removed without lifecycle record: "
            + ", ".join(sorted(removed))
        )
    for document_id, current in current_by_id.items():
        prior = prior_by_id.get(document_id)
        if prior is None:
            if current["document_revision"] != 1:
                raise DocumentLifecycleError(
                    f"new document revision must start at 1: {document_id}"
                )
            continue
        prior_revision = prior.get("document_revision")
        current_revision = current["document_revision"]
        if not isinstance(prior_revision, int) or isinstance(prior_revision, bool):
            raise DocumentLifecycleError(
                f"committed document revision is invalid: {document_id}"
            )
        changed_identity = current.get("path") != prior.get("path") or current.get(
            "content_sha256"
        ) != prior.get("content_sha256")
        if changed_identity and current_revision != prior_revision + 1:
            raise DocumentLifecycleError(
                f"committed document revision must advance once: {document_id}"
            )
        if not changed_identity and current_revision not in {
            prior_revision,
            prior_revision + 1,
        }:
            raise DocumentLifecycleError(
                f"committed document revision is not continuous: {document_id}"
            )


def _validate_supersedes_provenance(
    root: Path,
    entry: dict[str, Any],
    document_id: str,
    prior_revision: int,
) -> None:
    change = entry["change"]
    provenance = change.get("supersedes_provenance")
    if not isinstance(provenance, dict) or set(provenance) != {
        "git_commit",
        "manifest_sha256",
        "content_sha256",
    }:
        raise DocumentLifecycleError(f"supersedes provenance is missing: {document_id}")
    commit = provenance["git_commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise DocumentLifecycleError(
            f"supersedes provenance commit is invalid: {document_id}"
        )
    _validate_git_commit(root, commit)
    for field in ("manifest_sha256", "content_sha256"):
        if not isinstance(provenance[field], str) or not _SHA256_RE.fullmatch(
            provenance[field]
        ):
            raise DocumentLifecycleError(
                f"supersedes provenance digest is invalid: {document_id}"
            )
    manifest_bytes = _git_blob(root, commit, "profiles/document-control-manifest.yaml")
    if _sha256_bytes(manifest_bytes) != provenance["manifest_sha256"]:
        raise DocumentLifecycleError(
            f"supersedes provenance manifest hash drift: {document_id}"
        )
    try:
        import yaml

        prior_manifest = yaml.safe_load(manifest_bytes.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise DocumentLifecycleError(
            f"supersedes provenance manifest is invalid: {document_id}"
        ) from exc
    prior_entries = (
        prior_manifest.get("documents") if isinstance(prior_manifest, dict) else None
    )
    prior_entry = next(
        (
            candidate
            for candidate in prior_entries or []
            if isinstance(candidate, dict)
            and candidate.get("document_id") == document_id
        ),
        None,
    )
    if (
        prior_entry is None
        or prior_entry.get("document_revision") != prior_revision
        or prior_entry.get("path") != entry.get("path")
        or prior_entry.get("content_sha256") != provenance["content_sha256"]
    ):
        raise DocumentLifecycleError(
            f"supersedes provenance prior manifest mismatch: {document_id}"
        )
    prior_content = _git_blob(root, commit, entry["path"])
    if _sha256_bytes(prior_content) != provenance["content_sha256"]:
        raise DocumentLifecycleError(
            f"supersedes provenance content hash drift: {document_id}"
        )


def _validate_change(
    root: Path,
    entry: dict[str, Any],
    category: str,
    document_id: str,
    governance_revision: int,
) -> None:
    change = entry.get("change")
    if not isinstance(change, dict):
        raise DocumentLifecycleError(f"change receipt is missing: {document_id}")
    base_keys = {
        "change_type",
        "authority_ref",
        "supersedes",
        "affected_tasks",
        "next_review",
    }
    revision = entry.get("document_revision")
    if not set(change).issubset(
        base_keys | {"supersedes_provenance"}
    ) or not base_keys.issubset(change):
        raise DocumentLifecycleError(
            f"change receipt fields are invalid: {document_id}"
        )
    if revision == 1 and "supersedes_provenance" in change:
        raise DocumentLifecycleError(
            f"supersedes provenance is not allowed for initial revision: {document_id}"
        )
    if change["change_type"] not in _ALLOWED_CHANGE_TYPES[category]:
        raise DocumentLifecycleError(
            f"change_type is not allowed for {category}: {document_id}"
        )
    authority_ref = change["authority_ref"]
    exact_authority_refs = {
        "governance": f"governance-event://master/revision/{governance_revision}",
        "routing": f"governance-event://master/revision/{governance_revision}",
        "policy": f"governance-event://master/revision/{governance_revision}",
        "architecture": f"governance-event://master/revision/{governance_revision}",
        "report": f"state-revision://repository/{governance_revision}",
        "projection": f"state-revision://repository/{governance_revision}",
    }
    content_digest = entry.get("content_sha256")
    exact_evidence_refs = {
        "research": f"evidence-bundle://repository/{content_digest}",
        "migration": f"verification-run://repository/{content_digest}",
    }
    reference_is_valid = (
        authority_ref == exact_authority_refs[category]
        if category in exact_authority_refs
        else authority_ref == exact_evidence_refs[category]
    )
    if not reference_is_valid:
        raise DocumentLifecycleError(f"authority_ref is invalid: {document_id}")
    affected_tasks = change["affected_tasks"]
    if (
        not isinstance(affected_tasks, list)
        or not affected_tasks
        or not all(
            isinstance(task, str) and re.fullmatch(r"M\d+-\d+", task)
            for task in affected_tasks
        )
        or len(affected_tasks) != len(set(affected_tasks))
    ):
        raise DocumentLifecycleError(f"affected_tasks are invalid: {document_id}")
    next_review = change["next_review"]
    if next_review is not None:
        if not isinstance(next_review, str) or not next_review.startswith(
            ("date:", "task:", "event:")
        ):
            raise DocumentLifecycleError(
                f"next_review must use a tagged reference: {document_id}"
            )
        if next_review.startswith("date:"):
            _parse_timestamp(next_review.removeprefix("date:"), "next_review")

    expected_supersedes = (
        None
        if revision == 1
        else f"context.document://{document_id}/revision/{revision - 1}"
    )
    supersedes = change["supersedes"]
    if supersedes == expected_supersedes:
        if revision > 1:
            _validate_supersedes_provenance(root, entry, document_id, revision - 1)
        return
    if revision == 1:
        raise DocumentLifecycleError(
            f"supersedes is not allowed for initial revision: {document_id}"
        )
    match = (
        _SUPERSEDES_RE.fullmatch(supersedes) if isinstance(supersedes, str) else None
    )
    if match is None:
        if revision > 1:
            raise DocumentLifecycleError(
                f"{document_id} revision {revision} must supersede revision {revision - 1}"
            )
        raise DocumentLifecycleError(f"supersedes reference is invalid: {document_id}")
    prior_id, prior_revision_text = match.groups()
    if prior_id != document_id or int(prior_revision_text) != revision - 1:
        raise DocumentLifecycleError(
            f"{document_id} revision {revision} must supersede revision {revision - 1}"
        )


def _validate_evidence_refs(
    root: Path,
    entry: dict[str, Any],
    *,
    as_of: datetime,
) -> int:
    refs = entry.get("evidence_refs")
    if not isinstance(refs, list):
        raise DocumentLifecycleError("evidence_refs must be an array")
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) != {
            "ref_id",
            "path",
            "content_sha256",
            "valid_until",
        }:
            raise DocumentLifecycleError("evidence reference fields are invalid")
        ref_id = ref["ref_id"]
        if not isinstance(ref_id, str) or not ref_id or ref_id in seen:
            raise DocumentLifecycleError("evidence ref_id must be unique")
        seen.add(ref_id)
        _, evidence_path = _safe_relative_path(root, ref["path"], "evidence path")
        try:
            actual_digest = _sha256_bytes(evidence_path.read_bytes())
        except OSError as exc:
            raise DocumentLifecycleError(
                f"evidence reference is missing: {ref['path']}"
            ) from exc
        expected_digest = ref["content_sha256"]
        if not isinstance(expected_digest, str) or not _SHA256_RE.fullmatch(
            expected_digest
        ):
            raise DocumentLifecycleError(f"evidence digest is invalid: {ref_id}")
        if actual_digest != expected_digest:
            raise DocumentLifecycleError(
                f"evidence hash drift: {ref['path']} expected={expected_digest} actual={actual_digest}"
            )
        valid_until = ref["valid_until"]
        if valid_until is not None and as_of > _parse_timestamp(
            valid_until, "evidence valid_until"
        ):
            raise DocumentLifecycleError(f"expired evidence reference: {ref_id}")
    return len(refs)


def _validate_recovery_fields(
    master: str, status: str, governance_revision: int
) -> tuple[int, int]:
    recovered = 0
    revision_match = _REVISION_RE.search(status)
    if revision_match and int(revision_match.group(1)) == governance_revision:
        recovered += 1
    active_match = _ACTIVE_WORK_RE.search(status)
    if active_match is None:
        raise DocumentLifecycleError("STATUS recovery field is missing: active work")
    task_id, status_symbol = active_match.groups()
    recovered += 1
    active_tasks = re.findall(r"^\| M\d+-\d+ \| 🟡 \|", master, re.MULTILINE)
    if len(active_tasks) != 1:
        raise DocumentLifecycleError("MASTER must contain exactly one active leaf")
    master_match = re.search(
        _MASTER_TASK_RE_TEMPLATE.format(task_id=re.escape(task_id)),
        master,
        re.MULTILINE,
    )
    if master_match is None or master_match.group(1) != status_symbol:
        raise DocumentLifecycleError(
            f"STATUS active work does not match MASTER: {task_id}"
        )
    recovered += 1
    for field in ("hard blocker", "next action"):
        if re.search(
            _STATUS_FIELD_RE_TEMPLATE.format(field=re.escape(field)),
            status,
            re.MULTILINE,
        ):
            recovered += 1
        else:
            raise DocumentLifecycleError(f"STATUS recovery field is missing: {field}")
    return 5, recovered


def canonical_manifest_bytes(manifest: dict[str, Any]) -> bytes:
    """Return deterministic JSON bytes for a generated document manifest."""
    return (
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode("utf-8")


def build_document_control_manifest(
    root: Path, config: dict[str, Any]
) -> dict[str, Any]:
    """Build measured manifest fields from a versioned, declarative config."""
    root = root.resolve()
    if not isinstance(config, dict) or set(config) != {
        "schema_version",
        "generated_at",
        "governance_revision",
        "documents",
    }:
        raise DocumentLifecycleError("document control config fields are invalid")
    if config["schema_version"] != "context.document-control-config/v1alpha1":
        raise DocumentLifecycleError(
            "unsupported document control config schema_version"
        )
    entries = config["documents"]
    if not isinstance(entries, list) or not entries:
        raise DocumentLifecycleError("document control config requires documents")
    source_contents: dict[str, str] = {}
    source_bytes: dict[str, bytes] = {}
    source_categories: dict[str, str] = {}
    for configured in entries:
        if not isinstance(configured, dict):
            raise DocumentLifecycleError("configured document must be an object")
        path_text, path = _safe_relative_path(
            root, configured.get("path"), "document path"
        )
        if path_text in source_contents:
            raise DocumentLifecycleError(f"document path is duplicated: {path_text}")
        try:
            content_bytes = path.read_bytes()
            content = content_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise DocumentLifecycleError(
                f"managed document is unreadable: {path_text}"
            ) from exc
        category = configured.get("category")
        if category not in _DOCUMENT_CATEGORIES:
            raise DocumentLifecycleError(f"document category is invalid: {path_text}")
        source_bytes[path_text] = content_bytes
        source_contents[path_text] = content
        source_categories[path_text] = category
    reference_depths = _managed_reference_depths(
        root, source_contents, source_categories
    )

    manifest_entries: list[dict[str, Any]] = []
    for configured in entries:
        entry = {
            key: value for key, value in configured.items() if key != "reference_depth"
        }
        path_text = entry["path"]
        content_bytes = source_bytes[path_text]
        content = source_contents[path_text]
        entry["content_sha256"] = _sha256_bytes(content_bytes)
        entry["metrics"] = _markdown_metrics(
            content, reference_depth=reference_depths[path_text]
        )
        binding = entry.get("projection_binding")
        if binding is not None:
            if not isinstance(binding, dict):
                raise DocumentLifecycleError(
                    f"projection_binding is invalid: {path_text}"
                )
            try:
                master_digest = _sha256_bytes((root / "MASTER.md").read_bytes())
            except OSError as exc:
                raise DocumentLifecycleError(
                    "MASTER is required for projection binding"
                ) from exc
            entry["projection_binding"] = {
                **binding,
                "master_digest": master_digest,
                "content_hash": entry["content_sha256"],
            }
        refs = entry.get("evidence_refs")
        if not isinstance(refs, list):
            raise DocumentLifecycleError(f"evidence_refs are invalid: {path_text}")
        generated_refs: list[dict[str, Any]] = []
        for configured_ref in refs:
            ref = dict(configured_ref)
            _, evidence_path = _safe_relative_path(
                root, ref.get("path"), "evidence path"
            )
            try:
                ref["content_sha256"] = _sha256_bytes(evidence_path.read_bytes())
            except OSError as exc:
                raise DocumentLifecycleError(
                    f"evidence reference is missing: {ref.get('path')}"
                ) from exc
            generated_refs.append(ref)
        entry["evidence_refs"] = generated_refs
        manifest_entries.append(entry)
    return {
        "schema_version": "context.document-control-manifest/v1alpha1",
        "generated_at": config["generated_at"],
        "governance_revision": config["governance_revision"],
        "documents": manifest_entries,
    }


def _managed_markdown_paths(root: Path) -> set[str]:
    paths = set()
    for path in root.rglob("*.md"):
        relative = path.relative_to(root)
        if not path.is_file() or _UNMANAGED_MARKDOWN_PARTS.intersection(relative.parts):
            continue
        paths.add(relative.as_posix())
    return paths - _UNMANAGED_MARKDOWN_PATHS


def validate_document_control_manifest(
    root: Path,
    manifest: dict[str, Any],
    *,
    as_of: datetime | None = None,
) -> dict[str, int]:
    """Validate managed documents and return independently recomputed metrics."""
    root = root.resolve()
    if not isinstance(manifest, dict):
        raise DocumentLifecycleError("document manifest must be an object")
    if set(manifest) != {
        "schema_version",
        "generated_at",
        "governance_revision",
        "documents",
    }:
        raise DocumentLifecycleError("document manifest fields are invalid")
    if manifest.get("schema_version") != "context.document-control-manifest/v1alpha1":
        raise DocumentLifecycleError("unsupported document manifest schema_version")
    _parse_timestamp(manifest.get("generated_at"), "generated_at")
    governance_revision = manifest.get("governance_revision")
    if (
        not isinstance(governance_revision, int)
        or isinstance(governance_revision, bool)
        or governance_revision < 1
    ):
        raise DocumentLifecycleError("governance_revision must be a positive integer")
    effective_time = as_of or datetime.now(timezone.utc)
    if effective_time.tzinfo is None:
        raise DocumentLifecycleError("trusted as_of must include timezone")
    effective_time = effective_time.astimezone(timezone.utc)

    entries = manifest.get("documents")
    if not isinstance(entries, list) or not entries:
        raise DocumentLifecycleError("documents must be a non-empty array")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    contents: dict[str, str] = {}
    categories: dict[str, str] = {}
    metrics_by_path: dict[str, dict[str, int]] = {}
    evidence_ref_count = 0

    for entry in entries:
        if not isinstance(entry, dict):
            raise DocumentLifecycleError("document entry must be an object")
        base_fields = {
            "document_id",
            "path",
            "category",
            "document_revision",
            "content_sha256",
            "metrics",
            "authority",
            "change",
            "evidence_refs",
        }
        if not set(entry).issubset(
            base_fields | {"projection_binding"}
        ) or not base_fields.issubset(entry):
            raise DocumentLifecycleError("document fields are invalid")
        document_id = entry.get("document_id")
        if (
            not isinstance(document_id, str)
            or not _DOCUMENT_ID_RE.fullmatch(document_id)
            or document_id in seen_ids
        ):
            raise DocumentLifecycleError("document_id must be canonical and unique")
        seen_ids.add(document_id)
        path_text, path = _safe_relative_path(root, entry.get("path"), "document path")
        if path_text in seen_paths:
            raise DocumentLifecycleError(f"document path is duplicated: {path_text}")
        seen_paths.add(path_text)
        category = entry.get("category")
        if category not in _DOCUMENT_CATEGORIES:
            raise DocumentLifecycleError(f"document category is invalid: {path_text}")
        expected_category = _expected_category(path_text)
        if category != expected_category:
            raise DocumentLifecycleError(
                f"document category does not match path: {path_text} expected={expected_category}"
            )
        categories[path_text] = category
        revision = entry.get("document_revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
            raise DocumentLifecycleError(f"document revision is invalid: {path_text}")
        try:
            content_bytes = path.read_bytes()
            content = content_bytes.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise DocumentLifecycleError(
                f"managed document is unreadable: {path_text}"
            ) from exc
        actual_digest = _sha256_bytes(content_bytes)
        expected_digest = entry.get("content_sha256")
        if not isinstance(expected_digest, str) or not _SHA256_RE.fullmatch(
            expected_digest
        ):
            raise DocumentLifecycleError(f"document digest is invalid: {path_text}")
        if actual_digest != expected_digest:
            raise DocumentLifecycleError(
                f"document hash drift: {path_text} expected={expected_digest} actual={actual_digest}"
            )
        metrics = entry.get("metrics")
        if not isinstance(metrics, dict) or set(metrics) != {
            "utf8_bytes",
            "section_count",
            "local_link_count",
            "external_link_count",
            "reference_depth",
        }:
            raise DocumentLifecycleError(f"document metrics are invalid: {path_text}")
        reference_depth = metrics.get("reference_depth")
        if (
            not isinstance(reference_depth, int)
            or isinstance(reference_depth, bool)
            or not 0 <= reference_depth <= 2
        ):
            raise DocumentLifecycleError(
                f"reference depth exceeds two hops: {path_text}"
            )
        actual_metrics = _markdown_metrics(content, reference_depth=reference_depth)
        if {
            key: value for key, value in metrics.items() if key != "reference_depth"
        } != {
            key: value
            for key, value in actual_metrics.items()
            if key != "reference_depth"
        }:
            raise DocumentLifecycleError(
                f"document metrics drift: {path_text} expected={metrics} actual={actual_metrics}"
            )
        metrics_by_path[path_text] = metrics
        if (
            path_text == "STATUS.md"
            and actual_metrics["utf8_bytes"] > _STATUS_MAX_BYTES
        ):
            raise DocumentLifecycleError("STATUS exceeds the 12 KiB capacity limit")
        if path_text == "MASTER.md":
            for section, size in _second_level_sections(content):
                if size > _MASTER_SECTION_MAX_BYTES:
                    raise DocumentLifecycleError(
                        f"MASTER section exceeds the 24 KiB capacity limit: {section} ({size} bytes)"
                    )
        _validate_authority(root, entry, category, path_text, governance_revision)
        _validate_change(root, entry, category, document_id, governance_revision)
        contents[path_text] = content

    _validate_committed_lineage(root, manifest, entries)

    required = {"MASTER.md", "STATUS.md", "docs/architecture/target-state.md"}
    missing = required - set(contents)
    if missing:
        raise DocumentLifecycleError(
            "required managed documents are missing: " + ", ".join(sorted(missing))
        )
    unregistered = _managed_markdown_paths(root) - set(contents)
    if unregistered:
        raise DocumentLifecycleError(
            "unregistered normative document: " + ", ".join(sorted(unregistered))
        )
    measured_depths = _managed_reference_depths(root, contents, categories)
    for path_text, measured_depth in measured_depths.items():
        declared_depth = metrics_by_path[path_text]["reference_depth"]
        if declared_depth != measured_depth:
            raise DocumentLifecycleError(
                "document metrics drift: "
                f"{path_text} reference_depth expected={declared_depth} "
                f"actual={measured_depth}"
            )
    for path_text in ("MASTER.md", "STATUS.md"):
        revision_match = _REVISION_RE.search(contents[path_text])
        if (
            revision_match is None
            or int(revision_match.group(1)) != governance_revision
        ):
            raise DocumentLifecycleError(
                f"governance revision drift: {path_text} expected={governance_revision}"
            )

    occurrences: dict[str, list[str]] = defaultdict(list)
    for path_text, content in contents.items():
        for block in set(_prose_blocks(content)):
            occurrences[_sha256_bytes(block.encode("utf-8"))].append(path_text)
    duplicates = [paths for paths in occurrences.values() if len(set(paths)) > 1]
    if duplicates:
        paths = ", ".join(sorted(set(duplicates[0])))
        raise DocumentLifecycleError(f"duplicate full prose across documents: {paths}")

    for entry in entries:
        evidence_ref_count += _validate_evidence_refs(root, entry, as_of=effective_time)

    recovery_total, recovery_recovered = _validate_recovery_fields(
        contents["MASTER.md"], contents["STATUS.md"], governance_revision
    )
    return {
        "document_count": len(entries),
        "evidence_ref_count": evidence_ref_count,
        "drifted_documents": 0,
        "drifted_evidence_refs": 0,
        "duplicate_prose_blocks": 0,
        "expired_evidence_refs": 0,
        "recovery_fields_total": recovery_total,
        "recovery_fields_recovered": recovery_recovered,
    }
