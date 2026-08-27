# Changelog

[中文](CHANGELOG.md)

## 0.1.0-alpha.7

### Changes Since 0.1.0-alpha.1

- Completed the local SQLite runtime boundaries for typed state, checkpoints, event replay, claims/leases, and evidence gates.
- Added a public Codex plugin bundle with SessionStart, PreCompact, PostCompact, PreToolUse, and PostToolUse hooks, an MCP server, and the continuity Skill.
- New Sessions discover the project root automatically and show a one-time startup receipt; compact recovery remains bounded and free of recovery narration.
- Added Git common-dir binding so the main checkout and sibling worktrees resolve to one project state and cannot bypass it.
- Added atomic source-stale owner heartbeat/reclaim recovery, guarded by actor, claim, checkpoint, and scope validation.
- Added atomic idle-to-delivery activation binding source, predecessor Work, implementation evidence, baseline HEAD, pending worktree delta, and exact effect scopes.
- Fixed local `rsync` being classified as a remote effect; real remote transfers remain effect-gated.
- The public release compiler now emits the Python wheel, Codex plugin, and public marketplace together.
- Preserved public benchmarks, privacy scans, and cross-platform installation verification; cache-hit rate is not presented as token savings.
- `0.1.0-alpha.7` is a public alpha. The complete Docmost connector, Canvas/Bases, and shared-strong deployment remain planned.

### Installation

Core package:

```bash
python -m pip install continuity-plane==0.1.0a7
```

Codex plugin:

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.7
codex plugin add continuity-plane@continuity-plane
```

Start a new Session after installing or upgrading the plugin. The core package works alone; the
plugin is a host integration layer and has no authority to write canonical state.

## 0.1.0-alpha.1

- Published the initial public identity as Continuity Plane.
- Licensed the project under Apache-2.0 with NOTICE and third-party notices.
- Added optional brand guidance and a static `Managed with Continuity Plane` badge.
- Added local-embedded project initialization and verification CLI.
- Added revisioned state, append-only events, claims, checkpoints, and replay gates.
- Added bounded Execution Packets, Skill selection, evidence lineage, and retrieval receipts.
- Added collaboration claims, notifications, handoffs, and unattended local workflows.
- Added measured reference benchmarks and a privacy-gated public release builder.
- Published `continuity-plane==0.1.0a1` on PyPI.
- Passed install, verify, and uninstall probes on Linux, macOS, and Windows; migration and rollback remain in M10-09.
