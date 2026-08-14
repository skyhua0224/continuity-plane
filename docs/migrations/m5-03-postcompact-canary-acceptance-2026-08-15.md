# M5-03 PostCompact Deterministic Canary Acceptance

Version: 1  
Date: 2026-08-15  
Status: verified local-embedded shadow adapter

```yaml
document_id: context.m5-03-postcompact-canary-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m5-03-postcompact-canary-v1
affected_tasks: [M5-03, M5-06, M5-07, M5-08]
next_review: M5-04
```

## Scope

M5-03 verifies restored execution context against an M2-06 checkpoint and an
expected M5-01 Execution Packet loaded from local content-addressed storage.
The trusted binding fixes project and task revisions, event head, governance
digest, registry digest, state digest, active Work and effect high watermark.
The evaluator opens only the execution gate; State write and provider-native
authority remain false.

Pi cut point and split-turn metadata and DeepSeek semantic checkpoint metadata
are shadow adapter inputs. The benchmark does not claim that a live provider
performed compaction. PostgreSQL, Docker, Docmost, network services and remote
models are not required.

## Verification

| Gate | Result |
|---|---|
| strict contracts | `context.postcompact-canary/v1alpha1` and `context.postcompact-canary-benchmark/v1alpha1` registered; runtime and Draft 2020-12 validation pass |
| focused behavior | deterministic restore, critical-field loss, forged packet set, delta/watermark, Pi and DeepSeek mismatch tests `10/10` pass |
| replay | `1000/1000`; replay mismatch `0` |
| critical recovery | decision, constraint and active Work `100%` |
| fault injection | `8000/8000` rejected; eight fault classes with false accepts `0` |
| authority | State write and provider-native authority violations `0` |
| latency | p50 `1.06194 ms`; p95 `1.125964 ms`; max `1.980451 ms`; state-only restore gate `<2000 ms` passed |
| external services | `0` |
| receipt | [`m5-03-postcompact-canary-results.json`](../../experiments/routing/m5-03-postcompact-canary-results.json) passes strict benchmark and provenance validation |

## Boundaries

The canary proves deterministic local restore for the versioned Pi and
DeepSeek fixtures. Live provider token, cache and billing data remain
unavailable and are assigned to M5-05. M5-07 must emit real project dogfood
observations through a versioned trace contract before trend claims are
permitted.

## Reproduction

```text
.venv/bin/python tools/run_postcompact_canary_benchmark.py --samples 1000 --generated-at 2026-08-15T00:30:00Z
.venv/bin/python -m unittest tests.test_m5_03_postcompact_canary tests.test_m5_03_postcompact_canary_benchmark -q
.venv/bin/python tools/verify_repository.py --root .
```

Implementation, schema, test, runner and registry-entry hashes are recorded in
the benchmark receipt and recomputed by its validator.
