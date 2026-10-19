"""Gitea access through the member's local tea CLI; no token extraction."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import threading
import tomllib
from urllib.parse import quote, urlencode, urlsplit


class GiteaError(ValueError):
    """Safe diagnostic: CLI response bodies and credentials are never echoed."""


class GiteaUnavailable(GiteaError):
    """A read failed, or a write may have reached the server."""


def validate_config(value):
    if not isinstance(value, dict) or set(value) != {
        "backend",
        "instance",
        "repository",
        "credential_ref",
    }:
        raise GiteaError(
            "collaboration requires backend, instance, repository, credential_ref only"
        )
    if value["backend"] != "gitea-issues":
        raise GiteaError("unsupported collaboration backend")
    if not all(isinstance(v, str) and v and len(v) <= 2048 for v in value.values()):
        raise GiteaError("invalid collaboration configuration")
    url = urlsplit(value["instance"])
    try:
        port = url.port
    except ValueError:
        raise GiteaError("invalid instance port") from None
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.query
        or url.fragment
        or ".." in url.path
        or any(c.isspace() for c in value["instance"])
        or (port is not None and port < 1)
    ):
        raise GiteaError(
            "instance must be HTTPS without credentials, query or fragment"
        )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value["repository"]):
        raise GiteaError("repository must be owner/name")
    if any(s in {".", ".."} for s in value["repository"].split("/")):
        raise GiteaError("invalid repository")
    reference = value["credential_ref"]
    login = reference[4:] if reference.startswith("tea:") else ""
    if (
        not 1 <= len(login.encode("utf-8")) <= 256
        or login != login.strip()
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in login)
    ):
        raise GiteaError(
            "credential_ref must be tea:LOGIN; use this member's local Gitea CLI login, never a token"
        )
    return {**value, "instance": value["instance"].rstrip("/")}


def project_config(root):
    path = Path(root) / ".continuity" / "collaboration.toml"
    if not path.exists():
        return None
    if path.stat().st_size > 16384:
        raise GiteaError("collaboration declaration too large")
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        raise GiteaError(
            "unreadable collaboration declaration; refusing local fallback"
        ) from None
    return validate_config(value.get("collaboration"))


def run_tea(command, data=None):
    """Bound both output streams; only terminate the child spawned by this call."""
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError:
        raise GiteaError(
            "local tea CLI unavailable; install it in this member's environment"
        ) from None
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()

    def read(stream, target):
        try:
            while chunk := stream.read(65536):
                if len(target) + len(chunk) > 4 * 1024 * 1024:
                    overflow.set()
                    process.kill()
                    return
                target.extend(chunk)
        finally:
            stream.close()

    readers = [
        threading.Thread(target=read, args=(stream, buffers[i]), daemon=True)
        for i, stream in enumerate((process.stdout, process.stderr))
    ]
    for reader in readers:
        reader.start()

    def feed():
        try:
            process.stdin.write(data or b"")
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    writer = threading.Thread(target=feed, daemon=True)
    writer.start()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise GiteaUnavailable(
            "local tea request timed out; write outcome may be unknown"
        ) from None
    finally:
        for reader in readers:
            reader.join(timeout=2)
        writer.join(timeout=2)
    if overflow.is_set():
        raise GiteaUnavailable("tea response exceeds 4 MiB; no partial success assumed")
    if any(reader.is_alive() for reader in readers):
        raise GiteaUnavailable("tea output incomplete; no partial success assumed")
    return process.returncode, bytes(buffers[0]), bytes(buffers[1])


class GiteaClient:
    def __init__(self, config):
        self.config = validate_config(config)
        self.tea = shutil.which("tea")
        if not self.tea:
            raise GiteaError(
                "local Gitea CLI tea missing; install tea with authenticated api support"
            )
        self.login = config["credential_ref"].split(":", 1)[1]
        self.repo = "/repos/" + "/".join(
            quote(p, safe="") for p in config["repository"].split("/")
        )
        code, raw, _ = run_tea([self.tea, "logins", "list", "--output", "json"])
        try:
            logins = json.loads(raw)
            matches = [
                r
                for r in logins
                if r.get("Name", r.get("name")) == self.login
                and r.get("URL", r.get("url", "")).rstrip("/")
                == self.config["instance"]
            ]
        except (ValueError, TypeError, AttributeError):
            matches = []
        if code or len(matches) != 1:
            raise GiteaError(
                "named local tea login does not match configured instance; authenticate locally, never paste a token"
            )
        code, help_text, _ = run_tea([self.tea, "api", "--help"])
        if code or b"--include" not in help_text or b"--data" not in help_text:
            raise GiteaError(
                "local tea needs api --include and --data support (tea 0.15 or compatible)"
            )

    def request(self, method, path, payload=None, query=None, *, site=False):
        if (
            not path.startswith("/")
            or path.startswith("//")
            or any(c in path for c in ("?", "#", ".."))
        ):
            raise GiteaError("invalid API path")
        endpoint = self.config["instance"] + ("" if site else "/api/v1") + path
        if query:
            endpoint += "?" + urlencode(query)
        data = (
            json.dumps(payload, ensure_ascii=False).encode()
            if payload is not None
            else None
        )
        if data is not None and len(data) > 131072:
            raise GiteaError("request exceeds 128 KiB")
        command = [
            self.tea,
            "api",
            "--login=" + self.login,
            "--repo=" + self.config["repository"],
            "--method",
            method,
            "--include",
        ]
        if data is not None:
            command.extend(["--data", "@-"])
        command.append(endpoint)
        code, raw, headers = run_tea(command, data)
        statuses = re.findall(rb"(?m)^HTTP/\S+\s+(\d{3})\b", headers)
        status = int(statuses[-1]) if statuses else None
        if status is not None and 300 <= status < 500 and status not in {408, 429}:
            raise GiteaError(
                f"Gitea HTTP {status}; check own login and repository permissions"
            )
        if code or status is None or status in {408, 429} or status >= 500:
            raise GiteaUnavailable(
                "local tea request failed; check login/connectivity; write outcome may be unknown"
            )
        if not 200 <= status < 300:
            raise GiteaError(
                f"Gitea HTTP {status}; check own login and repository permissions"
            )
        try:
            return json.loads(raw) if raw.strip() else None
        except ValueError:
            raise GiteaUnavailable(
                "invalid tea JSON response; no partial success assumed"
            ) from None

    def all(self, path, query=None):
        if path.endswith("/comments"):
            rows = self.request("GET", path, query=query)
            if not isinstance(rows, list) or len(rows) > 10000:
                raise GiteaError("invalid or oversized Issue comments response")
            return rows
        result = []
        for page in range(1, 101):
            rows = self.request(
                "GET", path, query={**(query or {}), "page": page, "limit": 50}
            )
            if not isinstance(rows, list):
                raise GiteaError("invalid paginated response")
            result.extend(rows)
            if not rows:
                return result
        raise GiteaError(
            "remote scan exceeds 100 pages; narrow scope; no partial success"
        )

    def identity(self):
        user = self.request("GET", "/user")
        login = user.get("login") if isinstance(user, dict) else None
        if not isinstance(login, str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", login
        ):
            raise GiteaError("authenticated account unavailable")
        return login

    def doctor(self):
        login = self.identity()
        repo = self.request("GET", self.repo)
        self.request(
            "GET",
            self.repo + "/issues",
            query={"limit": 1, "state": "all", "type": "issues"},
        )
        schema = None
        try:
            schema = self.request("GET", "/swagger.v1.json", site=True)
        except GiteaError:
            pass
        review_path = "/repos/{owner}/{repo}/pulls/{index}/requested_reviewers"
        return {
            "status": "connected",
            "backend": "gitea-issues",
            "transport": "local-tea-cli",
            "account": login,
            "repository": self.config["repository"],
            "private": repo.get("private"),
            "repository_permissions": repo.get("permissions", {}),
            "issue_read": True,
            "issues_enabled": repo.get("has_issues"),
            "pull_requests_enabled": repo.get("has_pull_requests"),
            "review_request_api": bool(
                schema and "post" in schema.get("paths", {}).get(review_path, {})
            ),
            "write_scope": "not proven by read-only check; mutations report server denial",
            "claim_guarantee": "coordination-only; no global exclusive claim or lease",
            "conditional_updates": False,
            "repository_settings_changed": False,
            "credential_access": "tea only; Continuity never reads or exports tokens",
            "tls_redirect_policy": "managed by member-local tea login and HTTP client",
        }
