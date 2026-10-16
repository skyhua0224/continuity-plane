# Changelog

Adoption: [deployment](docs/deployment.en.md), [recent release notes](docs/releases.en.md), [current coverage/known gaps](docs/validation.en.md). Historical entries retain their original scope. Regular GitHub Release classification does not remove alpha semantics.

## 0.1.0-alpha.26

- Attach final worker reports after early card updates; preserve newer attempts, reassignment and manual status. Replay interrupted metadata writes.
- Separate stable MCP executor_id from connection/session identity. Card-projected Todo activation uses an explicit assignee and refuses unidentified activation or another executor's takeover.
- Supply complete hook commands from known context, deduplicate oversized-checklist advice and avoid a second automatic Todo for card-based work.
- Import/export source-index.tsv with stable keys, extra columns, atomic conflict rejection and candidate declarations. Never overwrite the repository index or imply sources were read.
- Route MCP lookup through an explicit exact-root SSH bridge mapping. Do not forward local cache/data paths, read native account data or expose arbitrary remote process administration.

## 0.1.0-alpha.25

- Forward dispatch/resume timeouts and provider effort; filter open-card Todo projections; accept OR event waits, atomic body-only updates and batch metrics.
- Add complete rule text export/mirror drift checks, bounded hash-bound code routes, project lessons, session/batch binding and prospective observed-token deltas.
- Separate authoritative integration refs from checkout HEAD; refresh structured controller handoffs before controller compaction and after batch records, shared across new sessions.
- Explicit project controller fencing, atomic dispatch reservations, host/profile/client-family slots and serialized controller run. Guarded ledger v4 blocks older dispatchers; arbitrary Git outside the tool is not intercepted.
- Observe native Claude subagent lifecycle without copying transcripts or inferring exit codes/success. Controlled hook tests do not prove natural live subagent coverage or longitudinal savings.
- Upgrade every participating CLI/MCP dispatcher before explicitly enabling controller fencing. Installation does not enable guarded ledger v4 automatically; reconnect existing MCP sessions after upgrade.
- Revalidate batch writes under the controller/ledger lock order; identical-record retries repair handoff projections. Fallback swaps reservations atomically and preserves finished results when lease/capacity prevents replacement. Late results cannot overwrite manual operation acknowledgements; compaction reports deferred shared-handoff refreshes without blocking local saves.

## 0.1.0-alpha.24

- Validate cards before spawning; project current-attempt finishes to review/blocked with late-result protection and durable outbox replay. Concurrent event cursors, pagination and quiet waits preserve undelivered events.
- Add dispatch/resume/report, durable report attachments, bounded same-thread transient recovery, actual-run balancing and cooldowns. Authentication failures, policy refusals and incomplete output do not trigger automatic provider switching.
- Project Todo from cards; add commit-bound structured verification, single-update merged, integration batches, source-bound decisions and generated status. Metadata does not authorize actual merging, pushing or deployment.
- Deliver global/project audience-scoped rules with versions, hashed attachments, complete exports, launch snapshots and affected-worker queries. Assemble prompts from cards/rules and keep primary-source references outside Git.
- Share planning entry points across CLI, MCP, help, Skills and hooks; provide brief retrieval, strict budgets, changed-only deduplicated notices and controller-scene recovery.
- Support explicit SSH hook/CLI endpoints and fix installed host-hook --config routing. Real owner-host Claude manual /compact passed; automatic-threshold and longitudinal business-session effects remain unverified.
- Ingest real host token/cache metadata in place; distinguish injection overhead, byte estimates and unknown prices. No universal savings or actual-cost claim.
- Enforce owning-host/profile routing and inspect persistent work storage without copying credentials, private configuration or native session stores. Public exports refuse nonempty destinations.
- Withdraw the native Claude automatic quota-query plan by maintainer decision; no quota polling service is enabled. Explicit records and approved handoffs remain. No external account-system integration.

## 0.1.0-alpha.23

