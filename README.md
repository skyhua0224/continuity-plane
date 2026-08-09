# Context Control Plane

Provider-neutral context, task-state, Skill, evidence, replay, and collaboration control plane for long-running software projects.

The project exists to prevent context compaction, task switching, model changes, and team collaboration from silently changing the active goal, reviving rejected decisions, repeating side effects, or losing verification evidence.

Current status: research and shadow-pilot scaffolding. No production state service is implemented yet.

## Repository Boundary

- This repository owns schemas, state-service code, task routing, Skill resolution, context composition, replay validators, provider adapters, project profiles, and human-console integrations.
- The selected StateStore profile and artifact store own live state, events, checkpoints, and large outputs. SQLite is the default embedded backend; PostgreSQL is an optional shared backend.
- Provider archives retain raw chat transcripts outside Git.
- Project repositories contain only small integration manifests and project-owned canonical documents.
- AlkaidLab Platform, Moonlight, Sunshine, and other products retain independent build and runtime lifecycles.

## Start Here

- [`STATUS.md`](STATUS.md): small current-work router; this is the normal agent entry point.
- [`MASTER.md`](MASTER.md): canonical project goal, dependency graph, work ledger, and acceptance gates.
- [`docs/architecture/target-state.md`](docs/architecture/target-state.md): default human/Agent target-state table, permissions, delivery phases, and public-project projection boundary.
- [`docs/policies/conversation-ingestion.md`](docs/policies/conversation-ingestion.md): what can be migrated from chat history and where it belongs.
- [`docs/policies/documentation-style.md`](docs/policies/documentation-style.md): normative documentation language and information placement.
- [`docs/policies/archive-lifecycle.md`](docs/policies/archive-lifecycle.md): retention classes, sealed export/import, tombstones, and deletion-proof boundaries.
- [`docs/policies/reference-evidence-lifecycle.md`](docs/policies/reference-evidence-lifecycle.md): reference discovery, snapshot, freshness, assertion, adoption, and supersedes policy.
- [`docs/policies/schema-version-release-governance.md`](docs/policies/schema-version-release-governance.md): schema identity, compatibility, migration, replay, release, and rollback gates.
- [`docs/policies/project-dogfooding-observation.md`](docs/policies/project-dogfooding-observation.md): compaction, Skill-load, plan-evolution, and verification trend protocol.
- [`docs/policies/git-collaboration.md`](docs/policies/git-collaboration.md): provider-neutral branch, commit, PR, merge, and Git admission contract.
- [`docs/policies/continuous-integration.md`](docs/policies/continuous-integration.md): Gitea verification jobs, authority boundary, local parity, secret scan, and required-gate contract.
- [`docs/architecture/idea-continuity.md`](docs/architecture/idea-continuity.md): natural-language Idea capture, correction, controlled switching, compaction, and context return.
- [`docs/architecture/skill-orchestration.md`](docs/architecture/skill-orchestration.md): Skill 来源、resolver、分层装载、协作角色和验收合同。
- [`docs/research/agent-harness-assessment-2026-08-09.md`](docs/research/agent-harness-assessment-2026-08-09.md): Codex/Claude harness capabilities, authority boundaries, and adoption matrix.
- [`docs/research/external-skill-and-mcp-catalog-assessment-2026-08-09.md`](docs/research/external-skill-and-mcp-catalog-assessment-2026-08-09.md): 官方 Skill、Agent Skills 标准、MCP Registry 和市场候选。
- [`docs/research/context-compression-benchmark-2026-08-09.md`](docs/research/context-compression-benchmark-2026-08-09.md): E0/E1 合成压缩与 Execution Packet 量化结果。
- [`profiles/skill-catalog.example.yaml`](profiles/skill-catalog.example.yaml): 可供项目初始化和 resolver 使用的外部 Skill 候选与直接来源地址。
- [`profiles/reference-catalog.example.yaml`](profiles/reference-catalog.example.yaml): Codex/Claude harness reference snapshots, hashes, validity, refresh triggers, and direct source URLs.
- [`schemas/registry.yaml`](schemas/registry.yaml): machine-readable schema/profile registry and artifact hashes.
- [`docs/migrations/m0-07-schema-governance-acceptance-2026-08-09.md`](docs/migrations/m0-07-schema-governance-acceptance-2026-08-09.md): M0-07 compatibility, migration, replay, and rollback verification evidence.
- [`experiments/dogfood/observations-2026-08-09.yaml`](experiments/dogfood/observations-2026-08-09.yaml): first project self-observation baseline.
- [`docs/research/context-reliability-assessment-2026-08-09.md`](docs/research/context-reliability-assessment-2026-08-09.md): evaluated components and AlkaidLab pilot design.
- [`replay/fixtures/README.md`](replay/fixtures/README.md): replay corpus contract.
