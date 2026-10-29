# Command and Tool Reference

[中文](commands.md) · [Project home](../README.en.md) · [Docs index](README.en.md)

This page lists Continuity commands, MCP tools and hooks by feature, for looking up parameters and limits. New users should start with the [project home](../README.en.md) and [usage guide](../USAGE.en.md). `continuity help <operation>` is the authority for each command's parameters.

<a id="start"></a>
## Install and try one useful action

Python 3.11+ is required; code indexing also needs Git. A pipx installation makes the CLI available to host integrations:

```bash
pipx install 'continuity-plane==0.1.0a36'
continuity --version
# Register inside your repository; replace demo with its stable project ID
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity whoami --root . --assignee controller --controller controller
```

See [deployment](deployment.en.md) for existing installations, venvs, Windows and pipx/uv upgrades. The host process must find the CLI on PATH; with a venv, use absolute executable paths in MCP configuration.

Registration is enough for the Todo and collaboration examples below. Use `continuity init --root . --project-id demo` when you also want project-local .continuity files, governance templates and authoritative State; do not initialize over an existing directory. whoami declares this invocation's identity, not a controller lease. Give concurrent executors stable, distinct identities rather than transient MCP connection IDs.

<a id="todo"></a>
## Todo

**Personal checklist:** compile explicit work into a durable queue, inspect its items and record results as work finishes.

```bash
# A single agent's execution checklist
continuity todo compile --project-id demo --session-id controller --prompt 'Fix parser boundaries; add regression tests; summarize verification'
continuity todo list --project-id demo --format brief
continuity todo complete --project-id demo --item-id ITEM_ID --result 'Completed this item and retained verification'
```

Copy ITEM_ID from list. Use `activate --item-id … --reason …` to switch explicitly, `block --item-id … --reason …` for a blocker, and link to associate an item with a card. Complete any open item by ID; work need not finish in its original order.

**Already using cards?** Make Todo a live view of those cards instead of maintaining two plans:

```bash
continuity todo from-cards --project-id demo --assignee worker-a
continuity todo list --project-id demo --format brief
```

The view follows card status and defaults to open states. Repeat --status or use --all to change the selection. In brief output, `execution_state` separates queued, assigned, running, blocked and review: an assigned worker that was never dispatched is assigned, and only a live execution binding is running. Blocked rows include a bounded reason. Legacy queue contents remain as history. Card-view activation uses --assignee or CONTINUITY_ASSIGNEE; session-id is only a recovery label. Discover parameters with `continuity help todo.compile` and `continuity help todo.from-cards`.

<a id="collaboration"></a>
## collaboration

Alpha35 retains the [private Gitea Issue / PR backend](gitea-collaboration.en.md): members use their own local CLI / MCP and tea login for shared tasks, claims, accepted handoffs and real PR review requests, without a Continuity server. Issues own task state; local snapshots are caches. Conflicts are explicit; claims are not strong cross-machine locks. The [storage/configuration contract](collaboration-storage.en.md) separates committed declarations from member-local data. Local-card examples below are a separate mode.

### Create, assign and update task cards

A card stores instructions, objective, next action, acceptance criteria, assignee, lane and priority. Optional fields cover worktree, allowed files, branch, commits, verification and reports. Create one in the registered demo project:

```bash
# The controller assigns a focused card to an independent executor instance
continuity collaboration task add --project-id demo --task-id parser-fix --lane-id parser --mode patch --priority p1 \
  --title 'Fix parser boundaries' --objective 'Limit changes to the parser and regression tests' \
  --next-action 'Locate symbols and reproduce boundary input' --exit-criterion 'Reproducible regression and explicit unverified scope' \
  --assignee worker-a --allowed-file src/parser.py --allowed-file tests/ \
  --body 'Check existing parser constraints, add a regression, fix it, and preserve commands, results and out-of-scope findings.'
continuity collaboration task list --project-id demo --assignee worker-a --status queued --format brief
continuity collaboration packet --project-id demo --assignee worker-a
```

Packet gives the assigned main card and bounded collaboration entry points; task show expands instructions. Prompt assembly combines the rules and card. For manual execution, claim and update it:

```bash
continuity collaboration task claim --project-id demo --task-id parser-fix --assignee worker-a
continuity collaboration task update --project-id demo --task-id parser-fix --body 'Add the confirmed scope and test entry point'
continuity collaboration task show --project-id demo --task-id parser-fix --include-report --format brief
```

Body-only updates preserve status. Moving to queued preserves the assignee; only explicit `--unassign` clears it. An active controller-lease holder may reassign with a recorded `--reason` without impersonating the old owner. States include queued, dispatched, active, blocked, ready-for-review, completed, merged, rejected and superseded; local completion is separate from acceptance. Filter lists by status, assignee, lane, query, update time and result count. --all includes closed cards; --full expands fields. task show defaults to 64 KiB and larger lists continue from next_cursor. Priorities are p0/p1/p2, and lanes separate parallel work. Allowed-file declarations are not an OS sandbox.

