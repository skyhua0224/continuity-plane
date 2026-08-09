# M2-09 SQLite StateStore Acceptance

版本：1  
日期：2026-08-10  
状态：Linux verified / Windows and macOS collaborator required

```yaml
document_id: context.m2-09-sqlite-state-store-acceptance
document_revision: 1
change_type: evidence
authority_ref: repository-tests-and-linux-fault-injection-2026-08-10
supersedes: null
affected_tasks: [M2-04, M2-05, M2-06, M2-09, M10-09]
next_review: windows-and-macos-live-runner-evidence
```

## Scope

M2-09 provides the default `local-embedded` StateStore through Python's standard-library SQLite implementation. The adapter persists typed snapshots and complete Event envelopes in one local `context.sqlite` file. It requires no database daemon, container, PostgreSQL installation or network service.

The storage boundary uses WAL, `FULL` synchronous mode, foreign keys, a bounded busy timeout and `BEGIN IMMEDIATE`. Every commit validates the expected revision, Event sequence, adjacent revisions, hash chain, reducer result and project head before publishing the snapshot and Event atomically. SQLite application and schema version identifiers prevent accidental ownership of unrelated database files.

## Verification

| Gate | Result |
|---|---|
| M2-09 targeted tests | 48/48 passed |
| Full repository after revision 24 projection | 282 tests; 0 failed; 27 PostgreSQL tests skipped without `CONTEXT_TEST_POSTGRES_DSN` |
| Repository verifier | passed |
| Python compile | passed |
| SQLite durability configuration | actual WAL, `FULL`, foreign-key and busy-timeout values verified on every connection |
| Local multi-writer | same-file cross-connection and spawned-process conflicts were explicit; silent overwrite 0 |
| Process termination | pre-commit SIGKILL rolled back completely; post-commit SIGKILL recovered exactly one Event |
| Append-only Event storage | canonical SQL signature and behavioral triggers rejected UPDATE and DELETE |
| Corruption | header and non-header page damage produced typed integrity errors |
| File lifecycle | Unicode path, checkpoint, close, rename, reopen, WAL/SHM cleanup and delete passed on Linux |
| Service footprint | managed daemon 0; managed container 0 |

The 40-sample benchmark used the same completed-work fixture, operation boundary, validator path and sample count as the PostgreSQL receipt. SQLite commit p95 was `0.6500 ms`; state-only restore p95 was `0.2678 ms`. The database occupied `274432 bytes` after a truncate checkpoint. Commit p95 was 97.0481% lower and read p95 was 97.8973% lower than the measured optional PostgreSQL backend on this host. The canonical receipt generator records implementation, runner, fixture, PostgreSQL input and fault-test hashes, actual CLI arguments and the complete receipt schema. It is reproducible with:

```bash
.venv/bin/python tools/run_sqlite_benchmark.py --samples 40 --stream-events 1000 --output experiments/state/m2-09-sqlite-state-store-results.yaml
```

## Scaling Boundary

The 1,000-Event stream completed with final restore latency `21.7868 ms` and database size `2932736 bytes`. Commit p95 was `34.9701 ms`; cumulative commit time was `18735.9483 ms`. Revalidating the complete Event history on every commit creates observed quadratic cumulative commit growth. M2-06 and M5 must add an incremental verified-head path while retaining explicit full-chain restore and audit verification. Current performance evidence applies to one Linux x86_64 workstation.

## Authority Boundary

The SQLite adapter provides local typed-state and append-only Event authority. It does not provide remote shared authority, unique cross-device claims, lease time, checkpoint canary authority, artifact persistence or cross-backend migration. State MCP authorization remains M2-05 scope. Local artifact storage remains M2-04 scope.

## Open Platform Gates

Linux live verification is complete. Windows and macOS remain blocked until dedicated native runners execute WAL/SHM lifecycle, cross-process locking, SIGKILL recovery, Unicode paths, rename/delete and non-header page corruption fixtures. Linux, WSL, Wine and synthetic platform values do not satisfy these gates.

M2-09 remains `🧑‍💻` until both native platform receipts pass. The required queue advances to M2-04 because its dependency is M2-01 and the unavailable platform runners do not block content-addressed artifact storage.
