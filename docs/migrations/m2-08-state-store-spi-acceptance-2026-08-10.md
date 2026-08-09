# M2-08 StateStore SPI Acceptance

版本：2  
日期：2026-08-10  
状态：verified

```yaml
document_id: context.m2-08-state-store-spi-acceptance
document_revision: 2
change_type: evidence
authority_ref: repository-tests-and-postgresql-integration-2026-08-10
supersedes: context.m2-08-state-store-spi-acceptance@1
affected_tasks: [M2-05, M2-08, M2-09, M8-08]
next_review: M2-09
```

## Scope

M2-08 defines the minimum snapshot and Event persistence boundary shared by state-store adapters. The contract contains create, read, Event read and expected-revision commit operations; generic conflict, not-found, integrity and capability errors; a strict versioned capability manifest; and a guarded invocation path.

The capability manifest declares authority mode, supported operations, shared authority, offline write, unique claim, multi-writer behavior, lease clock, artifact scope, expected revision and migration roles. Runtime validation rejects undeclared operations, missing callables, invalid types and contradictory guarantees before adapter code executes.

## Verification

| Gate | Result |
|---|---|
| M2-08 targeted tests | 52/52 passed |
| Capability boundary | 18/18 passed |
| Memory test adapter conformance | 17/17 passed |
| PostgreSQL adapter conformance | 17/17 passed against PostgreSQL 18.4 |
| M2-03 PostgreSQL regression | 11/11 passed |
| M2-02/M2-03/M2-08 regression | 77/77 passed with PostgreSQL integration enabled |
| Full repository after revision 23 projection | 232/232 passed with PostgreSQL integration enabled |
| Independent implementation review | third review: High 0, Medium 0 |
| Capability schema | strict `context.state-store-capabilities/v1alpha1`, registry hash verified |
| Dispatch authorization | undeclared operation and missing callable produced capability errors before adapter execution |
| Malformed manifest normalization | list, object, null and number operation entries produced `StateStoreCapabilityError` |
| Snapshot and Event ownership | input and returned snapshot/Event objects remained defensive copies |
| CAS and replay | stale commit remained atomic; committed snapshot and Event replay were byte-equivalent |

The PostgreSQL manifest declares only verified shared transaction, expected-revision and multi-writer guarantees. Unique claim, lease clock, artifact storage and migration capabilities remain disabled. Existing PostgreSQL-specific exceptions preserve their legacy catch boundary and also implement the generic StateStore error contract.

## Authority Boundary

The memory adapter is a conformance test implementation and has no production authority. The PostgreSQL adapter remains an optional `shared-strong` backend. M2-08 does not provide the default embedded backend, State MCP authorization, claim/lease, artifact persistence, migration execution or forge synchronization.

M2-09 implements the default SQLite embedded backend with the same conformance harness. M2-05 adds the State MCP boundary after SQLite and PostgreSQL behavior can be compared through one SPI. M8-08 may implement forge projection only with an explicit projection capability manifest.

## Completion

The M2-08 completion gate is satisfied. Protocol/runtime consistency, adapter-neutral rollback and Event-chain coverage, schema/runtime equivalence, static descriptor inspection, ownership boundaries and unknown-project behavior are covered by regression tests. The independent third review reported High 0 and Medium 0. M2-09 is authorized to reuse this contract for the default SQLite backend.
