# Usage

[中文](USAGE.md)

## Install

Python 3.11 or newer is required. The current release candidate is verified on
Linux x86_64. Windows and macOS use the same local SQLite profile but have not
completed the native release matrix.

Install from a release checkout:

```bash
python -m pip install .
```

For a source checkout:

```bash
python -m venv .venv
.venv/bin/python -m pip install --editable .
```

### Install Once For Many Projects

Use a dedicated user virtual environment when one person manages several
projects:

```bash
python3 -m venv ~/.local/share/continuity-plane/venv
~/.local/share/continuity-plane/venv/bin/python \
  -m pip install /path/to/continuity-plane-release
```

Run the installed CLI with an explicit project root whenever the command is not
on `PATH`:

```bash
~/.local/share/continuity-plane/venv/bin/continuity \
  init --root /path/to/project --project-id my-project
```

### Isolate One Project

For a project that pins its own control-plane version:

```bash
cd /path/to/project
python3 -m venv .venv
.venv/bin/python -m pip install /path/to/continuity-plane-release
.venv/bin/continuity init --root . --project-id my-project
```

For control-plane development, replace the release path with an editable
install of the source checkout. The package/repository name in these commands
is `continuity-plane`; `project_id` and `display_name` are chosen by the
project owner.

## Initialize A Project

```bash
continuity init \
  --root /path/to/project \
  --project-id my-project \
  --display-name "My Project"
```

The command refuses to overwrite an existing control-plane directory. Project
state stays under `.continuity/` and is independent of the agent or
model currently in use.

```text
.continuity/
  project.yaml
  MASTER.md
  MASTER.en.md
  STATUS.md
  STATUS.en.md
  state.sqlite3
```

Verify the installation:

```bash
continuity verify --root /path/to/project
continuity doctor --root /path/to/project
continuity state show --root /path/to/project
```

`state show` reads the typed snapshot through the same authorization and
validation boundary used by integrations. The initial snapshot has revision
zero and one proposed Work item named `work-initial`.

## Choose A Runtime Profile

### Local Embedded: Default

`local-embedded` stores typed state, events, and local checkpoints in:

```text
.continuity/state.sqlite3
```

It requires no PostgreSQL, Docker, Docmost, network service, or Agent plugin.
This is the recommended profile for personal projects and contributors working
independently in their own clone.

### Forge-Coordinated: Team Without A Shared Database

Use the existing Git forge for shared visibility:

- commit the project-owned `project.yaml` and canonical `MASTER.md` when the
  team wants shared governance;
- keep each contributor's SQLite database, personal STATUS overlay, artifacts,
  and credentials local;
- use Issues, PRs, branch ownership, review, and CI as the team coordination
  boundary;
- run `verify` and the project's normal test/build profile before publishing
  evidence.

This avoids requiring every contributor to connect to a PostgreSQL server. A
local database does not provide a cross-machine unique claim, so the team must
use the forge workflow for visibility until a shared State MCP adapter is
deployed.

### Personal PostgreSQL: Optional

PostgreSQL is useful for one user who wants SQL inspection, durable backups, or
several local workers sharing one database. It is not a requirement for a
single-user installation. Install the optional extra in the environment that
will run the adapter:

```bash
python -m pip install '.[postgres]'
```

The alpha CLI still defaults to SQLite. PostgreSQL is selected by an explicit
Python or provider adapter, for example:

```python
from continuity_plane.postgres_state_store import PostgresStateStore

store = PostgresStateStore("postgresql://localhost/context")
store.initialize()
```

Keep the DSN outside Git. This adapter is a library integration boundary, not a
reason to deploy PostgreSQL for every project.

### Personal Docmost: Optional Human View

Docmost is a human console and projection surface. It can display project
graphs, decisions, evidence, context health, approvals, and replay views when
an external State MCP provider and a Docmost connector are deployed.

Docmost does not become the typed-state authority, and it is not required for
local operation. The current alpha does not include a one-command Docmost
deployment; treat this integration as preview infrastructure.

### Shared-Strong: Team Authority Service

Use `shared-strong` only when workers on different machines need one authority
for claim, lease, ownership, expected revision, and external-effect admission.
The deployment must provide a State MCP adapter with authorization, CAS,
append-only events, durable receipts, project isolation, and audit retention.
PostgreSQL is one possible backend; it is not the only architectural option.

Do not put database credentials in `project.yaml`. The current release does not
turn a local project into shared-strong automatically; this is an explicit
deployment and adapter task.

