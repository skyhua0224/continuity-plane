# Architecture

[中文](architecture.md)

Continuity Plane is an integration library, not an application runtime
dependency. An agent host or project adapter invokes its services and provides
a trusted request context.

```text
Integration adapter
  -> authorization boundary
  -> State service
       -> typed snapshot
       -> append-only events
       -> claim / lease / effect records
  -> artifact store
  -> context composer
  -> checkpoint and replay canary
```

## Authority

Typed state owns the current task, decisions, constraints, claims, revisions,
and effect watermarks. Retrieval, memory, code graphs, model output, and human
views are candidate or projection sources. They submit requests through the
same authorization, revision, and validation boundary.

## Local-First Storage

SQLite is the default state adapter. It supports personal projects, local agent
sessions, and offline recovery without a service process. Shared deployments
may select a backend that implements the same capability and conformance
contracts.

The deployment levels are independent:

- `local-embedded` keeps the authority in one project's SQLite file;
- `forge-coordinated` adds Git forge visibility while contributors retain local
  state;
- a personal PostgreSQL adapter is useful for SQL inspection or local workers,
  but is not required;
- Docmost is a human projection and approval surface, never the typed-state
  authority;
- `shared-strong` moves claim, lease, and CAS authority behind an explicit
  shared State MCP service.

The alpha CLI provisions `local-embedded`. Other levels require an explicit
adapter or deployment configuration.

## Large-Project Observation Surfaces

Project Graph, Decision/Evidence, and Relationship/Impact projections transform
one State revision into bounded data for CLI, Web, Docmost, or Obsidian
adapters. A deterministic DAG presents the mainline and dependencies. The
force-directed relationship view explores clusters and impact. Layout
coordinates carry no State authority.

Docmost is an optional shared console. Obsidian is a generated offline,
read-only vault. The current alpha provides selected projection cores and a
signed-vault generator. Complete pages, connectors, Canvas/Bases output, and a
provider-neutral Context Health export follow the stages in
[`visual-products.en.md`](visual-products.en.md).

## Failure Model

Every externally visible operation has a stable identity. Checkpoints record
the event head, state revision, continuation cursor, and effect watermark.
Recovery validates these bindings before another side effect is admitted.
