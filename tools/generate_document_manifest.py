#!/usr/bin/env python3
"""Generate deterministic M0-10 document config and measured manifest."""

from __future__ import annotations

import argparse
import copy
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from context_control_plane.document_lifecycle import (  # noqa: E402
    _expected_category,
    _managed_markdown_paths,
    build_document_control_manifest,
    validate_document_control_manifest,
)

_REVISION_RE = re.compile(r"^版本：(?:revision )?(\d+)\s*$", re.MULTILINE)
_TASK_RE = re.compile(r"(?:^|[-/])(m\d+-\d+)(?:[-/.]|$)", re.IGNORECASE)


def _document_id(path: str) -> str:
    if path == "MASTER.md":
        return "master"
    if path == "STATUS.md":
        return "status"
    stem = path.removesuffix(".md").lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", stem).strip("-")
    return normalized


def _affected_tasks(path: str) -> list[str]:
    matches = {match.upper() for match in _TASK_RE.findall(path)}
    return sorted(matches) or ["M0-10"]


def _authority_ref(category: str, governance_revision: int, content_sha256: str) -> str:
    if category in {"governance", "routing", "policy", "architecture"}:
        return f"governance-event://master/revision/{governance_revision}"
    if category in {"research", "migration"}:
        return (
            f"evidence-bundle://repository/{content_sha256}"
            if category == "research"
            else f"verification-run://repository/{content_sha256}"
        )
    return f"state-revision://repository/{governance_revision}"


def _change_type(category: str) -> str:
    return {
        "governance": "decision",
        "routing": "status",
        "policy": "decision",
        "architecture": "decision",
        "research": "evidence",
        "migration": "evidence",
        "report": "projection",
        "projection": "projection",
    }[category]


def _local_link_paths(root: Path, path: str, content: str) -> list[str]:
    targets: set[str] = set()
    source = root / path
    for raw_target in re.findall(r"\[[^\]]*\]\((<[^>]+>|[^)\s]+)", content):
        target = raw_target.strip("<>").split("#", 1)[0]
        if not target or "://" in target or target.startswith("//"):
            continue
        resolved = (source.parent / target).resolve()
        try:
            relative = resolved.relative_to(root.resolve()).as_posix()
        except ValueError:
            continue
        if resolved.is_file():
            targets.add(relative)
    return sorted(targets)


def build_initial_config(
    root: Path, *, generated_at: str, governance_revision: int
) -> dict[str, Any]:
    documents: list[dict[str, Any]] = []
    acceptance_path = (
        "docs/migrations/m0-10-document-lifecycle-acceptance-2026-08-12.md"
    )
    for path in sorted(_managed_markdown_paths(root)):
        content = (root / path).read_text(encoding="utf-8")
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        category = _expected_category(path)
        document_id = _document_id(path)
        evidence_paths: list[str] = []
        if path in {"MASTER.md", "STATUS.md", "docs/architecture/target-state.md"}:
            evidence_paths.append(acceptance_path)
        if category == "report":
            evidence_paths.extend(_local_link_paths(root, path, content))
        entry: dict[str, Any] = {
            "document_id": document_id,
            "path": path,
            "category": category,
            "document_revision": 1,
            "authority": {
                "governance_authority": category == "governance",
                "active_state_authority": False,
                "controlled_action_entry": False,
            },
            "change": {
                "change_type": _change_type(category),
                "authority_ref": _authority_ref(
                    category, governance_revision, content_sha256
                ),
                "supersedes": None,
                "affected_tasks": _affected_tasks(path),
                "next_review": None,
            },
            "evidence_refs": [
                {
                    "ref_id": f"{document_id}-evidence-{index}",
                    "path": evidence_path,
                    "valid_until": None,
                }
                for index, evidence_path in enumerate(
                    sorted(set(evidence_paths)), start=1
                )
                if evidence_path != path
            ],
        }
        if category in {"report", "projection"}:
            entry["projection_binding"] = {
                "source_state_revision": governance_revision,
                "template_version": "context.document-projection/v1alpha1",
                "state_write_authority": False,
            }
        documents.append(entry)
    return {
        "schema_version": "context.document-control-config/v1alpha1",
        "generated_at": generated_at,
        "governance_revision": governance_revision,
        "documents": documents,
    }


