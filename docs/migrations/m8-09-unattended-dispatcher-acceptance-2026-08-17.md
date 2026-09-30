# M8-09 Unattended Dispatcher Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified local-embedded shadow contract

```yaml
document_id: context.m8-09-unattended-dispatcher-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-09-unattended-dispatcher-v2
affected_tasks: [M3-08, M5-08, M8-02, M8-06, M8-09, M10-00]
next_review: M8-10
```

## Scope

M8-09 defines a provider-neutral dispatcher for required, conditional and
optional Work obligations. The dispatcher selects one ready obligation at a
time, persists a digest-bound continuation cursor before each port action,
uses State MCP for claim and completion authority, and stops only at a typed
blocker or required-work closure. Optional Work cannot block closure.

The implementation uses the local embedded State MCP and an in-memory or
SQLite cursor store. Every port action has a stable request ID and must expose
a durable receipt resolver; the dispatcher reconciles a committed port result
before trusting mutable Work state after cursor-CAS loss. The dispatcher has no
State write authority, completion authority, provider authority or
external-effect authority. Provider calls, cross-device leases and live
compaction remain later deployment and M10 validation concerns.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.condition-decision/v1alpha1` | project, profile, obligation revision and evidence-bound conditional outcome |
| `context.blocking-decision/v2alpha1` | typed condition-evidence blocker with explicit evidence resume condition |
| `context.unattended-campaign-cursor/v1alpha1` | CAS and SHA-256 bound continuation state for every dispatcher boundary |
| `context.unattended-dispatch-step/v1alpha1` | ordered claim, packet, execution, verification and completion/release receipt |
| `context.unattended-campaign-receipt/v1alpha1` | campaign identity, revision interval, required/conditional/optional closure and evidence |
| `context.unattended-dispatcher-benchmark/v1alpha1` | closure, replay, duplicate, authority and latency acceptance receipt |

## Verification

| Gate | Result |
|---|---|
| focused dispatcher, cursor, completion, shared State MCP and contract tests | `48/48` pass |
| required Work completion | `3000/3000`; rate `1.0` |
| campaign closure | `1000/1000`; rate `1.0` |
| terminal replay equality | `1000/1000`; rate `1.0` |
| duplicate claim/execute/complete suppression | `3000/3000`; rate `1.0` |
| authority violations | `0` |
| provider invocations / external services | `0 / 0` |
| local benchmark latency | p50 `25.139580 ms`; p95 `30.820041 ms`; max `40.467023 ms` |
| benchmark verdict | `pass`; failed gates `[]` |
| receipt | [`m8-09-unattended-dispatcher-results.json`](../../experiments/evidence/m8-09-unattended-dispatcher-results.json), receipt SHA-256 `17ce871199e7784c4e564d1afd3b0e87b3a0232a96e22007c53a150099885e3e` |

The benchmark runs `1000` deterministic local samples. Each sample contains
three dependent required Work items and one optional manual Work item. The
counts measure repeated execution of a fixed structural fixture; they do not
represent `1000` independent provider or multi-device environments. Focused
tests additionally cover SQLite port-receipt reopen and tamper detection,
SQLite cursor reopen, every committed cursor boundary, response loss after
execute/complete/block, trusted-time lease expiry, same-actor claim adoption,
orphan active claims, foreign claim fencing, verification failure release,
typed condition blockers, stale governance decisions, semantic campaign chain
validation, conditional non-applicability correspondence, terminal identity
binding and byte-identical closed-campaign replay.

## Safety boundaries

The cursor stores continuation state, bounded step receipts and the terminal
receipt. It cannot create a claim, complete Work, invoke a provider or perform
an external effect. A
missing cursor with an active claim fails closed. A claimed cursor must match
actor, Work, project/profile revision, claim revision, lease epoch, fence,
scope and lease state before execution. Verification failure releases the
claim and records a typed blocker without completion. Conditional Work requires
a current `met` decision; `not-met` is recorded as non-applicable; `unknown`
requires the v2 typed evidence blocker and resume condition.

Independent review records High `0` and Medium `0`. Long campaigns currently
retain all completed step receipts in the cursor. The resulting cumulative
validation cost is a Low scalability finding; M8-10 and M10 must add a scale
benchmark and assess an append-only step index with a replay high watermark.
Live provider behavior, cross-device claim coordination, real external-effect
idempotency and host-native compaction callbacks remain M8-10 and M10-00
concerns.

## Reproduction

```text
.venv/bin/python tools/run_unattended_dispatcher_benchmark.py --root . --samples 1000
.venv/bin/python -m unittest discover -s tests -p 'test_m8_09*.py' -q
```
