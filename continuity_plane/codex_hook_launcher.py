"""Run one hook bundled with an installed Continuity Plane plugin."""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


def _resolve_hook_path(requested: Path) -> Path | None:
    if requested.is_file():
        return requested
    if requested.name != "continuity-hook.py":
        return None
    value = os.environ.get("PLUGIN_ROOT")
    if value:
        candidate = Path(value) / "scripts/continuity-hook.py"
        if candidate.is_file():
            return candidate
    return None


def main() -> int:
    """Load the explicitly selected plugin hook with the package interpreter."""
    if len(sys.argv) != 2:
        return 2
    path = _resolve_hook_path(Path(sys.argv[1]))
    if path is None:
        # A stale plugin cache must never turn a normal Codex operation into a failure.
        return 0
    spec = importlib.util.spec_from_file_location(
        "continuity_plane_plugin_hook", path
    )
    if spec is None or spec.loader is None:
        return 2
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hook_main = getattr(module, "main", None)
    return int(hook_main()) if callable(hook_main) else 2


if __name__ == "__main__":
    raise SystemExit(main())
