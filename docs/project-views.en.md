# Large-Project Views

[中文](project-views.md)

The hardest problem in a large repository is often not finding a string. It is
understanding where a file, module, decision, or task belongs in the whole
project. Continuity Plane provides read-only projections that turn one state
revision into data suitable for Web, CLI, Docmost, or static-vault views.

## Project Graph

The Project Graph includes:

- Campaign, Goal, Work, and Experiment hierarchy;
- active work set, primary leaf, and dependencies;
- owners, claims, leases, branches, and scope ownership;
- blockers, duplicate candidates, expired work, and return points;
- verification status and current revision.

It answers: What is the main line? What can run in parallel? Who owns this
scope? Where does this branch return? The accepted worst-path p95 for 128 Work
items and a 223 x 223 stress path is `6.319825 ms`.

## Decision Timeline And Evidence Matrix

The timeline shows decision time, status, and supersedes lineage. The matrix
connects Work, Decisions, Constraints, tests, source code, and official
references while distinguishing candidate, verified, stale, and rejected
evidence.

It answers: Why was this chosen? When was it reversed? What evidence supports
completion? The accepted 1,000-sample worst-path p95 is `47.421031 ms`.

## Context Health And Replay

Context Health summarizes compaction, Skill loading, retrieval, reference
freshness, token use, task-switch risk, and recovery metrics. Replay exposes the
checkpoint, event head, continuation cursor, and effect watermark.

It helps explain why a task drifted after compaction, which Skills changed, and
which restore failed its canary. Accepted p95 is `15.409254 ms` for regular
views and `253.994472 ms` for the 3,500-event scale case.

## Relationship / Impact Force-Directed Projection

The Relationship/Impact projection emits nodes, edges, clusters, filters,
focus sets, and impact sets from project state, dependencies, and verified code
clues. It also emits a force-directed layout contract and deterministic seed.
The renderer computes coordinates; they never enter State and carry no
authority.

It reveals relationship clusters and change impact that a directory tree
cannot show. A 2,000-node / 5,000-edge complete rebuild passed `25/25`; accepted
scale p95 is `187.459764 ms`.

## Obsidian Vault

The read-only vault emits signed Markdown projections for Project Graph,
Decision/Evidence, Context Health, and Replay. Manual edits, unmanaged files,
and content tampering are rejected and cannot update authoritative state.

## Current Availability

| Capability | Internal state | Alpha wheel |
|---|---|---|
| Project Graph / Work Ledger | verified | included |
| Decision Timeline / Evidence Matrix | verified | included |
| Relationship / Impact | verified | included |
| signed Obsidian Markdown vault | verified | included; adapter supplies complete inputs |
| external State provider / controlled governance | verified | included |
| Context Health / Replay | verified | not in the alpha wheel; provider-neutral canary SPI pending |
| Docmost Web UI and connector | preview | unpublished |
| Obsidian Canvas / Bases dashboard | planned | unpublished |

See [`visual-products.en.md`](visual-products.en.md) for the complete graphical
product surfaces, interaction model, authority path, and acceptance gates. Every
UI must use State MCP authorization, expected revision, and validators. Direct
database writes are not available to presentation adapters.
