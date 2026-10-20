# Reliable worker and rule workflows

[中文操作路径](workflows.md) · [Deployment](deployment.en.md) · [Current validation and known gaps](validation.en.md)

This is the detailed command reference. Use the capability matrix for present coverage; historical Alpha24 observations below are not a new Alpha25 field trial. Todo, claim and inbox are optional when an existing card/handoff workflow already provides the needed coordination.

## Source-tree fix: caller identity and reassignment

The reassignment correction below is not included in the published `0.1.0a29` package.
In the corrected source, task add/update accept `--actor` for the caller and
`--assignee` for the target worker. Actor defaults to `CONTINUITY_ASSIGNEE`;
creating a card records its creator. Do not use an MCP connection ID as this identity.

```bash
continuity collaboration task update --project-id demo --task-id parser-fix \
  --actor controller --assignee worker-b
```

The recorded creator can change a planned assignment until the worker claims or
starts it; the current owner can hand off its own non-running card. A running
attempt still requires an explicit audited override. Legacy cards without a
recorded creator are not silently attributed to the caller. Reassignments record
the caller, old/new assignees and any override reason. These are local planning
identities, not native account authentication or external-operation authorization.

For a controller MCP, configure `CONTINUITY_ROLE=controller` and a stable
`CONTINUITY_ASSIGNEE`: creating or changing a target no longer assigns the card
to the controller. Explicit worker identities retain their ownership/conflict checks.
Both CLI help and `continuity_help` expose the new actor parameter.

## Development source: controller recovery and change consumers

This section describes the current source-tree correction, not features already
installed by the published `0.1.0a29` package. It requires a later build and fresh
MCP connections; it does not migrate native accounts or provider sessions.

`handoff show` keeps `declared` (the saved plan/revision) separate from `observed`
(ledger, current-attempt terminal events and admitted report-file metadata at
`as_of`). `snapshot_scene` remains the original snapshot. Reads do not rewrite
the plan, mark tests passed or grant a controller lease. Events arriving after a
crash are therefore visible on the next read without relying on a Stop hook.

```bash
continuity handoff show --project-id demo
continuity handoff write --project-id demo --auto
continuity collaboration drift --project-id demo
```

Auto write preserves the declared plan and saves an evidence-linked candidate
draft. Normal controller lease checks still apply. Drift can report
`report-without-card-update` with a path and modification time; it only inspects
task-local reports beneath configured persistent artifact roots, not arbitrary
files or provider transcripts. Existence is not an implementation verdict.
Project-qualified work paths and job-index paths carry project attribution;
old global `work/TASK/report.md` paths are labeled unattributed candidates.
Verify project ownership before opening such a legacy report.

Named consumers use **at-least-once** delivery with explicit acknowledgement:

```bash
continuity collaboration changes --project-id demo --consumer controller-a --max-output-bytes 2048
# After processing that page, use its next_cursor (not high_water_cursor):
continuity collaboration changes --project-id demo --consumer controller-a --ack RETURNED_CURSOR
```

No ack means replay, including after a crash. State is project/consumer scoped,
locked and atomically stored in the data root; choose persistent storage. Budget
trimming only acknowledges rows the caller explicitly confirms. A reset requires
`--reset-consumer --since CURSOR`; it never deletes events. Consumers sharing a
name share a checkpoint, so independent listeners need distinct names.

`task list/show/add/update/claim/next` are aliases of the corresponding
`collaboration task` leaves; `task merged` retains its existing behavior.
Dispatch refuses executor/model conflicts and fences the prepared body/scope
and card timestamp immediately before child startup. A rejected prepared job
records `dispatch.validation-failed`, `executed:false` and preserves the card.

```bash
continuity dispatch --project-id demo --task-id parser-fix --model sol --assignee worker-sol --reassign --actor controller
```

Reassign is explicit, keeps owner/creator and running-attempt protections, and
cannot be combined with dry-run. A generic provider-neutral executor name is
allowed; recorded model bindings and unambiguous model names are checked.
Authorized fallback/handoff remains explicit. Upstream replay-reset codes take
precedence over an outer 429. Replay refusal can suggest an explicit new session;
real rate limits instead advise honoring provider cooldown (a new thread does
not reset quota). Recovery suggestions never authorize authentication/policy bypass.

The Claude source plugin and bridge add `SessionEnd` (nine hook types in this
source; the published Alpha29 plugin has eight). An owner-session end or a
changed observed boot marks matching native attempts **lost to observation**,
not exited successfully. Age without heartbeat is `unknown-stale`, not proof of
exit. Native account data stays on its owning machine/profile.

SessionEnd semantics follow the [official Claude hook contract](https://code.claude.com/docs/en/hooks#sessionend); Stop is a turn-end event and does not establish session exit.

## Alpha25 API details

The following APIs are included in Alpha25 (`0.1.0a25`), not in Alpha24.
Upgrade and reconnect an existing MCP before relying on these actions.

Workers accept `--timeout-seconds` and `--idle-timeout-seconds` through dispatch, prepare and both
resume forms. New jobs default to 86400/5400 seconds per attempt; recorded jobs retain their budgets,
including the legacy 3600/600 defaults when fields are absent. Explicit limits survive background
launches, same-thread retries and permitted transient fallback. Explicit provider effort names such
as medium/xhigh are preserved; GLM max is a default, not an override of an operator choice.

```bash
continuity dispatch --root /path/to/project --task-id TASK --model sol --effort xhigh \
  --timeout-seconds 86400 --idle-timeout-seconds 5400 --background
continuity resume TASK 'Continue verified work' --root /path/to/project --effort medium
continuity todo from-cards --root /path/to/project --status active --status dispatched
continuity todo from-cards --root /path/to/project --all
continuity collaboration wait --root /path/to/project --since CURSOR \
  --event attempt.finished --event attempt.timeout --event dispatch.policy-stopped
continuity collaboration task update --root /path/to/project --task-id TASK --body 'Updated instructions'
```

New Todo projections default to open cards, including ready-for-review; closing a card does not
delete its evidence. Existing projections retain their previous selection until explicitly changed.
Batch records accept optional `metrics`, also via `--metrics-json`, for example
`{"runtime_links_module":{"before":13,"after":6}}`. Baseline stays a commit; declared metrics do not
prove a test ran or grant permission to merge. Generated status shows these metrics.

Use Continuity rules as the canonical policy source. `rules export --audience worker --format text`
generates complete selected rule text with a digest and attachment references, or fails explicitly
when the budget is insufficient. A retained common.txt should be generated from this command,
never maintained independently. The tool does not modify an operator's existing scripts.
`rules check-mirror --file /path/to/common.txt --audience worker` compares it with current generated
text without importing edits, exposing contents or overwriting the mirror. Drift is a reported status,
not a global work blocker.
Prompts attach bounded hash-bound code routes from body identifiers and allowed files; absent
checkouts and registration-root fallback are labeled. Routes are neither full impact analysis nor
filesystem write enforcement.

```bash
continuity session bind --root /path/to/project --provider claude --session-id OWN_NATIVE_ID \
  --task-id TASK --batch-id batch16 --role controller
continuity lesson add --root /path/to/project --lesson-id load-vs-guard \
  --text 'Check the failing guard before diagnosing a load limit.' --source 'batch14 review' --tag verification
continuity handoff write --root /path/to/project --batch-id batch16 \
  --plan 'Regression fixes first; other branches after the transport gate.' \
  --pending 'Owner chooses baseline' --next-action 'Run the transport gate'
continuity handoff show --root /path/to/project
continuity collaboration context-set --root /path/to/project \
  --integration-root /path/to/authority-checkout --integration-ref refs/heads/main
```

Use the owning host's native session id, not an MCP connection id or another profile's private data.
Bindings are declared project context, not authentication. Unknown first counters become a baseline;
only subsequent deltas count, by task/batch, without assigning historic usage or inventing prices.
Multiple collection origins are not combined. Worker sessions should bind with `--role worker` so
their compaction cannot overwrite the shared controller plan. Planned batches may be bound before
an integration record exists and are labeled accordingly.

Recovery distinguishes an authoritative integration ref from checkout HEAD, includes current
workers, recent finish cursor, commitments and the shared plan, and reports missing data rather than
substituting a stale checkout. Lessons are project-visible reported experience with provenance,
not automatically enforced policy. A new controller reads the same handoff regardless of model.

```bash
continuity controller acquire --root /path/to/project --owner CONTROLLER \
  --ttl-seconds 10800 --client-limit codex=5
continuity controller status --root /path/to/project --owner OTHER_CONTROLLER
continuity dispatch --root /path/to/project --task-id TASK --model sol \
  --controller CONTROLLER --controller-token FENCE --background
continuity controller run --root /path/to/project --owner CONTROLLER --token FENCE \
  --operation integration -- YOUR_EXPLICIT_COMMAND ARG...
continuity controller renew --root /path/to/project --owner CONTROLLER --token FENCE --ttl-seconds 10800
```

First acquire enables project fencing and guarded ledger v4. Upgrade all participating dispatch
CLI/MCP builds first: older builds reject this project rather than bypassing the lease. Other
projects and ordinary code work are unaffected. The returned fence is project coordination metadata,
not an account credential. `CONTINUITY_CONTROLLER`/`CONTINUITY_CONTROLLER_TOKEN` can supply it to
hooks; an explicit SSH bridge may forward that fence, never native authentication.
The holder must renew before expiry. Existing workers are not killed when the holder changes.
Once fencing is enabled, batch-add, context-set and handoff-write also require the current controller
and token, supplied explicitly or by the controller environment. The token is not a provider key.
Dispatch reserves card/capacity atomically. A stale unknown reservation is not silently recycled;
after checking its attempt, use `controller clear-reservation` with exact task/job ids and a reason.
The token of a previous holder cannot authorize a new operation. `controller run` serializes
cooperating commands; it does not intercept arbitrary Git calls or grant external-operation approval.
Protected operations retain a durable started/finished record containing a command digest, not argv.
Batch writes revalidate the current lease after waiting on the ledger; replaying an identical
batch repairs a previously deferred handoff projection. Automatic fallback swaps the exact previous
reservation atomically. If lease expiry or destination capacity prevents replacement, the original
finished result remains readable and a `dispatch.fallback-deferred` event explains why no new worker
started. Renew the controller lease explicitly; workers do not acquire controller authority themselves.
If the controller disappears before recording completion, a new holder cannot silently replay the
operation. Inspect it, then acknowledge the exact id with `controller clear-operation --operation-id ID
--reason TEXT` and the current owner/token. Lease expiry alone does not erase unknown operations.
Late completion cannot replace a manual acknowledgement of that same operation. PreCompact still
saves the local session handoff when shared refresh is refused, but returns `saved-with-warning`
with a deferred-refresh reason; recovery retains that warning instead of claiming shared state is fresh.

Claude's SubagentStart/SubagentStop produce origin-scoped observational attempts in the changes
stream. Official schemas are tested; live installation is separate. No native transcript paths,
transcript bodies or final message text are forwarded. A stop is not a known exit code, successful
test, or handback report. SubagentStart cannot block creation under the native contract, so these
hooks do not enforce dispatch limits on agents started outside Continuity. Internal unmatched stops
create no phantom workers. Missing heartbeats do not prove death. These events never block a Stop
or restart an agent. See the [official hooks reference](https://code.claude.com/docs/en/hooks#subagentstart).

Release scope: Alpha25, with the Alpha24 baseline workflows below. Source verification,
installed profile verification and live model observations are reported separately; historical
Alpha24 observations do not prove natural live coverage of Alpha25's new native subagent hooks.

## Plans and cards

```bash
continuity todo from-cards --root /path/to/project --linked-only
continuity collaboration task list --root /path/to/project --format brief
continuity collaboration task list --root /path/to/project --cursor CURSOR --max-output-bytes 2048
continuity help task.update
```

The card view follows out-of-order completion. Explicit follow-up checklist actions create queued
cards and preserve existing evidence; repeated titles reuse cards. List cursors preserve an immutable
snapshot for one hour. The changes stream remains the source for subsequent activity.

## Full rules and policies

```bash
continuity rules update --scope global --rule-id sources --text 'Prefer applicable primary sources.'
continuity collaboration rule-add --root /path/to/project --rule-id review-policy \
  --audience reviewer --text 'Review the declared repository and test evidence.' --attachment-file /private/review-policy.md
continuity rules export --root /path/to/project --audience worker
continuity rules attachment --root /path/to/project --sha256 DIGEST --offset 0 --max-chars 4096
continuity rules affected --root /path/to/project
continuity rules snapshot-export --root /path/to/project
continuity rules snapshot-import --root /path/to/project --file /private/rules-snapshot.json
```

Project rules override global rules with the same id. Audience selection includes `all`, the requested
audience and an optional `lane:NAME`. Version history and attachments live outside Git. Long policy
content has a digest and an explicit range cursor. Complete exports fail with an actionable budget
message rather than silently dropping rules. Conflicting imports require review and an explicit
`--force --reason`. Sync is operator initiated; no account upload or account-system integration is included.

Compatible task writes retain the earlier ledger format during rollout. Allowing multiple active
cards for one actor/lane requires ledger v3; older readers then report a clear upgrade requirement.
Upgrade participating CLI/MCP runtimes before using that feature on a shared registry. A metadata
upgrade requirement does not put ordinary repository work into read-only mode.

## Evidence coverage

| Capability | Verified scope | Not established |
|---|---|---|
| Worker finish, card projection, retries, wait and outbox | Contract tests and controlled real subprocesses | Every provider outage/signaling combination |
| Rule delivery, prompt assembly, references and batches | Regression tests and actual rule intake | Automatic external account synchronization (excluded) |
| Claude SSH compaction bridge | Actual owner-host native manual `/compact`, exact return point | Automatic-threshold/large-context and longitudinal business sessions |
| Codex compaction hooks | Native wrapper sequence and deduplication probes | A new large-context live study for this release |
| Provider token/cache metadata | In-place real-host metadata ingestion, deduplication and attribution tests | Whole-account billing, unknown prices or causal net savings |
| Host/profile and storage guards | Origin mismatch and volatile-storage tests | OS sandboxing, cryptographic account attestation or backups |
| Native Claude automatic quota query | Withdrawn by maintainer decision | No poller or polling service is promised |

The bounded recovery packet contains a rule manifest and the complete-export command. A new worker
receives the complete selected rule text through prompt assembly, plus attachment references. Only
a caller that already supplied full rules may use `--known-rules-digest` to reuse an unchanged prefix.

## Dispatch, resume and reports

Configure only executors you are authorized to use. Model ids belong to the configured provider;
the examples use placeholders rather than selecting a provider for the operator.

```bash
continuity worker configure --model sol --provider codex --provider-model MODEL_ID
continuity collaboration task update --root /path/to/project --task-id TASK --status queued --body 'Implement X and verify Y.'
continuity collaboration prompt --root /path/to/project --task-id TASK --assignee WORKER
continuity dispatch --root /path/to/project --task-id TASK --model sol --effort high --dry-run
continuity dispatch --root /path/to/project --task-id TASK --model sol --background
continuity resume TASK 'Continue the assigned work; preserve verified progress.' --root /path/to/project
continuity report TASK --root /path/to/project
```

Dispatch uses the card checkout, captures a provider session id and saves the latest final report
under the external data root. Custom adapters accept argv templates, including `{prompt_file}`,
`{report_file}` and `{thread_id}`. Arguments are passed without a shell. No automatic Git merge,
push, deployment or credential change is part of dispatch.

Supervisor retries require an explicit limit and either an existing-thread resume template or
`--restart-on-retry`. Retry-After and explicit timezone-bearing reset times are respected within the
wait budget. Authentication errors and provider policy refusals are not transient retries. A declined
session is not blindly resumed or sent to another provider. Independent reviews describe the
legitimate repository scope, correctness checks and authorized fixtures accurately; wording changes
must not hide the nature of a task. Preferred scenario names are `independent-review` and
`boundary-validation`; older aliases remain compatible.

## Evidence and integration

`task update --verification-json` accepts an exact commit, actor and structured checks with `name`,
`command`, `result`, `status` and `required`. Optional unverified items are separate. Missing evidence
marks a metadata gap rather than blocking a factual merge record. `task list --all --evidence-gaps`
finds those cards. This never declares tests passed or grants permission for an external operation.

`collaboration decision-add` records an explicit source and scope; approval is never inferred from
silence. `batch-add` records task ids, baseline, resulting commit, verification and decision references.
`continuity task merged TASK --batch BATCH` links existing batch evidence. `--ctest-log` can parse
operator-supplied ctest summaries; it does not claim Continuity ran the tests or independently proved
which commit produced a log.

## Remote hooks and measurement

The CLI supports JSON argv transport without interpolating commands into a remote shell:
`continuity --remote STATE_HOST --remote-root /srv/project --remote-endpoint /opt/continuity/bin/continuity-remote-cli collaboration task list`.
Both roots and project ids are checked. Use background dispatch for long remote worker runs; a
transport timeout requires inspecting attempt events before repeating an execution request.

Install the bridge in the actual Claude profile. Its JSON config specifies an SSH host, an absolute
remote receiver executable, explicit local/remote root mappings and an optional local data root.
`continuity bridge install --profile PROFILE --config-file CONFIG --executable BRIDGE` retains
byte-identical backups and preserves unrelated settings. Optional MCP endpoints update only the two
Continuity services in that profile; existing connections need reconnection to load new tools.
When moving a bridge config from a staging path, list its old absolute path in
`supersedes_config_files`. Only those explicitly superseded hooks are consolidated; other bridges
remain untouched. Repeat installation is idempotent and keeps the pre-change backup.

The bridge forwards lifecycle metadata and normalized usage counters, without historical transcript
bodies or transcript paths. With `forward_checklists` enabled, it also forwards the current user
instruction up to 8 KiB for explicit checklist intake. Larger instructions are omitted with an intake
notice; a partial checklist is never silently saved. Set the config field to false to keep that text
on the controller. The bridge keeps a local return point when SSH is unavailable. Post-tool polling is coalesced
for three seconds and SSH connections are reused. Changed-only notices are deduplicated per session.
Claude validates at PostCompact and injects at SessionStart(compact); PostCompact cannot carry
context under the host contract. Codex uses its supported PostCompact context route. Deduplication
uses the handoff generation, so a callback that cannot inject never consumes the restore packet.
Set `CONTINUITY_DISABLED=1` to disable hooks; set `CONTINUITY_HOOK_DEBUG=1` for bridge diagnostics.

```bash
continuity observe ingest-usage --root /path/to/project --provider claude --session-id SESSION --transcript /private/session.jsonl
continuity observe report --root /path/to/project
continuity collaboration outbox-replay --root /path/to/project
```

Usage import is incremental and deduplicates message revisions and cumulative counters. Reports keep
Claude cache-read/create tokens distinct from Codex's cached-input subset. Model prices and actual
window capacity are not invented when absent. Compaction windows can report token deltas and project
task activity; these are not causal billing allocation. Checkout retrieval estimates remain separate
from provider tokens, prompt-cache counters and index-cache hits. Negative estimates are retained;
negative retrieval history narrows automatic brief budgets, while explicit caller limits are preserved.
`compaction_lifecycle` counts real save/restore generations and separates controlled probes from native
events. The older `paired_sessions` field counts State/core observation-source pairing. Bridge output
bytes are counted at the controller emission point and sent as cumulative snapshots on the next hook;
receiver-generated context is not counted a second time as model input.

Registry I/O failures use a separate local execution outbox. Replay retains audit records and does not
overwrite newer task attempts. Legacy event-cursor repair is explicit, retains a byte-identical backup
and requires consumer cursor reset. Metadata is advisory for ordinary work; authoritative State writes
and external operations retain their own authorization contracts.

## Host/profile ownership and durable work

Authentication, native quota queries and session resume execute on the owning machine in the owning
provider profile. Remote operation means invoking that machine's CLI through the explicit remote
endpoint, not copying credentials, provider configurations or private session stores to the controller.
Only authorized task artifacts and normalized observations cross the boundary. Usage observations
carry opaque host/profile origins; they are not reusable account state. Foreign or unscoped quota facts
do not change local availability. Adapter/job origins must match before execution or resume, including
a custom wrapper's declared `--profile-provider`. Do not put credential flags in stored adapter argv.
Provider profile environment variables must contain absolute paths. Relative values and unexpanded
literal `~` values are rejected before querying or launching: a worker's checkout must not change
which profile the native client opens.

Use `quota report` for subscription facts and `observe report` for token/byte measurements; neither
invents whole-account billing. `quota refresh --model MODEL --provider codex` queries the existing local Codex client;
select the remote endpoint instead when the account belongs elsewhere. A returned remote result must
retain its source rather than being relabeled as a local account query.
The refresh provider is explicit. Native Claude automatic quota querying is withdrawn from this
release; no background account polling service is enabled. The optional Codex refresh above is
caller-invoked only and never substitutes for a Claude account. Explicitly observed native Claude
counters can still be recorded with `quota record --provider claude` on that machine/profile.
No private or undocumented credential-transfer procedure is provided.
Declare `worker configure --official-subscription` only for an adapter actually funded by that native
account. Native client quota is not automatically the quota of an unrelated API or gateway route.

Recoverable task bodies, final reports, thread references, checkpoints and pending events belong in
persistent work/data storage. Worker scratch defaults to `DATA_ROOT/work`, with a persistent user-state
fallback for disposable registries. `CONTINUITY_WORK_ROOT` can select a different durable location.
`storage inspect --path PATH` observes temporary-path and Linux tmpfs/ramfs risks, including memory
mounts outside `/tmp`. `/var/tmp` is not assumed safe solely from its name. Only disposable reproducible
buffers should use temporary mounts. Atomic writes stage beside their destination, flush the file and
attempt directory synchronization; filesystem observations and fsync do not substitute for backups.
Relative `storage inspect --path` values are resolved against `--root`.
The fallback is checked too: a memory-backed home is not reported as durable. If no persistent scratch
is available, ordinary authorized commands may continue, but the attempt receipt and start event expose
the storage warning. A recovery state root explicitly placed on volatile storage remains the caller's
responsibility; scratch redirection cannot make that registry durable.
