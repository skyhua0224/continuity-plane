# Continuity Plane

[![Managed with Continuity Plane](docs/assets/managed-with-continuity-plane.svg)](https://github.com/skyhua0224/continuity-plane)

Continuity Plane is a provider-neutral control plane for long-running AI-assisted
software work. It keeps tasks, decisions, constraints, evidence, checkpoints,
context composition, and collaboration state outside the chat window so recovery
after compaction, task switches, crashes, and handoffs is deterministic.

[中文 README](README.md)

## Install

Install one CLI first:

```bash
python -m pip install continuity-plane==0.1.0a7
```

### Codex plugin (optional)

The core package does not depend on a plugin. To enable automatic bounded packet loading,
pre/post-compaction checkpoints, recovery canaries, and external-effect preflight, install the
Codex plugin from this repository's public GitHub marketplace:

```bash
codex plugin marketplace add skyhua0224/continuity-plane --ref v0.1.0-alpha.7
codex plugin add continuity-plane@continuity-plane
```

Start a new Session after installation. The plugin discovers the current project root and binds
the local `.continuity/` state automatically. It is only a provider integration; authoritative
state remains managed by the CLI/State MCP.

### One Project

Use this when one repository should own its state and pinned control-plane version.

```bash
continuity init --root . --project-id my-project --display-name "My Project"
```

### One CLI For Multiple Projects

Use this when one machine manages several repositories. Each project gets its own
`.continuity/` directory and SQLite state.

```bash
continuity init --root /path/to/project-a --project-id project-a --display-name "Project A"
continuity init --root /path/to/project-b --project-id project-b --display-name "Project B"
```

### Personal Use In A Collaborative Repository

Use this when joining a team repository while keeping coordination local to your own Sessions.

```bash
continuity init --root /path/to/team-repo --project-id team-project --display-name "Team Project"
```

### Team-Wide Use

Use this when the team needs shared Work, claims, PR/CI, and deployment state. Initialize
locally first, then enable `forge-coordinated` or `shared-strong` when the project needs it.
The default installation still requires neither PostgreSQL nor Docmost.

Common parameters: `--root` selects the repository, `--project-id` is a stable lowercase
identifier, and `--display-name` is the human-readable name. See the
[complete installation, usage, and profile switching guide](USAGE.en.md).

## Problems It Solves

| Scenario | Pain point | Details |
|---|---|---|
| Compaction and long Sessions | The agent was fixing a test, then repeats an old question and redoes completed work | [Use case](docs/use-cases.en.md#compaction-makes-the-agent-look-different) |
| Multi-Session and deployment races | Both Sessions think they can deploy until main, CI, and the environment conflict | [Use case](docs/use-cases.en.md#sessions-step-on-each-other) |
| Team and multi-Agent work | Unpublished local work is invisible, so collaborators implement and search twice | [Use case](docs/use-cases.en.md#people-and-agents-repeat-each-others-work) |
| Ideas and task switches | A casual idea pulls the agent away from the mainline and loses the return point | [Use case](docs/use-cases.en.md#a-casual-idea-pulls-the-mainline-away) |
| Large projects | Hundreds of modules and cross-repository dependencies hide the impact of a change | [Use case](docs/use-cases.en.md#large-projects-lose-their-shape) |
| Memory, Skills, and documentation drift | Old paths, decisions, and rules return after compaction | [Use case](docs/use-cases.en.md#memory-skills-and-documents-drift) |

## Measured Results

| Scenario | Result | Details |
|---|---|---|
| Recovery after compaction | input tokens `-40.25%`; near-limit history `-95.06%`; quality `3/3` | [Recovery benchmark](docs/benchmarks.en.md#compaction-and-recovery) |
| Code retrieval | input `-50.02%`; tool calls `-57.89%`; wall time `-27.41%`; quality `3/3` | [Retrieval benchmark](docs/benchmarks.en.md#code-retrieval) |
| Skill loading | source bytes `-96.54%`; quality `3/3` | [Skill benchmark](docs/benchmarks.en.md#skill-loading) |
| Multi-Session coordination | duplicate tool calls `-55.88%`; parallel wall time `-22.65%` | [Collaboration benchmark](docs/benchmarks.en.md#multi-session-collaboration) |
| Consistency | E0-E9 `10/10`; dual-Session `1000/1000`; authority violations `0` | [Consistency benchmark](docs/benchmarks.en.md#consistency-and-limitations) |
| Large-project views | 2,000 nodes / 5,000 edges; scale p95 `187.459764 ms` | [Project views](docs/project-views.en.md) |

These are scenario-level results from matched tasks and current fixtures, not one
universal savings percentage. User tokens, useful window utilization, and accepted
work between compactions are normalized by accepted Work and measured only when
host traces expose the required signals. [Full methods and limitations](docs/benchmarks.en.md).

## Architecture At A Glance

```text
Agent / IDE / CI / Human console
              |
              v
       Execution Packet
              |
     +--------+---------+
     |                  |
Typed State         Evidence index
revision + CAS      hashes + validity
     |                  |
     +--------+---------+
              |
      append-only events
              |
    checkpoint + replay canary
              |
      SQLite by default
```

Memory, retrieval, code graphs, and reviewers provide candidates only. Active
tasks, completion, and external effects pass through State MCP authorization,
expected revision or CAS, and validators.

## Quick Start

Requires Python 3.11 or later. From an installed CLI in the target project directory:

```bash
continuity verify --root .
continuity doctor --root .
continuity state show --root .
```

Initialization creates `.continuity/`, a SQLite state store, and project-owned
`MASTER.md`, `STATUS.md`, and English templates. The project chooses its own
`project_id` and `display_name`.

## Installation Profiles

| Profile | Extra services | Use case | Details |
|---|---|---|---|
| `local-embedded` | none | personal work, offline development, local Sessions | [Configuration](docs/configuration.en.md) |
| `forge-coordinated` | existing Git forge | ordinary open-source collaboration | [Configuration](docs/configuration.en.md#profiles) |
| personal PostgreSQL | local/private PostgreSQL | SQL inspection, backup, local workers | [Configuration](docs/configuration.en.md#profiles) |
| personal Docmost | Docmost plus connector | graphs, approvals, history | [Visual products](docs/visual-products.en.md) |
| `shared-strong` | explicit State MCP service | unique cross-device claims, leases, and CAS | [Configuration](docs/configuration.en.md#profiles) |

The default path is `local-embedded`. PostgreSQL, Docmost, and `shared-strong`
are optional enhancements.

## Authority Boundaries

- `Typed State`: current task, owner, revision, decisions, constraints, and gates;
- `Event Log`: append-only changes, supersedes, and hash chain;
- `Checkpoint`: recovery point for compaction, switches, handoffs, and crashes;
- `Evidence`: provenance for current source, standards, official documents, and tests;
- `MASTER.md`: project governance intent; `STATUS.md`: current recovery route;
- Docmost: optional human console whose actions are State MCP constrained;
- Obsidian: generated read-only view;
- SQLite: default local authority; PostgreSQL: an explicitly selected adapter.

## Documentation

- [Usage guide](USAGE.en.md)
- [Architecture](docs/architecture.en.md)
- [Configuration](docs/configuration.en.md)
- [Python API](docs/api.en.md)
- [Benchmark method](docs/benchmarks.en.md)
- [Use cases](docs/use-cases.en.md)
- [Large-project views](docs/project-views.en.md)
- [Docmost and Obsidian visual product plan](docs/visual-products.en.md)
- [Contributing](CONTRIBUTING.en.md)
- [Security](SECURITY.en.md)

## Release And License

The current alpha is available from [PyPI](https://pypi.org/project/continuity-plane/0.1.0a7/)
and [GitHub Releases](https://github.com/skyhua0224/continuity-plane/releases).
The GitHub release also provides the core wheel, source archive, Codex plugin marketplace, and SHA256SUMS; see the
[changelog](CHANGELOG.en.md).

Continuity Plane uses [Apache-2.0](LICENSE). Badges, README attribution, UI
labels, and telemetry are optional; legal attribution follows LICENSE and NOTICE.

## Current Status

Linux x86_64, macOS arm64, and Windows AMD64 have completed installation,
verification, and uninstall probes. Cross-platform export/import/rollback, the
complete Docmost connector, Obsidian Canvas/Bases, and `shared-strong` deployment
remain planned.
