# M9-04 Context, Reference, Harness Health And Replay Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified read-only projection contract

```yaml
document_id: context.m9-04-context-health-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m9-04-context-health-v1
affected_tasks: [M5-03, M5-05, M5-08, M7-06, M8-04, M8-06, M9-01, M9-03, M9-04, M9-05]
next_review: M9-05
```

## Scope

M9-04 consumes one authenticated M9-01 State projection, one validated M9-03
Decision/Evidence projection and provenance bundle, M8-04 trace events, M5-03
PostCompact canaries, M5-05 accounting receipts, M7-06 ReferenceWatcher
records, M8-06 Harness Runs and events, M2-06 checkpoints, and M5-08 durable
recovery inputs. It emits a signed, same-revision Context, Reference, Harness
Health and Replay projection.

The projection includes governance identity, complete source digests, trace and
Harness event heads, Reference validity watermark, checkpoint digests, health
summaries, SLO results and bounded failure drilldowns. Validation authenticates
the source projections and reconstructs the complete derived output before
canonical comparison.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.context-health-projection/v1alpha1` | signed Context, Reference, Harness, Replay, drilldown and zero-authority projection |
| `context.context-health-benchmark/v1alpha1` | local measured source-binding, health visibility, tamper, capacity and latency receipt |
| `context.metric-evidence/v1alpha1` | canonical accounting/corpus identifier and content digest plus route/provider/trace/run/time/name/value/unit/source binding for measured metrics |

Both contracts use strict JSON Schema and exact registry hashes. Unknown fields,
authority escalation, executable drilldown links, inline external URLs and
invalid timestamp/hash/identifier forms are rejected.

## Source Bindings

| Source | Admission rule |
|---|---|
| State | M9-01 signature, State schema, project, revision and State digest validate |
| Decision/Evidence | M9-03 signature, State binding, provenance bundle, assertion, claim and verdict replay validate |
| Trace | complete hash chain, project/revision/active Work, absolute timestamp and evidence refs validate |
| Compaction | PreCompact/PostCompact identity and order, material delta, hook receipt, checkpoint, Execution Packet and PostCompact canary validate |
| Accounting | M5-05 receipt validates, its content-addressed ref is present on the bound trace event, and every measured metric resolves to canonical typed evidence bound to the exact accounting and corpus digests, route, provider, trace event, run, time, name, value, unit and source kind |
| Reference | watch, observation, decision, trusted time, watermark, assertion membership and authority/source/revision/hash binding to validated M9-03 provenance validate |
| Harness | Work revision, active State claim, lease epoch/fence, scope ownership, checkpoint, packet, Reference watermark and lifecycle event validate |
| Replay | checkpoint manifest, signed State digest, event head, Work revision, durable first action, acknowledged input, reserved effect, effect watermark and read budget validate; durable recovery emits stable error codes for first-action mismatch, read budget, input replay, effect replay, State binding and recovery-contract failure |

Failed recovery and Harness records remain visible through opaque IDs, status,
hashes and artifact refs. First-action text, acknowledged input text, transcript
content, raw trace attributes and external source URLs are excluded.

## Verification

| Gate | Result |
|---|---|
| M9-04 behavior, benchmark, runner and schema tests | `40/40` pass |
| M9-01 through M9-04 combined tests | `106/106` pass |
| same revision, source binding and Context binding | `1000/1000` each; rate `1.0` |
| stale assertion, Harness failure and Replay failure visibility | `1000/1000` each; rate `1.0` |
| unavailable measurement honesty | `1000/1000`; rate `1.0` |
| coordinated tamper rejection | `1000/1000`; rate `1.0` |
| veto matrix | expired Claim, Reference conflict, Harness terminal digest, partial accounting, read budget classification, acknowledged-input replay classification, missing metric evidence, canary tamper and effect watermark drift each `1000/1000`; rate `1.0` |
| capacity matrix | `11/11` boundaries rejected: seven source-list limits, relationship count, source nodes, source string and 257th drilldown |
| false healthy / false current assertion | `0 / 0` |
| authority violations / provider invocations / external services | `0 / 0 / 0`; isolated provider adapter, recall/reviewer, socket and subprocess effect probe |
| operational projection latency | 1,000 samples; p50 `10.703734 ms`; p95 `15.409254 ms`; max `47.535172 ms`; threshold p95 `<50 ms` |
| 3,500-event scale latency | 25 samples; p50 `216.786916 ms`; p95 `253.994472 ms`; max `258.900477 ms`; threshold p95 `<500 ms` |
| benchmark verdict | `passed`; failed gates `[]` |
| receipt | [`m9-04-context-health-results.json`](../../experiments/evidence/m9-04-context-health-results.json), receipt SHA-256 `1dab7f5d6bdeeeeae04ff3632331ae8328cb83b746f9612ed1fd2612f3a119ab`, file SHA-256 `c39276b7ecd5debf867d54170fb51c1dd4574c47a5ec48c6ec7f989d6341aa4a` |

Each local iteration measures the complete operational build, source-bound
rebuild validation and coordinated re-sign tamper rejection path and exercises
the nine-case veto matrix. The receipt preserves all 1,000 operational samples
and 25 independent 3,500-event scale samples. Its validator recomputes counts,
rates, both latency distributions, gate, receipt digest and implementation,
benchmark, fixture and schema hashes.
The provenance set also binds the metric-evidence implementation and strict
schema hashes, so weakening either validator invalidates the receipt.

The receipt uses `attestation.mode=local-unattested` and
`measurement_authenticity=false`. A trusted CI signature or independent
verifier rerun is required for authenticated performance provenance.
Provider invocation and external-service counts come from a runtime effect
probe around the complete benchmark path. The probe intercepts provider Skill,
recall and reviewer adapters plus socket and subprocess entry points; an
attempted external effect fails the benchmark.

## Capacity And Integrity Boundaries

Each trace, compaction, accounting, Reference, Harness Run and recovery source
is limited to 10,000 records. Harness events and aggregate relationships are
limited to 50,000. Source traversal is limited to 200,000 nodes and strings to
16,384 characters before canonical hashing or deep validation. One projection
contains at most 256 drilldowns; the 257th fails closed. Drilldown relationship
and artifact-ref lists remain schema-bounded.

Valid provider-neutral `context.*` trace event names remain extensible while
the eight required families retain separate coverage accounting. Future timestamps, broken or truncated chains, duplicate IDs, unbound Harness
lifecycle events, stale or expired claims, incorrect Work revisions, forged or
source-mismatched Reference assertions, unbound accounting receipts, invalid
canaries, replayed acknowledged input, effect watermark drift and coordinated
projection re-signing fail closed or produce an explicit failed recovery
drilldown. RFC3339 ordering uses parsed absolute time across offset forms.

## Measurement Availability

Provider token and cache totals remain `unavailable` because the fixed M5-05
fixture has no provider trace export. Context-window utilization remains
`unavailable`; no current contract measures provider window capacity. Retrieval
byte totals are `measured` only when a validated M5-05 accounting receipt is
bound through a trace evidence ref and every measured metric has an exact typed
evidence binding. Arbitrary resolvable bytes, wrong provider/run/time/value,
accounting/corpus content drift, schema-unknown metric names, units or source
kinds, non-canonical JSON and digest mismatch are rejected. Partial provider
routes produce `unavailable` and `null` totals; missing metric evidence is
rejected.

Harness effect health remains
`unavailable-v1-revision-semantics`. Harness Run v1 carries Work revision but
does not carry a distinct State revision, while the M8-06 coordinator effect
gate uses one revision field for both concepts. A versioned M8-06 contract must
separate `state_revision` and `task_revision` before complete effect health can
be claimed.

## Authority Boundaries

The projection has zero State commit, completion, approval, provider and
external-effect authority. It performs local validation and replay only. It
does not dispatch an effect, refresh an external Reference or invoke a model.

This acceptance does not prove Docmost browser rendering, browser pagination,
HTTP/SSE transport, artifact download authorization, CSP, controlled approval,
promotion or correction actions. M9-05 and later integration work retain those
completion gates.

The benchmark fixture is repository acceptance material and imports the
versioned M9-04 test fixture when the benchmark is executed. Runtime projection
and validation modules have no dependency on the test package.

## Reproduction

```text
.venv/bin/python tools/run_context_health_benchmark.py --root . --iterations 1000 --generated-at 2026-08-17T23:00:00+08:00 --output experiments/evidence/m9-04-context-health-results.json
.venv/bin/python -m unittest tests.test_m9_04_context_health_projection tests.test_m9_04_context_health_benchmark tests.test_m9_04_contract_schemas -v
ruff check context_control_plane/context_health_projection.py context_control_plane/context_health_benchmark.py context_control_plane/metric_evidence.py tests/test_m9_04_context_health_projection.py tests/test_m9_04_context_health_benchmark.py tests/test_m9_04_contract_schemas.py tools/run_context_health_benchmark.py
```
