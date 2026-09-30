# M9-05 Controlled Human Governance Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified controlled governance contract

```yaml
document_id: context.m9-05-human-governance-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m9-05-human-governance-v1
affected_tasks: [M2-05, M3-05, M8-05, M9-01, M9-05, M9-06]
next_review: M9-06
```

## Scope

M9-05 defines four request-bound human governance actions:

| Action | State record | Required control |
|---|---|---|
| `context.experiment.promotion.propose` | promotion proposal | claimed Experiment attempt, current evidence and expected revision |
| `context.experiment.promotion.approve` | promotion approval | independent approver, proposal binding and expected revision |
| `context.idea.correction.protect` | correction protection | bounded affected Work/scope and causation/correlation references |
| `context.idea.correction.release` | correction release | verified, non-future release evidence and expected revision |

The facade accepts a transport session identifier, resolves the trusted
`RequestContext` inside the service boundary, and invokes only the four mapped
State MCP tools. It does not accept a caller-provided subject, authorization
reference, generic State MCP tool, completion authority, provider authority or
external-effect authority.

## Authorization And Receipt Binding

`TenantProjectAuthorizer` records authorization before any State lookup. M9-05
adds `context.authorization-audit-event/v2alpha1` for governed requests. The
event contains `request_id` and the canonical State request SHA-256 in the
tenant append-only audit chain. A successful human receipt includes the exact
policy digest, audit event ID/hash and request SHA-256 together with the
immutable State event binding.

The facade rejects a successful State response when the authorization event is
missing, has a different request identity, project, action, subject,
authorization reference, policy binding, decision or hash. SQLite audit storage
deduplicates an exact governed request identity and rejects request-SHA drift.
The promotion replay integration uses durable SQLite State and audit stores;
after a later State revision and service restart it returns the original receipt
without appending another authorization event.

Every retry evaluates the current policy. A historical allow can replay only
after State MCP finds the exact committed State event bound to the request;
an audit-only request never creates a State record after its grant is revoked.
Facade and State MCP process caches each retain at most 1,024 request entries.
Evicted committed requests recover through the durable State and audit records.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.human-governance-request/v1alpha1` | bounded action-discriminated request without caller authority fields |
| `context.human-governance-response/v1alpha1` | zero-authority receipt with State event and authorization audit bindings |
| `context.authorization-audit-event/v1alpha1` and `v2alpha1` | one stable schema family; v1 preserves general authorization history and v2 binds governed requests to canonical State requests |
| `context.human-governance-benchmark/v1alpha1` | local four-action, replay, scope and tamper receipt |

All contracts use strict JSON Schema and registry hashes. Public request size is
limited to 32 KiB; request IDs are bounded to 200 characters; list and
criterion evidence bounds are enforced before session resolution or State
access.

## Verification

| Gate | Result |
|---|---|
| M9-05 behavior, benchmark and schema tests | `34/34` pass |
| affected M8-05 authorization and isolation tests | `16/16` pass |
| affected M2-05 State MCP tests | `26/26` pass; PostgreSQL parity `1` conditional skip without `CONTEXT_TEST_POSTGRES_DSN` |
| policy revocation and bounded cache | audit-only promotion/correction writes reject after revocation; committed State event replay survives revocation, restart and cache eviction |
| four-action success | `4000/4000`; rate `1.0` |
| generic bypass, invalid session, exact replay and request-ID drift | each `1000/1000`; rate `1.0` |
| State event binding and authorization audit binding tamper rejection | each `1000/1000`; rate `1.0` |
| project-scoped same request ID and invalid input rejection | each `1000/1000`; rate `1.0` |
| unauthorized State calls, authority violations, provider invocations and external services | `0 / 0 / 0 / 0` |
| local facade-contract latency | 1,000 samples; p50 `1.615073 ms`; p95 `3.068316 ms`; max `4.923184 ms`; threshold p95 `<50 ms` |
| benchmark verdict | `passed`; failed gates `[]` |
| receipt | [`m9-05-human-governance-results.json`](../../experiments/evidence/m9-05-human-governance-results.json), receipt SHA-256 `10ca55c87b3da10609dd81bba139435c2d8cd485d9e0362f0513edf6a8091379`, file SHA-256 `f8a65ff885f561dc4b1979ac5e73505020f702a43ea5088438b6112155f55a6b` |

The benchmark uses a local strict State probe to measure facade contract work.
It does not measure Docmost browser rendering, PostgreSQL, SQLite transactions,
network transport, provider latency or external service latency. SQLite State
and authorization-audit behavior, audit-write failure, independent approval,
future evidence rejection and post-restart promotion receipt replay are covered
by the integration tests.

## Authority Boundary

The facade is a controlled request adapter. It holds no direct State write,
completion, approval, provider-native or external-effect authority. A request
requires a valid transport session, authorization audit success, State MCP
expected revision/CAS and domain validator gates. Failed authorization audit
writes deny the request before State lookup or mutation.

M9-05 does not prove a Docmost browser UI, HTTP/SSE transport, CSP, artifact
download controls, real PostgreSQL latency or a shared production deployment.
M9-06 and M10 retain those integration and release gates.

## Rollback

The authorization audit reader retains v1 and v2 validation. Rollback stops
new governed v2 writes and replays existing v1 and v2 events in their immutable
wire versions. A v1 event is not rewritten as v2 because it lacks a canonical
State-request digest; a v2-only governance binding missing that digest is
quarantined.

## Reproduction

```text
.venv/bin/python tools/run_human_governance_benchmark.py --root . --iterations 1000 --generated-at 2026-08-19T22:55:00+08:00 --output experiments/evidence/m9-05-human-governance-results.json
.venv/bin/python -m unittest tests.test_m9_05_human_governance tests.test_m9_05_contract_schemas tests.test_m9_05_human_governance_benchmark -v
.venv/bin/python -m unittest tests.test_m8_05_authorization_audit tests.test_m8_05_authorization_benchmark tests.test_m8_05_contract_schemas tests.test_m8_05_state_mcp_isolation tests.test_m2_05_state_mcp -v
ruff check context_control_plane/authorization_audit.py context_control_plane/state_mcp.py context_control_plane/human_governance.py context_control_plane/human_governance_benchmark.py tests/test_m9_05_human_governance.py tests/test_m9_05_contract_schemas.py tests/test_m9_05_human_governance_benchmark.py tools/run_human_governance_benchmark.py
```
