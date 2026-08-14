# M5-02 Material Event PreCompact Checkpoint Acceptance

Version: 1  
Date: 2026-08-14  
Status: verified local-embedded shadow adapter

```yaml
document_id: context.m5-02-compaction-checkpoint-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m5-02-compaction-checkpoint-v1
affected_tasks: [M5-02, M5-03, M5-05, M5-06, M5-07, M5-08]
next_review: M5-03
```

## Scope

M5-02 publishes a bounded material-event delta before compaction. The delta is
content-addressed and bound to the project revision, event head, active task
revision, effect high watermark, Execution Packet digest and canonical plan
digest. Pi and DeepSeek adapters emit shadow hook receipts only; provider-native
authority and State write authority remain false.

The implementation is provider-neutral and local-embedded. It does not import
provider transcripts, start a provider process, or require PostgreSQL, Docker,
Docmost, a network service or a remote model. Stale bases, event gaps,
duplicates and provider metadata that attempts to widen authority or payload
are rejected closed.

## Verification

| Gate | Result |
|---|---|
| strict contract | `context.material-event-delta/v1alpha1` and `context.provider-compaction-hook/v1alpha1` registered; runtime validators and Draft 2020-12 schemas pass |
| focused behavior | M5-02 delta, publication and Pi/DeepSeek shadow-hook tests `5/5` pass |
| benchmark validation | receipt schema, provenance and negative veto tests `3/3` pass |
| replay | `1000/1000` deterministic delta and hook receipts; delta mismatch `0`, hook mismatch `0` |
| watermark and authority | task/effect watermark mismatch `0`; provider-native and State authority violations `0` |
| capacity | min/p50/p95/max delta `1093/1093/1093/1093 B` |
| latency | PreCompact p50 `0.639104 ms`; p95 `0.666804 ms`; max `1.111154 ms`; gate `<500 ms` passed |
| external services | `0` |
| receipt | [`m5-02-compaction-checkpoint-results.json`](../../experiments/routing/m5-02-compaction-checkpoint-results.json) passes strict benchmark and provenance validation |

## Boundaries and next work

This task establishes the rolling delta and host adapter bracket. It does not
claim that a real provider performed compaction or that post-compaction state
was restored. M5-03 must add the deterministic PostCompact canary and Pi /
DeepSeek cut-point fixtures before any provider-specific live claim is made.
M5-05 will account for provider token/cache/retrieval data when a permitted
runtime trace is available.

## Reproduction

```text
.venv/bin/python tools/run_compaction_checkpoint_benchmark.py --samples 1000 --generated-at 2026-08-14T20:30:00Z
.venv/bin/python -m unittest tests.test_m5_02_compaction_checkpoint tests.test_m5_02_compaction_checkpoint_benchmark -q
.venv/bin/python tools/verify_repository.py --root .
```

Implementation and fixture hashes are recorded in the benchmark receipt and
are recomputed by its validator; this document does not replace those checks.
