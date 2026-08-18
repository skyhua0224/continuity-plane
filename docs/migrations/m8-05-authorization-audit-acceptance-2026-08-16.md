# M8-05 Authorization, Audit, and Isolation Acceptance

Version: 1  
Date: 2026-08-16  
Status: verified local authorization and audit

```yaml
document_id: context.m8-05-authorization-audit-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m8-05-authorization-audit-v1
affected_tasks: [M2-05, M8-02, M8-05, M8-06, M8-08, M8-10]
next_review: M8-06
```

## Scope

M8-05 defines exact actor credential, tenant, project and action grants from a
validated immutable policy snapshot. The authorizer requires the trusted
policy SHA-256, enforces grant activation, expiry and revocation, and rejects
disabled or unregistered projects. State MCP authorization runs before project
lookup, request replay or ledger mutation. External denial responses remain
generic; detailed reason codes are retained in the audit ledger.

Every authorization decision is committed before it is returned. Audit write
failure, timestamp regression or chain failure rejects the operation. The
portable store is process-local. The embedded durable store uses SQLite with
per-tenant sequences and hash heads, restart replay, database triggers that
reject row update and deletion, explicit connection closure and tenant-scoped
reads.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.authorization-policy/v1alpha1` | projects, exact grants, validity windows and immutable policy identity |
| `context.authorization-audit-event/v1alpha1` | tenant-scoped append-only decision sequence and hash-chain entry |
| `context.authorization-isolation-benchmark/v1alpha1` | fault matrix, latency, veto metrics and bounded provenance |

## Verification

| Gate | Result |
|---|---|
| focused behavior and contract tests | `22/22` pass |
| valid authorization | `1000/1000` allowed and audited |
| unauthorized matrix | `9000/9000` rejected across nine fault classes |
| audit failure | `1000/1000` rejected before authorization could succeed |
| audit evidence | `10000` decision events; missing events `0`; chain failures `0`; tenant head collisions `0` |
| isolation veto metrics | unauthorized, same-tenant ungranted project, cross-tenant, unknown project, stale grant and disabled project accepts all `0` |
| policy and valid-call veto metrics | policy digest mismatches `0`; valid authorization denials `0` |
| embedded durability | restart replay, rehashed time-regression replay, hash tamper, row update/delete, append-time regression and connection lifecycle gates pass |
| latency | p50 `0.783990 ms`; p95 `1.159996 ms`; max `4.412026 ms` |
| provider and external service calls | `0`; shared authority claim `false` |

The formal receipt is
[`m8-05-authorization-isolation-results.json`](../../experiments/evidence/m8-05-authorization-isolation-results.json).
Its file SHA-256 is
`bf9663977fecfbb2f4fb54431482f471f89e09a2df458fbbfc1bb8930595e592`
and its receipt SHA-256 is
`16260336b6275a3aecc9596903bce0082c541dc6de91566b70afc832a5c435d8`.

## Authority Boundary

The host supplies authenticated `subject_ref`, `authorization_ref` and trusted
time. This result does not claim an identity provider, credential issuance,
network transport authentication, policy distribution service or distributed
consensus. The SQLite audit ledger proves local append and restart behavior;
cross-device multi-writer audit remains conditional on a shared backend that
passes the same contracts.

Authorization grants no state write authority. Typed State, revision/CAS,
claim/lease and effect transitions remain exclusive to State MCP. Audit records
are evidence and cannot modify active state. Request receipt identity is scoped
by project so equal request IDs in different projects cannot collide in the
shared service cache.

## Reproduction

```text
PYTHONWARNINGS=error::ResourceWarning .venv/bin/python -m unittest tests.test_m8_05_authorization_audit tests.test_m8_05_state_mcp_isolation tests.test_m8_05_contract_schemas tests.test_m8_05_authorization_benchmark -v
.venv/bin/python tools/run_authorization_isolation_benchmark.py
.venv/bin/python tools/verify_repository.py --root .
```
