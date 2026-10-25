"""Shared release identity and detection of packages upgraded under a live MCP."""

from importlib.metadata import PackageNotFoundError, version as package_version
import hashlib
from pathlib import Path

VERSION = "0.1.0a35"
DISTRIBUTION = "continuity-plane"


def _installed_version() -> str | None:
    try:
        return package_version(DISTRIBUTION)
    except PackageNotFoundError:
        return None


_STARTED_PACKAGE_VERSION = _installed_version()


def _code_fingerprint():
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        name = str(path.relative_to(root)).encode()
        digest.update(name + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"unavailable:" + name)
    return digest.hexdigest()


_STARTED_CODE_FINGERPRINT = _code_fingerprint()


def runtime_version_issue() -> str | None:
    installed = _installed_version()
    # Source checkouts may intentionally run beside an older installed build.
    # Detect a change after startup without treating that workflow as corruption.
    if (
        installed is not None
        and _STARTED_PACKAGE_VERSION is not None
        and installed != _STARTED_PACKAGE_VERSION
    ):
        return (
            f"MCP process is running Continuity {VERSION}, installed package is {installed}; "
            "restart this MCP process; ordinary project work remains allowed"
        )
    if _code_fingerprint() != _STARTED_CODE_FINGERPRINT:
        return "Continuity code changed on disk at the same package version; restart this MCP process; ordinary project work remains allowed"
    return None
