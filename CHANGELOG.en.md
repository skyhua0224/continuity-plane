# Changelog

[中文](CHANGELOG.md)

## 0.1.0-alpha.9.1

### Fixes And Improvements

- Fixed persistence of Session bindings, observations, and recovery budgets when a Codex host omits `PLUGIN_DATA`; the plugin now falls back to a stable per-user data directory.
- Accepted both the short `continuity_resume` tool name and the full MCP name so post-resume binding works consistently across Codex hosts.
- Resolved effect commands through registered delivery workspaces first, validating repository digest, allowed effects, and unique matches so a stale `cwd` cannot route a multi-project Session to the wrong governance root.
- Added the public plugin `PreToolUse` effect-scope gate: push, merge, deployment, and release commands are denied before execution when there is no claim, the scope does not match, or the workspace is unregistered.

### Verification

- Public smoke/contracts/benchmark `5/5` pass; plugin script compilation and JSON contract validation pass.
- The patch preserves alpha.9's sanitized public boundary; business repositories, internal state, and raw sessions are excluded from the public tree.

### Installation

```bash
python -m pip install continuity-plane==0.1.0a9.post1
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.9.1
codex plugin add continuity-plane@continuity-plane
```

## 0.1.0-alpha.9

### Changes Since 0.1.0-alpha.8

- One Codex Session can explicitly bind multiple independent project roots. The
  MCP server no longer pre-binds from `cwd`; each successful
  `continuity_resume(root=...)` adds a project and switches the active root.
  Claims, checkpoints, revisions, and effect permissions remain isolated per
  project.
- Once a Session binding exists, an unbound root, missing profile, profile-digest
  mismatch, or corrupt binding is rejected before any CLI/State write. The
  terminal `cwd` cannot override an explicit project identity.
- A governance root and external delivery workspace can be used alternately in
  one Session. The external repository remains constrained by its workspace
  registry, repository digest, expected HEAD/ref, and `repo://` scope.
- The packaged MCP and Codex plugin multi-root contracts are synchronized. The
  handshake reports the alpha.8 protocol line, and legacy single-root migration
  plus multi-root profile-integrity checks are covered.

### Verification And Boundaries

- MCP binding, plugin lifecycle, root switching, unbound-write rejection, and
  strict-schema focused tests pass `46/46`; ruff checks pass.
- Explicit dual-project resume for two independent governance roots succeeds
  while the process runs from an unrelated workspace cwd; project identity is
  selected by the requested root, with no product-repository changes or direct
  SQLite writes.
- Alpha.9 remains a prerelease. Automatic cross-project Work merging, a
  cross-device unique claim, and cross-project token A/B remain governed by the
  selected runtime profile and later acceptance gates.

### Installation

Core package:

```bash
python -m pip install continuity-plane==0.1.0a9
```

Codex plugin:

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.9
codex plugin add continuity-plane@continuity-plane
```

Start a new Session after installing or upgrading the plugin. In a multi-project
Session, explicitly call `continuity_resume(root=...)` before working in each
project.

## 0.1.0-alpha.8

### Changes Since 0.1.0-alpha.7

- The public tag-tree delta from `v0.1.0-alpha.7` is `22` files (`2` added and
  `20` modified; `+2,081/-166` lines). The complete alpha.1 capability list remains
  in the alpha.7 audit and alpha.1 entry below; the bullets here are only the
  user-visible additions in alpha.8.

- Added the `continuity autorun` CLI and `continuity_autorun` MCP tool. After a
  verified checkpoint and valid permissions, the same Session re-enters its
  current Work. A local idempotency record prevents duplicate State Events for
  one checkpoint; leases are heartbeated near expiry and reclaimed with a new
  claim after expiry.
- MCP/plugin transient `transport closed`, connection-reset, and timeout errors
  retry on the same project root and return an explicit `failed_gate` when the
  retry budget is exhausted.
- Successor activation after `attach refresh` can atomically bind the new
  canonical source evidence in the same State Event instead of rejecting an
  evidence ID that has not yet entered State.
- Effect intent resources now distinguish provider, host, repository, worktree,
  and branch. Sessions on one resource remain mutually exclusive while different
  repositories cannot block one another.
- The public wheel now installs both `continuity` and `continuity-mcp`, so the
  Codex plugin MCP entry does not depend on a manually placed host script.
- A source-control push, PR, merge, or release scope now covers its required local
  commit prerequisite without granting any unrelated external effect.
- Read-only queries such as `git tag --list` and `gh release view` no longer enter
  the effect gate.
- Sequential effects from one Session no longer conflict with each other; other
  Sessions remain fenced by the repository-level intent.

### Verification And Boundaries

- Control-plane, MCP binding, plugin lifecycle, activation, and autorun focused
  tests passed `61/61`.
- A sanitized live snapshot returned `continued → already-continued` in one MCP
  Session; the repeated call created no State Event, while the project state and
  its unpassed external blocker were preserved.
- Public smoke/contracts/benchmark `5/5`, wheel/sdist `twine check`, public
  privacy scan, Linux/macOS/Windows installation matrix, and repository
  verification passed.
- Alpha.8 remains a prerelease. The Docmost connector, Obsidian Canvas/Bases,
  one-command shared-strong deployment, and cross-project matched token A/B do
  not constitute completion claims.

### Installation

Core package:

```bash
python -m pip install continuity-plane==0.1.0a8
```

Codex plugin:

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.8
codex plugin add continuity-plane@continuity-plane
```

