# M8-10 Collaboration Notification Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified local-embedded shadow contract

```yaml
document_id: context.m8-10-collaboration-notification-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-10-collaboration-notification-v1
affected_tasks: [M8-02, M8-05, M8-06, M8-09, M8-10, M9-01, M9-05, M10-00]
next_review: M9-01
```

## Scope

M8-10 defines a local-first collaboration notification log and Agent inbox
contract. SQLite stores project-scoped append-only Events and immutable
subscriptions. HMAC-SHA256 binds each Event, subscription, cursor and complete
delivery batch. A host policy adapter authorizes tenant/project scope before
lookup and verifies the current State revision, Work identity and evidence
references before publish.

SSE is the baseline delivery adapter. `Last-Event-ID` binds sequence and signed
Event hash, reconnect performs a new authorization decision, and `limit`
provides bounded delivery. WebSocket activation remains capability-gated and
optional. Agent inbox projection is display-only: context injection, operation,
approval submission and State write authority are all false.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.collaboration-notification-publish/v1alpha1` | tenant/project, State revision, Work and evidence-bound publish request |
| `context.collaboration-notification/v1alpha1` | signed append-only Event with sequence and previous hash |
| `context.collaboration-subscription-request/v1alpha1` | provider, transport and event-kind subscription request |
| `context.collaboration-subscription/v1alpha1` | signed immutable project-scoped subscription |
| `context.collaboration-subscription-cursor/v1alpha1` | signed subscriber and Event high-watermark cursor |
| `context.collaboration-delivery-batch/v1alpha1` | signed Event set, cursor, continuation and authority boundary |
| `context.agent-inbox-item/v1alpha1` | display-only provider inbox projection |
| `context.collaboration-notification-benchmark/v1alpha1` | delivery, replay, authority, latency and campaign-scale receipt |

## Verification

| Gate | Result |
|---|---|
| focused behavior, benchmark and schema tests | `21/21` pass |
| publish | `1000/1000`; rate `1.0` |
| Codex/Claude dual-session equality | `1000/1000`; rate `1.0` |
| restart and offline catch-up | `2000/2000`; rate `1.0` |
| duplicate publish suppression | `1000/1000`; rate `1.0` |
| signed Event validation | `2000/2000`; rate `1.0` |
| same State revision delivery | `2000/2000`; rate `1.0` |
| context injection, operation and State authority violations | `0` |
| provider invocations / external services | `0 / 0` |
| publish latency | p50 `0.252584 ms`; p95 `0.385539 ms`; max `0.487312 ms` |
| dual-session catch-up latency | `199.683240 ms` total |
| benchmark verdict | `pass`; failed gates `[]` |
| receipt | [`m8-10-collaboration-notification-results.json`](../../experiments/evidence/m8-10-collaboration-notification-results.json), receipt SHA-256 `0fb4a4bbd3d0c00dc3600bc3e4dff0bbcd97194f8e31f5c618944e2aa895df49` |

Focused fault coverage includes concurrent publishers, append-only SQLite
triggers, exact request replay and conflict, cross-tenant denial before lookup,
project-scoped subscription receipt identity, stale State source rejection,
Event and batch tamper detection, Event omission, cursor substitution, wrong
HMAC key restart, provider capability rejection and SSE reconnect after process
restart.

## Scale decision

The M8-09 cursor validates every retained completed step for each commit. The
M8-10 scale run measured `32,896` validations for `256` steps, compared with
`256` incremental index validations, for `128.5x` amplification. The projected
count at `1000` steps is `500,500`.

The adoption decision is `adopt-append-only-step-index`. M10 production pilot
must add the step index and replay high watermark before long campaigns are
enabled. This decision is bound to the current cursor-store, validator,
benchmark implementation and schema hashes in the benchmark receipt.

## Safety boundaries

`target_refs` are routing metadata and never replace subscription authorization.
The tenant/project subscription scope and host authorization policy define
visibility. A notification cannot claim State, provider, external-effect,
operation or context-injection authority. Approval-bearing notifications remain
display-only until the M9 controlled State MCP approval entry is verified.

The local signer currently accepts one active HMAC key. Wrong-key restart fails
closed. Key rotation and historical verification key retention are required for
the M10 production profile. The SSE implementation is a host-neutral pull and
reconnect adapter; TLS, HTTP connection lifecycle and deployment-specific
backpressure remain host responsibilities. WebSocket delivery is not activated
by this acceptance.

## Reproduction

```text
.venv/bin/python tools/run_collaboration_notification_benchmark.py --root . --event-count 1000 --campaign-steps 256
.venv/bin/python -m unittest tests.test_m8_10_collaboration_notifications tests.test_m8_10_benchmark tests.test_m8_10_contract_schemas.M810ContractSchemaTests -v
```