- Share release identity across CLI and both MCP servers; replace stale alpha20 handshakes and detect package upgrades under live processes with an explicit restart hint, without restricting ordinary project work.

- Shared bounded session handoffs for Codex and Claude: save before compaction, quietly continue afterward, deduplicate recovery events and isolate sessions.
- Ship Claude manifest, MCP configuration and lifecycle hooks. `continuity-host-hook` uses the installed core interpreter for checkpoint create/verify.
- Include hook-injected bytes in signed net estimates by session/command; measure actual CLI brief/JSON output and report without a State profile.
- Carry executor preferences in task/Todo projections for routine, deep flexible, fast and capacity-blocked tasks. Explicit task preferences survive compaction.
- Balance suitable routine work across Sonnet/Sol/Grok/GLM least-planned-first and repair historical skew; purpose breaks ties. Reserve the Astra/Opus pool for hard work/adversarial review. Carry effort guidance across hosts; saves are atomic/idempotent and reads do not affect balancing.
- Add overridable scenario presets, congestion deprioritization and unavailable-model exclusions, with independent-split/handoff guidance and coupled-task safeguards. Suggestions neither dispatch nor block local work.
- Align the core, three Codex plugins and Claude plugin versions. Hosts remain responsible for enabling plugins and reloading running processes.

## 0.1.0-alpha.22

- Index C/C++/Objective-C functions, structs, enums, typedefs, macros and configuration keys; multi-term queries use AND semantics.
- Collaboration task list defaults to summaries with filters and byte budgets; add dispatched/completed/merged lifecycle and branch/commit/verification fields.
- Stale source or lease recovery is visibly marked `[STALE RECOVERY]` and starts with current-state inspection.
- Packets combine bounded in-flight cards and TodoQueue; add a fail-open host lifecycle bridge example.
- Record retrieval response bytes, estimated grep bytes and cache status locally; expose aggregates in observe reports.
- `continuity status --root ROOT` renders directly. Usage-driven provider handoff remains disabled.

## 0.1.0-alpha.21

- Add source-bound `work add`, `decision add` and `constraint add`: preview by default, explicit State MCP/CAS apply, refreshed checkpoint/projection and idempotent replay.
- Allow assignee changes through task update; count active cards per lane, support child identities and fix locking across atomic document replacement.
- Resolve mixed-language, spaced, full-width and case-folded aliases without partial Latin identifier matches.
- Add durable CLI/MCP inbox delivery/acknowledgement and bounded packet/hook hints, without forced host wake-up or writer takeover.
- Return concise structured diagnostics for expected CLI errors, support direct status rendering, and document running-client MCP reload/tool-visibility checks.
- Align the core package and all three Codex plugins. The HTML prototype is excluded. No unmeasured token-saving claims are made.

[中文](CHANGELOG.md)

## 0.1.0-alpha.20

- Long-command progress guidance is silent by default, so successful tools do not trigger periodic interim reports.
- Added concise technical Chinese/STE100-style output rules as a report renderer, not a resident skill or execution gate.

## 0.1.0-alpha.19

- MASTER plan routing is now bound into TodoQueue; an old Work no longer overrides the current plan order.
- A changed plan hash marks the queue for reconciliation while preserving completed evidence and real blockers.
- Added read-only `continuity plan inspect` for the current MASTER path, hash, and bounded section excerpt.

## 0.1.0-alpha.18

- Exposed `continuity_todo_compile/list/complete/block` over MCP so an Agent can advance the ordered queue directly.
- TodoQueue tools write only the project sidecar; they do not write State/SQLite or grant external-effect authority.

## 0.1.0-alpha.17

- Added an intent TodoQueue that decomposes multi-item user requests, persists them locally,
  advances them in order, and switches to the next item after completion.
- The queue is a project sidecar: it does not write SQLite or grant claim, completion, or external-effect authority; read-only State does not stop ordinary code/tests.
- UserPromptSubmit injects the current todo and exact completion command; the Stop hook continues while the queue remains active.

