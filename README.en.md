# Continuity Plane

Tasks, rules, handoffs and verification evidence for Codex, Claude Code and other agents. Dispatch workers, resume interrupted work, receive completion events and retrieve bounded code references.

[中文](README.md) · [Quick start](USAGE.en.md) · [Deployment](docs/deployment.en.md) · [Workflows](docs/workflows.en.md) · [Capabilities and validation](docs/validation.en.md) · [Download](https://github.com/skyhua0224/continuity-plane/releases/latest)

[![PyPI](https://img.shields.io/pypi/v/continuity-plane?include_prereleases)](https://pypi.org/project/continuity-plane/)
[![Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Managed with Continuity Plane](docs/assets/managed-with-continuity-plane.svg)](https://github.com/skyhua0224/continuity-plane)

## Problems It Solves

| When this happens | What you can use today | Details |
|---|---|---|
| Compaction loses the next action; the next agent starts exploring again | Bounded continuation and a shared controller handoff: plan, batch, workers, pending decisions and verification | [Lifecycle](docs/lifecycle-parity.md) |
| Agents cannot see each other's progress | Task cards, inbox, incremental changes and blocking event waits | [Workflows](docs/workflows.en.md) |
| A worker exits quietly and its report is buried in logs | `dispatch` / `resume` / `report`, supervised attempts, exit codes and attached reports | [Dispatch reference](docs/field-workflows.md#dispatch-resume-and-reports) |
| Rules drift across scripts and workers miss constraints | Global/project audience rules, hashed policies, complete exports, prompt assembly and primary-source records | [Rules and sources](docs/workflows.en.md#rules-sources-and-lessons) |
| Finding one symbol fills the context with source | Incremental indexing, brief output, bounded dependency queries and code routes in prompts | [Coverage](docs/validation.en.md) |
| Plans drift, commitments disappear and merge evidence is unclear | Card-backed Todo, commit-bound verification, batches, decisions, commitments, lessons and drift checks | [Workflows](docs/workflows.en.md) |

The core is one CLI. Tasks, retrieval and collaboration work without plugins. The default needs no cloud account, PostgreSQL, Docmost or resident daemon. Native agent clients keep their own authentication.

## Five-minute start

Requires Python 3.11+. This example uses a macOS/Linux shell; see [deployment](docs/deployment.en.md) for Windows paths.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'continuity-plane==0.1.0a26'
.venv/bin/continuity init --root . --project-id demo
.venv/bin/continuity collaboration register --project-id demo --control-root . --repository-root .
.venv/bin/continuity whoami --root . --assignee controller --controller controller
.venv/bin/continuity collaboration packet --project-id demo --assignee controller
```

Run inside the target repository and choose a stable ID. Initialization refuses to overwrite an existing `.continuity/`; use the [State adoption guide](docs/state-workflows.en.md) for existing governance documents. Later examples assume `continuity` is on PATH.

| Your workflow | Start here |
|---|---|
| One person protecting work from compaction | [Quick start](USAGE.en.md), then enable host hooks |
| One controller with workers | [A complete two-worker path](docs/workflows.en.md#one-controller-and-two-workers) |
| Multiple machines or a team | [SSH deployment](docs/deployment.en.md#separate-controller-and-state-hosts), then opt into fencing when needed |

## Core capabilities

- **Work in progress:** bind sessions to tasks/batches; recover the shared plan, rules, commitments and lessons. The integration ref is distinct from checkout HEAD.
- **Dispatch lifecycle:** assemble rules and card bodies, then run a configured adapter under the supervisor. New attempts default to 24 hours total / 90 minutes idle; limits and effort are explicit.
- **Failure and resume:** classify failures and resume a recorded native thread where available. Policy refusals do not switch channels. Lease/capacity refusal preserves the finished result and explains deferral.
- **Coordination:** filtered cards, paginated changes, multi-event waits and durable messages. Explicit controller fences and atomic reservations protect tool-mediated operations.
- **Rules and evidence:** complete rule delivery, hashed attachments, source records, reports, structured verification, batch metrics and attributable decisions.
- **Visible accounting:** response/injection bytes, host tokens and prompt-cache counters remain separate. Negative retrieval estimates stay visible; cache hits are not a savings claim.

Discover parameters through `continuity help` and command `--help`. Ordinary collaboration needs no State claim.

## Codex and Claude Code

| Entry point | Provides | Boundary |
|---|---|---|
| Codex plugin `continuity-plane` | Bounded recovery and lifecycle hooks | Core does not register State MCP; hooks still need host trust and actual events |
| Codex plugin `continuity-plane-search` | `continuity_context_lookup` | Independent of State; use narrow search after a miss |
| State plugin / explicit `continuity-mcp` | State tools and metadata tools such as `continuity_plan` | Metadata needs no State resume; authoritative writes keep their gates |
| Claude Code plugin | MCP and eight host event hooks | SubagentStart/Stop are observations, not native process control |
| Other agents | CLI or stdio MCP | No required provider or model |

See [deployment](docs/deployment.en.md) for installation, discovery, hook checks, duplicate-server troubleshooting and removal. Running sessions do not hot-load new MCP code: reconnect or start a new session after upgrading.

## Validation

The [capability matrix](docs/validation.en.md) separates synthetic CLI measurements, automated regressions, user feedback and uncovered behavior. It covers dispatch, failed exits, reports, rules, events, handoffs and fencing.

Historical compaction, retrieval, Skill and coordination comparisons remain in [benchmark methods](docs/benchmarks.en.md). They are not universal or current-version reduction rates. Useful window utilization, longitudinal per-task cost and causal token savings still require matched real-session evidence.

## Data and operation boundaries

- Credentials, private provider configuration and native sessions stay on their owning machine/profile. Remote use executes there instead of moving an account to another host.
- Only authorized project records, artifact references and origin-labeled counters are shared. Keep recoverable work on persistent storage.
- Inbox and hooks cannot wake every session. Reports are not permission to merge, push or deploy.
- Controller leases coordinate cooperating clients, not arbitrary Git/scripts or an OS sandbox.
- First `controller acquire` enables guarded ledger v4 for that project. Upgrade every participating dispatcher first; installation alone does not enable it.

## Documentation

| Need | Guide |
|---|---|
| Installation, upgrades, OS paths, remote hooks, rollback | [Deployment](docs/deployment.en.md) |
| Solo, controller and team paths | [Quick start](USAGE.en.md) · [Workflows](docs/workflows.en.md) |
| Agent startup and task brief | [Four commands and convention](docs/agent-start.en.md) |
| Current coverage and missing validation | [Validation](docs/validation.en.md) · [Historical experiments](docs/benchmarks.en.md) |
| Detailed collaboration / advanced State | [Command reference](docs/field-workflows.md) · [State](docs/state-workflows.en.md) |
| Changes, compatibility and release conventions | [Changelog](CHANGELOG.en.md) · [Release policy](docs/releases.en.md) |
| Architecture, configuration, API, optional views | [Index](docs/README.en.md) · [Project views](docs/project-views.en.md) · [Visual products](docs/visual-products.en.md) · [Use cases](docs/use-cases.en.md) |

## Feedback and license

Report the version, sanitized command, expected/actual behavior and a minimal reproduction in [Issues](https://github.com/skyhua0224/continuity-plane/issues). Do not upload raw sessions, account files, device identifiers or private project data.

[Apache-2.0](LICENSE); attribution requirements are in [NOTICE](NOTICE). README badges and telemetry are optional.