For SSH, zsh or any shell where quoting a long body is error-prone, put the updates in JSON:

```bash
continuity collaboration task update --project-id demo --batch updates.json
cat updates.json | continuity collaboration task update --project-id demo --batch -
```

Updates run sequentially, at most 50 per file, and stop at the first failure with completion/failure indexes. The batch is not an all-or-none transaction. Shared Gitea mode still requires `operation_id` and `expected_remote_revision` per item.

`--dependency ID` (repeatable) expresses prerequisite cards in both local and Gitea modes; completed, merged and superseded are terminal. `task list --dependencies-ready` returns only cards satisfying that rule. Claim, activation and dispatch reject unmet dependencies. A legacy “Depends on:” sentence is surfaced as a migration hint and never silently inferred as a dependency. Local and Gitea list calls now share one envelope: `items`, `total_items`, `next_cursor`, `has_more`.

`task add` requires a non-empty `--body`/`--body-file`; without one the card could never be dispatched. On the local backend `task update --body` (and `--title`, `--objective`, `--priority`, `--exit-criterion`) revises a card at any time: each changed body gets a revision number and a sha256, both recorded in the change log, and a dispatch stores the body sha256 and revision in its job. A dependency may be declared before it is complete: `task add --assignee X --dependency Y` creates the card as `queued` and claim, activation and dispatch refuse it until Y is completed, merged or superseded. `--mode review` is a valid mode; a rejected mode, priority or status lists the valid values.

Per-card model limits: `task add/update --min-tier strong|standard|light --deny-model NAME` (repeatable; `task update --clear-model-constraints` removes them). Dispatch, the queue, handoff and fallback refuse a model below the tier or in the deny list and name the allowed models. Operators label an adapter with `worker configure --model NAME ... --tier strong`; unlabeled adapters use a built-in family default (opus, sol, grok strong; sonnet, astra, glm standard; luna, haiku light; anything else standard). Fallback candidates that a card forbids are skipped.

### Inbox, incremental changes and waiting

Inbox messages have durable IDs and explicit acknowledgement. They do not guarantee a wake-up in every host. `send` may omit `--message-id`; Continuity generates and returns the final ID. An explicit ID remains an idempotency key. `ack` supports one ID, a time cutoff, or `--all` with a bound, followed by archive of acknowledged messages.

```bash
continuity collaboration inbox send --project-id demo --sender worker-a --recipient controller --message-id parser-note-1 --task-id parser-fix --body 'Please confirm the compatibility requirement'
continuity collaboration inbox list --project-id demo --recipient controller
continuity collaboration inbox ack --project-id demo --recipient controller --message-id parser-note-1
continuity collaboration inbox ack --project-id demo --recipient controller --before 2026-10-20T00:00:00Z --limit 1000
continuity collaboration inbox ack --project-id demo --recipient controller --all --limit 1000
continuity collaboration inbox archive --project-id demo --recipient controller --limit 1000
continuity collaboration inbox unread --project-id demo --recipient controller --count
continuity collaboration inbox wait --project-id demo --recipient controller --follow
```

Use changes for “what happened while I was away?” and wait to block on worker events instead of rereading the ledger or polling processes:

```bash
# Zero is the first cursor; subsequently use the returned next_cursor
continuity collaboration changes --project-id demo --since 0 --max-output-bytes 2048
continuity collaboration wait --project-id demo --since 0 --event attempt.finished --event attempt.timeout --event dispatch.policy-stopped --timeout-seconds 30
```

If truncated, keep reading from next_cursor. Jumping to high-water skips undelivered events. Multiple event filters match any listed event; a wait timeout is not worker failure. `collaboration drift` checks stale/contradictory records; collaboration doctor reports ledger writer versions. inbox wait observes new messages by default (use --include-unread to start from existing unread); --follow has no deadline, and reconnecting scripts use stream_cursor.

<a id="worker"></a>
## Worker: dispatch, supervision, resume and reports

Configure an already authorized native client or custom adapter on the execution host. Replace YOUR_MODEL_ID, worktree and verification requirements with real values; adapter configuration does not authenticate a provider.