## 0.1.0-alpha.16

- Added a guarded Stop hook that creates one continuation when explicit execution intent,
  fresh writable State, an active Work, and a resumable next action are all present.
- Stale or read-only State, root mismatches, open blockers, and Stop re-entry remain silent to
  prevent loops and accidental continuation in the wrong repository.
- Added lifecycle tests for continuation, re-entry protection, and stale/read-only no-op behavior.

## 0.1.0-alpha.15

- Improved continuity_work_activate validation errors with missing fields, unknown fields,
  standard/delivery conflicts, and expected-head format diagnostics.
- Queued task packets now explicitly say they cannot replace authoritative Work; older Sessions
  receive one compatibility hint even when the new MCP tool is not visible.

## 0.1.0-alpha.14

- Added a provider-neutral collaboration ledger with project registration, task cards, temporary
  assignees, ready-for-review, and a read-only effect request queue. Collaboration metadata grants
  no completion, push, merge, or deployment authority; local work continues when it is damaged.
- Added global/project vocabulary memory and bounded collaboration packets. Project aliases override
  global aliases, plaintext secrets are rejected, and the Codex UserPromptSubmit hook injects a small
  packet only once when a task or vocabulary entry matches.
- Added report-only skills-audit and worktree-inventory commands for metadata budgets, duplicate
  or malformed skills, dirty worktrees, and cleanup candidates. They never edit host configuration or
  delete Git objects.
- Doctor now reports collaboration ledger health separately from State and packet health. Collaboration
  degradation never turns a project read-only.
- Added the read-only continuity_collaboration_packet MCP tool for Codex and other clients
  to consume the same bounded task and vocabulary packet.

## 0.1.0-alpha.13

- The development installer limits edits to Continuity integration fields and uses read-only
  fingerprints to protect provider credentials, auth files, models, routing, and independent profiles.
  Common snippets reject authentication data; concurrent changes never trigger whole-file rollback.
  This installer is not a business-command gate. The native probe verifies actual plugin trust and
  waits for the compaction turn to finish before continuing.
- Added independent advisory pre/post tool hooks. Ordinary successes use `1/16` sampling; failures,
  recognized effects, and Continuity MCP calls retain observations. This entry cannot access State,
  run Git, rewrite input, or reject commands, even with a leftover `strict` environment setting.
  Records contain no commands, source code, or response bodies.
- If the compact recovery entry is missed, the next tool gets one retrieval hint of at most `1 KiB`;
  if a user message arrives first, it attempts current resume once. Markers are Session/project
  scoped, expire after 10 minutes, and are atomically consumed or cleared after delivery. Failure
  continues ordinary work without reinstalling plugins, editing Codex configuration, or reviving
  unverified Work.
- Doctor matches observations to the requested project and distinguishes successful recovery from
  degraded guidance; installation alone is not proof of adoption.
- The advisory script works with the released alpha12 launcher without requiring a new CLI argument.
  Observations follow explicit working directories, free-text labels are bounded, and unverified
  checkpoints or expired leases cannot inject an active Work into context.
- Added a native-host probe that checks hook discovery before invoking real compaction. Untrusted or
  unloaded hooks do not count as adoption. Actual host windows and configured limits are recorded
  separately, and cache hits are not treated as token savings.
- The hook launcher now falls back from a deleted plugin-cache path to the current `PLUGIN_ROOT`.
  With no usable script it exits quietly, so stale caches do not turn ordinary operations into tool
  errors or cause an untrusted version to be loaded.
- The installer now detects Codex configuration takeover by cc-switch and returns `deferred` before
  any write. It never races provider projection or reports a transient marketplace, hook, or MCP
  write as persistently active.
- A fully uninstalled cc-switch is now a supported machine state, while partial leftovers remain
  fail-closed. OpenCodex-managed Codex config/catalog and stable systemd/basic-auth surfaces are
  covered by process-keyed HMAC fingerprints; concurrent drift aborts the Continuity installation.
