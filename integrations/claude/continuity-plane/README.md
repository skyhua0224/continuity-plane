# Continuity Plane for Claude Code

This integration is advisory. It injects a bounded recovery/collaboration hint at session
start and after compaction, and records no provider credentials. It does not block tools,
claim Work, or wake a second Claude process.

Install the core package, register the project with `continuity collaboration register`,
then enable this directory as a Claude Code plugin. Its manifest, MCP configuration and
hooks use `continuity-host-hook` from the core package, rather than system Python.
Use `hooks/settings.example.json` only when your host needs explicit hook configuration.
A running Claude Code process may require reconnection/reload or a new session after adding
the MCP server or hooks; inspect the actual tool catalog and event receipts.

PreCompact stores a session-specific next action, boundaries, verification and Git return point,
then attempts checkpoint creation. PostCompact verifies and injects a quiet bounded continuation.
SessionStart(compact) is also supported when the host uses that event for post-compaction recovery.
Repeated post-compaction events do not repeat the injected context. Failed State verification does
not revive an old Work or prevent ordinary authorized work.

The local `observe report` includes returned bytes, hook-injected bytes and a signed replacement
estimate by command/session. A negative estimate is visible. It is not a measured token-saving rate.
