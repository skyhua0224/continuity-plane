#!/usr/bin/env python3
"""Forward the State plugin launcher to the canonical packaged MCP server."""

import sys
from pathlib import Path


_RELEASE_ROOT = Path(__file__).resolve().parents[3]
if (_RELEASE_ROOT / "continuity_plane" / "codex_mcp_server.py").is_file():
    sys.path.insert(0, str(_RELEASE_ROOT))

from continuity_plane.codex_mcp_server import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
