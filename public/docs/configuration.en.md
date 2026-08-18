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
