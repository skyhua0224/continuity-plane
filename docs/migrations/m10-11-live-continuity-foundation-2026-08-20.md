# M10-11 Live Continuity Foundation

Version: 4  
Date: 2026-08-23  
Status: implementation and local lifecycle verification complete; matched live gate open

```yaml
document_id: context.m10-11-live-continuity-foundation
document_revision: 4
change_type: evidence
authority_ref: verification-run://m10-11/foundation/revision-1
supersedes: null
affected_tasks: [M5-02, M5-03, M5-05, M5-08, M10-01, M10-11]
next_review: M10-11
```

## Scope

M10-11 now has strict contracts for a pre-registered study, sanitized provider
segments, a derived comparison report, open provider capability and event
adapters, interaction cursors, and checkpoint-bound recovery envelopes. The
comparison implementation derives its verdict from source-bound inputs. It does
not accept a caller-supplied improvement result.

The Codex integration uses the official `PreCompact`, `PostCompact`, and compact
`SessionStart` lifecycle events documented at
`https://learn.chatgpt.com/docs/hooks.md`. `PreCompact` creates a checkpoint
without model-visible output. `PostCompact` verifies the checkpoint and stops
continuation when the canary fails. Compact `SessionStart` injects one bounded
Recovery Envelope.

## Implemented Gates

| Gate | Result |
|---|---:|
| live continuity wire contracts | `7` strict schemas registered with exact content hashes |
| matched sample minimum | baseline/candidate `>=3` per exact match key |
| match key | provider/model/reasoning/window/threshold/cache/tool/sandbox/adapter/repository/State/Profile/task/verification |
| portability claim | requires `>=2` project profiles, provider contracts, and collaboration modes |
| packet budget | `<=12,288 bytes` |
| recovery re-read policy | Skill/document/code/memory rereads after compaction are vetoes |
| consistency vetoes | active Work, claim, revision, first action, acknowledged input replay |
| response vetoes | direct answer `<100%`, recovery narration, unrequested table |
| improvement gates | input and total tokens/accepted Work, Work/compaction, controlled context ratio `>=30%`; output tokens/Work `>=10%` |
| authority | study, segment, report, cursor, envelope, adapter all have State/completion authority `false` |

Accepted Work must carry a unique State completion receipt. A segment that is
missing provider usage, token attribution, accepted Work, a typed compaction
boundary, or an eligible question response produces `insufficient-evidence`.
Byte counts cannot substitute for provider tokens.

## Codex Lifecycle Verification

The installed local development plugin is
`continuity-plane 0.1.0-alpha.2+codex.20260823103338`. The plugin source is
versioned under `integrations/codex/continuity-plane`; the personal marketplace
cache is a generated installation target.

One real local State/CLI/hook smoke used the current self-dogfood project and a
sanitized cursor derived from a Codex rollout tail:

| Metric | Result |
|---|---:|
| Recovery Envelope bytes | `3,037` |
| checkpoint verified | `true` |
| interaction cursor present | `true` |
| confirmed current inputs | `1` |
| `no_restate` | `true` |
| response mode | `continue-silently` |
| Skill lock | `measured`; `8` stable rule IDs |
| raw response text in envelope | `false` |

The adapter reads at most a 2 MiB rollout tail for the interaction cursor,
stores only content hashes and cursor state, and never admits the transcript to
Git or Typed State. The Codex rollout metric adapter processes the provider
archive as a single stream and retains counters, event hashes, and message
counts rather than transcript text.

## Provider Measurement Correction

The M10-00 700K compaction result is a valid historical 950K-window observation,
but its generated receipt used a text search and bound `compaction_events` to an
`item.completed/error` event. Re-reading the exact source rollout through the
strict M10-11 adapter produced:

| Metric | Result |
|---|---:|
| effective context window | `950,000` provider-reported tokens |
| explicit compaction events | `1` `event_msg/context_compacted` |
| pre-compact last input | `783,628` tokens |
| compacted context | `24,776` total-only tokens |
| first post-compact input | `59,172` tokens |

