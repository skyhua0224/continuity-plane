# M9-03 Decision Timeline And Evidence Matrix Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified read-only projection contract

```yaml
document_id: context.m9-03-decision-evidence-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m9-03-decision-evidence-v1
affected_tasks: [M7-01, M7-02, M9-01, M9-03, M9-04, M9-05]
next_review: M9-04
```

## Scope

M9-03 consumes one authenticated M9-01 State projection and emits a signed,
same-revision Decision Timeline, Constraint Matrix, Evidence Matrix and health
projection. Current Decisions come only from `project.current_decision_ids`.
Current Constraints come only from `project.active_constraint_ids`. Decision
and Constraint supersedes relationships retain predecessor and successor
identity without restoring historical objects to the current set.

The Evidence Matrix classifies current, candidate, stale, rejected,
superseded and unreferenced Evidence independently from object status. Reverse
references cover Work, Idea, Decision, Constraint, Blocker, Effect, Idea
relationship, Idea review, correction protection and experiment promotion
criterion evidence.

State-only projections declare `metadata-only` provenance. Validated support
requires an optional provenance bundle that binds Typed Evidence to an M7
assertion, retrieval receipt, claim, replayed gate verdict and exact State
object. Projection validation rebuilds all derived fields from the signed
source and optional bundle before canonical comparison.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.decision-evidence-projection/v1alpha1` | signed Decision, Constraint, Evidence, health, capability and zero-authority projection |
| `context.decision-evidence-provenance-bundle/v1alpha1` | source-bound M7 assertion, claim, verdict, retrieval receipt and Typed Evidence bindings |
| `context.decision-evidence-benchmark/v1alpha1` | local measured completeness, provenance, classification, tamper, capacity and latency receipt |

## Verification

| Gate | Result |
|---|---|
| M9-03 behavior, benchmark, runner and schema tests | `23/23` pass |
| same revision and timeline completeness | `1000/1000`; rate `1.0` |
| Constraint and Evidence Matrix completeness | `1000/1000` each; rate `1.0` |
| current Decision and supersedes chain preservation | `1000/1000` each; rate `1.0` |
| metadata-only honesty and validated provenance | `1000/1000` each; rate `1.0` |
| Evidence classification and coordinated tamper rejection | `1000/1000` each; rate `1.0` |
| false support / old Decision resurrection | `0 / 0` |
| authority violations / provider invocations / external services | `0 / 0 / 0`; fixture instrumentation |
| 10,001-object / 50,001-reference capacity rejection | `true / true` |
| 50,001 provenance binding preflight | rejected before bundle digest |
| RFC3339 offset ordering and future provenance admission | absolute-time order preserved; future assertion and verdict rejected |
| worst-path projection latency | 1,000 samples; p50 `35.644633 ms`; p95 `47.421031 ms`; max `62.571604 ms`; p95 threshold `<50 ms` |
| benchmark verdict | `passed`; failed gates `[]` |
| independent review | High `0`; Medium `0`; Low `0` |
| receipt | [`m9-03-decision-evidence-results.json`](../../experiments/evidence/m9-03-decision-evidence-results.json), receipt SHA-256 `a0673e10f7f98be8bb727193cf3475e0f413b5e5f32e9ae85482f85b8b0e21dd`, file SHA-256 `37b3e2914ddf98ca56d375a5fca7e28b3a9b31b431c3d2153ecefc4a4501f978` |

Each of the 1,000 local runs records the maximum of the validated provenance
path, 128-node Decision and Constraint chain, 4,096-cell matrix and coordinated
tamper rebuild. The receipt retains all 1,000 latency samples and its validator
recomputes min, p50, p95 and max. Object and aggregate reference capacity are
separate veto gates.

Latency becomes an acceptance gate only at 25 or more samples. Smaller unit
runs retain raw latency data but declare `latency_gate_evaluated=false`; this
prevents cold-start and scheduler outliers in two- or three-sample functional
runs from being represented as a reproducible p95 decision. The 1,000-sample
receipt declares `latency_gate_evaluated=true`.

The receipt uses `attestation.mode=local-unattested` and
`measurement_authenticity=false`. It is source-bound and internally
consistent, but it is not an independently authenticated performance
attestation. A trusted CI signature or an independent verifier rerun is
required before claiming authenticated measurement provenance.

## Capacity And Integrity Boundaries

The projection rejects more than 10,000 objects in any projected collection,
more than 50,000 aggregate Evidence references, more than 50,000 provenance
bindings or more than 50,000 nested provenance items. Identifier, display text
and provenance text lengths are bounded. Provenance container count, nesting,
field count and string length are checked before the bundle is canonicalized
and hashed.

Decision chronology uses parsed timezone-aware RFC3339 timestamps. A Decision
cannot supersede a Decision from another Work or reverse chronological order.
Evidence verification cannot precede observation. Assertions and verdicts
cannot postdate the projection observation time. Recomputing projection hashes
and HMAC signatures after semantic field changes does not bypass source-bound
rebuild validation.

M9-01 validates, hashes and copies the complete snapshot before M9-03 applies
its projection limits. End-to-end transport byte preflight remains a transport
boundary. Browser HTML escaping, executable URL handling and artifact scheme
allow-lists remain presentation-layer requirements for M9-04 and later UI
work.

## Authority Boundaries

The projection exposes no State commit, completion, approval, provider or
external-effect authority. A validated M7 binding changes support display only;
it cannot activate a Decision, Constraint, promotion or correction action.
M9-05 retains controlled human approval, promotion, correction and audit
entrypoints through State MCP authorization, revision/CAS and validators.

This acceptance does not prove a Docmost UI, browser rendering, HTTP/SSE
transport, Context Health page or controlled governance action. M9-04 through
M9-07 retain their own completion gates.

## Reproduction

```text
.venv/bin/python tools/run_decision_evidence_benchmark.py --root . --iterations 1000 --generated-at 2026-08-17T22:00:00+08:00 --output experiments/evidence/m9-03-decision-evidence-results.json
.venv/bin/python -m unittest tests.test_m9_03_decision_evidence_projection tests.test_m9_03_decision_evidence_benchmark tests.test_m9_03_contract_schemas -v
ruff check context_control_plane/decision_evidence_projection.py context_control_plane/decision_evidence_benchmark.py tests/test_m9_03_decision_evidence_projection.py tests/test_m9_03_decision_evidence_benchmark.py tests/test_m9_03_contract_schemas.py tools/run_decision_evidence_benchmark.py
```
