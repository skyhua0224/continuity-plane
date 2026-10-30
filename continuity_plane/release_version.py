"""Shared release identity and detection of packages upgraded under a live MCP."""

from importlib.metadata import PackageNotFoundError, version as package_version
import hashlib
from pathlib import Path

VERSION = "0.1.0a37"
DISTRIBUTION = "continuity-plane"


def _installed_version() -> str | None:
    try:
        return package_version(DISTRIBUTION)
    except PackageNotFoundError:
        return None


_STARTED_PACKAGE_VERSION = _installed_version()


# The package directory at import. Tests may patch __file__ to fingerprint a
# copy; the start-up fingerprint must still describe the code that was loaded.
_PACKAGE_ROOT = Path(__file__).parent


def _code_fingerprint(root=None):
    digest = hashlib.sha256()
    root = Path(root) if root is not None else Path(__file__).parent
    for path in sorted(root.rglob("*.py")):
        name = str(path.relative_to(root)).encode()
        digest.update(name + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"unavailable:" + name)
    return digest.hexdigest()


def _code_stat(root=None):
    """Cheap metadata signature of the package sources (no file reads)."""
    root = Path(root) if root is not None else _PACKAGE_ROOT
    signature = []
    for path in sorted(root.rglob("*.py")):
        try:
            info = path.stat()
            signature.append((str(path.relative_to(root)), info.st_size, info.st_mtime_ns))
        except OSError:
            signature.append((str(path.relative_to(root)), None, None))
    return signature


# Hashing every source file costs about half of a CLI start-up, and only
# long-lived servers compare against it. Record the cheap signature now and hash
# on first use; if the sources changed in between, the start digest is unknowable
# and the code is reported as changed. The result is cached privately so a
# temporary patch of _STARTED_CODE_FINGERPRINT cannot leave a stale value behind.
_STARTED_CODE_STAT = _code_stat(_PACKAGE_ROOT)
_started_fingerprint_cache = None


def _started_code_fingerprint():
    explicit = globals().get("_STARTED_CODE_FINGERPRINT")
    if explicit is not None:
        return explicit
    global _started_fingerprint_cache
    if _started_fingerprint_cache is None:
        unchanged = _code_stat(_PACKAGE_ROOT) == _STARTED_CODE_STAT
        _started_fingerprint_cache = (
            _code_fingerprint(_PACKAGE_ROOT) if unchanged else "changed-before-first-check"
        )
    return _started_fingerprint_cache


def __getattr__(name):
    if name == "_STARTED_CODE_FINGERPRINT":
        return _started_code_fingerprint()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    if _code_fingerprint() != _started_code_fingerprint():
        return "Continuity code changed on disk at the same package version; restart this MCP process; ordinary project work remains allowed"
    return None
