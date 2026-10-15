"""Git-backed identities for an ordinary checkout and its linked worktrees."""

from pathlib import Path


def matches(root, identity):
    root = Path(root).resolve()
    if (root / ".git").is_dir() and identity == root.name:
        return True
    try:
        # Git's documented gitfile / commondir layout. Read these small markers
        # on every check so relinking invalidates immediately, without spawning
        # Git once per evidence item. Do not cache mutable repository identities.
        marker = root / ".git"
        if not marker.is_file() or marker.stat().st_size > 8192:
            return False
        line = marker.read_text().strip()
        if not line.startswith("gitdir: "):
            return False
        gitdir = Path(line[len("gitdir: ") :])
        if not gitdir.is_absolute():
            gitdir = root / gitdir
        shared = gitdir / "commondir"
        if not shared.is_file() or shared.stat().st_size > 8192:
            return False
        common = Path(shared.read_text().strip())
        if not common.is_absolute():
            common = gitdir / common
        common = common.resolve()
        return (
            common.is_dir() and common.name == ".git" and identity == common.parent.name
        )
    except (OSError, ValueError, UnicodeError):
        return False