The numerical result remains historical evidence. The old receipt's
`compaction_events` array is not accepted as event provenance by M10-11. The
separate M10-00 default-catalog thread reported `258,400` tokens and three
explicit compactions; it is not a 1M-window sample.

A new Codex preflight with the fixed catalog reported `950,000`, one explicit
compaction, and a first post-compact input of `59,177` tokens. `codex exec`
reported `plugins_instructions=false` even with the `plugins` and `hooks`
features enabled, so that surface did not load the personal plugin and cannot
serve as a candidate arm. This absence is recorded as an adapter capability
gap rather than a zero-value improvement.

## Claude Baseline

Claude Code `2.1.222` exposes `--autocompact`, `stream-json`, hook events and
explicit plugin directories. A live print-mode preflight reached the provider
but returned API status `403` with zero usage. The live candidate is therefore
`unavailable`; zero tokens are not admitted as a measured result.

The streaming Claude archive adapter was also run against a historical real
Claude Code session. It deduplicates repeated content-block rows by
`message.id`, retains only usage/event hashes, and maps cache creation/read
without retaining message or compaction-summary text:

| Metric | Result |
|---|---:|
| unique assistant messages | `96` |
| explicit compactions | `1` auto `system/compact_boundary` |
| pre/post compaction | `1,001,838 -> 13,041` tokens |
| compaction duration | `171,480 ms` |
| normalized total input | `13,094,365` tokens |
| cache read/write | `12,854,111 / 226,325` tokens |
| output | `104,387` tokens |
| reasoning output | `unavailable` |

This is a second-provider baseline, not a matched candidate. The provider usage
contract permits an unavailable reasoning-token field rather than fabricating
zero.

## Security And Portability

The provider core accepts an open provider contract ID; a new provider can
register an external adapter without editing a provider enum. Unknown or
missing telemetry remains unavailable.

The Codex MCP server uses the process startup root only when that directory is
already a Continuity project. A global plugin process starts inside the plugin
cache, so its first `continuity_resume` call must provide an absolute project
root and establishes the immutable MCP-session binding. Write-before-bind,
cross-project roots, mismatched actor/claim/Work, and read-only envelopes are
rejected before the CLI runs. The plugin manifest declares both Read and Write,
matching the tools it exposes.

## Heartbeat Incident And Repair

Platform reported `state changed concurrently` while `resume` still showed a
valid writable claim. The State event log showed that revision `35/35` was a
successful heartbeat, while the local CLI reused the fixed request/event
identity `heartbeat-claim-n-69-09-live` for later renewals. State MCP correctly
rejected the reused identity as a different intent. This was an adapter
idempotency defect, not silent concurrent overwrite or SQLite corruption.

The repair binds each local recovery request to action, claim, expected
revision, lease TTL, and successor identity. A retry at the same expected
revision remains idempotent; the next heartbeat creates a new Event. The
regression test covers two consecutive CLI heartbeats (`revision 2 -> 3 -> 4`)
and distinct Event IDs. Commit `671e6a4` is deployed to the Skyinux development
CLI. The expired Platform claim was reclaimed through the normal State path as
`claim-n-69-09-reclaimed-2` at revision/event `36/36`.

## MCP Project-Root Incident And Repair

The Platform pilot exposed a second adapter defect after the plugin was enabled
globally. Codex correctly launched each stdio server from the installed plugin
cache. The server incorrectly treated that process directory as the active
project root, so a valid Platform heartbeat was rejected with MCP error
`-32000` before the CLI or State store ran. Platform remained read-only and
continued measurement-only work; the claim, checkpoint and State database were
not damaged.

