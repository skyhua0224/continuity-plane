# Architecture and authority boundaries

[中文](architecture.md) · [Deployment](deployment.en.md) · [Validation](validation.en.md)

Continuity is provider-neutral developer tooling, not an application runtime dependency. CLI, MCP, plugins, hooks and existing SSH scripts can use the same capabilities.

```text
CLI / MCP / host hook
  ├─ collaboration registry → cards, changes, rules, reports, handoff
  ├─ supervisor             → managed attempts, output capture, resume
  ├─ retrieval / references → bounded code routes and source declarations
  └─ optional State service → authorization, revision/CAS, claim, checkpoint
```

## Separate coordination from State gates

Cards, Todo, messages, rules, lessons and batches are collaboration records. They need no State claim and grant no push/merge/deployment permission. Explicit project controller fencing coordinates tool-mediated dispatch and integration.

Typed State has its own authoritative commit boundary: source freshness, trusted request context, expected revision/CAS, validators, claims and checkpoints. Retrieval, reports and model conclusions supply evidence candidates. This boundary is not a compulsory preflight ritual for ordinary source work.

## Local-first and remote execution

Default State uses project SQLite; collaboration and caches can live in user storage. No resident service is required. Remote use selects explicit SSH CLI/MCP endpoints. Hook project mappings do not migrate native account/session state.

Default installation does not provide a turnkey hosted shared-strong service, complete Docmost console or OS sandbox. Optional adapters and projections have separate coverage/deployment conditions: [State](state-workflows.en.md), [views](project-views.en.md), [visual products](visual-products.en.md).

## Attempts and evidence

The supervisor wraps configured adapters, records starts/heartbeats/finishes/exit codes and captures reports. Controllers decide suitability, approval and integration. Native Claude subagent callbacks are observations, not process control, heartbeat proof or inferred success.

Packets are bounded startup entries; read large artifacts by reference. Rules, task bodies and code routes retain provenance. A hash proves content identity, not correctness. Shared plans and lessons do not create user authorization.

## Failure and isolation

- Atomic writes stage beside their destination; persistent storage still needs backups.
- Late-result protection separates attempt completion from card projection; outbox replay must not overwrite newer execution bindings.
- Leases/reservations protect cooperating tool clients, not arbitrary Git/scripts or native agent creation.
- Missing metadata does not prohibit ordinary authorized work; protected writes without a valid fence are refused with an explicit reason.
- Credentials, native sessions and account queries remain on their owning host/profile; public receipts contain allowlisted aggregates.
- The validation matrix records report attachment, reconnect ownership, source TSV and automatic retrieval mapping gaps rather than declaring them solved by this diagram.