```bash
continuity worker configure --model primary --provider codex --provider-model YOUR_MODEL_ID
continuity collaboration prompt --project-id demo --task-id parser-fix --assignee worker-a
# Preview does not launch a model; start only after checking the plan
continuity dispatch --project-id demo --task-id parser-fix --model primary --effort high --dry-run
continuity dispatch --project-id demo --task-id parser-fix --model primary --effort high --background --timeout-seconds 86400 --idle-timeout-seconds 5400
continuity report parser-fix --project-id demo
continuity worker events --project-id demo --since-cursor 0 --follow
continuity worker events --all-projects --since-cursor 'demo:120,other:80' --follow
continuity worker wait --project-id demo --task-id parser-fix --timeout-seconds 3600
continuity worker report --task-id parser-fix --project-id demo --offset 2048
continuity worker jobs --project-id demo --status ended
continuity collaboration reconcile --project-id demo --format brief
```

Claude and custom adapters are also supported. Dispatch assembles complete rules, the card body, attachment references and bounded code routes. It retains run IDs, logs, native session references and reports. The card must exist. Prepare isolated worktrees before parallel edits. After background dispatch, wait for that task's end event before reading its report.

Use Continuity's persistent queue when a request should wait instead of starting immediately; external filler scripts are unnecessary:

```bash
continuity dispatch --project-id demo --task-id parser-fix --model primary --enqueue --request-id parser-fix-a --priority 10
continuity worker queue run --project-id demo --watch --controller controller
continuity worker queue status --project-id demo
continuity worker queue pause --reason 'maintenance'
continuity worker queue resume
```

One runner can hold the non-blocking single-instance lock for a project/data root. It reuses normal dispatch validation for cards, controller lease, client-family capacity and observed quota/cooldown. MCP safely exposes `worker.queue.enqueue/status/pause/resume/cancel/requeue`; `worker.queue.run`, which starts provider processes, remains CLI-only. Capacity or quota gates leave requests queued; transient 429/quota/gateway outcomes return with backoff. Enqueue, waiting, blocked, retry and launch transitions enter the change feed for `changes`/`wait` and hooks. `pause` is a host switch and does not kill workers already running. See `continuity help worker.queue.run`.

worker events emits newline-delimited started, progress, completed, failed, rate-limited and blocked facts from cursors rather than a monitoring time window. A single-project cursor is an integer; --all-projects uses project:cursor pairs and every row carries stream_cursor.

Worker events say what really happened. An in-job retry (a transient failure the same job resumes or restarts after a backoff) is `attempt.retrying` with kind `retrying`: it is not terminal, raises no "ended" notice and never wakes `worker wait`. A job ends exactly once, with `job.completed`, `job.failed`, `job.interrupted` (killed, signalled, timed out) or `job.lost`, each carrying `status`, `reason`, `failure_class` and `job_id`; the existing `attempt.finished` is still written for the same terminal outcome (additive, so older consumers keep working) and carries `job_id`, which makes `worker wait` and hook notices act on the `job.*` event only, once per outcome. Older `attempt.finished` rows without `job_id` still count as terminal. Kinds are `started`, `progress`, `retrying`, `retry-scheduled`, `completed`, `failed`, `interrupted`, `timeout`, `rate-limited`, `blocked` and `lost`; `worker wait` wakes only on the terminal kinds `completed`, `failed`, `interrupted`, `timeout`, `rate-limited`, `blocked` and `lost`.

worker wait blocks outside the model: for the given `--task-id` (repeatable), or any task in `--project-id` / `--all-projects`, it exits at the first terminal event (completed, failed, interrupted, timeout, rate-limited, blocked, or a finished with unknown status). It starts at the current tail and never replays history; pass the `stream_cursor` of the last line to `--since-cursor` when restarting so events between two waits are not missed. Each event is one compact JSON line (project_id, task_id, kind, reason / failure_class and `next`, the MCP call to make). After `--max-events` events (default 1) it exits 0; on `--timeout-seconds` (0 = no limit) it prints one `status: timeout` line and exits 3. Output is flushed and no TTY is needed, so a host can run it as a background task and wake the model only when the process exits. This avoids looping `continuity_events_wait`, which waits at most 50 seconds and makes the model re-read its whole context on every return.

worker report now returns a summary page by default: the first 2 KB of the report, its markdown headings with character offsets for `--offset`, the total size and `next_offset_chars`. `--offset N` reads a page from that position (bounded by `--max-output-bytes`); `--full` returns everything when it fits the budget. The MCP `continuity_worker_report` uses the same default.

