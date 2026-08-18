# M9-07 Relationship And Impact Projection Acceptance

Version: 1  
Date: 2026-08-17  
Status: verified read-only projection contract

```yaml
document_id: context.m9-07-relationship-impact-acceptance
document_revision: 1
change_type: evidence
authority_ref: verification-run://repository/m9-07-relationship-impact-v1
affected_tasks: [M6-02, M9-02, M9-05, M9-07, M10-00]
next_review: M10-00
```

## Scope

M9-07 consumes an authenticated M9-02 Project Graph projection and emits one
deterministic Relationship and Impact view. Work nodes and parent, dependency,
supersedes, return-point and promotion-target edges remain bound to the exact
State revision, State digest and Project Graph projection digest.

An optional M6-02 CodeGraph receipt can add symbol nodes and `references`
edges only after its `rg + LSP` evidence is revalidated against a trusted local
repository root. Each code source preserves its own index revision, verified
time and receipt digest. Its State revision binding is explicitly `null` and
its authority class is `verified-non-authoritative-clue`. The projection does
not infer Work-to-symbol relationships from names, titles or path substrings.

## Filter And Focus Contract

The view request binds project ID and expected State revision. It accepts only
bounded node kinds, relation kinds, focus IDs, direction, depth, terminal Work
selection and node/edge limits. Filters are applied before deterministic BFS.
Unknown fields, stale revision, absent or filtered focus nodes, duplicate IDs,
unsupported relations, depth above `8`, more than `64` focus nodes, or a result
above `2,000` nodes and `5,000` edges fail closed.

The projection records source, eligible, excluded, outside-focus and returned
node/edge counts. `truncated` is always `false` and `impact_complete` is always
`true`; no partial result can claim complete impact. Dependency and dependent
sets are derived from typed State dependency edges. Connected components use
stable member-derived IDs.

## Force-Directed Layout Boundary

The semantic projection contains a deterministic layout seed and identifies
the candidate algorithm as `force-directed`. Coordinates, zoom, drag state and
animation are frontend-local data with no authority and no State persistence.
They do not participate in impact, completion, approval or governance
decisions.

Graph interactions expose read-only navigation metadata. Controlled actions
remain routed through the M9-05 facade and require trusted session resolution,
authorization-before-lookup, expected revision/CAS, validators, State event and
audit binding. The view exposes no direct State tool.

## Contracts

| Contract | Responsibility |
|---|---|
| `context.relationship-impact-view-request/v1alpha1` | bounded, revision-pinned filter and focus request |
| `context.relationship-impact-projection/v1alpha1` | signed Project Graph relationship, impact, cluster and code-clue projection |
| `context.relationship-impact-benchmark/v1alpha1` | correctness, tamper, capacity, authority and scale latency receipt |

All three contracts use strict JSON Schema and exact registry hashes.

## Verification

| Gate | Result |
|---|---|
| M9-07 behavior, schema, benchmark and runner tests | `14/14` pass |
| same State revision and complete projection | each `1000/1000`; rate `1.0` |
| focus direction and code revision-clock separation | each `1000/1000`; rate `1.0` |
| resigned projection tamper and stale/invalid filter rejection | each `1000/1000`; rate `1.0` |
| 2,000-node complete build and rebuild | `25/25`; rate `1.0`; no truncation |
| authority violations / provider invocations / external services | `0 / 0 / 0` |
| regular full + focused + verified-code path | 1,000 samples; p50 `1.442236 ms`; p95 `2.556033 ms`; max `3.709347 ms`; threshold p95 `<50 ms` |
| 2,000-node build, HMAC and full rebuild path | 25 samples; p50 `154.680627 ms`; p95 `187.459764 ms`; max `200.053427 ms`; threshold p95 `<250 ms` |
| benchmark verdict | `passed`; failed gates `[]` |
| receipt | [`m9-07-relationship-impact-results.json`](../../experiments/evidence/m9-07-relationship-impact-results.json), receipt SHA-256 `6eeefb35b7016028aba8f5538555852b061b1607b1d6c8e5b19d8b1c0a62f012`, file SHA-256 `3f4ef70cfff74d9c35f22836623870135627a2ec47dbe0f6c2ced4dba2603046` |

The benchmark is local and in-process. It validates the projection contract,
current-root code evidence and deterministic rebuild. It does not measure a
Docmost browser, WebSocket transport, graph animation, shared deployment,
PostgreSQL, provider execution or network latency. The selected Docmost fork
remains a candidate integration surface.

## Reproduction

```text
.venv/bin/python tools/run_relationship_impact_benchmark.py --root . --iterations 1000 --scale-iterations 25 --generated-at 2026-08-17T23:30:00+08:00 --output experiments/evidence/m9-07-relationship-impact-results.json
.venv/bin/python -m unittest tests.test_m9_07_relationship_impact_projection tests.test_m9_07_relationship_impact_benchmark tests.test_m9_07_contract_schemas -v
ruff check context_control_plane/relationship_impact_projection.py context_control_plane/relationship_impact_benchmark.py tests/test_m9_07_relationship_impact_projection.py tests/test_m9_07_relationship_impact_benchmark.py tests/test_m9_07_contract_schemas.py tools/run_relationship_impact_benchmark.py
```
