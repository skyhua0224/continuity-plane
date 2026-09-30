# M8-06 Provider-neutral Harness Run Acceptance

Version: 1  
Date: 2026-08-16  
Status: verified local provider-neutral contract

```yaml
document_id: context.m8-06-harness-run-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-06-harness-benchmark-v1
affected_tasks: [M2-05, M4-05, M5-03, M8-04, M8-06]
next_review: M8-07
```

## Scope

M8-06 defines the provider-neutral Harness Run boundary for a coordinator and
its workers. A run carries the task revision, active claim and lease fence,
Execution Packet and Skill digests, tool grants, checkpoint, verification and
reference watermarks, scope, and trace identity. Provider-native transcripts,
durable logs, state writes, and external effects remain non-authoritative.

The reference implementation is in-memory and offline. It provides the
admission and replay contract for a future State MCP or provider adapter; it
does not claim a network transport, a provider SDK, or distributed consensus.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.harness-run/v1alpha1` | strict run identity, claim/lease fence, provider contract, scope and authority-denial flags |
| `context.harness-event/v1alpha1` | append-only provider-neutral event sequence and hash chain |
| `context.harness-handoff/v1alpha1` | checkpoint-bound handoff, expected first action and acceptance state |
| `context.harness-benchmark/v1alpha1` | fault matrix, replay, fan-out/fan-in, latency and bounded provenance receipt |

## Verification

| Gate | Result |
|---|---|
| focused behavior and contract tests | `11/11` pass |
| provider contract drift | `1000/1000` rejected before worker dispatch |
| worker loss | `1000/1000` left State MCP authority unchanged |
| effect scope/claim gate | `1000/1000` invalid effects rejected; valid effect path remains idempotent |
| stale handoff | `1000/1000` rejected |
| first-action mismatch | `1000/1000` rejected |
| provider-neutral replay | `1000/1000` Codex/Claude projections match |
| fan-out/fan-in | `2000` worker dispatches and `1000/1000` terminal fan-ins |
| terminal event binding | worker-loss, worker-completed and fan-in events bind the final Harness Run SHA-256 |
| latency | p50 `1.258092 ms`; p95 `1.675527 ms`; max `3.518922 ms` (threshold p95 `50 ms`) |
| provider and external service calls | `0`; shared authority claim `false` |

The formal receipt is
[`m8-06-harness-benchmark-results.json`](../../experiments/evidence/m8-06-harness-benchmark-results.json).
Its file SHA-256 is
`c3d08d7227efe9e9e9309d5ab70f36665290135e225390a6165ceb0a4182af85` and its
receipt SHA-256 is
`a38f1fbf5268a954eceaef7713accb6c29ed6b20644e552e70ef9662fae9344d`.

## Authority Boundary

Harness admission does not grant state-write or effect authority. A worker
must carry the coordinator's provider contract, task revision, active claim,
lease fence, and an admitted scope. Effects are keyed for idempotence and are
not re-dispatched after a completed key. Worker loss only changes the local
run projection; State MCP remains the sole authority for task, claim, lease,
revision, evidence, and external-effect state.

Terminal lifecycle events bind the final Harness Run digest, so a provider or
run-field rewrite cannot retain a trusted worker-loss, worker-completed or
fan-in summary. Handoffs bind a checkpoint and task revision. A target cannot accept an action
different from the recorded first action. Fan-in accepts only terminal worker
states. Replay consumes provider-neutral events and ignores provider identity,
which is why the dual-provider projections must match.

## Reproduction

```text
PYTHONWARNINGS=error::ResourceWarning .venv/bin/python -m unittest tests.test_m8_06_harness_run tests.test_m8_06_contract_schemas -v
.venv/bin/python tools/run_harness_benchmark.py
.venv/bin/python tools/verify_repository.py --root .
```

The next planned leaf is M8-07 ProjectAdaptation. M8-06 remains local and
provider-neutral; provider SDK integration, distributed leases, and shared
network durability require later adapters and are not implied by this result.
