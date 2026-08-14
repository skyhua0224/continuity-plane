# M5-04 Bounded Artifact Expansion Acceptance

Version: 1  
Date: 2026-08-15  
Status: verified local-embedded shadow adapter

```yaml
document_id: context.m5-04-bounded-expansion-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m5-04-bounded-expansion-v1
affected_tasks: [M5-04, M5-05, M6-07]
next_review: M5-06
```

## Scope

M5-04 composes a prompt projection from a summary, a content-addressed
artifact reference and explicitly requested non-overlapping byte ranges. The
CAS verifies the artifact digest before returning excerpts. Returned bytes and
full-object integrity scan bytes are separately accounted and bounded. State
write authority and provider-native authority remain false.

## Verification

| Gate | Result |
|---|---|
| strict contract | `context.bounded-artifact-expansion/v1alpha1` and benchmark schema registered; receipt and prompt validators pass |
| focused behavior | bounded ranges, budgets, digest, UTF-8 and prompt projection tests `9/9` pass; M2-04 regression `29/29` |
| replay | `1000/1000`; replay mismatch `0` |
| returned payload | maximum `96 B`, budget `256 B` |
| scan accounting | range scan bytes measured separately and exceed returned bytes by design |
| fault injection | returned-budget and digest faults rejected `2000/2000` |
| prompt reduction | measured byte reduction is positive; byte reduction is not reported as token reduction |
| external services | `0` |
| receipt | [`m5-04-bounded-expansion-results.json`](../../experiments/routing/m5-04-bounded-expansion-results.json) passes strict benchmark and provenance validation |

## Reproduction

```text
.venv/bin/python tools/run_bounded_artifact_expansion_benchmark.py --samples 1000 --generated-at 2026-08-15T03:00:00Z
.venv/bin/python -m unittest tests.test_m5_04_bounded_artifact_expansion tests.test_m5_04_bounded_artifact_expansion_benchmark -q
```
