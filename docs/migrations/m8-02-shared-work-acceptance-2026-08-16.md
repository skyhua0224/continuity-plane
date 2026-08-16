# M8-02 Shared Work Coordination Acceptance

Version: 1  
Date: 2026-08-16  
Status: verified local coordinator

```yaml
document_id: context.m8-02-shared-work-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-02-shared-work-v1
affected_tasks: [M2-01, M2-05, M3-04, M8-02, M8-03, M8-06]
next_review: M8-03
```

## Scope

M8-02 provides deterministic Work identity and overlap checks, claim acquire,
heartbeat, release, revoke, expiry, atomic reclaim, lease epochs, fencing and a
dispatch-time effect gate. Every accepted local coordinator transition commits
the typed snapshot, append-only Event, project revision and durable request
receipt in one SQLite transaction. Exact request replay returns the committed
receipt; payload or action reuse with the same request ID fails closed.

The default profile remains `local-embedded`. The opt-in
`local-coordinator` profile provides authority-instance claim uniqueness and
requires no PostgreSQL, container, daemon or network service. PostgreSQL
`shared-strong`, cross-device ownership and tenant isolation remain conditional
capabilities and are not inferred from this result.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.typed-state/v6alpha1` | Work provenance and dedupe fields, claim revision/lease/closure fields, and effect dispatch fence fields |
| `context.typed-state-v5-v6-migration-receipt/v1alpha1` | deterministic migration bound to snapshot, Event cursor, registry digest and authorization |
| `context.state-store-capabilities/v2alpha1` | explicit authority scope, lease clock, fencing, atomic transition and durable receipt claims |
| `context.state-mcp/v3alpha1` | authorized claim lifecycle and effect dispatch request/response boundary |
| `context.shared-work-benchmark/v1alpha1` | replayable concurrency, revocation, reclaim, duplicate and latency gates |

## State MCP V3

<a id="state-mcp-v3"></a>

State MCP v3 derives actor identity and time from trusted host inputs. Each
lifecycle action has a distinct authorization action. Client time and absolute
expiry are rejected. Durable request IDs use a service-wide v3 namespace, so
cross-action reuse conflicts after restart. Effect dispatch requires current
project revision, claim revision, lease epoch, fence, scope owner and active
lease before an adapter may run.

## Capability Boundary

<a id="capability-boundary"></a>

SQLite v1 continues to advertise only `local-embedded` capabilities. The
explicit coordinator wrapper advertises `authority-instance` uniqueness,
process clock leases, atomic transitions, fencing and durable receipts because
those operations share the canonical SQLite transaction. The current
PostgreSQL adapter does not advertise lease/fence proofs and cannot activate
`shared-strong`. A future shared backend must pass the same capability
validator and E8/E9 fault gates.

## Rollback

<a id="rollback"></a>

The v5 to v6 migration assigns deterministic positive fence values to active
legacy claims. Rollback to v5 is allowed only while every v6 field equals its
deterministic migration image and the persisted revision, snapshot and Event
head remain at the migration boundary. Any claim lifecycle or effect dispatch
fact makes rollback lossy and is rejected. Migration transaction faults leave
neither a partial snapshot nor a partial receipt. SQLite schema v4 preserves
the append-only receipt journal while allowing a new upgrade receipt after an
explicit lossless rollback.

## Verification

| Gate | Result |
|---|---|
| M8-02 focused tests | `81/81` pass, including `6/6` independent cross-boundary regressions and two repeatable migration regressions |
| scenario replay | 10 scenarios, `10000/10000` pass |
| duplicate and stale-worker safety | silent overwrite, duplicate claim/effect, post-revoke effect and old-worker admission all `0` |
| orphan reclaim | `1000/1000` within the benchmark SLO |
| authority consistency | Event, revision and hash mismatches all `0` |
| transaction fault | typed snapshot, Event, claim and request receipt all roll back together |
| migration cycle | schema v3 to v4 and upgrade to rollback to upgrade both preserve every receipt |
| latency | p50 `0.209816 ms`; p95 `0.528895 ms`; max `0.801009 ms` |
| provider and shared authority claims | provider calls `0`; benchmark shared authority claim `false` |

The measured receipt is
[`m8-02-shared-work-results.json`](../../experiments/evidence/m8-02-shared-work-results.json),
file SHA-256
`a1cec22668d427f98b5d38a52e6e12dce572cebcb8c8a887faba40cf476b58b1`.

## Boundaries

The in-memory benchmark proves deterministic interleaving behavior. The SQLite
tests prove local transaction, restart and request replay behavior. Neither
result claims distributed consensus, remote availability, tenant isolation,
Temporal orchestration or production network authentication. Those concerns
remain assigned to M8-03, M8-05 and deployment adapters.

## Reproduction

```text
.venv/bin/python -m unittest discover -s tests -p 'test_m8_02*.py' -v
.venv/bin/python -m unittest tests.test_m2_01_typed_state tests.test_m2_02_state_events tests.test_m2_09_sqlite_state_store -v
.venv/bin/python tools/verify_repository.py --root .
```
