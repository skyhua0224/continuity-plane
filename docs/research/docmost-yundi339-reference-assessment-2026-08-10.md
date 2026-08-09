# Yundi339 Docmost Reference Assessment

版本：1  
日期：2026-08-10  
状态：candidate reference evidence

```yaml
document_id: context.docmost-yundi339-reference-assessment
document_revision: 1
change_type: research
authority_ref: explicit-user-selected-docmost-fork
source_url: https://github.com/Yundi339/docmost
source_branch: feat/native-database-fusion
source_revision: dcf85124087a11ccb24daad5c4801e528208db9d
tree_manifest_sha256: 7d96c2e499faeba703cd4d3c4c6c1926cbe8ab6222baddd563ba05ec4eee7777
license: AGPL-3.0-only
affected_tasks: [M9-01, M9-02, M9-03, M9-04, M9-05, M9-07]
next_review: M9-01
```

## Snapshot

The selected source is the public `Yundi339/docmost` fork. Its default branch is `main`; the selected active branch is `feat/native-database-fusion` at commit `dcf85124087a11ccb24daad5c4801e528208db9d`, committed on 2026-07-28. The selected branch is 235 commits ahead and 0 commits behind the fork's `main` at retrieval time. The tree-manifest digest is the SHA-256 of `git ls-tree -r --full-tree HEAD`.

The checkout is reference evidence. It has no State MCP authority, does not become a runtime dependency, and does not authorize direct writes to Docmost storage. Adoption requires M9 contract tests, license review, current snapshot verification, and a State MCP/CAS/validator boundary.

## Candidate Surfaces

| Surface | Source area | Candidate use | Required boundary |
|---|---|---|---|
| Relationship graph | `apps/client/src/features/space/graph`, `apps/server/src/core/space-graph` | Project DAG and force-directed relationship view | deterministic Project DAG remains canonical; graph is a projection |
| MCP sessions | `apps/client/src/features/system-status`, `apps/server/src/ee/mcp` | State MCP session health, tool registry and access patterns | Context Control Plane authorization and revision gates remain authoritative |
| Audit | `apps/client/src/ee/audit`, `apps/server/src/integrations/audit` | approval, correction and user MCP activity timeline | audit entries reference immutable state revisions and evidence |
| Verification | `apps/client/src/ee/page-verification`, `apps/server/src/ee/page-verification` | controlled verification and approval entry patterns | completion and promotion require control-plane validators |
| Collaboration | `apps/server/src/collaboration`, `apps/server/src/ws` | live projection refresh and collaborator presence | presence cannot grant claim, lease or effect permission |
| Database evolution | `apps/server/src/database/migrations`, repository layer | migration and projection compatibility reference | no direct coupling to Docmost tables from core state code |

## Adoption Status

All surfaces remain `candidate`. M9 must compare the selected branch with its then-current revision, identify reusable extension points, and verify that Docmost only reads or submits controlled proposals through State MCP. Obsidian remains an independent generated read-only projection.
