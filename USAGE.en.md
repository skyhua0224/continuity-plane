# Usage

[中文](USAGE.md) · [README](README.en.md) · [Deployment](docs/deployment.en.md) · [Documentation](docs/README.en.md)

## Choose a path

| Goal | Start |
|---|---|
| Solo work that survives compaction | Initialization and continuation below |
| Dispatch workers, wait and read reports | [Two-worker example](docs/workflows.en.md#one-controller-and-two-workers) |
| A team or separate state host | [Team path](docs/workflows.en.md#a-team) and [SSH deployment](docs/deployment.en.md#separate-controller-and-state-hosts) |
| Existing MASTER/STATUS or explicit State claims | [Advanced State adoption](docs/state-workflows.en.md) |

## Install

Python 3.11+. Prefer an isolated environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'continuity-plane==0.1.0a26'
```

On Windows use `.venv\Scripts\python.exe` and `.venv\Scripts\continuity.exe`. Later examples assume `continuity` is on PATH. Global pipx installation, upgrades, offline wheels and rollback are in [deployment](docs/deployment.en.md).

## Initialize

Run in your repository:

```bash
continuity init --root . --project-id demo
continuity collaboration register --project-id demo --control-root . --repository-root .
continuity whoami --root . --assignee controller --controller controller
continuity collaboration packet --project-id demo --assignee controller
```

`init` creates project-local `.continuity/` with project.yaml, State SQLite and governance templates. `register` adds the project to the user collaboration registry. Do not reinitialize or overwrite existing plans; see State adoption first.

`whoami` reports this invocation's identity; it neither acquires a lease nor logs into a provider. For a persistent shell set `CONTINUITY_ASSIGNEE` and `CONTINUITY_CONTROLLER`. Give concurrent worker instances separate identities.

## Connect your agent

A working CLI does not prove hooks are enabled. Choose Codex, Claude Code or generic MCP in [deployment](docs/deployment.en.md), then inspect actual tools and event receipts. Keep native authentication and routing intact.

## Compaction and continuation

Save the small increment needed to continue:

```bash
continuity handoff write --project-id demo --plan 'Finish the current regression before the next item' --next-action 'Run the task tests'
continuity handoff show --project-id demo
continuity lesson add --project-id demo --lesson-id check-logs --text 'Inspect the failing check before diagnosing load' --source 'Current regression record'
```

Enabled hooks save a session return point before compaction and restore it through the host's supported event. Claude injects at SessionStart(compact); PostCompact only verifies. Without hooks this does not happen automatically. A deferred shared-handoff refresh is a warning, not proof that shared state is current.

## Collaboration

Cards are the work list; Todo can project them instead of becoming a second stale queue.

```bash
continuity todo from-cards --project-id demo
continuity collaboration task list --project-id demo --status dispatched --format brief
continuity collaboration changes --project-id demo --since 0
continuity collaboration wait --project-id demo --since 0 --event attempt.finished --event attempt.timeout --timeout-seconds 30
```

Persist `next_cursor` and continue pages while `truncated` is true. A nonzero wait timeout is not a failed worker. Recipients must read and acknowledge messages; inbox is not a process wake-up mechanism. See [workflows](docs/workflows.en.md) for dispatch, handback and evidence.

## Read less code

```bash
continuity context index --root .
continuity context lookup --root . --query YOUR_SYMBOL --format brief --max-output-bytes 1024
continuity context dependents --root . --query YOUR_HEADER --format brief
```

Replace the identifiers. Indexing uses current Git tracked files; follow a miss with narrow search. Dependents are indexed relations, not a complete compiler call graph. Legacy subcommands do not share every flag: inspect `--help` rather than adding unsupported packet budget options.

## Rules and primary sources

Make rules canonical; generate any script mirror and reference long policies by hash.

```bash
continuity rules export --project-id demo --audience worker --format text
continuity reference list --root .
continuity help reference.add
```

Record current standards and official OS/library documentation, sections read and adoption reasons. These are source declarations, not proof of correct implementation. Missing sources leave explicit unverified scope, not an invented “best” implementation.

## Accounting and validation

```bash
continuity observe report --root .
continuity help
```

Response bytes, hook overhead, replacement estimates and host tokens/cache counters are separate. Negative estimates remain visible. Unknown pricing, capacity or provenance stays unknown. See [validation](docs/validation.en.md) for current capabilities and synthetic measurements.

## Storage

State, collaboration data, index caches and native sessions are different stores. Paths, data-root flags, environment variables and remote isolation are in [deployment](docs/deployment.en.md). Do not move an account directory to query it elsewhere or keep recoverable artifacts on volatile storage.

## Backup and removal

Back up project `.continuity/`, the collaboration data root and custom artifacts before removing software. State bundle export/import/rollback covers only its declared scope, not the entire collaboration registry or native accounts. See [State](docs/state-workflows.en.md).

## Releases and PyPI

Current package: `0.1.0a26`; Git tag: `v0.1.0-alpha.26`. [GitHub Releases](https://github.com/skyhua0224/continuity-plane/releases/latest) use the regular Release category. The alpha version still signals evolving compatibility; PyPI installation requires an exact pin or `--pre`.

[Changelog](CHANGELOG.en.md) · [Release policy](docs/releases.en.md)