- Clarified retrieval fallback guidance: use CLI for an unavailable tool and narrow search after a
  successful empty lookup. MCP metadata labels empty results while preserving receipt hashes and
  byte accounting. This does not establish host tool visibility or real-task acceptance.
- Fixed a live-handle adoption gap for long tests and builds: when advisory `PostToolUse` sees a
  still-running shell `session_id`, it emits one `write_stdin` polling hint before new reads or
  progress reports. It does not call State, acquire a claim, or block commands; completed commands
  remain silent.
- Doctor now resolves the plugin selected by the marketplace and validates all six hook commands,
  script entrypoints, no-op stubs, package version, the 80% active-lease warning, and resume packet
  size. A stale cache can no longer be reported as ready. The interaction cursor preserves explicit
  continue/execute/finish requests as continuations while only information requests answer in place;
  PostCompact only verifies the canary, leaving one recovery injection at compact SessionStart.
- Migration note: a `remain-read-only` state written by an old version requires one controlled reclaim
  event before it recovers; the new version no longer produces that default wait state. Projects with
  no legacy State need no extra migration command.

### Installation


~~~bash
python -m pip install --upgrade continuity-plane==0.1.0a13
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.13
codex plugin add continuity-plane@continuity-plane
codex plugin add continuity-plane-search@continuity-plane
codex plugin add continuity-plane-state@continuity-plane
~~~

Start a new Session after installation so the new manifest, MCP tool table, and hook hashes load.

## 0.1.0-alpha.12

### Changes Since 0.1.0-alpha.11

- Integrated idle source rebinding, checkpoint-bound source/evidence validation, and compaction
  recovery fixes. When a source changes, recovery reports the exact gate instead of silently
  injecting an old Work into the current Session.
- Added the `continuity-codex-hook` entry point. Hooks reuse the installed Python runtime and no
  longer depend on stale cache paths or platform-specific `python3`/`py -3` aliases.
- Replanning unchanged canonical bytes now creates current attach evidence. A fully bound delivery
  Work can use controlled rebinding only after source, workspace, predecessor, expected ref, and
  effect checks pass.
- Large-repository startup checks read Git file lists in bounded chunks, reducing hook peak memory
  and context input. `SessionStart(source=compact)` continues to use a bounded packet without
  reinjecting the full history.
- Retained alpha11's zero-blocking core, optional search/State MCP profiles, incremental index,
  non-blocking checkpoint lifecycle, and atomic Work transition. Ordinary edits, builds, tests, and
  reads continue when State writes are temporarily unavailable.

### Verification And Boundaries

- Focused recovery, activation, MCP binding, plugin, and public-build tests pass `91/91`; the public
  mirror contains `148` files, public tests pass `52/52`, and privacy/dependency/repository checks
  pass.
- Alpha12 is published to PyPI and GitHub Releases. Current matched A/B runs do not meet the uniform
  `30%` input-token gate, so no universal token-savings claim is made.

### Installation

```bash
python -m pip install --upgrade continuity-plane==0.1.0a12
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.12
codex plugin add continuity-plane@continuity-plane
codex plugin add continuity-plane-search@continuity-plane
codex plugin add continuity-plane-state@continuity-plane
```

Start a new Session after installing the plugins so the new manifest, MCP tool list, and hook hashes
are loaded.

## 0.1.0-alpha.11

### Changes Since 0.1.0-alpha.10

- Ordinary code edits, builds, tests, and reads now continue when Continuity State writes are
  temporarily unavailable. MCP results scope the condition with
  `read_only_scope=continuity-state`, `ordinary_project_work_allowed=true`, and
  `project_next_action=continue-ordinary-project-work`; generated STATUS no longer presents the
  whole Session as read-only.
- The lifecycle adapter no longer injects an old Work when a claim expires, canonical sources are
  stale, or a projection lags. Startup, compaction, and adapter failures remain non-blocking. The
  core still registers only `SessionStart`, `PreCompact`, and `PostCompact`, with no command hook.
