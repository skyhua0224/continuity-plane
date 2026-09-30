# M8-01 Durable Operation Acceptance

Version: 1  
Date: 2026-08-16  
Status: verified local-embedded durable execution

```yaml
document_id: context.m8-01-durable-operation-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-01-durable-operation-v1
affected_tasks: [M5-03, M8-01, M8-02, M8-06]
next_review: M8-02
```

## Scope

M8-01 defines a provider-neutral durable operation with immutable project,
operation and effect identity; append-only revision and hash-chain history; a
State MCP intent and state-commit authority adapter; a checkpoint and canary
gate; an effect adapter; recovery decisions; and revision-bound trace events.
The default implementation uses SQLite and a local content-addressed checkpoint
store. It requires no PostgreSQL, container, remote coordinator or provider API.

The operation phases are `prepared`, `intent-committed`, `effect-in-flight`,
`outcome-unknown`, `effect-settled`, `response-committed`, `terminal` and
`quarantined`.
Authority and checkpoint results are validated before dispatch. A committed
intent is claimed through exclusive revision CAS before either concurrent
runner may invoke the effect adapter. A `safe` effect retries with the same
idempotency key after an unknown outcome. A `never` effect requires settlement
verification or manual recovery. State Effect provenance binds the durable
request SHA-256. A reconciled `succeeded` Effect rebuilds local settlement
without another adapter dispatch. Shared local trace writers serialize,
revalidate and deduplicate each append. Terminal replay is a no-op.

