"""Opaque collection identities; credential/profile contents never leave their host."""

import hashlib
import os
import socket
from pathlib import Path


def current(provider):
    machine = socket.gethostname()
    try:
        machine += Path("/etc/machine-id").read_text().strip()
    except OSError:
        pass
    variable = {
        "claude": "CLAUDE_CONFIG_DIR",
        "codex": "CODEX_HOME",
        "custom": "CONTINUITY_PROVIDER_PROFILE",
    }[provider]
    default = {
        "claude": ".claude",
        "codex": ".codex",
        "custom": ".local/state/continuity/provider-profiles/default",
    }[provider]
    configured = os.environ.get(variable)
    profile = Path(configured) if configured else Path.home() / default
    # Native clients inherit this environment but can run in another worktree.
    # Resolving a relative value here would bind the controller's cwd while the
    # client could load a different account from the child's cwd. Literal '~'
    # is also not assumed to have native-client expansion semantics.
    if not profile.is_absolute():
        raise ValueError(
            f"{variable} must be an absolute path on its owning host/profile; do not resolve account state relative to a worker checkout"
        )
    profile = profile.resolve()
    return {
        "provider": provider,
        "host_sha256": hashlib.sha256(machine.encode()).hexdigest(),
        "profile_sha256": hashlib.sha256(str(profile).encode()).hexdigest(),
    }


def validate(value):
    import re

    if not isinstance(value, dict) or set(value) != {
        "provider",
        "host_sha256",
        "profile_sha256",
    }:
        raise ValueError("invalid runtime origin")
    if value["provider"] not in {"claude", "codex", "custom"} or any(
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
