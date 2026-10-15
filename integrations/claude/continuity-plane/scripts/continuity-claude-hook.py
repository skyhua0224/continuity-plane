"""Claude Code lifecycle bridge; advisory and fail-open."""

from __future__ import annotations

def main() -> int:
    try:
        from continuity_plane.lifecycle import host_main
        return host_main()
    except ImportError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
