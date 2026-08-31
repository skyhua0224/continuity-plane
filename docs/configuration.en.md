# Configuration

[中文](configuration.md)

The generated `.continuity/project.yaml` is the project-owned entry
point.

```yaml
schema_version: context.project/v1alpha1
project_id: my-project
display_name: My Project
runtime_profile: local-embedded
state_store:
  adapter: sqlite
  path: .continuity/state.sqlite3
collaboration:
  mode: solo
  shared_state: false
```

Use environment-specific files or secret stores for credentials. Do not place
provider tokens, database passwords, raw transcripts, or machine paths in the
project profile.

Shared coordination is opt-in. A shared adapter must declare support for CAS,
claims, lease fencing, durable receipts, and project isolation before it can be
selected for authority-bearing work.

`project_id` and `display_name` are project-owned values. The package and
repository name are release identifiers, not required project names.

## Profiles

| Profile | Default state | Extra infrastructure | Availability |
|---|---|---|---|
| `local-embedded` | local SQLite | none | supported alpha default |
| `forge-coordinated` | local SQLite plus forge records | existing Git forge | integration contract |
| personal PostgreSQL | explicit PostgreSQL adapter | local/private PostgreSQL | optional library adapter |
| personal Docmost | State MCP remains authoritative | Docmost plus connector | optional projection preview |
| `shared-strong` | shared State MCP | conformed service, often PostgreSQL | explicit deployment preview |

PostgreSQL and Docmost are choices, not dependencies. A project can remain
fully local for its entire lifecycle.

## Continuity policy and lightweight probes

Existing projects require no migration. A missing `continuity_policy` selects
the `balanced` defaults. Add this section to `.continuity/project.yaml` only
when an override is needed:

```yaml
continuity_policy:
  preset: balanced
  resume:
    explicit_policy: once_per_connection
  checkpoint:
    on_pre_compact: true
    on_work_complete: true
    after_state_writes: false
    min_interval_seconds: 30
  verification:
    startup_scope: recent
    deep_verify: manual
  observability:
    mode: minimal
    probes_enabled: true
    slow_call_threshold_ms: 1000
    resource_sampling: boundaries_failures_and_slow
    retention_max_bytes: 67108864
```

The supported presets are `balanced`, temporary `diagnostic`, and
`reliability-first` for recovery incidents. Explicit fields override the
preset. Unknown fields, wrong types, and out-of-range values fail validation.
PreCompact and Work-completion checkpoints are safety boundaries and cannot be
disabled.
In the current alpha, checkpoint intervals and verification fields constrain
the automation contract. They do not add background work when no additional
automatic checkpoint or deep-verifier scheduler is present.

`CONTINUITY_OBSERVABILITY_MODE=diagnostic` may temporarily raise the
observation level; persistent policy belongs in the project profile. In
minimal mode, ordinary successful reads update MCP in-memory counters. Only
boundaries, State writes, failures, slow calls, and the Session summary are
persisted. Observation failures do not roll back or block a completed State
operation.

Build an offline probe report with:

```bash
continuity observe report --root .
```

The report reads only privacy-preserving local observations. It does not read
transcripts, source code, or tool response bodies, and it never changes the
policy. Token usage remains explicitly unavailable unless the provider or host
supplies it.