[OpenAI Codex issue #37903](https://github.com/openai/codex/issues/37903)
records the current host limitation: plugin MCP launchers receive no workspace
root substitution or environment value, and the Codex MCP client does not
advertise the protocol `roots` capability. The explicit first-resume binding is
the local adapter boundary until Codex exposes a host-attested project root.

The previous handshake test reproduced the plugin-cache launch but stopped
after `initialize`. Functional tool tests started the same server from a
synthetic project directory, omitting the installed-plugin condition. The new
regression starts from a separate plugin-cache directory, establishes the root
with one read-only resume, performs a bound heartbeat, and rejects a second
valid project root. A separate gate proves the initial resume performs one CLI
State read rather than two. Focused binding and lifecycle tests pass `6/6` and
`9/9`; a real plugin-cache-shaped read smoke resolved Platform revision `75`,
Work `N-69-06`, the current claim and `read_only=false`.

The original task retained its conversation but also retained the deleted MCP
transport after the first plugin upgrade. Repeated calls returned `Transport
closed` until the task-local `/plugins` control disabled and re-enabled
Continuity, which restarted stdio processes from the current cache without
creating another task. A compatibility cache alias keeps the task-pinned Skill
path valid during this approved migration.

The same live run exposed a stale-checkpoint loop: heartbeat advanced State
revision while the following checkpoint tool required the preceding resume
binding, and resume correctly rejected the old checkpoint. Plugin
`0.1.0-alpha.2+codex.20260823103338` makes heartbeat/reclaim and checkpoint
refresh one adapter operation. Failure injection verifies that a checkpoint
refresh error marks the complete operation failed. The live Platform gate then
advanced `76 -> 77`, produced checkpoint
`artifact://sha256/2ad3285ab126ecc9416621cddb8fca4e45f7218ac6ea42a598260c753ba01785`,
and independently resumed with `checkpoint_verified=true`,
`lease_valid=true`, and `read_only=false`.

## Checkpoint-Bound Dependency Transition

Platform N-69-06 reached a durable network-control boundary while its final
cross-machine gate remained blocked by file Backend latency and source-prefetch
starvation. Completing N-69-06 would have asserted an unpassed performance gate;
expanding its `network-cc-reliable` claim into file transfer would have bypassed
scope ownership. The local adapter previously exposed only complete, activate,
and claim recovery, while the provider-neutral route core already supported a
checkpoint-bound suspend/switch event.

`continuity work suspend-dependency` now exposes that route with expected
revision, current claim/actor verification, source freshness, checkpoint
verification, State authorization, scope conflict validation and final
checkpoint publication. Legacy v1 projects store the same CAS changes as a
legacy state-transition event; v2+ projects retain native task-transition
metadata. The suspended Work remains incomplete and carries an open typed
blocker; the prerequisite is parented to it and the resume packet projects the
parent as the return point.

Focused route, event and CLI gates pass `41/41`. The live Platform transition
used source commit `c98e74dc` and checkpoint
`artifact://sha256/e8b191101effba6f1ed81b8c012cd1afd609f0fa9e67b33a7aaba427245057df`.
State advanced `80 -> 82`: N-69-06 is `ready` with blocker
`blocker-dependency-a4ee90fb27c69d03`, its network claim is released, and
N-69-09-IO is active under claim
`claim-route-592b33d74724c2a47cc1ae84acac01c6e77e39135d7d99ed7ada4d064aba78ae`
with sole scope `capability:filetransfer-transfer`. The verified final checkpoint
is `artifact://sha256/df45fe2f1a7f5fa6a07e6851b4abdaf63f4fd6b7747d9c7b00ba54f7f7b8c1c7`;
the normal resume packet reports return point N-69-06 and `read_only=false`.
The installed local candidate wheel SHA-256 is
`e4261728b11c6328c1d1d6bbfb5421a0355564b906a835db25762cc82418448c`.

## Revision 6 Autonomous Dependency Completion

The local runtime now exposes
`context.state.work.transition` and the Codex
`continuity_work_transition` MCP tool. The request binds the current revision,
active Work and claim owner, verified checkpoint and evidence, current source
proposal, clean Git workspace and expected commit, declared parent return point,
unchanged successor scope, resolved dependency blocker and optional remaining
blocker.

One State Event applies the following changes under one CAS revision:

- the dependency Work becomes completed and receives its evidence;
- its active claim becomes released;
- the dependency blocker becomes resolved;
- the predeclared parent becomes active;
- one successor claim is issued with a refreshed lease;
- an unresolved independent gate remains an open typed blocker when supplied.

The candidate checkpoint is published and restored before the State Event is
committed. A pending checkpoint reference permits crash recovery between
artifact publication and reference promotion. Injected checkpoint publication,
dirty-workspace and SQLite after-event-insert failures preserved the original
snapshot, event head and revision. Each failure returned a named `failed_gate`.
The isolated N-69 dependency fixture advanced revision/event `4/4 -> 5/5` with
one active Work, one active claim and one new Event.

The Platform business Session independently invoked the alpha.3 MCP tool after
the original return had already reached revision/event `88/88`. The result was
`already-transitioned`; `completion_policy.status=granted`, authority was
`checkpoint-bound-local-transition`, and `scope_expanded=false`. Revision and
event remained `88/88`, proving that the live retry added no State write. The
active Work remained N-69-06 under `claim-n-69-06-return-io-1`; checkpoint,
source and lease were valid, and `read_only=false`. The external Windows
physical-path blocker remained open.

The first plugin upgrade also exposed a host lifecycle defect: loaded threads
retained a versioned plugin-cache working directory after the installer removed
that directory. Alpha.4 moves the MCP server into the wheel and exposes the
stable `continuity-mcp` console entry. The plugin MCP configuration no longer
contains a version-directory `cwd` or script path. A handshake test starts the
same server from an unrelated temporary directory.

The installed candidates are CLI `0.1.0a4`, wheel SHA-256
`e4894a71499503b35f79abee5d250fed7250bdf9bc9cd8cf836c984ad06fa82f`,
and Codex plugin `0.1.0-alpha.4+codex.20260823134124`. The live autonomous
transition proof belongs to alpha.3; alpha.4 changes only MCP launcher
durability and preserves the validated transition contract. The structured
receipt is
[`m10-11-autonomous-work-transition-results.json`](../../experiments/evidence/m10-11-autonomous-work-transition-results.json).
Focused atomic, MCP, recovery and schema gates passed `115/115` with one
conditional adapter skip. The full repository release runner executed `1,926`
tests in `367.926 s` with failure/error `0`; receipt SHA-256 is
`f339c0b09d637dec99c04e9737765075491261cabb5835f894557226b2c1671d`.

Direct CLI claim recovery now publishes and verifies the new checkpoint before
returning. MCP recovery delegates to that single CLI operation and no longer
adds a second checkpoint command. The regression executes two consecutive
heartbeats and resumes at the second revision without a stale-checkpoint error.

## Revision 7 Idle State And Atomic Activation

Completing the only active Work previously produced a valid typed State with no
active Work or claim, while recovery required exactly one of each. The MCP
binding depended on recovery, and successor activation depended on that binding.
This created a terminal-to-successor deadlock.

The recovery envelope now accepts two authority states:

- active: one primary active Work and one matching active claim;
- idle: `active_work=null`, `claim=null`, no active claim, and
  `next_action=activate-next-work`.

Idle recovery verifies source and checkpoint, sets `lease_valid=true` for the
claim-free state, and remains writable when source evidence is current. A stale
checkpoint created by an older completion adapter is repaired from current
State only for this validated idle case. Missing or corrupt checkpoint content
continues to fail closed.

`context.state.work.activate` now creates or activates the source-bound Work,
issues one claim and derives the primary projection in one Event and one CAS
revision. Its candidate checkpoint is published and restored before the Event
commit. A checkpoint-publication fault leaves the idle snapshot and event head
unchanged. `continuity_work_complete` also publishes the terminal checkpoint
before returning, so new completions do not require idle recovery repair.

The isolated full-chain fixture completed the last active Work at revision 3,
resumed idle, and activated its successor at revision 4. The activation added
one Event, left one active Work and one active claim, and produced a verified
checkpoint. No external Session or direct SQLite write was used.

The Platform business Session executed the alpha.5 chain independently. Its
first resume returned idle revision/event `95/95`, null Work and claim,
`activate-next-work`, current source and checkpoint, and `read_only=false`.
`continuity_work_activate` then added exactly one Event and returned revision
`96/96`, active Work N-69-07, claim `claim-n-69-07`, both requested scopes and a
verified checkpoint. The Session continued the ECN/BBR verification without a
control-plane State write.

The installed candidates are CLI `0.1.0a5`, wheel SHA-256
`d0690c751eb83dc06e7243c6690fbb2ff973c13301088c11982c9333c76f4a75`,
and Codex plugin `0.1.0-alpha.5+codex.20260823152725`. Focused idle, atomic,
MCP and recovery gates pass `120/120` with one conditional adapter skip. The
full repository release runner executed `1,932` tests in `353.122 s` with
failure/error `0`; receipt SHA-256 is
`2a7cbe608a99e2e43c92bc8a040bbb93e4414772551bfc1fa8d3484b0a70391e`.

## Read-Only Expired-Claim Recovery Binding Repair

The Platform pilot later resumed revision `111`, Work N-69-08 and claim
`claim-n-69-08` with current source and checkpoint but an expired lease. The
Recovery Envelope correctly returned `lease_valid=false`, `read_only=true` and
`remain-read-only`. The same bound MCP Session then submitted a reclaim request,
but the adapter applied the generic read-only write gate before inspecting the
recovery action and returned MCP error `-32002`. The State recovery transaction
was never invoked. This ordering made controlled reclaim depend on an external
Session.

The alpha.6 binding gate admits one read-only write path: `reclaim` for the
bound active claim when source and checkpoint are verified, the lease alone is
invalid, `next_action=remain-read-only`, and actor/claim identity matches
exactly. Checkpoint creation, completion, transition, activation and heartbeat
remain denied while read-only. Unknown recovery fields, including caller-supplied
scope, are rejected; the CLI derives scope from authoritative claim State.

The successor identity supports an idempotent replay only when the old expired
claim and the current active successor have the same actor, Work, scope and
reclaim timestamp. Replay verifies the current checkpoint and returns without a
new State Event. Wrong actor, wrong claim, scope injection, stale source,
unverified checkpoint and non-expired reclaim remain fail closed.

Claim recovery now publishes and restores the candidate checkpoint before the
State CAS. A publication or restore failure removes the pending checkpoint and
leaves revision, Event head and claims unchanged. The final checkpoint pointer
is promoted only after the State Event commits, using the same pre-commit
publication contract as atomic activation and dependency transition.

The regression starts the real stdio MCP server against a CLI-created temporary
project; no SQLite row is edited by the test. It resumes an expired claim in
read-only mode, proves every other write is denied, reclaims one successor,
resumes with `source_fresh=true`, `checkpoint_verified=true`,
`lease_valid=true`, `read_only=false`, replays the reclaim without advancing
revision, then heartbeats the successor and verifies the refreshed checkpoint.

CLI `0.1.0a6` wheel SHA-256 is
`0080270f5ff8c190a10798cc337415a7f4a0187dee153795bd6dbe9410d69e3c`;
the sdist SHA-256 is
`a92a544302f9b667fe221afe7bf2c69836a2bf9aedfd3448404e30c77564dcd2`.
Codex plugin `0.1.0-alpha.6+codex.20260823220129` is installed and enabled from
the personal marketplace. The alpha.6 targeted set passed `57/57`; full
discovery passed `1,934` tests with `31` conditional skips in `348.445 s` and no
failure or error. No Platform repository file or Continuity State was modified
during the repair.

## Revision 4 Implementation And Pilot Update

The local implementation now includes the following source-bound behavior:

- Codex SessionStart runs a no-State-write `attach refresh` when the proposal is
  structurally valid but its MASTER or STATUS source hash is stale. It stops with
  an explicit governance-approval requirement; the hook does not change State or
  create a checkpoint. Approval, checkpoint creation and resume remain a
  user-controlled transaction. Refresh failure or malformed packets remain
  read-only.
- SessionStart hook output uses the documented `5,000` token additional-context
  threshold, while the recovery contract remains bounded to `12,288` bytes.
- Hook observations include plugin manifest and hook contract digests, without
  session IDs, turn IDs or transcript text.
- Codex rollout inspection distinguishes cumulative interval deltas from
  request-level input/cache/output usage and accepts an explicit observation
  interval with a prior cumulative baseline.
- `continuity status render` writes `STATUS.current.md`, `STATUS.current.en.md`
  and a signed-by-digest, zero-authority projection metadata file. It never
  overwrites the governance-bound STATUS source.

Focused implementation gates are green: lifecycle `9/9`, rollout adapter `6/6`,
release CLI `12/12`, and status projection `4/4`.

The same revision also re-ran the collaboration and project-view receipts:

| Receipt | Result |
|---|---:|
| Shared Work/claim/lease | `10,000/10,000`; silent overwrite `0`; duplicate effect `0`; p95 `0.559859 ms` |
| Forge projection/replay/conflict | `2,000/2,000` mappings and replays; stale ref rejection `2,000/2,000`; authority escalation `0` |
| Notification delivery | publish `1,000/1,000`; dual delivery `1,000/1,000`; offline catch-up `2,000/2,000`; duplicate suppression `1,000/1,000` |
| Project Graph | `1,000/1,000`; p95 `7.388756 ms` |
| Relationship/Impact | regular `1,000/1,000`; 2,000-node scale `25/25`; scale p95 `188.185641 ms` |

These receipts verify coordination and projection correctness. They do not close
the live provider improvement gate.

The final repository regression run discovered `1,913` tests: `1,882` passed,
`31` conditional skips, and `0` failures in `460.645 s`. One concurrent durable
operation barrier test had a transient scheduler failure in the preceding run;
that test passed in ten consecutive isolated repetitions before the final run.

The Platform host now has plugin
`0.1.0-alpha.2+codex.20260823103338` installed and enabled from the personal
marketplace. The bundled MCP server uses a plugin-root-relative command and
completed a real Codex initialize handshake. The prior `${PLUGIN_ROOT}` value
inside the MCP `args` array was not expanded by Codex; a manifest-driven
handshake regression test now covers this boundary.

The accompanying local candidate CLI is `0.1.0a2`, installed from wheel
SHA-256 `fa06941b32ed694df50844ebb0653583fbf61a4e149c9f9f9ae5c7dfbbdc881f`.
`continuity verify` and bilingual current-only STATUS rendering passed against
Platform revision `74`. This wheel is a local pilot artifact and has not been
published to PyPI.

Codex `/hooks` reported exactly three Continuity hooks. PreCompact,
PostCompact, and SessionStart changed from review-required to active after the
current hashes were inspected and trusted. A native startup probe over Platform
State revision/event `74/74`, Work `N-69-06`, and its verified checkpoint
produced a sanitized SessionStart observation with `plugin_loaded=true`,
`success=true`, eight locked rule IDs, and transcript admission `false`. The
single minimal provider request used `31,898` input tokens, including `11,008`
cached tokens, and `5` output tokens. This is an activation smoke sample, not a
matched improvement result.

The original Platform Codex task was resumed with an explicit Platform working
directory while retaining its task identity and historical conversation. Its
first bounded check correctly rejected an expired claim. The claim was reclaimed
through State as `claim-n-69-06-reclaimed-2` at revision/event `75/75`, and a new
verified checkpoint was created. The same original task then resumed with
`source_fresh=true`, `lease_valid=true`, and `read_only=false`; no transcript was
copied into Git or local State.

ProjectCompute remains unmodified. Its latest constant 950K-window baseline
covered `23.649 h`, `2,588` provider requests, `66` user messages, and `4`
compactions. Provider cumulative usage was `1,218,755,959` input,
`1,069,740,216` cached input, `149,015,743` uncached input, `837,837` output,
and `275,284` reasoning output tokens. The window then changed to `828,400`;
the adapter rejected a mixed-window aggregate and measured that segment
separately. Project activation is deferred until the current production PR and
deployment reach a durable boundary.

## Open Gates

- the original Platform Codex task must complete the updated MCP resume-bind and
  heartbeat live gate, then collect real Work and PreCompact/PostCompact
  candidate segments; the CLI startup smoke does not substitute for that sample;
- Codex `exec` currently lacks the personal-plugin candidate surface in the
  measured host configuration;
- matched baseline/candidate runs require at least three segments per exact
  match key and a second live provider candidate;
- longitudinal `accepted Work / compaction` and token/Work remain unavailable;
- the current local legacy State does not yet persist an active compiled Skill
  lock; the Codex plugin supplies a content-bound plugin rule lock;
- v1-to-v6 project migration and the Platform DAG policy remain separate gates.

M10-11 remains open. Contract tests and local smoke measurements do not grant a
cross-project, cross-provider, token-saving, or release-completion claim.

Repository verification after the implementation discovered `1,895` tests:
`1,864` passed, `31` conditional skips, and `0` failures in `443.167 s`. The
unified repository verifier, Ruff, plugin validator, schema governance, and
document lifecycle gates also passed.

After adding the streaming Claude adapter and nullable reasoning-token
capability, the final discovery run contained `1,900` tests: `1,869` passed,
`31` conditional skips, and `0` failures in `433.160 s`.

## Alpha.7 Cross-project Control Repair

The local `0.1.0a7` candidate closes four live control defects without claiming
the M10-11 matched improvement gate:

- `resume` verifies the projection receipt, packet digest, revision, and both
  current STATUS files. Missing, stale, or tampered projections are rebuilt;
  an unverifiable rebuild stops resume.
- A content-equivalent attach refresh preserves its source identity. A nested
  dependency transition may bind an existing verified attach evidence with the
  same source-content digest to its declared return Work in the same Event.
- A delivery activation binds an opaque source, completed predecessor, verified
  implementation evidence, Git head/ref, and an allowlist of effect scopes.
  An implementation claim without effect scopes may create local commits but
  cannot push, create or merge a PR, deploy, perform remote installation, or
  publish a package.
- A local Git common-dir binding resolves the repository root and sibling
  worktrees to one canonical control root. Multiple unbound candidates fail
  closed.

ProjectCompute reproduced the nested source failure at revision/event `78/78`.
The business Session used the new CLI to return `issue-776` to `issue-774` at
`79/79`, verified independent issue-774 evidence, and returned to `issue-773`
at `80/80`. Both transitions returned verified checkpoints and writable current
packets; no direct SQLite edit or split complete/activate sequence occurred.

The same installed CLI repaired Platform `STATUS.current` from `122/N-69-08`
to authoritative `162/N-69-14` and ProjectCompute from `7/issue-681` to
`80/issue-773`. ProjectCompute's main checkout contains no second State and
resolves to its execution worktree through the local Git binding.

The final repository discovery ran `1,951` tests in `406.527 s`: `1,920`
passed, `31` conditional skips, and `0` failures or errors. The installed wheel
SHA-256 is `cd6192a17fc651067a4a7c162858497ecace068d33b6bce66db7770db57d78df`;
the installed plugin version is
`0.1.0-alpha.7+codex.20260825165330`. Source and installed hook hashes match.

These results establish control correctness. Platform and ProjectCompute still
require three matched baseline/candidate segments per exact match key before
token, compaction interval, or useful-context improvement can be claimed.
