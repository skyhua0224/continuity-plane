"""Keep the owner channel running: one poller per host, started at login.

Linux uses a systemd user unit, macOS a launchd agent, and Windows the
per-user Run key, which needs no administrator rights. Every form runs the
same `notify serve` command with the interpreter that installed Continuity.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SERVICE_NAME = "continuity-owner-channel"
LAUNCHD_LABEL = "io.github.continuity-plane.owner-channel"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ContinuityOwnerChannel"
MAX_LOG_BYTES = 1024 * 1024


class ServiceError(ValueError):
    pass


def channel_dir(data_root: Path | str | None) -> Path:
    from .collaboration_registry import default_data_root

    return Path(data_root) if data_root is not None else default_data_root()


def log_path(data_root: Path | str | None) -> Path:
    return channel_dir(data_root) / "owner-channel" / "serve.log"


def _interpreter(background: bool) -> str:
    executable = Path(sys.executable)
    if background and os.name == "nt":
        windowless = executable.with_name("pythonw.exe")
        if windowless.exists():
            return str(windowless)
    return str(executable)


def serve_command(data_root: Path | str | None, *, background: bool = True) -> list[str]:
    """Login services do not inherit the shell environment, so name the data
    root explicitly; a background process has no usable stderr, so log to a file."""
    package = __package__ or "continuity_plane"
    root = channel_dir(data_root).resolve()
    command = [_interpreter(background), "-m", f"{package}.cli", "notify", "serve"]
    command += ["--data-root", str(root)]
    if background:
        command += ["--log-file", str(log_path(root))]
    return command


@contextmanager
def single_instance(data_root: Path | str | None) -> Iterator[None]:
    """Hold a non-blocking lock so a second poller exits instead of competing."""
    path = channel_dir(data_root) / "owner-channel" / "serve.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+", encoding="utf-8")
    try:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ServiceError("another owner channel is already running on this host") from None
        yield
    finally:
        handle.close()  # closing releases either lock


class LogFile:
    """Append-only log with one rotated copy; never records message bodies."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def __call__(self, line: str) -> None:
        from datetime import UTC, datetime

        try:
            if self.path.exists() and self.path.stat().st_size > MAX_LOG_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(f"{datetime.now(UTC).isoformat(timespec='seconds')} {line}\n")
        except OSError:
            pass


def _unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / f"{SERVICE_NAME}.service"


def _systemd_word(word: str) -> str:
    if any(c.isspace() or c in "\"'\\;$%" for c in word):
        escaped = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
        return f'"{escaped}"'
    return word


def systemd_unit(command: list[str]) -> str:
    return (
        "[Unit]\n"
        "Description=Continuity owner notification channel\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={' '.join(_systemd_word(word) for word in command)}\n"
        "Restart=on-failure\n"
        "RestartSec=30\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _run(*command: str) -> subprocess.CompletedProcess:
    return subprocess.run(list(command), capture_output=True, text=True, timeout=60)


def _windows_command_line(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


def install(data_root: Path | str | None, *, start: bool = True) -> dict[str, Any]:
    command = serve_command(data_root)
    if os.name == "nt":
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _windows_command_line(command))
        if start:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                close_fds=True,
            )
        return {"installed": "windows-run-key", "started": start}
    if sys.platform == "darwin":
        path = _plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as stream:
            plistlib.dump(
                {
                    "Label": LAUNCHD_LABEL,
                    "ProgramArguments": command,
                    "RunAtLoad": True,
                    "KeepAlive": {"SuccessfulExit": False},
                    "ThrottleInterval": 30,
                },
                stream,
            )
        if start:
            domain = f"gui/{os.getuid()}"
            _run("launchctl", "bootout", domain, str(path))
            result = _run("launchctl", "bootstrap", domain, str(path))
            if result.returncode:
                raise ServiceError("launchctl could not start the agent")
        return {"installed": "launchd", "started": start}
    path = _unit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(systemd_unit(command), encoding="utf-8")
    for step in (("daemon-reload",), ("enable", "--now" if start else "--quiet", SERVICE_NAME)):
        result = _run("systemctl", "--user", *step)
        if result.returncode:
            raise ServiceError(
                f"systemctl --user {' '.join(step)} failed; a transient unit with the same name must be stopped first"
            )
    return {
        "installed": "systemd-user",
        "started": start,
        "hint": "run `loginctl enable-linger` to keep it running after logout",
    }


def uninstall() -> dict[str, Any]:
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except FileNotFoundError:
            return {"removed": False}
        return {"removed": True, "note": "a running channel stops at logout or when ended"}
    if sys.platform == "darwin":
        path = _plist_path()
        if not path.exists():
            return {"removed": False}
        _run("launchctl", "bootout", f"gui/{os.getuid()}", str(path))
        path.unlink()
        return {"removed": True}
    path = _unit_path()
    if not path.exists():
        return {"removed": False}
    _run("systemctl", "--user", "disable", "--now", SERVICE_NAME)
    path.unlink()
    _run("systemctl", "--user", "daemon-reload")
    return {"removed": True}


def installed() -> str | None:
    if os.name == "nt":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, RUN_VALUE)
        except FileNotFoundError:
            return None
        return "windows-run-key"
    if sys.platform == "darwin":
        return "launchd" if _plist_path().exists() else None
    return "systemd-user" if _unit_path().exists() else None


__all__ = [
    "LogFile",
    "ServiceError",
    "install",
    "installed",
    "log_path",
    "serve_command",
    "single_instance",
    "systemd_unit",
    "uninstall",
]
