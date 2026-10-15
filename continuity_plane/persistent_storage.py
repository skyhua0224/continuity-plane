"""Prefer persistent work roots; temporary buffers must not become recovery state."""

import os
import sys
from pathlib import Path


def describe(path, *, mountinfo=None):
    path = Path(path).expanduser().resolve()
    fs_type = "unknown"
    mount = None
    if mountinfo is None and sys.platform.startswith("linux"):
        try:
            mountinfo = Path("/proc/self/mountinfo").read_text()
        except OSError:
            pass
    if isinstance(mountinfo, str):
        for line in mountinfo.splitlines():
            before, separator, after = line.partition(" - ")
            fields = before.split()
            filesystem_fields = after.split()
            if not separator or len(fields) < 5 or not filesystem_fields:
                continue
            # mountinfo escapes whitespace and literal backslashes in mount paths.
            place = Path(
                fields[4]
                .replace("\\040", " ")
                .replace("\\011", "\t")
                .replace("\\012", "\n")
                .replace("\\134", "\\")
            )
            if path.is_relative_to(place) and (
                mount is None or len(str(place)) > len(str(mount))
            ):
                mount, fs_type = place, filesystem_fields[0]
    temporary_location = any(
        path.is_relative_to(Path(p).resolve())
        for p in (
            "/tmp",
            "/var/tmp",
            "/dev/shm",
            "/private/tmp",
            "/private/var/tmp",
            "/private/var/folders",
        )
    )
    if os.name == "nt":
        temporary_location = any(
            path.is_relative_to(Path(os.environ[k]).resolve())
            for k in ("TEMP", "TMP")
            if os.environ.get(k)
        )
    volatile = fs_type in {"tmpfs", "ramfs"} or temporary_location
    unverified = sys.platform.startswith("linux") and fs_type == "unknown"
    return {
        "path": str(path),
        "filesystem": fs_type,
        "memory_backed": fs_type in {"tmpfs", "ramfs"},
        "temporary_location": temporary_location,
        "recovery_suitable": not volatile and not unverified,
        "warning": "Do not retain task bodies, reports, threads or checkpoints here; choose a persistent workspace"
        if volatile
        else "Filesystem could not be verified; do not assume this is persistent storage"
        if unverified
        else None,
        "durability": "location/filesystem observation, not a backup or power-loss guarantee",
    }


def workspace(data_root=None):
    explicit = os.environ.get("CONTINUITY_WORK_ROOT")
    base = (
        Path(explicit).expanduser()
        if explicit
        else Path(data_root) / "work"
        if data_root
        else Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
        / "continuity/work"
    )
    requested = base
    rejected = []
    # A user home may itself be memory-backed. Never label the fallback durable
    # without checking it as well, and never silently create recovery state there.
    for candidate in dict.fromkeys(
        (base, Path.home() / ".local/state/continuity/work")
    ):
        observation = describe(candidate)
        if not observation["recovery_suitable"]:
            rejected.append({**observation, "reason": "volatile-location"})
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            rejected.append({**observation, "reason": "unwritable-location"})
            continue
        # Resolve again after creation (including previously missing symlinks).
        observation = describe(candidate)
        if not observation["recovery_suitable"]:
            rejected.append({**observation, "reason": "volatile-location"})
            continue
        if rejected:
            observation["fallback_from"] = str(requested)
            observation["rejected_locations"] = rejected
        return candidate.resolve(), observation
    return None, {
        "path": None,
        "recovery_suitable": False,
        "rejected_locations": rejected,
        "warning": "Persistent scratch unavailable; ordinary command may continue but recovery artifacts cannot be guaranteed",
    }


def sync_directory(path):
    if os.name != "posix":
        return False
    try:
        fd = os.open(str(Path(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return True
    except OSError:
        return False


def command(args):
    import json

    path = Path(args.path).expanduser()
    if not path.is_absolute():
        path = Path(args.root).expanduser().resolve() / path
    print(json.dumps(describe(path), ensure_ascii=False, sort_keys=True))
    return 0


def register_cli(commands):
    parser = commands.add_parser(
        "storage",
        help="Inspect persistence of task/artifact locations; no account or State access",
    )
    group = parser.add_subparsers(dest="storage_action", required=True)
    inspect = group.add_parser(
        "inspect",
        help="Detect temporary locations and Linux tmpfs/ramfs even outside /tmp",
    )
    inspect.add_argument("--path", required=True)
    inspect.add_argument("--root", default=".")
    inspect.set_defaults(handler=command)