def _git_output(root: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def _matching_prior_provenance(
    root: Path,
    *,
    head_commit: str,
    document_id: str,
    path: str,
    document_revision: int,
    content_sha256: str,
) -> dict[str, str]:
    """Find a commit where the declared manifest revision matches its document."""
    commits = _git_output(
        root,
        "rev-list",
        head_commit,
        "--",
        "profiles/document-control-manifest.yaml",
        path,
    ).decode().splitlines()
    for commit in commits:
        try:
            manifest_bytes = _git_output(
                root,
                "show",
                f"{commit}:profiles/document-control-manifest.yaml",
            )
            prior_manifest = yaml.safe_load(manifest_bytes.decode("utf-8"))
            document_bytes = _git_output(root, "show", f"{commit}:{path}")
        except (subprocess.CalledProcessError, UnicodeError, ValueError):
            continue
        entries = (
            prior_manifest.get("documents")
            if isinstance(prior_manifest, dict)
            else None
        )
        entry = next(
            (
                item
                for item in entries or []
                if isinstance(item, dict)
                and item.get("document_id") == document_id
                and item.get("path") == path
                and item.get("document_revision") == document_revision
                and item.get("content_sha256") == content_sha256
            ),
            None,
        )
        if entry is None or hashlib.sha256(document_bytes).hexdigest() != content_sha256:
            continue
        return {
            "git_commit": commit,
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "content_sha256": content_sha256,
        }
    raise ValueError(
        "no committed document/manifest pair matches prior revision: "
        f"{document_id}@{document_revision}"
    )


def sync_document_control_config(
    root: Path,
    config: dict[str, Any],
    *,
    generated_at: str,
    governance_revision: int,
) -> dict[str, Any]:
    """Discover documents and advance changed entries from the committed manifest."""
    seed = build_initial_config(
        root,
        generated_at=generated_at,
        governance_revision=governance_revision,
    )
    if not isinstance(config, dict) or not isinstance(config.get("documents"), list):
        raise ValueError("document control config is invalid")
    configured_by_path = {
        entry["path"]: entry
        for entry in config["documents"]
        if isinstance(entry, dict) and isinstance(entry.get("path"), str)
    }
    seed_by_path = {entry["path"]: entry for entry in seed["documents"]}
    removed = set(configured_by_path) - set(seed_by_path)
    generated_output_paths = {
        path
        for path in removed
        if any(
            part in {"build", "dist"} or part.endswith(".egg-info")
            for part in Path(path).parts
        )
    }
    for path in generated_output_paths:
        configured_by_path.pop(path, None)
    removed -= generated_output_paths
    if removed:
        raise ValueError(
            "managed documents require an explicit removal record: "
            + ", ".join(sorted(removed))
        )

    commit = _git_output(root, "rev-parse", "HEAD").decode().strip()
    manifest_bytes = _git_output(
        root, "show", "HEAD:profiles/document-control-manifest.yaml"
    )
    prior_manifest = yaml.safe_load(manifest_bytes.decode("utf-8"))
    prior_by_id = {
        entry["document_id"]: entry
        for entry in prior_manifest["documents"]
        if isinstance(entry, dict) and isinstance(entry.get("document_id"), str)
    }
    release_paths = {
        "README.md",
        "USAGE.md",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
    }

    documents: list[dict[str, Any]] = []
    for path, initial in sorted(seed_by_path.items()):
        entry = copy.deepcopy(configured_by_path.get(path, initial))
        entry["document_id"] = initial["document_id"]
        entry["path"] = path
        entry["category"] = initial["category"]
        entry["authority"] = initial["authority"]
        entry.setdefault("evidence_refs", initial["evidence_refs"])
        content_sha256 = hashlib.sha256((root / path).read_bytes()).hexdigest()
        change = entry.setdefault("change", initial["change"])
        change["change_type"] = _change_type(entry["category"])
        change["authority_ref"] = _authority_ref(
            entry["category"], governance_revision, content_sha256
        )
        affected_tasks = set(change.get("affected_tasks", []))
        if path in release_paths or path.startswith("public/"):
            affected_tasks.add("M10-08")
        change["affected_tasks"] = sorted(affected_tasks or {"M0-10"})

        prior = prior_by_id.get(entry["document_id"])
        changed = prior is not None and (
            prior.get("path") != path or prior.get("content_sha256") != content_sha256
        )
        if prior is None:
            entry["document_revision"] = 1
            change["supersedes"] = None
            change.pop("supersedes_provenance", None)
        elif changed:
            prior_revision = prior["document_revision"]
            entry["document_revision"] = prior_revision + 1
            change["supersedes"] = (
                f"context.document://{entry['document_id']}/revision/{prior_revision}"
            )
            change["supersedes_provenance"] = _matching_prior_provenance(
                root,
                head_commit=commit,
                document_id=entry["document_id"],
                path=path,
                document_revision=prior_revision,
                content_sha256=prior["content_sha256"],
            )

        if entry["category"] in {"report", "projection"}:
            binding = entry.setdefault(
                "projection_binding", initial["projection_binding"]
            )
            binding["source_state_revision"] = governance_revision
        else:
            entry.pop("projection_binding", None)
        documents.append(entry)

    return {
        "schema_version": "context.document-control-config/v1alpha1",
        "generated_at": generated_at,
        "governance_revision": governance_revision,
        "documents": documents,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="generate document lifecycle artifacts"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--config", type=Path, default=Path("profiles/document-control-config.yaml")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("profiles/document-control-manifest.yaml")
    )
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--sync", action="store_true")
    parser.add_argument("--generated-at", default="2026-08-12T00:00:00Z")
    parser.add_argument("--governance-revision", type=int, default=39)
    args = parser.parse_args()
    root = args.root.resolve()
    config_path = root / args.config
    output_path = root / args.output
    write_config = False
    if args.initialize and args.sync:
        parser.error("--initialize and --sync are mutually exclusive")
    if args.initialize:
        config = build_initial_config(
            root,
            generated_at=args.generated_at,
            governance_revision=args.governance_revision,
        )
        write_config = True
    elif args.sync:
        current = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config = sync_document_control_config(
            root,
            current,
            generated_at=args.generated_at,
            governance_revision=args.governance_revision,
        )
        write_config = True
    else:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    manifest = build_document_control_manifest(root, config)
    validate_document_control_manifest(root, manifest)
    if write_config:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    print(f"document manifest: {len(manifest['documents'])} documents")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
