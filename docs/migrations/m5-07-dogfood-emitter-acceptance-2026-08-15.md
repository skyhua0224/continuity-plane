# M5-07 Project Dogfood Emitter Acceptance

Version: 1  
Date: 2026-08-15  
Status: verified local-embedded shadow adapter

```yaml
document_id: context.m5-07-dogfood-emitter-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m5-07-dogfood-emitter-v1
affected_tasks: [M5-07, M8-04, M8-07]
next_review: M6-07
```

## Scope

M5-07 emits input routing, visible compaction, Skill selection, plan revision,
multi-Agent dispatch/handoff and accepted delivery observations through the
append-only local `context.*` trace contract. Coverage uses explicit ingress,
compaction, Skill, revision, dispatch and delivery denominators. Candidate Idea
observations preserve the active Work and return point. Observation and trace
receipts have no State MCP or provider authority.

## Verification

| Gate | Result |
|---|---|
| strict contract | `context.dogfood-event/v1alpha1`, `context.dogfood-coverage/v1alpha1` and benchmark schemas pass Draft 2020-12 checks and runtime validation |
| focused behavior | emitter, candidate Idea, digest, full coverage and regression gates `5/5` pass |
| replay | `1000/1000`; replay mismatch `0` |
| event coverage | `8000` emitted events; all seven coverage dimensions `1000000/1000000` |
| veto detection | missing handoff, late canary, first-action mismatch and acknowledged-input replay `4000/4000` detected |
| authority | observation, trace and provider authority violations `0` |
| latency | local emit, trace validation and coverage p50/p95/max `1.335444/1.377223/2.223157 ms` |
| external services | `0` |
| receipt | [`m5-07-emitter-results.json`](../../experiments/dogfood/m5-07-emitter-results.json) passes strict benchmark and current implementation hash validation |

## Boundary

The benchmark measures the local emitter and deterministic fixtures. Provider
context-window occupancy, token/cache usage and invisible compaction signals
remain `unavailable` without admitted provider telemetry. The observations are
evidence candidates and cannot modify active Work, claims, decisions or effects.

## Reproduction

```text
.venv/bin/python tools/run_dogfood_emitter_benchmark.py --samples 1000 --generated-at 2026-08-15T05:30:00Z
.venv/bin/python -m unittest tests.test_m5_07_dogfood_emitter tests.test_m5_07_dogfood_emitter_benchmark -q
```
