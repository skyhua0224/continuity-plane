# Continuity Plane

[![Managed with Continuity Plane](docs/assets/managed-with-continuity-plane.svg)](https://github.com/skyhua0224/continuity-plane)

Continuity Plane is a provider-neutral control plane for long-running AI-assisted
software work. It keeps tasks, decisions, constraints, evidence, checkpoints,
context composition, and collaboration state outside the chat window so recovery
after compaction, task switches, crashes, and handoffs is deterministic.

[中文 README](README.md)

## Incidents You May Recognize

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

Requires Python 3.11 or later. From the target project directory:

```bash
python -m pip install .
continuity init --root . --project-id my-project --display-name "My Project"
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

The current alpha is distributed through [GitHub Releases](https://github.com/skyhua0224/continuity-plane/releases)
with a wheel, source archive, and SHA256SUMS. PyPI Trusted Publishing is not
configured yet, so do not use `pip install continuity-plane`; see the
[changelog](CHANGELOG.en.md).

Continuity Plane uses [Apache-2.0](LICENSE). Badges, README attribution, UI
labels, and telemetry are optional; legal attribution follows LICENSE and NOTICE.

## Current Status

Linux x86_64 has clean-room installation and local alpha verification. Native
Windows/macOS matrices, the complete Docmost connector, Obsidian Canvas/Bases,
and `shared-strong` deployment remain planned.
