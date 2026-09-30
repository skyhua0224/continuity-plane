# M10-15 External Delivery Workspace Acceptance

Version: 1  
Date: 2026-08-29  
Status: verified local candidate

```yaml
document_id: context.m10-15-external-delivery-workspace-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://m10-15/external-delivery-workspace/revision-1
supersedes: null
affected_tasks: [M8-02, M10-01, M10-11, M10-15]
next_review: M10-01
```

## Contract

A governance repository may register an external Git delivery workspace in the
ignored local registry `.continuity/local/delivery-workspaces.json`. Each entry
binds one workspace ID to an absolute root, Git common-dir digest, maximum effect
set, project ID, project profile digest, and registry digest. The registry has
mode `0600` and State/completion authority `false`.

Delivery activation accepts an external repository only when all of these values
match:

- the workspace ID exists in the current project registry;
- the resolved workspace root and Git repository digest match the entry;
- the Work requests a subset of registered effects;
- the claim receives the matching `repo://<workspace-id>` scope;
- predecessor Work, implementation evidence, HEAD, optional ref, and pending
  worktree delta satisfy the existing delivery gates.

`source-control.local` covers staging and ordinary commits.
`source-control.history-rewrite` separately controls amend, rebase, and reset.
The effect preflight derives its repository, worktree, and branch identity from
the command workdir. An unregistered repository or a Work without the matching
repo scope is denied before the shell command runs.

## Verification

| Gate | Result |
|---|---:|
| focused CLI, MCP, lifecycle, schema, and public builder tests | `71/71` |
| full repository suite | `1975` passed, `31` environment skips |
| external activation without registration | rejected; State/Event unchanged |
| registered external activation | Work, claim, evidence, workspace and effects bound |
| unregistered source-control workdir | denied before shell |
| effect intent repository identity | external workspace digest matched |
| completed effect intent | released by Session/tool-use identity |
| registry file mode | `0600` |
| live registration State revision/event | unchanged at `75/75` |
| live service worktree | clean before and after registration |
| direct SQLite changes | `0` |

The live receipt records only aggregate gates and opaque evidence identifiers.
Absolute local paths and repository remotes remain in the ignored local registry
and are excluded from Git admission.
