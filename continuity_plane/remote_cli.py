"""Explicit SSH CLI transport using JSON argv and mapped roots, never shell interpolation."""

import contextlib
import io
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path


def forward(
    argv,
    *,
    host,
    root,
    endpoint="continuity-remote-cli",
    timeout=30,
    runner=subprocess.run,
):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}", host):
        raise ValueError("invalid SSH host alias")
    if not isinstance(root, str) or not root.startswith("/"):
        raise ValueError("absolute remote root is required")
    if Path(endpoint).name != "continuity-remote-cli" or any(
        c in endpoint for c in "\r\n\x00"
    ):
        raise ValueError("endpoint must name continuity-remote-cli")
    if not 0 < timeout <= 86400:
        raise ValueError("remote timeout must be 0–86400 seconds")
    clean = []
    skip = False
    passthrough = False
    flags = {"--remote", "--remote-root", "--remote-endpoint", "--remote-timeout"}
    for token in argv:
        if passthrough:
            clean.append(token)
            continue
        if token == "--":
            clean.append(token)
            passthrough = True
            continue
        if skip:
            skip = False
            continue
        if token in flags:
            skip = True
            continue
        if any(token.startswith(f + "=") for f in flags):
            continue
        clean.append(token)
    request = {"root": root, "argv": clean}
    if len(json.dumps(request).encode()) > 131072:
        raise ValueError("remote argv exceeds 128 KiB")
    try:
        response = runner(
            [
                "ssh",
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=5",
                "-o",
                "ClearAllForwardings=yes",
                host,
                shlex.quote(endpoint),
            ],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            "remote command timed out; inspect attempt events before retrying an execution request"
        ) from exc
    if response.returncode != 0 and not response.stdout.strip():
        raise ValueError("remote Continuity unavailable: " + response.stderr[:400])
    result = json.loads(response.stdout)
    if not isinstance(result, dict) or type(result.get("return_code")) is not int:
        raise ValueError("invalid remote CLI receipt")
    return result


def receive_main():
    from .cli import build_parser, main
    from .collaboration_registry import resolve_project

    try:
        raw = sys.stdin.buffer.read(131073)
        if len(raw) > 131072:
            raise ValueError("remote argv exceeds 128 KiB")
        request = json.loads(raw)
        argv = request.get("argv")
        root = request.get("root")
        if (
            not isinstance(argv, list)
            or not all(isinstance(v, str) for v in argv)
            or not isinstance(root, str)
            or not Path(root).is_absolute()
        ):
            raise ValueError("JSON argv and absolute root required")
        if "--remote" in argv or any(v.startswith("--remote=") for v in argv):
            raise ValueError("recursive remote CLI is not permitted")
        parsing_output = io.StringIO()
        parsing_errors = io.StringIO()
        try:
            with (
                contextlib.redirect_stdout(parsing_output),
                contextlib.redirect_stderr(parsing_errors),
            ):
                parsed = build_parser().parse_args(argv)
        except SystemExit as exc:
            print(
                json.dumps(
                    {
                        "return_code": int(exc.code or 0),
                        "stdout": parsing_output.getvalue(),
                        "stderr": parsing_errors.getvalue()[:4096],
                    }
                )
            )
            return 0
        public_start = parsed.command == "controller" and getattr(parsed, "memory_action", None) == "start" and getattr(parsed, "public_only", False)
        if not public_start and parsed.command not in {
            "collaboration",
            "todo",
            "rules",
            "context",
            "observe",
            "worker",
            "dispatch",
            "report",
            "task",
            "whoami",
            "help",
        }:
            raise ValueError(
                "remote CLI supports collaboration workflows; authoritative State uses its dedicated interface"
            )
        if parsed.global_root not in {None, root}:
            raise ValueError("global root disagrees with the mapped root")
        project = resolve_project(root, getattr(parsed, "data_root", None))
        if project is None:
            raise ValueError("remote root is not registered")
        if getattr(parsed, "project_id", None) not in {None, project["project_id"]}:
            raise ValueError("project id does not match the mapped remote root")
        if hasattr(parsed, "root"):
            if getattr(parsed, "root", ".") not in {".", root}:
                raise ValueError("subcommand root disagrees with the mapped root")
            argv = ["--root", root] + argv
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = main(argv)
            except (OSError, ValueError) as exc:
                code = 2
                print(str(exc), file=sys.stderr)
        stdout = output.getvalue()
        stderr = errors.getvalue()
        if len(stdout.encode()) > 131072:
            raise ValueError(
                "remote output exceeds 128 KiB; narrow the query or use an explicit page/budget"
            )
        print(
            json.dumps(
                {"return_code": code, "stdout": stdout, "stderr": stderr[:4096]},
                ensure_ascii=False,
            )
        )
        return 0
    except (OSError, ValueError, TypeError, KeyError, SystemExit) as exc:
        print(json.dumps({"return_code": 2, "stdout": "", "stderr": str(exc)[:1000]}))
        return 0
