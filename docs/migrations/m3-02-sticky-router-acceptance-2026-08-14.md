# M3-02 Sticky Router Acceptance

版本：1  
日期：2026-08-14  
状态：implemented offline routing contract

```yaml
document_id: context.m3-02-sticky-router-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/current-head
supersedes: null
affected_tasks: [M3-02]
next_review: M3-03
```

## 范围

M3-02 defines a deterministic `context.task-route-request/v1alpha1` to `context.task-route-decision/v1alpha1` contract over a validated Typed State snapshot. Request replay binds project revision, active Work revision, opaque input ref/hash, classifier provenance and optional authorization-candidate provenance.

The router returns candidate route labels. It does not write Typed State, append an Event, persist an Idea, create a Checkpoint, change a Claim, activate a target or execute an Effect. `authorization_verified` and `state_write_authority` remain `false` for every result. M3-03 must independently validate authorization, checkpoint evidence and revision/CAS before activation.

## Results

```yaml
verification_receipt:
  path: experiments/routing/m3-02-verification-receipt.json
  content_sha256: c4d79d7c41b7f85ae84e1434d7109c09bcd3a6ffc8ba7de7f725c4e20ef6aef9
route_benchmark:
  sample_count: 1000
  active_leaf_preserved: 1000
  deterministic_replays: 1000
  unauthorized_switches: 0
  requests_unchanged: true
  state_unchanged: true
  p50_route_latency_ms: 0.053213
  p95_route_latency_ms: 0.104553
  max_route_latency_ms: 0.187436
```

| Gate | Result |
|---|---|
| strict schema and registry | Request and decision schemas reject unknown fields and bind current registry hashes. |
| canonical input | Wire values use canonical snake_case; legacy hyphen forms and M3-08-only input kinds are rejected. |
| sticky behavior | Nine supported input kinds preserve the active Work ID and Work revision. |
| guarded switch proposal | Only a snapshot-ready target with complete dependencies and no open blocker can produce a proposal; both Work and Blocker reverse references participate in the blocker gate. Authorization remains an unverified candidate. Trusted expiry evaluation remains M3-05. |
| safety signals | Low-confidence Idea is retained as a candidate; low-confidence correction requires review and write protection; low-confidence switch requires review. |
| replay | Golden request and decision bytes bind input, classifier provenance, project revision, Work revision and target revision; tampered authorization or revision is rejected. |
| regression | `16/16` focused tests pass; final repository test and admission totals are recorded by the commit gate. |

## Boundaries

M3-03 owns checkpoint persistence, route Events, return frames and CAS activation. M3-06 owns Idea persistence, deduplication and parking. M3-08 owns `blocking_decision`, `stop_or_replace`, next-ready selection and bounded escalation. M3-02 provides no provider token, context-window or live compaction evidence.

## Reproduction

```text
.venv/bin/python -m unittest tests.test_m3_02_sticky_router -v
.venv/bin/python tools/verify_repository.py --root .
```
