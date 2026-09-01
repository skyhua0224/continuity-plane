#!/usr/bin/env python3
"""Forward the State plugin launcher to the canonical packaged MCP server."""

from continuity_plane.codex_mcp_server import main


if __name__ == "__main__":
    raise SystemExit(main())
