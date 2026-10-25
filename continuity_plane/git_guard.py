"""Continuity-owned Git wrapper for configured canonical repository roots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import uuid
from pathlib import Path

from .collaboration_registry import project_data_dir, default_data_root
from .project_context import worker_write_guard


_WRAPPER = r'''#!{python}
import json
import os
import subprocess
import sys

REAL_GIT = {real_git!r}
PROTECTED = {protected!r}


def inside(path):
    try:
        path = os.path.realpath(path)
        return any(path == root or path.startswith(root + os.sep) for root in PROTECTED)
    except (OSError, ValueError):
        return False


args = sys.argv[1:]
repo = os.getcwd()
position = 0
while position < len(args):
    if args[position] == "-C" and position + 1 < len(args):
        repo = os.path.abspath(os.path.join(repo, args[position + 1]))
        position += 2
        continue
    if args[position] in {{"-c", "--git-dir", "--work-tree"}} and position + 1 < len(args):
        position += 2
        continue
    if args[position].startswith("-C") and len(args[position]) > 2:
        repo = os.path.abspath(os.path.join(repo, args[position][2:]))
    if not args[position].startswith("-"):
        break
    position += 1
command = args[position] if position < len(args) else ""
mutating = command in {{"push", "update-ref", "receive-pack"}}
target_is_protected = inside(repo)
if mutating:
    common = subprocess.run(
        [REAL_GIT, *args[:position], "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=False,
    )
    if common.returncode == 0 and inside(common.stdout.strip()):
        target_is_protected = True
if command == "push":
    # Resolve configured remote URLs too: a local origin can point directly
    # at the protected canonical repository.
    remotes = subprocess.run(
        [REAL_GIT, *args[:position], "config", "--get-regexp", r"^remote\..*\.(url|pushurl)$"],
        capture_output=True, text=True, check=False,
    )
    remote_paths = [line.split(None, 1)[1] for line in remotes.stdout.splitlines() if len(line.split(None, 1)) == 2]
    for value in remote_paths:
        candidate = value[7:] if value.startswith("file://") else value
        if "://" not in candidate and ":" not in candidate and inside(os.path.join(repo, candidate)):
            target_is_protected = True
    for value in args[position + 1 :]:
        candidate = value[7:] if value.startswith("file://") else value
        if not candidate.startswith("-") and "://" not in candidate:
            candidate = os.path.abspath(os.path.join(repo, candidate))
        if os.path.isabs(candidate) and inside(candidate):
            target_is_protected = True
if mutating and target_is_protected:
    print(
        json.dumps(
            {{
                "status": "blocked",
                "reason": "canonical-repository-worker-write-guard",
                "command": command,
                "repository": repo,
                "message": "Worker Git ref writes into canonical repositories are disabled.",
                "authority": "Continuity PATH wrapper; absolute git paths are not intercepted",
            }}
        ),
        file=sys.stderr,
    )
    raise SystemExit(78)
os.execv(REAL_GIT, [REAL_GIT, *args])
'''


def prepare(data_root, project_id):
    """Materialize an isolated wrapper directory, or return None when disabled."""

    config = worker_write_guard(data_root, project_id)
    if not config.get("enabled"):
        return None
    roots = config.get("canonical_roots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("canonical repository write guard has no protected roots")
    protected = []
    for value in roots:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("canonical repository guard path must be absolute")
        protected.append(str(path.resolve()))
    real_git = shutil.which("git")
    if not real_git:
        raise ValueError("canonical repository write guard cannot find the real git executable")
    identity = hashlib.sha256(
        json.dumps({"git": real_git, "roots": protected}, sort_keys=True).encode()
    ).hexdigest()[:16]
    directory = (
        project_data_dir(Path(data_root or default_data_root()), project_id)
        / "worker-git-guard"
        / identity
    )
    directory.mkdir(parents=True, exist_ok=True)
    wrapper = directory / "git"
    content = _WRAPPER.format(
        python=sys.executable,
        real_git=real_git,
        protected=tuple(protected),
    )
    if not wrapper.exists() or wrapper.read_text(encoding="utf-8") != content:
        temporary = wrapper.with_name(
            ".git.tmp-" + str(os.getpid()) + "-" + uuid.uuid4().hex
        )
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, wrapper)
    mode = wrapper.stat().st_mode
    wrapper.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory
