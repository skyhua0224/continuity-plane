# Reliable worker and rule workflows

Release scope: Alpha24. Source verification, installed profile verification and live model
observations are reported separately; the coverage table below identifies their limits.

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
