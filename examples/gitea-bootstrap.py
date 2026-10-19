#!/usr/bin/env python3
"""Check member-local Gitea access; installing a build needs explicit arguments."""

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--project-id", required=True)
    parser.add_argument(
        "--install-from",
        help="User-approved local build, pinned package, or HTTPS artifact",
    )
    parser.add_argument(
        "--install-prefix",
        type=Path,
        help="New persistent venv; existing directories are refused",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    declaration = root / ".continuity/collaboration.toml"
    if not declaration.is_file() or declaration.stat().st_size > 16384:
        parser.error("missing or oversized collaboration declaration")
    if (
        tomllib.loads(declaration.read_text()).get("collaboration", {}).get("backend")
        != "gitea-issues"
    ):
        parser.error("project does not declare gitea-issues")
    command = shutil.which("continuity")
    if args.install_from:
        if not args.install_prefix or args.install_prefix.exists():
            parser.error("installation requires an explicit new --install-prefix")
        prefix = args.install_prefix.resolve()
        if "tmp" in prefix.parts or str(prefix).startswith(("/var/tmp/", "/dev/shm/")):
            parser.error("use a persistent, non-temporary directory")
        spec = args.install_from
        url = urlsplit(spec)
        if url.scheme:
            if (
                url.scheme != "https"
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                parser.error(
                    "artifact URL must be HTTPS without credentials, query or fragment"
                )
        elif not Path(spec).exists() and not re.fullmatch(
            r"continuity-plane==[A-Za-z0-9.+-]+", spec
        ):
            parser.error(
                "use a local build, HTTPS artifact, or pinned continuity-plane version"
            )
        venv.EnvBuilder(with_pip=True).create(prefix)
        directory = prefix / ("Scripts" if sys.platform == "win32" else "bin")
        python = directory / ("python.exe" if sys.platform == "win32" else "python")
        subprocess.run([str(python), "-m", "pip", "install", "--", spec], check=True)
        command = str(
            directory / ("continuity.exe" if sys.platform == "win32" else "continuity")
        )
    if not command:
        parser.error(
            "Continuity missing; explicitly authorize an install or install it yourself"
        )
    subprocess.run([command, "--version"], check=True)
    probe = subprocess.run([command, "help", "backend.doctor"], capture_output=True)
    if probe.returncode:
        parser.error(
            "installed build lacks native Gitea support; upgrade in an authorized environment"
        )
    registered = subprocess.run(
        [command, "collaboration", "list"], capture_output=True, text=True, check=True
    )
    existing = next(
        (
            p
            for p in json.loads(registered.stdout)
            if p["project_id"] == args.project_id
        ),
        None,
    )
    if existing and Path(existing["control_root"]).resolve() != root:
        parser.error(
            "project ID is already registered at another checkout; choose a distinct local project ID"
        )
    if not existing:
        subprocess.run(
            [
                command,
                "collaboration",
                "register",
                "--project-id",
                args.project_id,
                "--control-root",
                str(root),
            ],
            check=True,
        )
    result = subprocess.run(
        [
            command,
            "collaboration",
            "backend",
            "doctor",
            "--project-id",
            args.project_id,
            "--format",
            "json",
        ]
    )
    if result.returncode:
        print(
            "Configure this member's own local tea CLI login; do not paste tokens into AI.",
            file=sys.stderr,
        )
        return result.returncode
    return subprocess.run(
        [
            command,
            "collaboration",
            "backend",
            "sync",
            "--project-id",
            args.project_id,
            "--consumer",
            "agent-start",
        ]
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
