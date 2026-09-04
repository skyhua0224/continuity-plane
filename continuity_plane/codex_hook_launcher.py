"""Run the hook bundled with an installed Continuity Plane plugin."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def main() -> int:
    """Load one explicit plugin hook path with the package's Python runtime."""

    if len(sys.argv) != 2:
        return 2
    path = Path(sys.argv[1])
    if not path.is_file():
        return 2
    spec = importlib.util.spec_from_file_location("continuity_plane_plugin_hook", path)
    if spec is None or spec.loader is None:
        return 2
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hook_main = getattr(module, "main", None)
    return int(hook_main()) if callable(hook_main) else 2


if __name__ == "__main__":
    raise SystemExit(main())
