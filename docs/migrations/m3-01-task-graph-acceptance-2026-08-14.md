# M3-01 Task Graph Acceptance

版本：1  
日期：2026-08-14  
状态：implemented offline graph contract / native dependency gate open

```yaml
document_id: context.m3-01-task-graph-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m3-01-task-graph/current-head
supersedes: null
affected_tasks: [M3-01, M3-02, M3-03, M3-05]
next_review: M3-02
```

## 范围

M3-01 adds a strict `context.task-graph/v1alpha1` projection for Campaign, Goal, Work and Experiment. The projection binds `project_id`, state revision and Work revision to the authoritative typed state. Existing `context.typed-state/v1alpha1` remains wire-compatible; the graph projection does not add fields to that wire version.

The runtime validator rejects missing or duplicate IDs, parent cycles, dependency cycles, parent/dependency combined cycles, orphan components, invalid kind edges, ancestor/descendant dependencies, non-executable active containers, and invalid Experiment return/promotion metadata. `context.typed-state/v2alpha1` persists the Experiment contract in authoritative Work objects. Event replay requires a monotonic Work revision for graph changes. SQLite validates the proposed snapshot before publication and preserves the prior snapshot and empty Event stream after rejection.

## Migration And Rollback

`migrate_v1alpha1_to_v2alpha1` requires an explicit Experiment contract and adds no inferred values. Reapplying the migration is byte-equivalent. `rollback_v2alpha1_to_v1alpha1` projects the prior wire fields for a legacy reader; append-only Events and v2 checkpoints remain immutable. The v1 reader remains supported for historical replay, while new M3 graph authority writes use v2.

## Results

```yaml
task_graph_schema:
  content_sha256: 6cdf220f697b57273f5a79203695a259b21308819b16837404850b892752ac4f
```

| Gate | Result |
|---|---|
| strict schema and registry | `context.task-graph/v1alpha1`; typed-state/Event v2 registry migrations include replay, idempotency and rollback evidence |
| deterministic graph | canonical node, root, dependency and exit-criteria ordering; projection binds state revision and Work title, kind, parent, dependency, revision and Experiment contract |
| failure matrix | parent, dependency, combined-edge, orphan, kind, descendant, return, promotion, container and terminal-revival variants are rejected; `26/26` focused tests passed |
| authoritative typed state | v2 persists Experiment return point, exit criteria, attempt budget, expiry, promotion target and mainline authority; non-ancestor or non-mainline promotion targets are rejected; v1 fixture replay remains valid |
| replay and persistence | invalid graph Event is atomic in SQLite and State MCP; valid v2 State MCP commit persists a v2 Event and SQLite replay equals the stored snapshot |
| validator latency | Linux x86_64, CPython 3.14, 1,000-node graph, 100 samples: p50 `2.8228 ms`, p95 `3.9736 ms`, max `4.8413 ms` |
| regression | current-head directed graph/state/store set `153` passed, `18` optional PostgreSQL tests skipped without DSN; repository verifier, `compileall` and `git diff --check` passed; full-chain regression remains the M3 integration gate |

## Boundaries

M3-01 does not classify natural-language input, activate or suspend tasks, persist return frames, consume Experiment budgets, authorize promotion, or select a next-ready leaf. Those behaviors remain M3-02 through M3-08. M3-01 does not claim provider token, context-window, or live compaction evidence. Final task completion remains gated by the M2-09 Windows/macOS native SQLite fixtures.

## Reproduction

```text
.venv/bin/python -m unittest tests.test_m3_01_task_graph -v
.venv/bin/python -m unittest tests.test_m2_01_typed_state tests.test_m2_02_state_events tests.test_m2_05_state_mcp tests.test_m2_08_state_store_conformance tests.test_m2_09_sqlite_state_store -v
.venv/bin/python tools/verify_repository.py --root .
```