Start a new Session after installing or upgrading the plugin. The core package
works alone; plugin state changes can only be submitted through controlled State
MCP tools.

## 0.1.0-alpha.7

### Audit Scope

- The public tag-tree diff from `v0.1.0-alpha.1` to `v0.1.0-alpha.7` contains
  `28` files: `16` added, `12` modified, and `+10,282/-94` lines.
- The corresponding development source from the alpha.1 candidate boundary to
  the alpha.7 tag source contains `27` commits. Their user-visible results are
  grouped below as installation and release, existing-project attachment, state
  and Work lifecycle, host integration, delivery gates, and documentation governance.
- The public repository is a sanitized release projection, so tag history may
  not form one linear chain. Immutable tag trees, release artifacts, and test
  results are the authority for the functional delta; commit count is not used
  as feature evidence.

### Local Runtime And CLI

- The default `local-embedded` profile keeps SQLite inside the project and
  requires no PostgreSQL, container runtime, Docmost, or network service.
- The public CLI adds the `status`, `attach`, `resume`, `checkpoint`, `work`,
  `export`, `import`, and `rollback` lifecycles.
- `attach plan/refresh/approve` imports an existing canonical MASTER/STATUS with
  source hashes, evidence, and idempotent approval; a changed source rejects a
  stale proposal.
- Local state-bundle export/import/rollback is implemented and tested for tamper
  rejection, atomic replacement, and lossless rollback. One-command cross-adapter
  profile switching is still unavailable.
- Current-only STATUS projections, bounded resume packets, immutable checkpoints,
  and sticky route application preserve the active Work, first action, return
  point, effect watermark, and acknowledged input.
- Work completion, dependency suspend/return transition, idle successor
  activation, and claim heartbeat/reclaim refresh and verify a checkpoint at one
  revisioned boundary without leaving a partial state on failure.

### Project Roots, Collaboration, And Delivery Gates

- Git common-dir binding resolves the main checkout and sibling worktrees to one
  project state instead of allowing a side Session to use another `.continuity/`.
- An active owner can atomically rebind evidence after a legitimate canonical
  source change only after actor, claim, lease, scope, and checkpoint validation.
- Delivery activation binds the source, predecessor Work, implementation evidence,
  expected Git head/ref, pending worktree delta, and exact effect set.
- Push, PR, merge, deploy, remote install, and package publication are checked for
  an active Work, claim, lease, fresh source, verified checkpoint, and scope before
  the shell action runs.
- Local `rsync` is no longer classified as a remote operation; transfers with a
  remote endpoint remain gated.

### Codex Plugin

- Added a public marketplace and plugin bundle with project-root discovery,
  bounded state injection, lifecycle checkpoints, state tools, and effect preflight.
- A new Session displays one project and authoritative revision receipt without
  exposing the complete Work, packet, or raw conversation.
- The plugin never writes the database directly. A write can only use State MCP
  tools guarded by authorization, revision/CAS, validators, claims, and checkpoints.

### Distribution, Documentation, And Verification

- Added bilingual README, usage, architecture, configuration, API, use-case,
  benchmark, large-project view, visual-product, contribution, security, branding,
  and third-party documentation.
- The release compiler emits the Python wheel, source archive, Codex plugin
  marketplace, `SHA256SUMS`, neutral templates, and privacy-scanned public history.
- Public smoke/contracts/benchmark passed `5/5`; wheel and sdist passed `twine
  check`; the public privacy scan reported `0` violations. The Linux, macOS, and
  Windows install/verify/uninstall matrix remains `18/18`.
- Benchmarks remain matched-scenario and sanitized-fixture results. Real-session
  token use, window utilization, and Work completed per compaction do not yet
  establish one universal savings rate.
- The complete Docmost connector, Obsidian Canvas/Bases, provider-neutral Context
  Health export, and one-command shared-strong deployment are not in alpha.7.

### Upgrading From Alpha.1

1. Back up the complete `.continuity/` directory.
2. Install `continuity-plane==0.1.0a7`.
3. Run `continuity verify --root .` and `continuity doctor --root .`.
4. For an existing canonical MASTER/STATUS, use `attach plan` and `attach approve`
   instead of replacing the project files with generated templates.
5. The Codex plugin is optional and new in alpha.7; core-only projects can continue
   without it.

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

Start a new Session after installing or upgrading the plugin. The core package works alone;
plugin state changes can only be submitted through controlled State MCP tools.

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
- Passed install, verify, and uninstall probes on Linux, macOS, and Windows;
  local state-bundle export/import/rollback was not available in alpha.1.