`worker report` states whether it is final. The header carries `final=true|false` and `job_status=` (the card's latest job: `completed`, `failed`, `running`, `interrupted`, `lost`, or `none` when the card has no job). While the job is `running`, `dispatched`, `interrupted` or `lost`, the output starts with a line `INTERIM report: job … ` so a partial report is never read as the outcome; the MCP `continuity_worker_report` adds `final` and `job_status` fields. `worker jobs` shows `lost` jobs: a `running` job whose runner and worker processes are gone (pid plus recorded start identity; a recycled pid counts as gone) and whose newest start or heartbeat is older than twice the 10 s heartbeat interval is recorded as `lost` (event `job.lost`, reason `process-lost`), its attempt is closed as interrupted, the card is blocked like any interrupted attempt, and its dispatch reservation is released, so it no longer holds client capacity. `worker jobs` and `worker dispatch` apply this check; `worker jobs --status running` never lists a lost job, and `--status lost` or `--status ended` does. Jobs on another host are never judged lost, and an in-job retry waiting out its backoff keeps its runner alive, so it stays running.

`continuity collaboration reconcile --project-id P [--root DIR] [--integration-ref REF] [--stale-days 3] [--format brief]` compares cards with git and jobs and is read-only by default. It suggests `merged` for cards whose recorded commit is an ancestor of the integration ref (default `refs/heads/main` of the project's integration root), `ready-to-merge` for `ready-for-review` cards whose review card (lane `review`, naming the card) is completed with an accept verdict, and `stale` for `active` cards with no running job and no update for `--stale-days` days. Each suggestion carries the command that applies it, one line each in `--format brief`. `--apply --actor A [--reason TEXT] [--task-id T …]` records only the `merged` suggestions (status `merged`, `landed_commit`, and a `task.reconciled` event with the actor and reason); merging a ready card and judging a stale one stay explicit `task update` commands.

Supervisor records attempt starts, heartbeats, no-output duration, completion reasons, exit codes and timeouts. After spawn it samples PID identity, actual cwd and Git HEAD. Current successful attempts become ready-for-review; failures/interruption become blocked. It preserves manually changed status while attaching the final report. A stale attempt cannot overwrite a newer run. job.json and worker-jobs.json persist running/terminal status and exit_code, so completion is no longer inferred only from result.json.

Dispatch and resume use a derived safety index: one sequential backfill after upgrade, then only appended change-log bytes. A damaged index or replaced journal rebuilds from authoritative JSONL. There is no need to delete history, and a missing index never bypasses a refusal gate.

After a host crash or reboot, run `continuity controller reservations --project-id demo --state stale` to inspect leftover job IDs. With the controller lease, `controller reconcile-host --apply --reason …` clears only reservations proven stale by host/boot or host-identity changes. Stale reservations no longer count against capacity. The default scope is current project + host + profile + client family. `controller host-capacity --host-client-limit total=8` adds an operator-declared host-wide local gate; it is not proof of a native account limit.

```bash
continuity worker resume --project-id demo --task-id parser-fix --continuation 'Continue saved progress; check existing verification first' --background
```

Use **worker resume** for executors; top-level resume belongs to advanced State recovery. Native continuation needs adapter support and session references on the owning host/profile. Never move account/thread directories to another host to resume there. Effort passes through subject to adapter support. New attempts default to 24-hour total and 90-minute idle budgets, both overridable. A usage limit is quota exhaustion, not a content-policy refusal. When the provider supplies a future reset, an official-subscription profile records it and pauses that origin/model; after reset Continuity prefers the recorded thread. Legacy jobs with recoverable evidence receive a recorded correction rather than a manual JSON edit.

A short provider rate limit (429, "rate limit", no long reset) first resumes the same session with a bounded backoff (3 resumes, base 10 s, capped at 30 s per wait and 120 s in total; `--rate-limit-retries N`, `--rate-limit-backoff-seconds S`, `0` disables). Only a quota exhaustion, a reset longer than that budget, or repeated failures move on to a fallback model, and the event reads `fallback from X (rate-limit) -> Y`. A session that has no recorded thread yet cannot be resumed and falls back at once.

To change model, resume is refused by design; use a handoff:

```bash
continuity worker handoff --project-id demo --task-id parser-fix --to-model other --continuation 'Run the parser tests next' --dry-run
continuity worker handoff --project-id demo --task-id parser-fix --to-model other --continuation 'Run the parser tests next' --background
```

The new attempt (normal dispatch gates and controller lease; reassigns the card to `worker-<model>`) receives the worktree path, branch and HEAD, `git status --short` and the diffstat of uncommitted changes, an excerpt of the last report and your continuation. The change log records `dispatch.handoff` with `from_model`, `to_model` and `reason`. It does not carry the old model's private session. MCP: `continuity_worker_handoff` (previews unless `launch=true`; callable by name, listed with `CONTINUITY_MCP_TOOLSET=full`).

A policy refusal is judged from the provider's own error events and plain diagnostic lines, never from tool output such as source code a worker just read. A provider message that states a reset time without a time zone (`... will reset at 2026-10-29 15:31:01`) is read in the host's local time, so the cooldown expires instead of staying open.

HTTP 401/403, Cloudflare/proxy interception pages and gateway diagnostics are not upgraded to content refusal merely because they contain generic "policy" text; only explicit model-safety markers yield `policy-review`. For a legacy false label, a controller may review the exact job and run `continuity worker clear-policy-review --task-id … --job-id job-… --actor … --reason …`, recording one auditable correction and permitting an explicit same-thread resume.

Existing launch scripts can use `collaboration run --task-id … --assignee … -- <command>` or supervisor run for command bookkeeping. `supervisor daemon` performs one owner-host liveness pass, not resident-service installation; it skips full-history replay when no attempt is running. Retries and a project fallback policy handle selected transient failures; authentication errors and policy refusals do not trigger automatic channel switching. `worker completion-hook` can launch a local argv asynchronously and record a receipt without blocking worker completion; it is not a remote wake-up.

For an explicitly protected canonical repository, set `--worker-write-policy canonical-read-only` and one or more `--canonical-repository-root` values in collaboration context-set. A Continuity Git wrapper on worker PATH blocks ordinary push/update-ref/receive-pack writes into that repository. This prevents mistakes, not hostile bypass through absolute git paths, another program or a more privileged account.

<a id="mcp"></a>
## MCP and plugins: direct agent access

The CLI implements the operations. **MCP exposes callable tools; plugins install MCP configuration, skills and/or hooks into a host.** Choose the components you need and do not manually register servers already supplied by a plugin.

MCP is a host-managed local stdio child. After a host reboot or kill, the Continuity state store cannot restart it by itself; use the CLI/SSH route until Codex or Claude opens a new MCP connection. `collaboration doctor` reports this lifecycle boundary.

| Integration | What it provides | Choose it when |
|---|---|---|
| Codex plugin continuity-plane | Session/compaction lifecycle hooks; no MCP registration | You want automatic recovery and notices |
| Codex plugin continuity-plane-search | continuity-search-mcp and its lookup tool | You only need bounded code retrieval |
| Codex plugin continuity-plane-state | continuity-mcp: Todo/collaboration plus advanced State tools | The agent should manage cards, rules and handoffs; ordinary planning needs no State claim |
| Claude Code plugin continuity-plane | Collaboration skill, lifecycle hooks and both continuity/continuity-search MCP servers | One plugin for planning, retrieval and lifecycle integration |
| Other stdio MCP clients | Connect the two servers below directly | You do not use a supported plugin marketplace |

### Install a plugin

Install the CLI first. On Codex CLI versions supporting these commands, choose the needed components:

```bash
# Core hooks, independent search, and planning/State are separate components
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.36
codex plugin add continuity-plane@continuity-plane
codex plugin add continuity-plane-search@continuity-plane
codex plugin add continuity-plane-state@continuity-plane
```

The host plugin browser can select the same packages. Review /hooks and trust the current definitions; installation does not automatically grant hook trust. See the [official mechanism](https://developers.openai.com/plugins/build/plugins) and [deployment/repinning instructions](deployment.en.md).

Install Claude Code in the actual active profile:

```bash
claude plugin marketplace add skyhua0224/continuity-plane
claude plugin install continuity-plane@continuity-plane --scope user
claude plugin list
```

Configure and verify multiple profiles separately. Remote-state deployments use the [SSH bridge](deployment.en.md#separate-controller-and-state-hosts), without duplicate local plugin hooks. A CLI upgrade does not refresh plugin caches or already running MCP processes. Check the package, plugin and fresh MCP serverInfo.version; reconnect or start a new session as needed.

### Which MCP tools are available?

From Alpha36, everyday operations are separate typed tools. Their parameters are generated from the CLI definitions and they run through the same code and guards as the CLI. `project_id` and identity default to the environment (`CONTINUITY_PROJECT_ID`, `CONTINUITY_TASK_ID`, `CONTINUITY_ASSIGNEE`) or to the registered project containing the working directory; every result carries `next`, the call that usually follows.

| Tool | When to use it | CLI equivalent |
|---|---|---|
| `continuity_start` | Session start, after compaction, on a new assignment: your cards, unread messages and recent changes in one call | `collaboration packet` + `inbox list` + `task show` |
| `continuity_task_add` | Split work into a card; `body` is required and says what to do, the scope and how to check it | `collaboration task add` |
| `continuity_task_update` | Status, next step, assignee, branch, commit or body changes | `collaboration task update` |
| `continuity_task_show` / `continuity_task_list` | Read one card / list cards by filter | `collaboration task show/list` |
| `continuity_handoff_write` / `continuity_handoff_show` | Write before stopping or compaction / read when taking over | `handoff write/show` |
| `continuity_worker_dispatch` | Send a card to a worker; previews by default, `launch=true` starts it in the background | `dispatch --dry-run` / `--background` |
| `continuity_worker_jobs` / `continuity_worker_report` | List runs / read a card's report | `worker jobs` / `worker report` |
| `continuity_events_wait` | One check for workers that started, ended, failed, hit rate limits or blocked (at most 50 s; never loop it), across all projects; the server remembers where you stopped. For long waits run `worker wait` as a background task | `worker events --all-projects` / `worker wait` |
| `continuity_progress` | A worker records a milestone | `collaboration task update --next-action` |
| `continuity_report_final` | A worker finishes: report saved, checks recorded, card ready for review, dispatcher told | `task update --report-file --verification-json` + `inbox send` |
| `continuity_review_request` | Create a review card for a finished card | `collaboration task add` (review lane) |

The tools below have existed since Alpha35 and remain available:

| Tool | Purpose and examples |
|---|---|
| continuity_help | Discover actual parameters by topic: todo.compile, task.add, rules.export |
| continuity_plan | Select actions for Todo, cards, inbox, rules, references, handoffs, lessons, sessions, changes/wait, batches/decisions, usage and worker.prepare previews |
| continuity_collaboration / continuity_collaboration_packet | Bounded collaboration reads and the assigned task entry, without a State write binding |
| continuity_todo_compile, continuity_todo_list, continuity_todo_complete, continuity_todo_block, continuity_todo_link, continuity_todo_activate | Direct Todo tools; continuity_plan is the common alternative |
| continuity_inbox | Send, list, single/bulk acknowledge and archive persistent messages; generated send IDs and structured failed_gate errors |
| continuity_context_lookup | The separate search server's bounded symbol/path lookup, brief by default |
| continuity_resume, continuity_inspect | Bind an advanced State recovery entry or inspect it read-only |
| continuity_checkpoint, continuity_autorun | Create/verify checkpoints or continue the current Work from a verified entry |
| continuity_work_activate, continuity_work_complete, continuity_work_transition, continuity_claim_recover | State Work lifecycle and leases, retaining revision/authorization/validator gates |

For example, pass these arguments to continuity_plan; it shares the CLI todo from-cards contract:

```json
{"action":"todo.from-cards","project_id":"demo","format":"brief","parameters":{"assignee":"worker-a"}}
```

For continuity_context_lookup, root is absolute on the host containing the code:

```json
{"root":"/workspace/project","query":"parse_message","format":"brief","max_results":5,"max_output_bytes":1024}
```

Not every CLI command is an MCP execution tool. **Dispatch/worker resume, adapter configuration, supervisor and controller run currently use the CLI.** An agent may invoke them through its authorized shell; planning MCP itself does not start providers, merge Git or deploy.

Without plugins, add this connection configuration to your MCP client. It is host configuration, not a one-shot shell command:

```json
{
  "mcpServers": {
    "continuity": {"command":"continuity-mcp","args":[]},
    "continuity-search": {"command":"continuity-search-mcp","args":[]}
  }
}
```

When state lives on another host, use `continuity-mcp --proxy ssh://HOST` (`continuity-search-mcp --proxy ssh://HOST` for search) as the host's MCP command: a local proxy caches the tool list and reconnects after drops, so SSH flaps no longer change the host's list. See [Deployment](deployment.en.md#stable-mcp-over-ssh).

<a id="hooks"></a>
## Hooks: what happens automatically?

| Event | Codex | Claude Code | Continuity behavior |
|---|---|---|---|
| SessionStart | Supported | Supported | Bounded recovery entry and meaningful changes, not mandatory full-history rereading |
| PreCompact | Supported | Supported | Save next action, constraints, verification and controller scene; checkpoint State when configured |
| PostCompact | Verify/recover through the host-supported path | Verify; inject at SessionStart(compact) | Deduplicate delivery for a recovery generation |
| UserPromptSubmit | Supported | Supported | Bounded planning/intake/change guidance, without a second forced Todo for card workflows |
| PreToolUse | Supported | Not supplied by this plugin | Lightweight Codex boundary advice, not mandatory interception of all shell/Git effects |
| PostToolUse | Supported | Supported | Bounded observation and changed worker-end/blocker/rule notices |
| Stop | Supported | Supported | Completion/continuation checks, not inferred verification or review acceptance |
| SubagentStart / SubagentStop | Not supplied by this plugin | Supported | Observe native children, without invented PIDs, exit codes, success or complete reports |

The Alpha35 plugin and source handoff retain SessionEnd and add resumable worker events, inbox wait/unread, job terminal state and actual-checkout observations. The [handoff implementation](field-workflows.md#development-source-controller-recovery-and-change-consumers) also includes read-time observations, consumer acknowledgements, dispatch snapshot checks and task aliases. Older installs require an upgrade and host reconnect.

Skills are instructions the model can load; MCP tools are callable operations; hooks run at lifecycle events. Notices are changed-only and do not guarantee waking an offline conversation. Managed attempt records require a worker running under supervision.

Automatic continuity recovers recorded state; it does not guarantee capturing every idea never written to a card or handoff.

The host must load and permit hooks. CONTINUITY_DISABLED=1 disables them temporarily. Check versions/entrypoints with doctor and inspect actual save/restore events. Installed configuration, controlled callback tests and naturally observed compaction/child lifecycles are distinct evidence.

<a id="context"></a>
## Code retrieval and context savings

```bash
# Substitute symbols and paths from the current repository
continuity context index --root .
continuity context lookup --root . --query parse_message --format brief --max-output-bytes 1024
continuity context search --root . --query 'parse_message' --path-prefix src --format brief --max-output-bytes 2048
continuity context dependents --root . --query include/parser.h --format brief
```

The incremental index covers Git-tracked files and returns positions/bounded excerpts. Indexed C/C++ includes and CMake links support reverse-dependency queries and directory aggregation; prompts can carry code routes too. This is not a complete compiler-semantic call graph. After a miss, narrow the source search rather than repeating the same empty lookup.

Brief omits the full hashed JSON receipt; use supported --format json for machine processing. Result counts and byte budgets follow each command's help, not one universal flag set: legacy packet does not accept every budget option. Consume paged changes from next_cursor, not clipped content as if it never happened.

<a id="knowledge"></a>
## Rules, memory, references and lessons

| Record | What it stores | When to use it |
|---|---|---|
| rules | Global/project constraints, audiences, versions and hashed long attachments | Workers/reviewers must receive the same applicable policy |
| memory | Scoped vocabulary, aliases and definitions | Shared terminology or abbreviations; not the whole conversation |
| reference | Standards, official OS/library docs and repositories, versions/sections and adoption rationale | Implement from primary evidence and retain what was actually checked |
| lesson | Project lessons and sourced corrections | Avoid repeating a mistake after a model/session change |

```bash
# Policies and source indexes remain separate records
continuity collaboration rule-add --project-id demo --rule-id focused-change --audience worker --text 'Stay within task scope; report actual checks and unverified items'
continuity rules export --project-id demo --audience worker --format text
continuity memory add --project-id demo --scope project --entity-id debug-build --alias 'debug configuration' --statement 'This project Debug build configuration' --source 'Project build instructions'
continuity reference add --project-id demo --reference-id python-json --title 'Official Python JSON documentation' --source https://docs.python.org/3/library/json.html --kind official-library
continuity lesson list --project-id demo
```

Select worker/reviewer/controller or lane-specific policies. Rules history/affected shows revisions and workers with old snapshots; rules attachment reads long text by hash. Complete export never silently drops constraints; increase an insufficient budget explicitly. Collaboration prompt assembles rules, card instructions and attachment references.

Rules import adopts an explicit rules manifest; rules check-mirror detects drift in generated policy files. Snapshot-export/snapshot-import exchanges selected rule snapshots, not entire native profiles.

Use reference import-tsv/export-tsv to exchange an existing source-index.tsv, without overwriting conflicts or existing output files. Record actual reading/adoption separately through reference record. Registering a URL proves neither reading, freshness nor correctness. Records default to the external data root, not native account/session files admitted to Git.

<a id="handoff"></a>
## Controller handoffs and unresolved work

Keep the controller plan shared rather than only in private chat. Handoff records the current batch, ordering, in-flight workers, pending decisions and next action. Commitments record promises that remain unfinished:

```bash
continuity handoff write --project-id demo --plan 'Fix the parser regression, then request independent verification' --next-action 'Check worker reports against actual verification' --pending 'Owner decision needed on compatibility'
continuity handoff show --project-id demo
continuity collaboration commitment-add --project-id demo --commitment-id compatibility-decision --text 'Waiting for the owner to confirm compatibility'
continuity collaboration commitment-list --project-id demo
```

`collaboration context-set --integration-base main` selects the project's default implementation base, distinct from a checkout HEAD. A locked `integration_base` in repository `.continuity/controller-profile.json` is also a project default. Worker prompt assembly includes the base and any available authoritative HEAD while stating that it grants no merge, push, deployment or ownership authority. Session bind declares controller/worker sessions and associates tasks/batches. Shared-scene refresh at controller compaction and batch recording respects role/lease boundaries; failed refresh remains a warning, not a successful new snapshot.

When `handoff show` is truncated, pending decisions, blocked cards, failed/interrupted attempts and report-without-card-update rows survive before ordinary changes. Fast handoff writes record a change cursor, so later reads inspect only newer events; rewrite an older handoff once to establish that boundary. The brief `unread` value means unacknowledged inbox messages and includes bulk acknowledgement plus archive commands. Acknowledging removes a message from unread; archive then moves it to recoverable storage.

The handoff `plan` keeps up to 4 KiB in `handoff show` (it was cut at 640 bytes when output was truncated, and at 300 characters in brief). Under a tight `--max-output-bytes` it still shrinks together with the other fields, after pending decisions and blocked/failed attention.

<a id="verification"></a>
## Verification, integration batches and decisions

Reports attach to cards rather than remaining buried in logs. Manual workers can submit one too:

```bash
# Use a real final report file from the task
continuity collaboration task update --project-id demo --task-id parser-fix --status ready-for-review --report-file /persistent/path/report.md
continuity collaboration task show --project-id demo --task-id parser-fix --include-report --format brief
continuity help batch-add
continuity help decision-add
```

Branch, commit, landed_commit and released_in store the corresponding Git facts. --verification-json separates checks from unverified items and binds evidence to a real commit. Batch-add records cards, baseline, result commit, verification, metrics and exception decisions. Decision-add retains real approval sources/corrections; status-render generates a projection.

Task update can set metadata and merged in one call when required evidence is present; `task merged … --batch …` uses an existing batch. Neither performs Git merging, fast-forwarding or push. Evidence gaps remain visible, overrides require reasons, and metadata cannot manufacture user approval.

Non-code decisions may omit a commit. If a commit is supplied, it is still strictly validated as a Git object identifier.

<a id="usage"></a>
## Measurement, model routing and availability

```bash
continuity observe report --project-id demo
continuity collaboration scenarios
continuity collaboration recommend --project-id demo --scenario quick-triage --busy-model grok
continuity quota report --project-id demo
continuity dispatch-health show --project-id demo --format brief
```

Observe distinguishes actual output/hook bytes, estimated replacement output, index caches and provider token/prompt-cache counters; net byte estimates may be negative. Explicit usage import reads Claude/Codex counters in place on the owning host/profile, not transcript bodies saved into the registry. Session binding attributes increments after its baseline to tasks/batches. Missing provenance, price or comparison data remains unknown; cache hits are not causal token savings.

Longitudinal benefits such as effective context-window utilization and work completed between compactions still require real-session comparisons; these counters alone do not establish them.

Run these only on that session's owning host/profile; substitute its actual session ID and local file:

```bash
# Collect in place; do not copy foreign accounts or raw sessions here
continuity session bind --project-id demo --provider claude --session-id OWN_SESSION_ID --task-id parser-fix --role worker
continuity observe ingest-usage --root . --provider claude --session-id OWN_SESSION_ID --transcript /path/on/owner/session.jsonl
```

Recommend considers scenario fit, actual run counts/duration and recorded cooldowns. Daily and expensive expert pools are distinct; restrict eligible-model or declare busy/unavailable models. It does not dispatch, guarantee exact equal shares or know unrecorded account capacity. Quota records known availability/limits and explicitly approved handoffs, without a promise of automatic native Claude quota queries.

`quota record` names the missing `--official-subscription` flag instead of implying that token counts are subscription limits. When a worker observes a clear usage limit and reset time, an official-subscription origin/model is recorded automatically. Without a usable reset, dispatch pauses with an unknown reset rather than inventing one.

`dispatch-health show` now explains the failure class, provider reason, task/attempt, observation time and reset. Capacity scope is included in controller status and reservation inventory.

<a id="advanced"></a>
## Remote, coordination and advanced commands

```bash
continuity --remote state-host --remote-root /srv/project collaboration task list --project-id demo --format brief
```

Explicitly configure SSH aliases, project roots and owning-host adapters. Bridge install forwards controller hooks; retrieval can run on the state host or use exact-root bridge_config mappings. Keep profiles separate; do not move native accounts, sessions or quota data. There is no arbitrary remote file/process administration API. Store recoverable work persistently; temporary storage is for reconstructible buffers.

| Command family | Purpose and boundary |
|---|---|
| controller acquire/renew/status/run | `acquire --token T` uses T as the lease token (16–128 characters of letters, digits, `_`, `.`, `-`) and always prints the active token; explicit controller fencing, capacity and cooperating serialized integration; cannot stop Git outside the tool. Upgrade all dispatchers before first guarded-v4 activation |
| collaboration effect request/list/update | External-operation requests, approvals and receipts; not automatic push/deploy execution |
| collaboration outbox-replay/repair-events | Recover execution facts retained during metadata I/O failures and inspect legacy cursor problems; apply repairs explicitly with backup |
| init/verify/attach, state/status/workspace | Project profiles, existing governance adoption and State/projections; optional advanced layer |
| resume/inspect/autorun/checkpoint/work | Revision/claim/authorization/validator-gated Work recovery and commits; not prerequisites for ordinary Todo/MCP |
| decision/constraint, export/import/rollback | Authoritative planning metadata and verified State backup/rollback; bundles do not cover the entire external collaboration root |
| plan inspect, worktree-inventory, skills-audit, storage inspect | Bounded plan routing and worktree/skill/persistence audits, without automatic user-file cleanup |
