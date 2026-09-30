#!/usr/bin/env python3
"""Project real first-parent development into a sanitized public Git history."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.build_public_release import (  # noqa: E402
    _BANNED,
    _LEGACY_PUBLIC_MARKERS,
    _PUBLIC_SCHEMA_FILES,
    _normalize_public_identity,
    _release_modules,
    build_public_release,
    scan_public_release,
)

_PUBLIC_NAME = "SkyHua"
_PUBLIC_EMAIL = "skyhua0224@users.noreply.github.com"
_FINAL_DATE = "2026-08-18T05:55:00+08:00"
_MERGE_SUBJECT_RE = re.compile(r"^Merge pull request '([^']+)' \(#\d+\).*$")


def _run(
    arguments: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    return subprocess.run(
        arguments,
        cwd=cwd,
        env=env,
        check=True,
        capture_output=True,
        text=text,
    )


def _git_text(source: Path, *arguments: str) -> str:
    return _run(["git", *arguments], cwd=source).stdout.strip()


def _git_file(source: Path, commit: str, path: str) -> bytes | None:
    reference = f"{commit}:{path}"
    exists = subprocess.run(
        ["git", "cat-file", "-e", reference],
        cwd=source,
        check=False,
        capture_output=True,
    )
    if exists.returncode != 0:
        return None
    return _run(["git", "show", reference], cwd=source, text=False).stdout


def _dependencies(source: str) -> set[str]:
    tree = ast.parse(source)
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1:
                if node.module:
                    dependencies.add(node.module.split(".", 1)[0])
                else:
                    dependencies.update(
                        alias.name.split(".", 1)[0] for alias in node.names
                    )
            elif node.level == 0 and node.module == "context_control_plane":
                dependencies.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif (
                node.level == 0
                and node.module
                and node.module.startswith("context_control_plane.")
            ):
                dependencies.add(node.module.split(".", 1)[1].split(".", 1)[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("context_control_plane."):
                    dependencies.add(alias.name.split(".", 2)[1])
    return dependencies


def _admitted_text(content: str, relative: str) -> bool:
    return not any(
        pattern.search(content) or pattern.search(relative)
        for pattern in (*_BANNED, *_LEGACY_PUBLIC_MARKERS)
    )


def _historical_tree(
    source: Path,
    commit: str,
    *,
    module_names: set[str],
) -> dict[str, bytes]:
    raw_modules: dict[str, str] = {}
    dependencies: dict[str, set[str]] = {}
    for name in sorted(module_names):
        payload = _git_file(source, commit, f"context_control_plane/{name}.py")
        if payload is None:
            continue
        try:
            original = payload.decode("utf-8")
            normalized = _normalize_public_identity(original)
            dependencies[name] = _dependencies(original)
        except (UnicodeDecodeError, SyntaxError):
            continue
        relative = f"continuity_plane/{name}.py"
        if _admitted_text(normalized, relative):
            raw_modules[name] = normalized

    changed = True
    while changed:
        changed = False
        for name in list(raw_modules):
            unavailable = {
                dependency
                for dependency in dependencies[name]
                if dependency in module_names and dependency not in raw_modules
            }
            if unavailable:
                del raw_modules[name]
                changed = True

    tree = {
        f"continuity_plane/{name}.py": content.encode("utf-8")
        for name, content in raw_modules.items()
    }
    for relative in _PUBLIC_SCHEMA_FILES:
        payload = _git_file(source, commit, f"schemas/{relative}")
        if payload is None:
            continue
        try:
            normalized = _normalize_public_identity(payload.decode("utf-8"))
        except UnicodeDecodeError:
            continue
        public_path = f"schemas/{relative}"
        if not _admitted_text(normalized, public_path):
            continue
        tree[public_path] = normalized.encode("utf-8")
        if relative == "m4-01/spdx-license-ids-3.28.0.json":
            tree[f"continuity_plane/schemas/{relative}"] = normalized.encode("utf-8")

    migration = _git_file(
        source, commit, "database/migrations/001_m2_03_postgres_state.up.sql"
    )
    if migration is not None:
        text = _normalize_public_identity(migration.decode("utf-8"))
        relative = "continuity_plane/database/migrations/001_postgres_state.up.sql"
        if _admitted_text(text, relative):
            tree[relative] = text.encode("utf-8")
    return tree


def _write_tree(root: Path, desired: dict[str, bytes], managed: set[str]) -> None:
    for relative in sorted(managed - set(desired)):
        path = root / relative
        if path.is_file() or path.is_symlink():
            path.unlink()
    for relative, content in desired.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_bytes() != content:
            path.write_bytes(content)


def _subject(source: Path, commit: str) -> str:
    subject = _git_text(source, "show", "-s", "--format=%s", commit)
    match = _MERGE_SUBJECT_RE.fullmatch(subject)
    if match:
        subject = match.group(1)
    return _normalize_public_identity(subject).replace("Gitea", "forge")


def _commit(root: Path, *, subject: str, occurred_at: str) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": _PUBLIC_NAME,
            "GIT_AUTHOR_EMAIL": _PUBLIC_EMAIL,
            "GIT_AUTHOR_DATE": occurred_at,
            "GIT_COMMITTER_NAME": _PUBLIC_NAME,
            "GIT_COMMITTER_EMAIL": _PUBLIC_EMAIL,
            "GIT_COMMITTER_DATE": occurred_at,
        }
    )
    _run(["git", "add", "--all"], cwd=root, env=environment)
    if not _git_text(root, "status", "--porcelain"):
        return
    _run(["git", "commit", "-s", "-m", subject], cwd=root, env=environment)


def _head_or_none(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    }


def build_public_history(source: Path, output: Path) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    if output == source or source in output.parents:
        raise ValueError("public history output must be outside the source repository")
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    _run(["git", "init", "-b", "main"], cwd=output)
    _run(["git", "config", "user.name", _PUBLIC_NAME], cwd=output)
    _run(["git", "config", "user.email", _PUBLIC_EMAIL], cwd=output)

    module_names = {path.stem for path in _release_modules(source)}
    managed = {
        *(f"continuity_plane/{name}.py" for name in module_names),
        *(f"schemas/{relative}" for relative in _PUBLIC_SCHEMA_FILES),
        "continuity_plane/database/migrations/001_postgres_state.up.sql",
        "continuity_plane/schemas/m4-01/spdx-license-ids-3.28.0.json",
    }
    commits = _git_text(source, "rev-list", "--first-parent", "--reverse", "HEAD").splitlines()
    projected_sources: list[str] = []
    for commit in commits:
        desired = _historical_tree(source, commit, module_names=module_names)
        _write_tree(output, desired, managed)
        before = _head_or_none(output)
        _commit(
            output,
            subject=_subject(source, commit),
            occurred_at=_git_text(source, "show", "-s", "--format=%aI", commit),
        )
        after = _head_or_none(output)
        if after and after != before:
            projected_sources.append(commit)

    with tempfile.TemporaryDirectory() as directory:
        final = Path(directory) / "release"
        build_public_release(source, final, initialize_git=False)
        for path in list(output.iterdir()):
            if path.name == ".git":
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        for path in sorted(final.rglob("*")):
            if path.is_file():
                destination = output / path.relative_to(final)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
        expected_hashes = _tree_hashes(final)

    _commit(
        output,
        subject="feat: publish Continuity Plane alpha",
        occurred_at=_FINAL_DATE,
    )
    violations = scan_public_release(output)
    if violations:
        raise RuntimeError("public history scan failed:\n" + "\n".join(violations))
    actual_hashes = _tree_hashes(output)
    receipt = {
        "schema_version": "continuity.public-history/v1",
        "source_first_parent_count": len(commits),
        "projected_source_commits": projected_sources,
        "projected_commit_count": int(_git_text(output, "rev-list", "--count", "HEAD")),
        "final_tree_matches_release": actual_hashes == expected_hashes,
        "head": _git_text(output, "rev-parse", "HEAD"),
    }
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description="build sanitized public Git history")
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = build_public_history(args.source, args.output)
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
