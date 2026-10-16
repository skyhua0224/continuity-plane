# State and existing governance adoption

[中文](state-workflows.md) · [Documentation](README.en.md)

This is the advanced path. Cards, retrieval, rules, messages and reports need no State claim first.

## Existing MASTER/STATUS

Do not overwrite existing plans with init templates. After project initialization, preview an attach proposal and approve explicitly:

```bash
continuity attach plan --root . --master /path/to/MASTER.md --status /path/to/STATUS.md --work-id current --work-title 'Current work' --owner-ref controller --scope testing
continuity attach approve --root . --actor-ref controller --claim-id claim-current
```

Approval is an authoritative write. Check source paths/hashes, actor and scope first. Source changes invalidate the proposal; replay does not create duplicate events. Follow current help and receipt next_action on errors.

## Typed Work, decisions and constraints

```bash
continuity work add --root . --work-id next --title 'Next step' --source-file .continuity/MASTER.md --scope testing
continuity decision add --root . --decision-id approach --work-id next --statement 'Use the tested path' --source-file .continuity/MASTER.md
continuity constraint add --root . --constraint-id safety --work-id next --statement 'Do not change production configuration' --source-file .continuity/MASTER.md
```

These default to preview. Explicit apply uses the receipt's expected revision and passes authorization, CAS and validators. Markdown completion marks are not State acceptance evidence. Advisory metadata does not bypass checkpoint, claim or source-freshness gates.

## Deployment modes

| Mode | Current meaning |
|---|---|
| local-embedded | Default local SQLite lifecycle |
| forge-coordinated | Local State plus forge contracts, not automatic service deployment |
| PostgreSQL adapter | Optional library/database dependency; deploy and verify explicitly |
| shared-strong | Explicit service/conformance requirements, not a turnkey remote server |
| Docmost/Obsidian | Partial projections and plans; not replacement authority |

Profile names and architecture diagrams are not running clusters. Editing project.yaml does not migrate data. Before cross-adapter migration, verify CAS, lease fencing, isolation, durable receipts and replay, then plan a maintenance window.

## Backup and rollback

`export`, `import` and `rollback` operate on State bundles. Read current help, preserve revision/hash and evidence, and never overwrite unknown data. Collaboration, worker artifacts, policy attachments, caches and native accounts are separate stores.

Compaction, handoff and authoritative task switches should remain checkpoint-replayable. Metadata health grants no additional project authority, and failed State writes do not prohibit ordinary source work.

[Architecture](architecture.en.md) · [Configuration](configuration.en.md) · [API](api.en.md) · [Views](project-views.en.md) · [Visual-product coverage](visual-products.en.md)