The DBOS assessment remains the adapter basis for local durable execution. Its
27 PostgreSQL recovery, retry, concurrency and idempotency tests are recorded in
the current reliability assessment. M8-01 adds the product-owned local-embedded
contract and crash matrix; it does not add a DBOS runtime dependency.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.durable-operation/v1alpha1` | immutable operation identity, phase, authority, effect, checkpoint, continuation and trace binding |
| `context.durable-operation-recovery/v1alpha1` | deterministic next action and replay permission |
| `context.durable-state-receipt/v1alpha1` | State MCP intent/state commit result and event head |
| `context.durable-authority-adapter/v1alpha1` | authority adapter identity and capability order |
| `context.durable-checkpoint-gate/v1alpha1` | checkpoint/canary validation result |
| `context.durable-checkpoint-adapter/v1alpha1` | checkpoint adapter identity and capability order |
| `context.durable-effect-adapter/v1alpha1` | effect identity, replay policy, status lookup and idempotency capabilities |
| `context.durable-operation-crash-fixture/v1alpha1` | process crash result at a durable boundary |
| `context.durable-operation-real-crash-fixture/v1alpha1` | reconciled checkpoint, State MCP and effect result |
| `context.durable-operation-benchmark/v1alpha1` | measured crash recovery, latency and semantic-effect counts |
| `context.deepseek-checkpoint-receipt/v1alpha1` | pinned DeepSeek fixture execution and source/report provenance |
| `context.typed-state/v5alpha1` | durable Effect request SHA-256 binding while preserving the v4 object model |
| `context.state-mcp/v2alpha1` | digest-bound effect request; v1 requests and responses remain separate legacy wires |
| `context.typed-state-v4-v5-migration-receipt/v1alpha1` | deterministic upgrade or rollback bound to state cursor, registry and authorization |

All fourteen schemas are strict and admitted once in `schemas/registry.yaml`.
The registry file SHA-256 is
`02ba83c80c090571204ec33e5c08a547da672d3e87cfcdc66159e554f0267865`.

## Versioned Migration

Published Typed State v1-v4 and State MCP v1 artifacts retain their original
bytes and SHA-256 values. Typed State v5 adds the required nullable
`request_sha256` field to each Effect. State MCP v2 requires that digest on the
v2 effect request and rejects v2 effect writes against pre-v5 snapshots.

The v4 to v5 migration assigns `null` without inferring historical payloads.
Its receipt binds source and target snapshots, project revision, event head,
registry digest, authorization reference and migration implementation. The
SQLite adapter applies the snapshot and receipt atomically; replay is
idempotent and transaction fault injection leaves neither a partial snapshot
nor a partial receipt.

## Rollback

Rollback to v4 is accepted only while every Effect digest remains `null` and
the persisted project revision, snapshot and event head still match the v5
upgrade boundary. A populated digest or any post-upgrade Event makes rollback
lossy and is rejected. Genesis projects use a null event head; non-genesis
projects require a sequence and matching event digest.

## Verification

| Gate | Result |
|---|---|
| focused behavior and contracts | `78/78` pass |
| process crash boundaries | all 9 boundaries recover; terminal recoveries `180/180` |
| SQLite transaction crash | schema initialization, operation create and transition transactions survive real `SIGKILL` |
| concurrency | two runners produce one terminal result and one explicit CAS conflict; semantic effect count `1` |
| effect accounting | 180 semantic effects; 200 adapter invocations; 20 same-key deduplications; duplicate semantic effects `0` |
| authority accounting | 180 intent commits and 180 state commits |
| restore latency | p50 `82.244484 ms`; p95 `105.039451 ms`; max `130.937087 ms` |
| provider calls in local benchmark | `0` |
| DeepSeek runnable checkpoint | pinned revision `47f943859bef60e4160492346772ded9b24f765a`; `2/2` tests; real `SIGKILL` `2`; recovery passes `2`; skipped/failed `0`; provider calls `0` |
| DeepSeek recovery signals | `request-interrupted`; `TOOL_OUTCOME_UNKNOWN`; blind tool replay prohibited |
| Pi durability evidence | fixed protocol/test oracle; implementation status `scaffold`; runnable dependency `false` |
| schema governance and wire compatibility | `16/16` focused pass; legacy release hashes preserved |
| State and provider-native authority in benchmark/oracle receipts | `false` |

The measured benchmark receipt is
[`m8-01-durable-operation-results.json`](../../experiments/evidence/m8-01-durable-operation-results.json),
file SHA-256
`a57652680f6ba5bb333f8df99873e6c201ea00f73fed034c363ff39d07ca504f`.
The DeepSeek receipt is
[`m8-01-deepseek-checkpoint-receipt.json`](../../experiments/evidence/m8-01-deepseek-checkpoint-receipt.json),
file SHA-256
`d1054f639adc393b44389b0153e30bf4aae85b84c0508c8d62d583b9338b0b56`.
Raw external Vitest output remains outside Git admission.

## Boundaries

M8-01 authorizes effect dispatch only after a State MCP intent has committed and
the local runner wins the expected-revision transition. Claim and lease expiry,
revocation, scope ownership and authority revision must be revalidated at
dispatch for shared workers; that protocol belongs to M8-02 and M8-06. This
acceptance does not claim cross-device unique ownership, tenant isolation,
Temporal replay or provider-native authoritative state.

The DeepSeek fixture proves provider session checkpoint recovery and explicit
unknown tool outcome handling. The local durable operation matrix proves one
semantic effect across retries and crashes. Each receipt retains its own
authority and evidence scope.

## Reproduction

```text
.venv/bin/python tools/generate_m8_01_acceptance.py --samples 20
.venv/bin/python -m unittest discover -s tests -p 'test_m8_01*.py' -v
.venv/bin/python -m unittest tests.test_schema_governance -v

git -C /tmp/deepseek-harness rev-parse HEAD
corepack pnpm@11.7.0 install --frozen-lockfile
DSH_E2E_MAX_WORKERS=1 corepack pnpm@11.7.0 exec vitest run --config vitest.e2e.config.ts packages/session/session-checkpoint-policy/tests/crash-recovery.e2e.ts --retry=0 --reporter=json --outputFile=/tmp/deepseek-checkpoint-vitest.json
.venv/bin/python tools/generate_m8_01_deepseek_receipt.py --report /tmp/deepseek-checkpoint-vitest.json --source-root /tmp/deepseek-harness
```
