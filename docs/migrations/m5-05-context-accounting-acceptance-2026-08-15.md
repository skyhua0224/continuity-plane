# M5-05 Context Accounting Acceptance

Version: 1  
Date: 2026-08-15  
Status: verified local-embedded shadow adapter

```yaml
document_id: context.m5-05-context-accounting-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m5-05-context-accounting-v1
affected_tasks: [M5-05, M5-07, M6-07]
next_review: M5-06
```

## Scope

M5-05 records token, cache, billing, retrieval, compaction and cut-point
metrics with source and evidence fields. Provider token/cache/billing metrics
are `unavailable` when a provider trace is absent. Local retrieval and timing
measurements remain usable; byte values cannot be converted into provider token
claims. Pi and DeepSeek use the same corpus digest and token budget, with
route-aware thresholds and model-free pruning recorded independently.

## Verification

| Gate | Result |
|---|---|
| strict contract | `context.context-accounting/v1alpha1` and benchmark schema registered; runtime receipt validator passes |
| focused behavior | evidence source, unavailable metric, finite value, route and authority tests `11/11` pass |
| replay | `1000/1000`; replay, corpus/budget, route and accounting failures `0` |
| provider data honesty | measured routes `0`; unavailable routes `2`; false provider improvement claims `0` |
| local measurements | `5/route` for retrieval queries/read/output bytes, retrieval latency and compaction latency; Pi host cut point measured, DeepSeek semantic cut point unavailable |
| latency | p50/p95/max are local receipt compose/validate wall time; p95 `0.295058 ms` |
| external services | `0` |
| receipt | [`m5-05-context-accounting-results.json`](../../experiments/routing/m5-05-context-accounting-results.json) passes strict benchmark validation |

## Boundary

This evidence does not claim provider billing, token or KV-cache improvement.
Those fields require an admitted provider trace and remain unavailable in the
offline fixture.

## Reproduction

```text
.venv/bin/python tools/run_context_accounting_benchmark.py --samples 1000 --generated-at 2026-08-15T02:30:00Z
.venv/bin/python -m unittest tests.test_m5_05_context_accounting tests.test_m5_05_context_accounting_benchmark -q
```
