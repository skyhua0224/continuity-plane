# M8-03 Temporal Workflow Acceptance

Version: 1  
Date: 2026-08-16  
Status: verified

```yaml
document_id: context.m8-03-temporal-workflow-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-03-temporal-workflow-v1
affected_tasks: [M8-02, M8-03, M8-05, M8-06, M8-08]
next_review: M8-05
```

## Scope

M8-03 defines a provider-neutral durable workflow chain with deterministic
identity, append-only history, replay receipts, versioned patch markers,
bounded Continue-As-New payloads, input acknowledgement watermarks and effect
settlement watermarks. The reference implementation is local and has no
provider, State MCP, effect dispatch or shared authority.

Temporal is an opt-in adapter. The default profile does not import the SDK and
does not require a Temporal service, container, PostgreSQL instance or network
connection. A remote start requires a committed State MCP backend-binding
receipt. A successful remote start returns a binding receipt; an ambiguous
remote start is never converted to local fallback.

## Contracts

The following twelve strict wires are registered in `schemas/registry.yaml`:

| Contract | Responsibility |
|---|---|
| `context.workflow-run/v1alpha1` | bounded run identity, authority, projections and hash |
| `context.workflow-history-event/v1alpha1` | append-only event and hash-chain entry |
| `context.workflow-definition/v1alpha1` | implementation, supported history and semantic patch manifest |
| `context.workflow-replay-receipt/v1alpha1` | replay result bound to run hashes and final history head |
| `context.workflow-rollover-receipt/v1alpha1` | Continue-As-New safe-point proof and watermarks |
| `context.workflow-backend-capabilities/v1alpha1` | explicit local/Temporal capability profile |
| `context.workflow-runtime-selection/v1alpha1` | pre-binding runtime selection receipt |
| `context.workflow-adapter-manifest/v1alpha1` | optional adapter identity and authority boundary |
| `context.temporal-workflow-input/v1alpha1` | history-free bounded Temporal start/rollover input |
| `context.workflow-run-receipt/v1alpha1` | provider run, worker and binding evidence |
| `context.workflow-backend-binding/v1alpha1` | State MCP committed backend binding |
| `context.workflow-orchestration-benchmark/v1alpha1` | measured replay and fault-injection receipt |

## Verification

| Gate | Result |
|---|---|
| focused behavior and contract tests | `43` pass; one optional SDK test is skipped when the SDK is absent |
| strict schema tests | `8/8` pass; all 12 wires reject unknown fields and missing required fields |
| formal offline benchmark | `1000/1000` chains; `3000` runs; `2000` rollovers; `20500` history events; `7000/7000` fault rejections |
| veto metrics | replay mismatch `0`; lost input `0`; duplicate effect `0`; authority violation `0`; chain-link failure `0`; nondeterminism failure `0` |
| offline latency | p50 `18.307415 ms`; p95 `23.122890 ms`; max `33.308625 ms` |
| provider and external service calls | `0` in the offline benchmark |
| real SDK API conformance | pinned `temporalio==1.31.0` source surface passed; `result_run_id`, namespace, conflict/reuse policies and async start verified |
| live Temporal service | not executed; deployment and worker-version routing remain conditional |

The formal receipt is
[`m8-03-workflow-results.json`](../../experiments/evidence/m8-03-workflow-results.json).
Its file SHA-256 is
`30ff0212623edcbe760a7505ecc10107dcf35aa907d0c3d70bcf8bad013c2f30` and its
receipt SHA-256 is
`2c3a5bacf9edbf3ddbb2c4799d077fa2d59d3458218c28f57f1f65fd0124f36a`.

The Temporal Python SDK source is pinned to release `1.31.0`, commit
`84b519e0ff407b049da88ac7d1711f110494ff4d`, tree
`6ca7d581e9e0bea3f19a0e1bf5f3a5ef9fec6d21`, with source listing SHA-256
`44fcd507cce70c1fd4210edcb554c9b0275b849bfe8ac9867aa0af7975f16435`.

## Boundaries

Temporal history remains an execution backend detail. Typed State, Event Log,
State MCP revision/CAS, claim/lease and effect authority remain the control
plane authority. Worker versioning is reported as SDK-supported and
configuration-unverified until a live worker deployment passes its own gate.
Temporal `patched()` decisions are checked against the core semantic patch
marker; a manifest flag alone is insufficient.

The adapter sends no transcript or complete history. Continue-As-New carries
hashes, projections and settlement/acknowledgement watermarks only. Provider
start outcome ambiguity requires reconciliation and cannot trigger a second
local execution.

Benchmark provenance binds the normalized registry entries for the twelve
M8-03 contracts. Unrelated schema and documentation registry changes do not
invalidate the receipt; any M8-03 registry entry change does.

## Reproduction

```text
.venv/bin/python -m unittest discover -s tests -p 'test_m8_03*.py' -v
.venv/bin/python tools/run_workflow_orchestration_benchmark.py --samples 1000 --generated-at 2026-08-16T20:00:00+08:00
PYTHONPATH=/dev/shm/context-control-plane-temporal-site:. .venv/bin/python -m unittest tests.test_m8_03_temporal_adapter.M803TemporalAdapterTests.test_real_temporal_sdk_start_surface_when_available -v
.venv/bin/python tools/verify_repository.py --root .
```
