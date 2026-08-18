# Use Cases

[中文](use-cases.md)

Continuity Plane addresses recurring state, retrieval, and collaboration failures in
long-running AI-assisted development. Each case separates verified control-plane
contracts from work that still needs a front-end or provider adapter.

## Compaction And Long Sessions

Typical failures include answering an already completed question again after compaction,
rereading large parts of the repository, reviving completed or reverted Work, or running
a side effect immediately after an incorrect restore.

The control plane stores the active leaf, latest decision, constraints, return point,
acknowledged input, continuation cursor, and effect watermark. A PostCompact canary checks
these fields before code changes, commits, deployments, or other effects. A mismatch
downgrades the run to read-only.

Matched-task measurements reduced redundant-history input by `40.25%` and near-limit
history input by `95.06%`, with expected-answer quality `3/3`. They do not yet prove a
general increase in real window utilization or compaction interval; see the
[benchmark method](benchmarks.en.md) for longitudinal metrics.

## Multi-Session, Team, And Deployment Races

Typical failures include Session A deploying while Session B merges a new main revision,
two Sessions publishing or rolling back the same migration, PR/CI/local branches showing
different heads, and retries repeating the same external operation.

The Work Ledger, claims and leases, path ownership, expected revisions, effect identities,
and notification cursors bind deployment, review, merge, and rollback to one state. The
SQLite local profile coordinates Sessions on one device. A unique cross-device claim
requires an explicit shared State or forge adapter. PostgreSQL and Docmost are not
prerequisites for an ordinary pull request.

Current collaboration measurements reduced duplicate tool calls by `55.88%` and parallel
wall time by `22.65%`. Same-revision dual-Session delivery was `1000/1000`, duplicate
notification suppression `1000/1000`, offline catch-up `2000/2000`, and authority
violations `0`.

## Duplicate Work Across Agents

One contributor may implement a module locally while another contributor, unable to see
unpublished Work, implements it again. Reviewers may repeat the same source and official-
document search, and a handoff may retain only a summary without the blocker, next action,
or return point.

Project Graph, Work Ledger, Evidence Matrix, and forge projections record owners, scopes,
branches, claims, evidence, and the current revision. Memory and model output can produce
candidates; they cannot complete Work or grant effect authority.

## Ideas, Interrupts, And Task Switching

An idea raised during execution enters a candidate or parked queue. The default action is
capture-and-continue, which leaves the active leaf unchanged. An Idea reaches the canonical
queue only after an explicit switch or a review, CAS, attempt budget, expiry, and promotion
gate.

The original task checkpoint retains its return point and forbidden effects. Recovery loads
only the current packet and relevant Idea references, keeping side discussions out of the
mainline.

## Large-Project Orientation And Impact

A directory tree cannot express cross-repository dependencies, task relationships, decision
history, or change impact. People and agents need to know which Campaign owns a Work item,
who owns its scope, what it depends on, and which Products and tests it can affect.

Project Graph provides a deterministic DAG. Relationship/Impact provides bounded nodes,
edges, clusters, focus sets, and impact sets. Decision Timeline and Evidence Matrix explain
why a choice was made, when it was superseded, and which evidence supports completion.

The projection core has passed its scale gates. A complete Docmost Web UI, Obsidian
Canvas/Bases surface, and cross-surface interaction follow the [visual product plan](visual-products.en.md).

## Memory, Skill, And Documentation Drift

Old paths, decisions, and constraints in historical memory remain candidates. Skills use
manifests, versions, hashes, rule IDs, applicability, licenses, dependencies, and expiry;
drift, conflicts, or missing content enter quarantine. MASTER holds governance intent,
STATUS holds the current route, and reports or projections cannot change active state.

Current Skill source bytes fell by `96.54%`. Provider tokens, window utilization, and
longitudinal compaction intervals are measured only when host traces expose them.

## Quality And Failure Recovery

Token reduction does not replace quality gates. E0-E9 currently pass `10/10`; compaction,
Idea, interrupt, and worker-loss faults pass `4/4`; stale-history revival, silent CAS
overwrite, and authority violations are `0`. Complete data, methods, verifier cost, and
limitations are in the [benchmark method](benchmarks.en.md).
