"""Shared release identity and detection of packages upgraded under a live MCP."""

from importlib.metadata import PackageNotFoundError, version as package_version

VERSION = "0.1.0a23"
DISTRIBUTION = "continuity"


def _installed_version() -> str | None:
    try:
        return package_version(DISTRIBUTION)
    except PackageNotFoundError:
        return None


_STARTED_PACKAGE_VERSION = _installed_version()


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
    return None
