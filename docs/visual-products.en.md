# Visual Product Plan

[中文](visual-products.md)

Continuity Plane's visual products support orientation, review, impact analysis,
and controlled governance in large projects. Every page reads signed projections
from one state revision. Views may filter, lay out, and drill into data. Authority-
bearing changes always pass through State MCP authorization, expected revision or
CAS, and validators.

## Current Delivery Boundary

| Capability | Internal contract | Alpha wheel | User interface |
|---|---|---|---|
| Project Graph / Work Ledger | verified | included | adapter pending |
| Decision Timeline / Evidence Matrix | verified | included | adapter pending |
| Relationship / Impact | verified | included | adapter pending |
| Obsidian signed Markdown vault | verified | included | callable generator; CLI export pending |
| Context Health / Replay | verified | not in the alpha wheel | provider-neutral canary SPI pending |
| Docmost console | projection/action contract verified | connector unpublished | preview |
| Obsidian Canvas / Bases | planned | unpublished | planned |

The verified projections establish completeness, same-revision binding, capacity
limits, and tamper rejection. They do not constitute a finished graphical
interface in a browser or Obsidian.

## Docmost Console

Docmost provides a shared operational console:

- **Project Map** combines a deterministic task DAG with a Relationship/Impact
  exploration mode. It shows the mainline, dependencies, owners, claims and
  leases, blockers, expired branches, and scope overlap.
- **Work Ledger** shows active and completed Work, parallel claims, PR/CI/deploy
  intent, conflicts, and offline catch-up.
- **Decision & Evidence** shows the decision timeline, supersedes chains,
  Constraints, and the Evidence Matrix.
- **Context Health & Replay** shows compaction, Skills, retrieval, reference
  freshness, continuation cursors, effect watermarks, and canary drilldowns.
- **Governance Inbox** shows approval, correction, promotion, review, and audit
  receipts.

The browser receives bounded projections and artifact references. SSE is the
default notification transport; WebSocket support is enabled only by an explicit
deployment capability. Every governance action carries a trusted session,
request identity, expected revision, and idempotency key. The UI has no direct
database write path.

## Obsidian Graphical Vault

Obsidian remains a zero-service, offline, read-only observation surface. The
existing Markdown vault will expand with:

- stable backlinks between Projects, Work, Decisions, Constraints, and Evidence;
- generated Canvas project and impact maps;
- Bases definitions for the Work Ledger, Evidence Matrix, and health tables;
- filters for owner, status, scope, revision, and evidence validity;
- a manifest containing source revision, template version, content hashes, and a
  signature.

Generated content and user-authored notes use separate namespaces. Refresh builds
into a temporary directory, validates the complete output, and atomically replaces
the managed tree. Manual changes to generated files, unmanaged files in that tree,
or mixed revisions prevent publication. User notes may link to generated objects
but cannot submit active state.

## Delivery Order

1. **Unified Presentation SPI** extracts provider-neutral Context Health/Replay
   inputs and fixes view manifest, routing, pagination, filtering, artifact
   authorization, and stale-view contracts.
2. **Obsidian graphical export** adds a CLI export, Markdown, Canvas, Bases,
   backlinks, incremental refresh, and offline validation.
3. **Docmost read console** implements Project Map, Work Ledger,
   Decision/Evidence, Context Health/Replay, and the notification inbox.
4. **Docmost controlled actions** connects approval, correction, promotion, and
   review through State MCP and the audit chain.
5. **Cross-surface acceptance** requires identical same-revision objects, filter
   results, artifact links, and health findings in Docmost and Obsidian.

## Completion Gates

| Metric | Gate |
|---|---:|
| projection matches State revision and digest | `100%` |
| stale, tampered, or mixed-revision view rejected | `100%` |
| direct State or database writes by UI/generator | `0` |
| 2,000-node / 5,000-edge projection completeness | `100%` |
| reference-desktop time to interactive p95 | `< 2 s` |
| loaded-view filter/focus p95 | `< 100 ms` |
| SSE duplicate delivery and missed offline events | `0` |
| visible-object parity between Docmost and Obsidian | `100%` |
| keyboard-critical paths and WCAG 2.2 AA automated checks | pass |
| browser/Electron screenshot, layout, and overlap checks | desktop/mobile target pass |
| added daemon/database in the default local profile | `0` |

Performance claims must bind the device class, data scale, browser or Obsidian
version, and at least 25 performance samples. The complete Docmost interface
remains preview until a real connector, authentication, and browser tests pass.
