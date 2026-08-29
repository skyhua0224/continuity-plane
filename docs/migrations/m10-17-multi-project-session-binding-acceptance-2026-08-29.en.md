# M10-17 Multi-Project Session Binding Acceptance

## Scope

This acceptance fixes the boundary for one Agent Session serving multiple independent
Project States. Project identity is not inferred from the process `cwd`; a Session must
establish or switch a binding with a successful explicit `continuity_resume(root=...)`.
Governance roots, implementation repositories, and external delivery workspaces retain
separate repository, claim, lease, revision, and effect-permission boundaries.

## Contract

- Bindings use `context.codex-session-project-bindings/v1alpha1` and store a Session
  digest, the bound project set, the active root, each project profile digest, and a
  binding digest. The raw Session ID and transcript body never enter State or Git.
- The MCP server starts unbound. A project is added only after an explicit absolute-root
  `continuity_resume` succeeds; that root becomes active. The same Session may explicitly
  resume another installed project.
- A relative root resolves only against the last successful active root. An unbound root,
  missing profile, profile-digest mismatch, or corrupt binding is rejected before any
  CLI/State/Effect call; the process `cwd` is not a fallback after binding exists.
- Lifecycle writes use the requested bound root. Switching projects never reuses a claim,
  checkpoint, or revision from another project. A project's governance root and external
  delivery workspace are further linked by that project's workspace registry.

## Acceptance evidence

| Gate | Result |
|---|---|
| Strict binding schema, registry hash, and public build manifest | Pass |
| MCP explicit-root binding, root switching, and unbound-write rejection | `46/46` focused tests pass |
| Hook `0600` binding, profile digest, legacy single-root migration, and corrupt fail-closed | Pass |
| Packaged MCP and plugin MCP contract parity | Pass |
| Read-only requests do not trigger State writes or extra effects | Pass |
| Real project state, product repositories, and SQLite | No writes or modifications in this acceptance |

## Entry point

In a cross-project Session, explicitly resume each governance root before working there:

```text
continuity_resume(root=/path/to/project-a)
continuity_resume(root=/path/to/project-b)
```

Subsequent Work, claim, checkpoint, and effect requests must use the matching project root.
Do not switch projects by relying on the terminal directory, and never copy a claim ID from
one project into another.

## Not claimed

This acceptance proves routing and permission isolation. It does not prove automatic
cross-project Work merging, a cross-project unique claim, or completion of any product
repository. A unique shared claim still requires `shared-strong` or a supported forge
adapter.
