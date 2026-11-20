"""Opaque collection identities; credential/profile contents never leave their host."""

import hashlib
import os
import socket
import sys
from pathlib import Path

NATIVE_PROVIDERS = ("codex", "claude", "antigravity")
PROVIDERS = (*NATIVE_PROVIDERS, "custom")


def current(provider):
    machine = socket.gethostname()
    try:
        machine += Path("/etc/machine-id").read_text().strip()
    except OSError:
        pass
    variable = {
        "antigravity": None,
        "claude": "CLAUDE_CONFIG_DIR",
        "codex": "CODEX_HOME",
        "custom": "CONTINUITY_PROVIDER_PROFILE",
    }[provider]
    default = {
        # agy-settings, CLI configuration and settings.json.
        "antigravity": ".gemini/antigravity-cli",
        "claude": ".claude",
        "codex": ".codex",
        "custom": ".local/state/continuity/provider-profiles/default",
    }[provider]
    configured = os.environ.get(variable) if variable else None
    # python-314-pathlib, Path.home and Path.resolve.
    profile = Path(configured) if configured else Path.home() / default
    # Native clients inherit this environment but can run in another worktree.
    # Resolving a relative value here would bind the controller's cwd while the
    # client could load a different account from the child's cwd. Literal '~'
    # is also not assumed to have native-client expansion semantics.
    if not profile.is_absolute():
        raise ValueError(
            f"{variable or 'HOME'} must be an absolute path on its owning host/profile; do not resolve account state relative to a worker checkout"
        )
    profile_identity = str(profile.resolve())
    if provider == "antigravity" and sys.platform.startswith("linux"):
        # xdg-base-directory, Environment variables. The native CLI reads its
        # credential-user-scope under this root while settings follow HOME.
        config_home = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        if not config_home.is_absolute():
            raise ValueError("XDG_CONFIG_HOME must be absolute to bind the native credential scope")
        credential_dir = (config_home / "antigravity").resolve()
        profile_identity += "\0" + str(credential_dir)
    return {
        "provider": provider,
        "host_sha256": hashlib.sha256(machine.encode()).hexdigest(),
        "profile_sha256": hashlib.sha256(profile_identity.encode()).hexdigest(),
    }


def validate(value):
    import re

    if not isinstance(value, dict) or set(value) != {
        "provider",
        "host_sha256",
        "profile_sha256",
    }:
        raise ValueError("invalid runtime origin")
    if value["provider"] not in PROVIDERS or any(
        not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{64}", value[k])
        for k in ("host_sha256", "profile_sha256")
    ):
        raise ValueError("invalid runtime origin identity")
    return dict(value)


def key(value):
    import json

    return hashlib.sha256(
        json.dumps(validate(value), sort_keys=True).encode()
    ).hexdigest()


def require_local(value, provider):
    if validate(value) != current(provider):
        raise ValueError(
            "runtime host/profile mismatch; run the query on its owning host and profile, never copy account/session data to query here"
        )
