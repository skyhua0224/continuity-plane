"""Claude Code lifecycle bridge; advisory and fail-open."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        cwd = Path(payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()).resolve()
        event = payload.get("hook_event_name", "SessionStart")
        from continuity_plane.lifecycle import handle_event
        output = handle_event({**payload, "cwd": str(cwd)}, host="claude")
        if "hookSpecificOutput" in output:
            print(json.dumps(output, ensure_ascii=False, separators=(",", ":")))
        if event == "SessionStart" and payload.get("source") == "compact" and output.get("status") != "already-delivered":
            event = "PostCompact"
        if event not in {"PreCompact", "PostCompact"}:
            return 0
        command = "create" if event == "PreCompact" else "verify"
        result = subprocess.run(
            [sys.executable, "-m", "continuity_plane.cli", "checkpoint", command, "--root", str(cwd)],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        from continuity_plane.usage_meter import record
        record(cwd, command="checkpoint:" + command + (":ok" if result.returncode == 0 else ":failed"),
               response_bytes=0, estimated_grep_bytes=0, session_id=payload.get("session_id"), host="claude")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
