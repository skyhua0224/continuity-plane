# Generic collaboration and memory plane

Continuity separates four concerns that are often forced into chat history:

1. **Project identity** — which repositories and worktrees belong together.
2. **Task context** — the one bounded task card a session should execute now.
3. **Vocabulary** — project and global aliases such as subsystem names and user intents.
4. **Workspace inventory** — read-only visibility into branches and worktrees.

These surfaces are collaboration hints. They never grant source-control,
deployment, completion, or memory authority. A damaged or missing sidecar file
must never make a repository read-only.

## Project registry

The local registry stores one JSON document per project under a user-local
Continuity data directory. A project may contain multiple repositories, which
supports monorepos, worktrees, and multi-repository governance models.

## Task cards

A task card is deliberately small and contains only the current execution
boundary: objective, next action, optional worktree, allowed files, effect
policy, exit criteria, report policy, and a temporary assignee.

There are no fixed worker or controller roles. A session obtains a temporary
role only while it holds a card. Any session may mark its own work
`ready-for-review`; acceptance and global completion remain separate review
decisions.

## Vocabulary memory

Vocabulary entries are scoped aliases with provenance. Global entries cover
stable cross-project vocabulary; project entries override global entries when
both match. Credentials and other secret values are rejected. Store a
secret-manager reference instead.

## Non-blocking contract

Every authority-bearing response must distinguish a denied state transition
from denied work. Missing registry, task, or memory data is degraded metadata,
not filesystem or shell permission. Local reads, edits, builds, and tests
continue.

## Workspace and skill inventories

Workspace and skill reports are dry-run observations. They classify candidates
and emit bounded recommendations, but never delete branches/worktrees or edit
host configuration. Cleanup remains an explicit, separately reviewed operation.
