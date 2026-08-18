#!/usr/bin/env python3
"""Run the provider-neutral local installation probe for M10-09."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from context_control_plane.cli import VERSION  # noqa: E402
from context_control_plane.native_install_matrix import build_native_install_matrix_receipt  # noqa: E402


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _step(status: str, duration_ms: float, output: str) -> dict[str, Any]:
    return {
        "status": status,
        "duration_ms": round(duration_ms, 4),
        "output_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def _run(command: list[str], *, env: dict[str, str]) -> tuple[str, float, int]:
    started = time.monotonic()
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    elapsed = (time.monotonic() - started) * 1000
    output = result.stdout + result.stderr
    return output, elapsed, result.returncode


def run_native_install_matrix(
    *,
    root: Path,
    artifact: Path | None = None,
    package_module: str = "context_control_plane",
) -> dict[str, Any]:
    """Run an offline wheel probe when an artifact is supplied.

    Migration/export/rollback stay explicitly blocked until their CLI contract
    is implemented. The receipt remains useful for native CI without overstating
    the current completion gate.
    """
    root = root.resolve()
    if artifact is not None:
        artifact = artifact.resolve()
        if not artifact.is_file():
            raise FileNotFoundError(f"artifact is missing: {artifact}")
        artifact_sha256 = _sha256(artifact)
    else:
        source_marker = root / "pyproject.toml"
        artifact_sha256 = (
            _sha256(source_marker)
            if source_marker.is_file()
            else hashlib.sha256(b"source-probe-without-artifact").hexdigest()
        )

    system = platform.system()
    platform_info = {
        "system": system,
        "release": platform.release() or "unknown",
        "machine": platform.machine() or "unknown",
        "python": platform.python_version(),
    }
    steps: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="continuity-native-") as directory:
        temporary_root = Path(directory)
        site = temporary_root / "site"
        project = temporary_root / "project"
        env = dict(__import__("os").environ)
        env["PYTHONPATH"] = str(site)

        if artifact is None:
            steps["install"] = _step("blocked", 0, "offline artifact is required")
        else:
            output, elapsed, returncode = _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--target",
                    str(site),
                    str(artifact),
                ],
                env=env,
            )
            steps["install"] = _step(
                "passed" if returncode == 0 else "failed", elapsed, output
            )

        if steps["install"]["status"] == "passed":
            project.mkdir()
            output, elapsed, returncode = _run(
                [
                    sys.executable,
                    "-m",
                    f"{package_module}.cli",
                    "init",
                    "--root",
                    str(project),
                    "--project-id",
                    "native-probe",
                ],
                env=env,
            )
            if returncode == 0:
                verify_output, verify_elapsed, verify_code = _run(
                    [
                        sys.executable,
                        "-m",
                        f"{package_module}.cli",
                        "verify",
                        "--root",
                        str(project),
                    ],
                    env=env,
                )
                output += verify_output
                elapsed += verify_elapsed
                returncode = verify_code
            steps["verify"] = _step(
                "passed" if returncode == 0 else "failed", elapsed, output
            )
        else:
            steps["verify"] = _step("blocked", 0, "install did not pass")

        blocked = "export/import/rollback CLI is not implemented"
        steps["export"] = _step("blocked", 0, blocked)
        steps["import"] = _step("blocked", 0, blocked)
        steps["rollback"] = _step("blocked", 0, blocked)

        started = time.monotonic()
        shutil.rmtree(project, ignore_errors=True)
        shutil.rmtree(site, ignore_errors=True)
        steps["uninstall"] = _step(
            "passed" if not project.exists() and not site.exists() else "failed",
            (time.monotonic() - started) * 1000,
            "temporary project and install target removed",
        )

    return build_native_install_matrix_receipt(
        project_id="native-probe",
        requested_profile="local-embedded",
        platform=platform_info,
        package={
            "name": "continuity-plane",
            "version": VERSION,
            "artifact_sha256": artifact_sha256,
        },
        steps=steps,
        generated_at=datetime.now(UTC).isoformat(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run native local-embedded install probe")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--package-module", default="context_control_plane")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    receipt = run_native_install_matrix(
        root=args.root,
        artifact=args.artifact,
        package_module=args.package_module,
    )
    rendered = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