- `continuity init` now creates a proposal, genesis Event, and verified checkpoint as one retryable
  initialization boundary. A checkpoint publication failure removes only that attempt's files, and
  legacy revision-0 projects remain attachable.
- Added a repository-isolated incremental code index over Git-tracked files. It reuses unchanged
  files and returns only path, line, symbol, and file-hash references. The optional search plugin
  exposes one `continuity_context_lookup` MCP tool without loading a Skill or creating State binding.
- The State plugin is MCP-only, so installing it no longer makes ordinary questions load a State
  Skill or call inspect. `continuity doctor` now verifies State MCP and Search MCP adoption
  independently.
- Fixed the public wheel to install the `continuity-search-mcp` entry point. PyPI installations and
  local development builds now expose the same search-plugin command contract.

### Verification And Boundaries

- The full suite passes `2037/2037`, with `31` environment skips. The affected surface passes
  `134/134`; the 142-file public build, repository verifier, privacy scan, real init/inspect, and
  stale-State MCP smoke all pass.
- Existing matched A/B input-token reductions remain below the `30%` target. Alpha.11 makes no
  universal token-savings claim and does not treat cache hits as proof of lower context cost.
- Under the official Codex plugin loading model, new tools become available in a new Session after
  installation. An already-running Session must start a new Session to reliably load the alpha.11
  plugin manifest and MCP tool list.

### Installation

```bash
python -m pip install --upgrade continuity-plane==0.1.0a11
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.11
codex plugin add continuity-plane@continuity-plane
```

Install `continuity-plane-search` for large-repository lookup and
`continuity-plane-state` only for explicit State operations. Start a new Session after upgrading.

## 0.1.0-alpha.10

### Changes Since 0.1.0-alpha.9

- Split the Codex integration into a lightweight core, an optional bounded-search profile, and
  an optional State MCP profile. The core registers only `SessionStart`, `PreCompact`, and
  `PostCompact`; it registers no `PreToolUse`, `PostToolUse`, or command-effect gate.
- The core now defaults to non-blocking `auto`: it creates a checkpoint before compaction and
  verifies a canary afterward. Recovery failures, stale sources, and unavailable State adapters
  are recorded and normal development continues without making the business Session read-only.
- Compaction recovery uses Codex native continuation instead of reinjecting a full Execution
  Packet after automatic compaction, reducing model-visible recovery input and repeated answers.
- Added `continuity context search`, a bounded current-worktree search with a complete JSON receipt
  bound to the Git revision, file hash, line hash, and output budget. Automatic Skill adoption still
  requires a project/task matched evidence gate.
- Added streaming sanitized provider JSONL observation and matched A/B comparison tools covering
  input/output tokens, tool output, Skill/governance reads, compaction chains, and repeated answers;
  raw transcripts never enter Git.

### Verification And Boundaries

- Core profile, State MCP, lifecycle, and public-builder focused tests pass; three repositories
  reached the shell for `6/6` no-effect probes, with zero command-effect gates.
- Two real matched A/B profiles each have `3+3` samples: median input tokens decreased `12.14%`
  and `2.36%`; median output tokens decreased `28.84%` and `13.70%`; tool output decreased
  `41.46%` for one profile and increased `2.62%` for the other. Both consistency veto counts are
  `0`, but the `>=30%` input gate did not pass, so alpha.10 makes no universal token-savings claim.
- Alpha.10 remains a prerelease. Natural `1M/900K` long-session compaction interval, recovery-read,
  and accepted-Work results remain under long-running measurement; the Docmost connector, Obsidian
  Canvas/Bases, and shared-strong deployment remain planned capabilities.

### Installation

Core package:

```bash
python -m pip install continuity-plane==0.1.0a10
```

Codex plugin:

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.10
codex plugin add continuity-plane@continuity-plane
```

Install `continuity-plane-search` separately for large-repository lookup and
`continuity-plane-state` only when explicit State MCP tools are needed. Start a new Session after
installing or upgrading the plugin.

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
