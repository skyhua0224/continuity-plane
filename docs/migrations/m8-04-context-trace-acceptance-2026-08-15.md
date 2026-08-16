# M8-04 Context Trace Acceptance

Version: 2  
Date: 2026-08-15  
Status: verified local-embedded trace contract

```yaml
document_id: context.m8-04-context-trace-acceptance
document_revision: 2
change_type: evidence
authority_ref: verification-run://repository/m8-04-context-trace-v2
supersedes: context.document://docs-migrations-m8-04-context-trace-acceptance-2026-08-15/revision/1
affected_tasks: [M8-04, M5-07, M8-06, M9-04]
next_review: M5-07
```

## Scope

M8-04 records provider-neutral `context.*` events for compaction, input
routing, Skill selection, plan revision, multi-Agent dispatch and handoff, and
accepted delivery. Every event binds project, State revision, active Work,
trace, span, run, operation and correlation identifiers. Source, evidence and
timezone-aware observation fields are required. Events form a canonical
append-only hash chain and grant no State or provider authority.

The local emitter persists canonical JSONL and validates the complete chain
before resuming. OTel export is optional. An absent exporter is recorded as
`unavailable`; an attempted export requires a structured exporter identity,
event count and evidence reference before the result can be recorded as
`exported`.

## Verification

| Gate | Result |
|---|---|
| strict contracts | `context.trace-event/v1alpha1`, `context.otel-export/v1alpha1` and `context.context-trace-benchmark/v1alpha1` pass runtime and Draft 2020-12 validation |
| focused behavior | event, chain, local persistence, OTel state and benchmark tests `15/15` pass |
| replay | `1000/1000`; `8000` trace events; replay mismatch and hash-chain failures `0` |
| required observations | eight event families covered; coverage `100%` |
| binding and evidence | State/run binding failures `0`; source/evidence failures `0` |
| authority | authority violations `0` |
| OTel honesty | exporter unavailable `1000/1000`; false exported success `0`; empty export success rejected |
| latency | p50 `0.826903 ms`; p95 `1.643596 ms`; max `1.711361 ms` for an eight-event local compose/validate cycle |
| external services | `0` |
| receipt | [`m8-04-context-trace-results.json`](../../experiments/observability/m8-04-context-trace-results.json) passes strict benchmark and implementation-hash validation |

## Boundary

This acceptance verifies the local-embedded M8-04 trace dependency used by
M5-07. It does not claim that a live OTel Collector accepted an export or that
provider-native token, cache or billing metrics are available. Enabling a live
exporter remains an optional deployment capability. Trace events remain
observations and cannot submit authoritative State changes.

## Reproduction

```text
.venv/bin/python -m unittest tests.test_m8_04_context_trace tests.test_m8_04_context_trace_benchmark -q
.venv/bin/python -c 'import json; from context_control_plane.context_trace_benchmark import benchmark_context_trace; print(json.dumps(benchmark_context_trace(samples=1000), indent=2, sort_keys=True))'
```