## Agent Integration

The control plane is installed beside the project and accessed by an Agent
through CLI, Python API, or a provider adapter. A provider-specific plugin is
not required by the core package. A future plugin may automate packet loading,
PreCompact/PostCompact checkpoints, and recovery canaries, but it must remain a
thin integration layer without state authority.

## MASTER And STATUS

`MASTER.md` stores stable intent, constraints, work dependencies, and completion
gates. `STATUS.md` stores the active work, blocker, next action, and recovery
entry. They are project templates, not copies of this repository's development
history.

Keep session narration and raw conversations out of both files. Dynamic history
belongs in typed state and append-only events.

## Daily Workflow

1. Read `STATUS.md`.
2. Claim the active Work at the current revision.
3. Compose a bounded Execution Packet.
4. Load only applicable Skill rules and current evidence.
5. Perform the action inside the claimed scope.
6. Run the project's verification profile.
7. Commit evidence and complete or release the claim.

New ideas default to `capture-and-continue`. They do not replace active Work
until a switch or promotion is explicitly admitted.

## Context Composition

The default packet contains only:

- active Work and revision;
- current decisions and constraints;
- blockers and claim scope;
- next action and return point;
- selected Skill rule IDs and hashes;
- evidence and artifact references;
- continuation cursor and effect watermark.

Large logs, diffs, reports, and source extracts remain content-addressed
artifacts. Expand them only when required by the current action.

## Skills

Treat Skills as versioned rule packages, not task memory. Pin their version,
digest, applicability, dependencies, conflicts, and expiry. Missing or changed
Skills are quarantined before execution.

Keep the always-loaded set small. A measured reference run reduced selected
Skill source bytes by more than 96% while preserving the expected result.

## Collaboration

### Local And Forge-Coordinated

For most teams, use the existing Git forge for visible Work, branches, reviews,
and CI. The control plane adds local claims, checkpoints, evidence receipts, and
conflict warnings. Contributors do not need access to a shared database.

### Shared-Strong

Use shared-strong mode only when workers require a single claim/lease authority
across machines. Every write or external effect must bind:

```text
project + active Work + claim + lease epoch + scope owner + expected revision
```

A stale lease or revision fails closed. Presence, chat messages, branches, and
UI state never grant execution authority.

### Handoff

A handoff includes the checkpoint, task revision, next action, return point,
effect watermark, claim status, and evidence refs. The receiving worker must
acknowledge the exact first action before side effects are allowed.

## Verification Profiles

Projects decide which gates apply. Typical gates include static checks, tests,
builds, contract fixtures, mutation testing, loopback, performance, fault
recovery, and live-device checks.

Token or latency savings never waive a required verification gate.

## Storage

The default SQLite file is:

```text
.continuity/state.sqlite3
```

The file is created during initialization. `verify` and `doctor` fail when it
is missing, belongs to another application, has a newer unsupported schema, or
fails integrity checks.

PostgreSQL is optional for shared coordination. Artifact storage may remain on
the local filesystem or use an external object store. The state profile records
which capabilities are available; unavailable enhancements degrade explicitly.

## Backup And Removal

Stop active workers before copying a live SQLite database. Back up the whole
`.continuity/` directory, including templates and content-addressed
artifacts referenced by current checkpoints.

To remove the local integration, archive or delete that directory after all
claims are closed. Project source and build behavior remain unchanged.

## Public Repositories

Commit project-owned templates only when the team wants shared governance.
Personal STATUS overlays, provider archives, secrets, raw transcripts, local
databases, and machine-specific paths belong in `.gitignore` or external
storage.

## Release And PyPI

The currently available distribution source is the GitHub Release:

<https://github.com/skyhua0224/continuity-plane/releases>

The `continuity-plane` name is available on PyPI, but this version has not been
uploaded yet. No trusted publishing credential is configured on the development
machine. Future PyPI publication should use GitHub Actions Trusted Publishing,
not a token stored in a repository or workstation. These docs will not claim
that `pip install continuity-plane` works until an actual PyPI release exists.

## Verify The Release

```bash
python -m unittest discover -s tests -p 'test_*.py'
python benchmarks/run_local_state.py --iterations 1000
python -m build
```

The public benchmark uses a temporary project and requires no network service.
Reference A/B measurements, sample sizes, limitations, and a content digest are
stored in `benchmarks/reference-results.json`.
