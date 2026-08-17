#!/usr/bin/env python3
"""Forward the State plugin launcher to the canonical package server."""
import sys
from pathlib import Path
for package_root in Path(__file__).resolve().parents:
    if any((package_root / name).is_dir() for name in ("continuity_plane", "continuity_plane")):
        sys.path.insert(0, str(package_root)); break
from continuity_plane.codex_mcp_server import main  # noqa: E402
if __name__ == "__main__":
    raise SystemExit(main())
