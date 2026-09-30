# M3-03 Route Events Acceptance

版本：1  
日期：2026-08-14  
状态：implemented local route-apply contract; PostgreSQL live parity pending

```yaml
document_id: context.m3-03-route-events-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/current-head
supersedes: null
affected_tasks: [M3-03, M3-04, M3-06]
next_review: M3-04
```

## 范围

M3-03 adds `context.state-event/v3alpha1` task-transition envelope and `context.task-route-apply-request/v1alpha1`. The route apply service consumes a validated M3-02 proposal plus trusted authorization context and applies one provider-neutral SQLite CAS Event. Generic snapshot mutation is not exposed.

`continue` has zero state writes. `child` creates a proposed Work and leaves the active leaf unchanged. `interrupt` and `switch` require a published checkpoint artifact, exact revision/Event-head/current-snapshot binding and trusted authorization, then commit suspension/activation semantics in one Event with ordered `task_suspended` and `task_activated` task events. Correction requires supersedes lineage and changed-key overlap in full and incremental replay. Every persisted route Event binds the SHA-256 of the canonical complete apply request. An Event identity reconstructs an exact receipt while it remains the current head; a later head or any changed request payload returns an explicit conflict.

## Results

```yaml
event_v3_schema_sha256: d89b2081d84786b387f9e82ababc56b429608e6324a56518e16914b70d5a4dea
route_apply_schema_sha256: 6d46559950e504aa894d00c78927f3170750e422c3f2079dc09cd95c1759b103
focused_tests:
  route_apply: 19
  event_v3: 6
  historical_return_point: 2
  combined_m2_m3: 190
  skipped_postgresql: 20
full_repository_tests:
  passed: 806
  skipped: 30
```

| Gate | Result |
|---|---|
| Event contract | strict v3 fields, nullable transition parity, ArtifactRef, route-kind/event-type/order, canonical hash, typed-state-v2 replay and correction fork/overlap checks pass |
| Route proposal binding | proposal hash, canonical complete apply-request hash, operation, project revision, active Work revision, target revision and checkpoint Event head are exact-bound |
| Continue/child | continue produces no Event; child remains proposed and unclaimed; active leaf remains unchanged |
| Switch atomicity | old Work/Claim release, target activation/Claim, primary projection and ordered task events are committed in one SQLite CAS Event |
| Checkpoint gate | missing/tampered/unrelated artifact, stale Event head and snapshot mismatch are rejected before CAS |
| Target/Claim gate | dependency/blocker readiness is rechecked; deterministic Claim identity cannot overwrite the released Claim |
| Authorization/CAS | candidate authorization is rejected; stale project/Work revisions and interrupt/switch input-kind drift are rejected without write |
| Correction lineage | correction target keys must overlap the superseded Event; duplicate object changes and a second correction branch from the same Event are rejected without write |
| Return and retry | return frame is bound to checkpoint/old Work; same current-head payload reconstructs an identical receipt; later head, shortened correction changes and other changed payloads conflict |
| Regression | 190 focused M2/M3 tests pass with 20 PostgreSQL skips; full repository `806/806` passed and `30` skipped; 53/53 Git admission tests, Python compile and diff checks pass |
| SQLite scaling | 40 samples commit p95 `0.5667 ms`; 1,000 Event stream commit p95 `32.9307 ms`; state-only restore p95 `0.2411 ms`; external services `0` |

## Remaining validation

PostgreSQL route-apply and correction-lineage conformance require a live DSN receipt. M3-04 scope/ownership/effect gates remain outside this leaf. Full historical receipt lookup after later Events remains M8 durable request work; M3-03 fails closed instead of returning a mixed receipt. The local route contract does not claim provider compaction or production authorization evidence.

## Reproduction

```text
.venv/bin/python -m unittest tests.test_m3_03_route_apply tests.test_m3_03_state_event_v3 tests.test_m3_03_return_point -v
.venv/bin/python -m unittest tests.test_m3_01_task_graph tests.test_m3_02_sticky_router tests.test_m2_02_state_events tests.test_m2_06_checkpoint_canary tests.test_m2_08_state_store_conformance tests.test_m2_09_sqlite_state_store tests.test_schema_governance -q
```
