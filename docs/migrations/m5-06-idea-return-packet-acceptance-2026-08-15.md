# M5-06 Idea Return Packet Acceptance

Version: 2  
Date: 2026-08-15  
Status: verified local-embedded shadow adapter

```yaml
document_id: context.m5-06-idea-return-packet-acceptance
document_revision: 2
change_type: evidence
authority_ref: verification-run://repository/m5-06-idea-return-packet-v2
supersedes: context.document://docs-migrations-m5-06-idea-return-packet-acceptance-2026-08-15/revision/1
affected_tasks: [M3-03, M3-06, M3-07, M5-03, M5-06, M5-07, M5-08]
next_review: M5-07
```

## Scope

M5-06 composes a bounded context return packet after an Idea interruption or
task switch. The M3-03 return frame and M5-03 PostCompact canary prove the
historical return point and checkpoint integrity. Current Typed State v4
remains authoritative for the active Work, current revisions, progress,
evidence and related Idea status.

The packet contains related Idea IDs, status, return Work ID and
`candidate-only` authority. Idea summaries, source references and transcript
body are excluded. Candidate Ideas receive no execution, State write or
external effect authority. State changes, Idea activation, task switches and
external effects continue to require State MCP authorization and validators.

Canonical MASTER or schema registry digest drift requires a digest-bound
migration receipt and an independent authority verifier. A structurally valid
self-issued receipt cannot open the return gate.

## Verification

| Gate | Result |
|---|---|
| strict contracts | four Draft 2020-12 schemas cover return packet, checkpoint binding, migration receipt and benchmark receipt; schema and runtime instance validation pass |
| focused behavior | current-authority selection, historical evidence boundary, canary binding, governance migration and authority escalation tests `11/11` pass |
| replay | `1000/1000`; replay mismatch `0` |
| task recovery | original active Work and return point recovery `100%`; mismatches `0` |
| Idea projection | related Idea ref mismatch `0`; Idea body and source-ref copies `0` |
| authority | candidate execution, candidate State write, packet State write and external effect authority violations `0` |
| fault injection | current active Work, checkpoint binding, canary return Work, MASTER digest, registry digest and return revision faults rejected `6000/6000` |
| packet bound | fixed fixture packet `1983 B`, below the `8192 B` maximum |
| latency | p50 `0.404141 ms`; p95 `0.416519 ms`; max `0.557485 ms` |
| external services | `0` |
| receipt | [`m5-06-idea-return-packet-results.json`](../../experiments/routing/m5-06-idea-return-packet-results.json) passes strict benchmark, schema and provenance validation |

## Boundaries

The benchmark uses local deterministic Typed State, return frame and
PostCompact canary fixtures. It does not claim a live provider compaction or a
remote multi-writer migration. Production migration verification must resolve
the recorded authority event through the active State MCP adapter. Live
compaction continuity and durable anti-reset observations remain M5-07 and
M5-08 evidence.

## Reproduction

```text
.venv/bin/python -m unittest tests.test_m5_06_idea_return_packet tests.test_m5_06_idea_return_packet_benchmark -q
```
