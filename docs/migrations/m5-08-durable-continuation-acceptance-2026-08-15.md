# M5-08 Durable Continuation Acceptance

Version: 2  
Date: 2026-08-15  
Status: verified local-embedded shadow adapter

```yaml
document_id: context.m5-08-durable-continuation-acceptance
document_revision: 2
change_type: evidence
authority_ref: verification-run://repository/m5-08-durable-continuation-v2
supersedes: context.document://docs-migrations-m5-08-durable-continuation-acceptance-2026-08-15/revision/1
affected_tasks: [M5-08, M5-07, M8-01, M8-06]
next_review: M5-07
```

## Scope

M5-08 provides a provider-neutral durable operation cursor. The cursor binds
`operation_id`, project and task revisions, Event head, phase, last durable
action, exact next action, acknowledged input IDs, reserved effect IDs,
per-effect `safe|never` replay policy and response mode. The phase FSM preserves
the atomic continuation point through `prepared`, `intent-committed`,
`effect-in-flight`, `effect-settled`, `response-committed` and `terminal`.

Recovery compares trusted state, restored state and a trusted authority binding
before opening the execution gate. A reset first action, duplicate response to
an acknowledged input, stale revision/Event head, invalid phase/effect or
`never` replay is rejected. Recovery reads produce a bounded receipt containing
source refs, content digests, byte count and budget. State MCP and provider
native authority remain false. Recovery failures carry stable error codes;
read-budget failure is distinct from recovery-contract integrity failure.

Pi `op.state` is used as a protocol field oracle for operation state, reserved
effect IDs and replay policy. The implementation does not depend on Pi or any
provider runtime.

## Verification

| Gate | Result |
|---|---|
| strict contracts | `context.durable-continuation/v1alpha1`, `context.durable-continuation-recovery/v1alpha1` and `context.durable-continuation-benchmark/v1alpha1`; all three Draft 2020-12 schemas check successfully |
| focused behavior | durable state, FSM, revision/Event head binding, anti-reset, duplicate-input, effect replay, bounded receipt and authority tests `14/14` pass |
| replay | `1000/1000`; replay mismatch `0`; continuation fields `10000/10000` |
| first action | first-action mismatches `0`; the post-restore action remains the durable cursor action |
| duplicate response | acknowledged input replays `0` |
| fault injection | `10000/10000` rejected across 10 fault classes; false accepts `0` |
| bounded recovery | maximum recovery read `3328 B` against `4096 B` budget |
| latency | local recovery receipt p50/p95/max `0.169188/0.19953/0.461365 ms` |
| authority | State write and provider-native authority violations `0` |
| external services | `0` |
| receipt | [`m5-08-durable-continuation-results.json`](../../experiments/routing/m5-08-durable-continuation-results.json) passes runtime, hash and schema validation |

## Boundaries

This evidence covers local deterministic state and recovery gating. It does
not claim live Pi or DeepSeek compaction, provider token/cache/billing data,
cross-process leases, durable effect execution, PostgreSQL behavior or
multi-Agent fan-out. Those remain M8 contracts.

## Reproduction

```text
.venv/bin/python -m unittest tests.test_m5_08_durable_continuation tests.test_m5_08_durable_continuation_benchmark -q
.venv/bin/python - <<'PY'
from context_control_plane.durable_continuation_benchmark import benchmark_durable_continuation, validate_durable_continuation_benchmark
receipt = benchmark_durable_continuation(samples=1000, generated_at="2026-08-15T04:30:00Z")
validate_durable_continuation_benchmark(receipt)
print(receipt)
PY
```
